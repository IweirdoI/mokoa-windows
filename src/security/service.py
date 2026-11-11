"""Sensitive information filtering and precise forgetting.

Owner: 2abac5a6

Phase 1 — 正则规则脱敏 + 加盐哈希占位符 + 索引文件:
  覆盖手机号、身份证号、邮箱、银行卡号、护照、统一社会信用代码、
  固定电话、出生日期、车牌号、IPv4、SSH私钥、设备标识、认证凭证等。
  脱敏正文写回 <HASH_...> 加盐哈希占位符，索引记录原文映射
  （原文经 AES-GCM 加密后落盘，见 src/security/index_crypto.py）。

Phase 2 — LLM 语义级敏感识别（可选增强，见 src/security/semantic_detector.py）:
  通过本地 LLM 识别正则难以覆盖的敏感信息；启用后检测失败会阻止本次处理。
  自然语言遗忘解析（forget）已实现。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from typing import Any
from pathlib import Path

from src.common.interfaces import SecurityService
from src.common.models import SensitiveEntry
from src.security.forget_parser import ForgetTargetParser, ForgetResolution
from src.security.knowledge_forget import KnowledgeForgetPlan, KnowledgeForgetResolver
from src.security.semantic_detector import SemanticMatch, SemanticPiiDetector
from src.security.placeholders import (
    PLACEHOLDER_RE as _PLACEHOLDER_RE,
    LLM_PLACEHOLDER_RE as _LLM_PLACEHOLDER_RE,
)

# 模块级盐值，用于哈希生成（固定盐：占位符需可复现，故不按用户轮换；
# 真正不可逆性来自 SHA256 + 该盐，且原文映射经 AES-GCM 加密单独落盘，见 index_crypto.py）。
SALT = "mokoa_security_salt_2024"

# 占位符模式 <HASH_8hex>/<_HASH_16hex_> 由 src/security/placeholders 统一定义并导入（见 _PLACEHOLDER_RE）。
# 规则循环中需将其保护起来，避免 qq_number/imei 等数字类规则把占位符里的 hex 数字段当敏感内容二次脱敏。


class SensitivePattern:
    """单条敏感信息匹配规则。"""

    def __init__(
        self,
        name: str,
        pattern: str,
        replacement: str = "[REDACTED]",
        *,
        case_insensitive: bool = True,
    ) -> None:
        self.name = name
        self._regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        self.replacement = replacement


# ---------------------------------------------------------------------------
# 预定义规则集（按 DLP 优先级排序：密钥 > 证件 > 金融 > 通讯 > 网络 > 其他）
# 注意：规则顺序很重要，先匹配更具体的，后匹配更通用的
# ---------------------------------------------------------------------------

_RULES: list[SensitivePattern] = [
    # =========================================================================
    # 密钥凭证类（最高优先级）
    # =========================================================================

    # SSH 私钥头
    SensitivePattern(
        "ssh_private_key",
        r"-----BEGIN\s+(?:(?:RSA|EC|DSA|ENCRYPTED|OPENSSH|PGP)\s+)?PRIVATE\s+KEY-----"
        r"[\s\S]*?(?:-----END\s+(?:(?:RSA|EC|DSA|ENCRYPTED|OPENSSH|PGP)\s+)?PRIVATE\s+KEY-----|\Z)",
        "[SSH_KEY_REDACTED]",
    ),
    # X.509 证书头
    SensitivePattern(
        "x509_certificate",
        r"-----BEGIN\s+CERTIFICATE-----",
        "[CERTIFICATE_REDACTED]",
    ),
    # UUID（必须在数字类规则之前，避免被误匹配为其他数字串）
    SensitivePattern(
        "uuid",
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "[UUID_REDACTED]",
    ),
    # Discord/Slack Bot Token（必须在数字类规则之前，避免被误匹配为驱动器号/银行卡号）
    SensitivePattern(
        "bot_token",
        r"xox[bapisr]-\d+-[A-Za-z0-9\-]+",
        "[BOT_TOKEN_REDACTED]",
    ),
    # Bearer Token / OAuth2（必须在 JWT 之前处理，优先匹配 Bearer 头中的完整令牌）
    SensitivePattern(
        "bearer_token",
        r"(?<=bearer\s)[A-Za-z0-9_\-\.]{20,}",
        "[BEARER_TOKEN_REDACTED]",
    ),
    # JWT 令牌
    SensitivePattern(
        "jwt_token",
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        "[JWT_REDACTED]",
    ),
    # OpenAI API Key: sk-proj-... / sk-user-... / sk-org-...
    SensitivePattern(
        "openai_key",
        r"sk-(?:proj|user|org)-[A-Za-z0-9_-]+",
        "[OPENAI_KEY_REDACTED]",
    ),
    # GitHub PAT: ghp_ 或 github_pat_
    SensitivePattern(
        "github_pat",
        r"ghp_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{40,}",
        "[GITHUB_PAT_REDACTED]",
    ),
    # Stripe: sk_live_ / pk_live_ / sk_test_ / pk_test_
    SensitivePattern(
        "stripe_key",
        r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{10,}",
        "[STRIPE_KEY_REDACTED]",
    ),
    # AWS Access Key ID: AKIA + 16 位大写字母数字
    SensitivePattern(
        "aws_access_key",
        r"AKIA[0-9A-Z]{16}",
        "[AWS_KEY_REDACTED]",
    ),
    # Git 仓库 URL（含用户名密码）
    SensitivePattern(
        "git_url_with_creds",
        r"(?:https?|git)://[^\s\"'/:]+:[^\s\"'@]+@[^/\s\"']+",
        "[GIT_CREDENTIALS_REDACTED]",
    ),
    # 数据库连接串（含密码）
    SensitivePattern(
        "db_connection_string",
        r"(?:mysql|postgres(?:ql)?|mongodb\+srv|mongodb|redis|sqlite|mssql|cockroachdb)://[^\s\"']+:[^\s\"']+@[^/\s\"']+",
        "[DB_CONNSTR_REDACTED]",
    ),
    # .env 配置文件中的 KEY=VALUE 格式密码（敏感变量名）
    SensitivePattern(
        "access_token",
        r"\bglpat-[A-Za-z0-9_-]{10,}",
        "[ACCESS_TOKEN_REDACTED]",
    ),
    SensitivePattern(
        "access_token",
        r"[\"']?(?:access[_-]?token|private[_-]?token|personal[_-]?access[_-]?token|"
        r"codeup[_-]?token)[\"']?\s*[:=]\s*"
        r"(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,，;；}]+)",
        "[ACCESS_TOKEN_REDACTED]",
    ),
    SensitivePattern(
        "env_credentials",
        r"[\"'](?:api[_-]?key|secret|token|password)[\"']\s*:\s*"
        r"(?:\"[^\"\r\n]+\"|'[^'\r\n]+')",
        "[ENV_SECRET_REDACTED]",
    ),
    SensitivePattern(
        "env_credentials",
        r"\b(?:password|passwd|db_password|secret|api_key|token)\s*=\s*"
        r"(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,，;；]+)",
        "[ENV_SECRET_REDACTED]",
    ),
    SensitivePattern(
        "env_credentials",
        r"(?:(?<=password=)|(?<=passwd=)|(?<=db_password=)|(?<=secret=)|(?<=api_key=)|(?<=token=))\S{6,}",
        "[ENV_SECRET_REDACTED]",
    ),
    # API Key / SecretKey 通用（在更具体的 key 之后，排除 AWS/Azure 等特定格式）
    # 注意：使用 negative lookahead 确保密钥部分不以 AKIA 开头，从而排除 AWS Access Key ID
    SensitivePattern(
        "api_key",
        r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?key)\s*[:：=\s]+\s*(?:(?!AKIA)[A-Za-z0-9_\-]{16,})",
        "[API_KEY_REDACTED]",
    ),
    # 加密压缩包密码 / 通用密码（覆盖英文和中文关键词，支持冒号/等号分隔，允许空白符）
    SensitivePattern(
        "archive_password",
        r"(?:(?<=password=)|(?<=password:)|(?<=passwd=)|(?<=pwd=)|(?<=密码：)|(?<=password：)|(?<=passwd：))\s*\S{6,}",
        "[PASSWORD_REDACTED]",
    ),
    # Wi-Fi 密码：只脱敏口令，不把 SSID / 访客网络名当成密码
    SensitivePattern(
        "wifi_password",
        r"(?:wifi[_ -]?pass(?:word|wd|phrase)?|wifi[_ -]?pwd|wi-?fi\s*密码|"
        r"无线(?:网络)?密码|wlan\s*(?:密码|password))\s*[:：=]\s*\S{6,}",
        "[WIFI_PASSWORD_REDACTED]",
    ),
    SensitivePattern(
        "archive_password",
        r"(?:密码|口令)\s*(?:是|为|[:：=])\s*[A-Za-z0-9!@#$%^&*_.+/?=-]{4,}",
        "[PASSWORD_REDACTED]",
    ),

    # =========================================================================
    # 证件类
    # =========================================================================

    # 身份证号（18 位，末尾可能为 X/x）
    SensitivePattern(
        "id_card",
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
        "[ID_CARD_REDACTED]",
    ),
    # 护照（中国）：E/G/P 开头 + 8 位数字
    SensitivePattern(
        "passport",
        r"(?<![A-Za-z0-9])[EGP]\d{8}(?!\d)",
        "[PASSPORT_REDACTED]",
    ),
    # 港澳通行证：W/H/M 开头 + 8 位数字
    SensitivePattern(
        "hk_macau_pass",
        r"(?<![A-Za-z])[WwHMhm]\d{8}(?!\d)",
        "[PASSPORT_REDACTED]",
    ),
    # 统一社会信用代码：18 位，包含字母数字（GB11643-1999）
    SensitivePattern(
        "unified_social_credit_code",
        r"(?<![A-Za-z0-9])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?!\d)",
        "[CREDIT_CODE_REDACTED]",
    ),
    # 外国人永久居留身份证：18 位（同 credit_code 格式，用不同名字区分）
    SensitivePattern(
        "foreign_permanent_resident_id",
        r"(?<![A-Za-z0-9])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?!\d)",
        "[FOREIGN_ID_REDACTED]",
    ),
    # 驾驶证：12 位数字
    SensitivePattern(
        "driver_license",
        r"(?<![A-Za-z0-9])[1-9]\d{11}(?!\d)",
        "[DRIVER_LICENSE_REDACTED]",
    ),
    # 英国 NINO：AB123456C 格式（2字母前缀+6数字+1字母校验，常见前缀）
    SensitivePattern(
        "uk_nino",
        r"(?<![A-Za-z0-9])(?:[A-CE-JK-PRT-Z][A-Z]|\d{2})\d{6}[A-D](?![A-Za-z0-9])",
        "[UK_NINO_REDACTED]",
    ),
    # 意大利税号：16 位字母数字（校验码在末位，整体格式较复杂，简化匹配）
    SensitivePattern(
        "italian_tax_code",
        r"(?<![A-Za-z0-9])[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z](?![A-Za-z0-9])",
        "[ITALIAN_TAX_CODE_REDACTED]",
    ),
    # 港澳台居民居住证：8 位数字（必须前后非数字字母边界，不抢 landline 中的数字段）
    SensitivePattern(
        "hk_macao_residence_permit",
        r"(?<![A-Za-z0-9\-])[1-9]\d{7}(?![A-Za-z0-9\-])",
        "[RESIDENCE_PERMIT_REDACTED]",
    ),

    # =========================================================================
    # 金融支付类
    # =========================================================================

    # 银行卡号（16-19 位数字，支持带空格/连字符分隔）
    SensitivePattern(
        "bank_card",
        r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}(?!\d)",
        "[BANK_CARD_REDACTED]",
    ),
    # CVV2 / CVV（信用卡背面 3 位纯数字，前后有特定语境）
    SensitivePattern(
        "cvv2",
        r"(?:cvv|cvv2|安全码)\s*[:：]?\s*\d{3}(?!\d)",
        "[CVV_REDACTED]",
    ),
    # 银行卡有效期 MM/YY 或 MM/YYYY
    SensitivePattern(
        "bank_card_expiry",
        r"(?:exp|到期|有效期)\s*[:：]?\s*(0[1-9]|1[0-2])[/\-]\d{2}(?!\d)",
        "[BANK_CARD_EXPIRY_REDACTED]",
    ),

    # =========================================================================
    # 手机号 & 通讯信息
    # =========================================================================

    # 手机号（中国大陆 11 位，1 开头，第二位 3-9）
    SensitivePattern(
        "phone",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "[PHONE_REDACTED]",
    ),
    # 带区号的手机号（+86 或 0086 或 86）
    SensitivePattern(
        "phone_with_country_code",
        r"(?:\+86|0086|86)[- ]?1[3-9]\d{9}",
        "[PHONE_REDACTED]",
    ),
    # 带分隔符的手机号（138-xxxx-xxxx、138 xxxx xxxx）
    SensitivePattern(
        "phone_formatted",
        r"(?<!\d)1[3-9]\d[ \t\u00a0\u3000-]+\d{4}[ \t\u00a0\u3000-]+\d{4}(?!\d)",
        "[PHONE_REDACTED]",
    ),
    # Spoken Chinese digits require an explicit telephone context.
    SensitivePattern(
        "phone_chinese",
        r"(?:手机号?|联系电话|电话号码|电话)\s*(?:是|为|[:：=])?\s*"
        r"[一幺][三四五六七八九][零〇一幺二两三四五六七八九]{9}"
        r"(?![零〇一幺二两三四五六七八九])",
        "[PHONE_REDACTED]",
    ),
    # 固定电话（带区号座机）：010-12345678、021-12345678
    SensitivePattern(
        "landline",
        r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)",
        "[LANDLINE_REDACTED]",
    ),
    # 短信验证码（4-6 位数字，必须有上下文提示词）
    SensitivePattern(
        "sms_verification_code",
        r"(?:验证码|verification[_-]?code|OTP|一次性密码)\s*[:：]?\s*\d{4,6}(?!\d)",
        "[SMS_CODE_REDACTED]",
    ),

    # =========================================================================
    # 网络身份
    # =========================================================================

    # 邮箱
    SensitivePattern(
        "email",
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "[EMAIL_REDACTED]",
    ),
    # IPv4 地址
    SensitivePattern(
        "ipv4",
        r"(?<!\d)(?<!\d\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?!\.\d)",
        "[IP_REDACTED]",
    ),
    # MAC 地址：XX:XX:XX:XX:XX:XX 或 XX-XX-XX-XX-XX-XX
    SensitivePattern(
        "mac_address",
        r"(?<![A-Fa-f0-9])(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}(?![A-Fa-f0-9])|(?<![A-Fa-f0-9])(?:[A-Fa-f0-9]{2}-){5}[A-Fa-f0-9]{2}(?![A-Fa-f0-9])",
        "[MAC_ADDRESS_REDACTED]",
    ),
    # IMEI 设备序列号（15 位数字）
    SensitivePattern(
        "imei",
        r"(?<![A-Za-z0-9])\d{15}(?!\d)",
        "[IMEI_REDACTED]",
    ),
    # SessionId / Cookie
    SensitivePattern(
        "session_id",
        r"(?:session[_-]?id|jsessionid|(?<![A-Za-z])sid)(?![A-Za-z])\s*[:=]\s*[A-Za-z0-9_\-]{16,}",
        "[SESSION_REDACTED]",
    ),
    # QQ needs a label: arbitrary counters and five-digit ports are not QQ IDs.
    SensitivePattern(
        "qq_number",
        r"(?:\bQQ(?:号码|号)?|扣扣(?:号)?)\s*(?:是|为|[:：=])?\s*[1-9]\d{4,9}(?!\d)",
        "[QQ_REDACTED]",
    ),

    # =========================================================================
    # 其他高频敏感格式
    # =========================================================================

    # A date is private here only with an explicit birth context.
    SensitivePattern(
        "birth_date",
        r"(?:出生日期|出生于|出生|生日|生于|\bbirth(?:day|\s+date)|\bdate\s+of\s+birth)"
        r"\s*(?:是|为|[:：=])?\s*(?:19|20)\d{2}[-/年](?:0?[1-9]|1[0-2])"
        r"[-/月](?:0?[1-9]|[12]\d|3[01])(?:日)?(?!\d)",
        "[BIRTH_DATE_REDACTED]",
    ),
    # 车牌号：省份简称 + 字母 + 5-6 位字符（蓝牌/绿牌）
    SensitivePattern(
        "license_plate",
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼\u4e00-\u9fa5][A-Za-z]\s*[A-Za-z0-9]{5,6}(?![A-Za-z0-9])",
        "[LICENSE_PLATE_REDACTED]",
    ),
    # 美国 SSN 社保号：XXX-XX-XXXX
    SensitivePattern(
        "us_ssn",
        r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
        "[SSN_REDACTED]",
    ),
    # 快递单号（常见格式，13-20 位数字/字母组合）
    SensitivePattern(
        "tracking_number",
        r"(?:快递单号|tracking[_-]?number|运单号)\s*[:：]?\s*[A-Za-z0-9]{8,20}",
        "[TRACKING_REDACTED]",
    ),
    # 订单号
    SensitivePattern(
        "order_number",
        r"(?:订单号|order[_-]?id|订单[_-]?编号)\s*[:：]?\s*[A-Za-z0-9_\-]{8,30}",
        "[ORDER_REDACTED]",
    ),
    # 工号 / 员工编号
    SensitivePattern(
        "employee_id",
        r"(?:(?<=工号是)|(?<=工号为)|(?<=员工编号是)|(?<=员工编号为))"
        r"[A-Za-z0-9][A-Za-z0-9_.-]{3,19}",
        "[EMPLOYEE_ID_REDACTED]",
    ),
    SensitivePattern(
        "employee_id",
        r"(?:工号|employee[_-]?id|员工编号)\s*[:：]?\s*[A-Za-z0-9_\-]{4,20}",
        "[EMPLOYEE_ID_REDACTED]",
    ),
    # 合同编号
    SensitivePattern(
        "contract_number",
        r"(?:合同编号|contract[_-]?number|合同[_-]?号)\s*[:：]?\s*[A-Za-z0-9_\-]{4,30}",
        "[CONTRACT_REDACTED]",
    ),
    # 密保问题答案
    SensitivePattern(
        "security_answer",
        r"(?:密保问题|security[_-]?question)\s*[:：]?\s*\S{4,}",
        "[SECURITY_ANSWER_REDACTED]",
    ),
    # Require the AADHAAR label: 4-4-4 digits alone can also be a version ID.
    SensitivePattern(
        "aadhaar_number",
        r"(?:\bAADHAAR(?:\s*(?:number|no\.?))?|印度身份证号)\s*[:：=]?\s*"
        r"\d{4}[ -]\d{4}[ -]\d{4}(?![A-Za-z0-9])",
        "[AADHAAR_REDACTED]",
    ),
    # 登机牌/车票检测
    SensitivePattern(
        "boarding_pass_ticket",
        r"(?:登机牌|机票|火车票|汽车票)\s*[A-Za-z0-9\-]{4,}",
        "[TRAVEL_BOOKING_REDACTED]",
    ),
    # Tor Onion 地址（必须在 crypto_wallet 之前，优先匹配，因为完整域名不应被部分匹配为钱包地址）
    SensitivePattern(
        "onion_address",
        r"[a-zA-Z0-9]{56}\.onion",
        "[ONION_ADDRESS_REDACTED]",
    ),
    # 虚拟货币钱包地址（BTC、ETH、TRX等） - 不使用捕获组
    SensitivePattern(
        "crypto_wallet",
        r"(?:[13][A-Za-z1-9]{25,34}|bc1[qz][a-z0-9]{39,59}|[Tt][A-Za-z0-9]{33}|0x[a-fA-F0-9]{40})",
        "[CRYPTO_WALLET_REDACTED]",
    ),
]


# ingestion 质量门控只认高风险 PII/密钥。IP、UUID、工号等运维上下文
# 仍由 filter_sensitive 脱敏，但不在摄取阶段直接拒绝有价值的工具经验。
_PRIVACY_RISK_RULE_NAMES = frozenset(
    {
        "access_token",
        "ssh_private_key",
        "x509_certificate",
        "bot_token",
        "bearer_token",
        "jwt_token",
        "openai_key",
        "github_pat",
        "stripe_key",
        "aws_access_key",
        "git_url_with_creds",
        "db_connection_string",
        "env_credentials",
        "archive_password",
        "id_card",
        "passport",
        "hk_macau_pass",
        "bank_card",
        "phone",
        "phone_with_country_code",
        "phone_formatted",
        "phone_chinese",
        "landline",
        "email",
        "us_ssn",
        "crypto_wallet",
    }
)


def detect_privacy_risk(text: str) -> bool:
    """判断正文是否含 ingestion 约定的高风险隐私/密钥。

    供 ``IngestionPipeline`` 在质量评分前打 ``privacy_risk`` 标记。
    最终脱敏与索引仍由 ``filter_sensitive`` 负责。
    """

    if not text:
        return False
    rules = [rule for rule in _RULES if rule.name in _PRIVACY_RISK_RULE_NAMES]
    return redact_sensitive_text(text, rules=rules) != text


def redact_sensitive_text(
    text: str,
    *,
    rules: list[SensitivePattern] | None = None,
) -> str:
    """无状态地脱敏文本，供原始归档等不需要反向恢复的场景使用。

    与 ``SecurityAndForgetService.filter_sensitive`` 不同，本函数不会保存原文到
    ``SensitiveIndex``，避免 staging 为了脱敏又额外产生一份敏感值映射。
    """

    sentinels = {value: f"\x00PH{i}\x00" for i, value in enumerate(dict.fromkeys(_PLACEHOLDER_RE.findall(text)))}
    result = text
    for value, sentinel in sentinels.items():
        result = result.replace(value, sentinel)
    for rule in _RULES if rules is None else rules:
        result = rule._regex.sub(rule.replacement, result)
    for value, sentinel in sentinels.items():
        result = result.replace(sentinel, value)
    return result


class SensitiveIndex:
    """敏感信息索引管理器。
    
    负责维护 原文 <-> 哈希占位符 的映射关系，
    支持持久化到本地文件。
    """
    
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.entries: dict[str, SensitiveEntry] = {}  # placeholder_id -> SensitiveEntry
        self.reverse_map: dict[str, str] = {}  # original_content -> placeholder_id
    
    def add_entry(
        self,
        original_content: str,
        placeholder_id: str,
        sensitive_type: str,
        user_id: str | None = None,
    ) -> None:
        """添加一个新的敏感信息条目到索引。"""
        entry = SensitiveEntry(
            placeholder_id=placeholder_id,
            original_content=original_content,
            sensitive_type=sensitive_type,
            user_id=user_id or self.user_id,
        )
        self.entries[placeholder_id] = entry
        self.reverse_map[original_content] = placeholder_id
    
    def get_original(self, placeholder_id: str) -> str | None:
        """根据占位符ID获取原始内容。"""
        entry = self.entries.get(placeholder_id)
        return entry.original_content if entry else None
    
    def get_placeholder(self, original_content: str) -> str | None:
        """根据原始内容获取占位符ID。"""
        return self.reverse_map.get(original_content)
    
    def remove_entry(self, placeholder_id: str) -> bool:
        """删除索引条目（软删除）。"""
        if placeholder_id in self.entries:
            self.entries[placeholder_id].is_deleted = True
            return True
        return False
    
    def clear_all(self) -> None:
        """清空索引。"""
        self.entries.clear()
        self.reverse_map.clear()
    
    def get_stats(self) -> dict[str, Any]:
        """获取索引统计信息。"""
        total = len(self.entries)
        deleted = sum(1 for e in self.entries.values() if e.is_deleted)
        active = total - deleted
        
        type_breakdown = {}
        for entry in self.entries.values():
            if not entry.is_deleted:
                stype = entry.sensitive_type
                type_breakdown[stype] = type_breakdown.get(stype, 0) + 1
        
        return {
            "total_count": total,
            "active_count": active,
            "deleted_count": deleted,
            "type_breakdown": type_breakdown,
        }
    
    def to_dict(self) -> dict[str, Any]:
        """将索引转换为字典格式，用于持久化。

        敏感原文（original_content）在落盘前加密，防止索引文件泄露时原文可读；
        placeholder_id / sensitive_type 等非敏感字段保持明文，保障查询与统计性能。
        """
        from src.security.index_crypto import encrypt_original

        return {
            "user_id": self.user_id,
            "entries": {
                pid: {
                    "placeholder_id": e.placeholder_id,
                    "original_content": encrypt_original(e.original_content),
                    "sensitive_type": e.sensitive_type,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "is_deleted": e.is_deleted,
                }
                for pid, e in self.entries.items()
            }
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SensitiveIndex":
        """从字典创建索引实例。

        兼容历史明文索引：无加密前缀的 original_content 原样读取；
        加密条目解密失败时降级为空串（该条无法还原，不中断加载）。
        """
        from src.security.index_crypto import decrypt_original

        instance = cls(user_id=data["user_id"])
        for pid, entry_data in data.get("entries", {}).items():
            entry = SensitiveEntry(
                placeholder_id=entry_data["placeholder_id"],
                original_content=decrypt_original(entry_data["original_content"]),
                sensitive_type=entry_data["sensitive_type"],
                user_id=data["user_id"],
            )
            if "created_at" in entry_data and entry_data["created_at"]:
                from datetime import datetime
                entry.created_at = datetime.fromisoformat(entry_data["created_at"])
            entry.is_deleted = entry_data.get("is_deleted", False)
            instance.entries[pid] = entry
            instance.reverse_map[entry.original_content] = pid
        return instance
    
    def save_to_file(self, filepath: str) -> None:
        """将索引保存到文件。"""
        dirpath = os.path.dirname(filepath)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
    
    @classmethod
    def load_from_file(cls, filepath: str, user_id: str) -> "SensitiveIndex":
        """从文件加载索引。"""
        if not os.path.exists(filepath):
            return cls(user_id=user_id)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        return cls.from_dict(data)


def _generate_placeholder_id(original_content: str, sensitive_type: str) -> str:
    """生成唯一的哈希占位符ID（存储/持久化场景）。

    使用SHA256哈希，确保唯一性和不可逆性。
    格式：<HASH_xxxxxxxx>（8 位 hex，尖括号包裹）。
    """
    combined = f"{original_content}:{sensitive_type}:{SALT}"
    hash_value = hashlib.sha256(combined.encode('utf-8')).hexdigest()
    short_hash = hash_value[:8]
    return f"<HASH_{short_hash}>"


# ---------------------------------------------------------------------------
# LLM 往返脱敏专用：不常见字符分隔符 + 更长哈希 + 日期排除
# ---------------------------------------------------------------------------

# LLM 往返占位符格式：<_HASH_xxxxxxxxxxxxxxxx_>
#   - <_ ... _> 包裹，与存储用的 <HASH_xxx>（8 位）区分开
#   - 16 位 hex（比存储用的 8 位更长），进一步降低碰撞概率
#   - 加盐哈希防彩虹表爆破
_LLM_PLACEHOLDER_PREFIX = "<_HASH_"
_LLM_PLACEHOLDER_SUFFIX = "_>"

# Explicit private dates remain protected; ordinary dates are not birth dates.
_LLM_EXCLUDED_RULE_NAMES = frozenset()

# 注入到系统提示词的脱敏说明，告知模型占位符含义
LLM_DESENSITIZATION_NOTICE = (
    "【安全提示】用户输入中的敏感信息（手机号、身份证、邮箱、银行卡号、"
    "API 密钥等）已被系统自动替换为 <_HASH_xxxxxxxxxxxxxxxx_> 格式的安全占位符。"
    "这些占位符代表原始敏感数据，你在回复中若需引用这些信息，请直接使用"
    "相同的占位符，不要尝试还原或编造原始值。普通日期可正常使用，"
    "明确的出生日期等私人信息仍需保护。"
)


def _generate_llm_placeholder(original_content: str, sensitive_type: str) -> str:
    """随机出站标识；不从敏感原文派生，避免固定盐字典猜测及跨请求关联。"""
    return f"{_LLM_PLACEHOLDER_PREFIX}{secrets.token_hex(8)}{_LLM_PLACEHOLDER_SUFFIX}"


class SecurityAndForgetService(SecurityService):
    """
    Owner: 2abac5a6
    Requirement: (5) PII detection + NL-driven forgetting

    Phase 1: 正则规则脱敏 + 哈希占位符 + 索引文件（默认启用）
    Phase 2: 本地 LLM 语义级识别（src/security/semantic_detector.py，
             由 security.semantic_detection.enabled 控制，默认关闭，需本地
             LM Studio）+ NL 遗忘（forget_parser / knowledge_forget，已落地）。
    """

    # 盐值，用于增加哈希的不可预测性
    SALT = "mokoa_security_salt_2024"

    def __init__(
        self,
        rules: list[SensitivePattern] | None = None,
        user_id: str = "default",
        index_path: str | None = None,
        semantic_detector: SemanticPiiDetector | None = None,
    ) -> None:
        self._rules = rules or list(_RULES)
        self.user_id = user_id
        self.index_path = index_path or os.path.join(os.getcwd(), "security_index.json")
        # 语义层为可选增强；None 时 filter_sensitive 仅走正则
        self.semantic_detector = semantic_detector
        # 普通记忆/偏好的 NL 目标解析器（P0 修复，见 forget_parser.py）
        self._forget_parser = ForgetTargetParser()
        # 知识库 NL 精准遗忘解析器（按自然语言条件批量定位遗忘）
        self._knowledge_resolver = KnowledgeForgetResolver()
        self._index = SensitiveIndex(user_id)
        # 尝试加载已保存的索引
        if os.path.exists(self.index_path):
            try:
                self._index = SensitiveIndex.load_from_file(self.index_path, user_id)
            except Exception:
                # 如果加载失败，使用空索引
                self._index = SensitiveIndex(user_id)

    def redact_for_export(self, text: str) -> str:
        """Apply configured detection without creating a reversible raw-value index."""
        matches = self.semantic_detector.detect(text, strict=True) if self.semantic_detector else []
        result = text
        for match in sorted(matches, key=lambda m: len(m.text), reverse=True):
            result = result.replace(match.text, "[SEMANTIC_REDACTED]")
        return redact_sensitive_text(result)

    def filter_sensitive(
        self,
        text: str,
        user_id: str | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        """检测并替换文本中的敏感信息。

        Args:
            text: 原始文本

        Returns:
            (脱敏后文本, 检测到的类型列表，去重保持首次出现顺序, 占位符映射表 {original: placeholder_id})
            
        双层检测：
        1. 正则层：确定性规则，覆盖 50+ 种类型
        2. 语义层（可选）：本地 LLM 补充识别正则遗漏的敏感信息
        脱敏正文统一使用 <HASH_...> 占位符，确保 restore_sensitive 可正确还原。
        """

        if not text:
            return text, [], {}

        # Finish optional inspection before creating any sensitive index entries.
        semantic_matches = self.semantic_detector.detect(text, strict=True) if self.semantic_detector else []
        owner = user_id or self.user_id
        detected: list[str] = []
        seen: set[str] = set()
        result = text
        placeholder_map: dict[str, str] = {}

        # 1. 正则层：确定性规则替换
        for rule in self._rules:
            # 保护已有占位符，避免数字类规则（qq_number/imei 等）把
            # <HASH_xxxxxxxx> 里的 hex 数字段当敏感内容二次脱敏
            placeholders = _PLACEHOLDER_RE.findall(result)
            sentinels = {
                p: f"\x00PH{i}\x00"
                for i, p in enumerate(dict.fromkeys(placeholders))
            }
            protected = result
            for placeholder, sentinel in sentinels.items():
                protected = protected.replace(placeholder, sentinel)

            matches = rule._regex.findall(protected)
            if matches:
                if rule.name not in seen:
                    seen.add(rule.name)
                    detected.append(rule.name)

                # Multiple patterns may recognize the same type; only the type
                # list is deduplicated, never the actual replacement work.
                def replace_match(match: re.Match) -> str:
                    full_match = match.group()
                    placeholder_id = _generate_placeholder_id(full_match, rule.name)
                    if not self._index.get_placeholder(full_match):
                        self._index.add_entry(
                            full_match, placeholder_id, rule.name, user_id=owner
                        )
                    placeholder_map[full_match] = placeholder_id
                    return placeholder_id

                protected = rule._regex.sub(replace_match, protected)

            # 恢复被保护的占位符
            result = protected
            for placeholder, sentinel in sentinels.items():
                result = result.replace(sentinel, placeholder)

        # 2. 语义层（可选增强）：识别正则遗漏的敏感片段
        if self.semantic_detector is not None:
            result, detected, placeholder_map = self._apply_semantic_matches(
                text,
                result,
                detected,
                placeholder_map,
                matches=semantic_matches,
                user_id=owner,
            )

        return result, detected, placeholder_map

    def _apply_semantic_matches(
        self,
        original_text: str,
        current: str,
        detected: list[str],
        placeholder_map: dict[str, str],
        *,
        matches: list[SemanticMatch],
        user_id: str | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        """将语义层识别到的敏感片段补充替换为占位符。

        只在片段仍存在于正文（正则未处理）时替换；与正则重复的匹配自动跳过。
        """
        for match in sorted(matches, key=lambda m: len(m.text), reverse=True):
            if match.text not in current:
                continue

            placeholder_id = _generate_placeholder_id(match.text, match.type)
            if not self._index.get_placeholder(match.text):
                self._index.add_entry(
                    match.text,
                    placeholder_id,
                    match.type,
                    user_id=user_id or self.user_id,
                )

            placeholder_map[match.text] = placeholder_id
            current = current.replace(match.text, placeholder_id)

            if match.type not in detected:
                detected.append(match.type)

        return current, detected, placeholder_map

    def restore_sensitive(self, text: str) -> str:
        """从占位符还原原始敏感内容。
        
        遍历索引中的所有占位符ID，将其替换为对应的原始内容。
        按占位符长度降序排列，避免短占位符被部分替换。
        
        Args:
            text: 包含占位符的文本

        Returns:
            还原后的文本
        """
        result = text
        
        # 按占位符ID长度降序，避免短ID被部分匹配
        placeholder_ids = sorted(
            [pid for pid, entry in self._index.entries.items() if not entry.is_deleted],
            key=len,
            reverse=True,
        )
        
        for placeholder_id in placeholder_ids:
            entry = self._index.entries[placeholder_id]
            placeholder = entry.placeholder_id
            if placeholder in result:
                result = result.replace(placeholder, entry.original_content)
        
        return result

    # -----------------------------------------------------------------------
    # LLM 往返脱敏-还原管线（发前脱敏 + 返回后还原）
    # -----------------------------------------------------------------------

    def desensitize_for_llm(
        self,
        text: str,
        *,
        user_id: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """为发送给远程 LLM 的文本执行可逆脱敏。

        与 ``filter_sensitive`` 的区别：
        - 使用 <_HASH_xxxxxxxxxxxxxxxx_> 格式的随机标识，不从原文派生
        - 包含出生日期、银行卡有效期等明确私人字段
        - 返回的 placeholder_map 用于后续 ``restore_from_llm`` 还原

        Args:
            text: 原始文本（用户输入/记忆上下文）
            user_id: 用户 ID（用于索引归属）

        Returns:
            (脱敏后文本, {原始值: LLM占位符} 映射表)

        Raises:
            SemanticDetectionUnavailable: 语义检测器可用但调用失败时抛出，
                使调用方（agent）能在检测未完成时中止发往模型的请求。
        """
        if not text:
            return text, {}

        # 语义层（可选增强）：本地 LLM 补充识别正则遗漏的敏感信息。
        # strict=True 时检测器失败会抛 SemanticDetectionUnavailable，沿用
        # redact_for_export 的契约，使 agent 能在检测未完成时中止发往模型。
        placeholder_map: dict[str, str] = {}
        result = text
        if self.semantic_detector:
            matches = self.semantic_detector.detect(text, strict=True)
            for match in sorted(matches, key=lambda m: len(m.text), reverse=True):
                ph = _generate_llm_placeholder(match.text, "semantic")
                placeholder_map[match.text] = ph
                result = result.replace(match.text, ph)

        # 正则表达式层（确定性规则），并排除日期类规则
        llm_rules = [r for r in self._rules if r.name not in _LLM_EXCLUDED_RULE_NAMES]

        owner = user_id or self.user_id

        # 保护已有 LLM 占位符，防止被二次匹配
        existing = _LLM_PLACEHOLDER_RE.findall(result)
        sentinels = {
            p: f"\x00LLM{i}\x00"
            for i, p in enumerate(dict.fromkeys(existing))
        }
        for p, s in sentinels.items():
            result = result.replace(p, s)

        for rule in llm_rules:
            def _make_replacer(r: SensitivePattern = rule):
                def _replace(match: re.Match) -> str:
                    full = match.group()
                    ph = placeholder_map.get(full) or _generate_llm_placeholder(full, r.name)
                    placeholder_map[full] = ph
                    sentinel = f"\x00LLM{len(sentinels)}\x00"
                    sentinels[ph] = sentinel
                    return sentinel
                return _replace

            result = rule._regex.sub(_make_replacer(), result)

        # 恢复保护的占位符
        for p, s in sentinels.items():
            result = result.replace(s, p)

        return result, placeholder_map

    @staticmethod
    def restore_from_llm(text: str, placeholder_map: dict[str, str]) -> str:
        """将 LLM 返回文本中的占位符还原为原始敏感值。

        按占位符长度降序替换，避免短占位符是长占位符子串时误替换。

        Args:
            text: LLM 返回的文本（可能包含 <_HASH_..._> 占位符）
            placeholder_map: ``desensitize_for_llm`` 返回的映射表

        Returns:
            还原后的文本（占位符替换回原始值）
        """
        if not text or not placeholder_map:
            return text

        result = text
        # 按占位符长度降序，避免短占位符部分匹配长占位符
        for original, placeholder in sorted(
            placeholder_map.items(), key=lambda item: len(item[1]), reverse=True
        ):
            if placeholder in result:
                result = result.replace(placeholder, original)
        return result

    def forget(self, user_id: str, nl_instruction: str) -> list[str]:
        """自然语言驱动的精确遗忘。

        从 NL 指令中提取关键词，匹配 SensitiveIndex 中的 original_content
        和 sensitive_type，只返回匹配的 placeholder_id。正式删除由 Orchestrator
        统一调用 ``delete_sensitive_entries`` 执行。

        匹配策略：
        1. 关键词直接匹配 original_content（模糊匹配，包含关系）
        2. 关键词匹配 sensitive_type（如"手机号"匹配"phone"类型）

        Args:
            user_id: 用户ID
            nl_instruction: 自然语言遗忘指令，如"忘记我的手机号"

        Returns:
            匹配到的 placeholder_id 列表
        """
        if not nl_instruction:
            return []

        matched_ids: list[str] = []
        instruction_lower = nl_instruction.lower()

        for pid, entry in self._index.entries.items():
            if entry.is_deleted:
                continue
            if entry.user_id != user_id:
                continue

            is_matched = False

            # 策略1: 关键词匹配 original_content
            orig_lower = entry.original_content.lower()
            for kw in self._extract_keywords(nl_instruction):
                if len(kw) >= 2 and (kw in orig_lower or orig_lower in kw):
                    is_matched = True
                    break

            # 策略2: 关键词匹配 sensitive_type
            if not is_matched:
                type_keyword_map = {
                    # 敏感类型 -> 匹配关键词列表
                    "phone": ["手机", "电话", "mobile", "phone", "cell"],
                    "id_card": ["身份证", "id_card", "证件", "resident"],
                    "email": ["邮箱", "email", "邮件", "mail"],
                    "bank_card": ["银行卡", "信用卡", "bank_card", "卡号", "debit", "credit"],
                    "password": ["密码", "password", "passwd", "pwd"],
                    "archive_password": ["密码", "password", "passwd"],
                    "wifi_password": ["密码", "wifi"],
                    "passport": ["护照", "passport", "旅行证"],
                    "ipv4": ["IP", "ip_address", "ip地址", "服务器", "host"],
                    "jwt_token": ["JWT", "token", "令牌", "bearer"],
                    "bearer_token": ["bearer", "token", "令牌"],
                    "api_key": ["API Key", "密钥", "api_key", "key"],
                    "openai_key": ["openai", "sk-proj"],
                    "github_pat": ["github", "pat", "访问令牌"],
                    "stripe_key": ["stripe", "stripe_key"],
                    "aws_access_key": ["aws", "akia"],
                    "ssh_private_key": ["SSH", "私钥", "ssh_key", "private key"],
                    "uuid": ["UUID", "标识符", "uuid", "guid"],
                    "mac_address": ["MAC", "mac地址", "mac_address"],
                    "sms_verification_code": ["验证码", "OTP", "verification code"],
                    "crypto_wallet": ["钱包", "BTC", "ETH", "crypto", "wallet", "地址"],
                    "boarding_pass_ticket": ["登机牌", "机票", "火车票", "boarding"],
                    "security_answer": ["密保问题", "security question", "答案"],
                    "aadhaar_number": ["aadhaar", "aadhar"],
                    "us_ssn": ["SSN", "社保号", "social security"],
                    "bank_card_expiry": ["有效期", "到期", "expiry"],
                    "phone_with_country_code": ["手机号", "电话"],
                    "phone_formatted": ["手机号", "电话"],
                    "landline": ["座机", "固话", "landline"],
                    "license_plate": ["车牌", "license plate", "车牌号"],
                    "birth_date": ["出生日期", "生日", "dob", "出生"],
                    "hk_macau_pass": ["通行证", "港澳"],
                    "uk_nino": ["NINO", "英国国民保险"],
                    "italian_tax_code": ["意大利税号", "tax code"],
                    "credit_card": ["信用卡", "credit card"],
                    "foreign_permanent_resident_id": ["永居身份证", "外国人"],
                    "driver_license": ["驾驶证", "driver license", "驾照"],
                    "dob": ["出生日期", "dob", "生日", "出生"],
                    "address": ["地址", "address", "location"],
                    "name": ["姓名", "name", "名字"],
                    "session_id": ["session", "会话", "cookie"],
                    "qq_number": ["QQ号", "qq"],
                    "tracking_number": ["快递单号", "tracking"],
                    "order_number": ["订单号", "order"],
                    "employee_id": ["工号", "employee"],
                    "contract_number": ["合同", "contract"],
                    "onion_address": ["暗网", "onion"],
                    "db_connection_string": ["数据库", "mysql", "mongodb", "连接串"],
                    "git_url_with_creds": ["git", "仓库", "git凭证"],
                    "x509_certificate": ["证书", "certificate"],
                    "bot_token": ["bot token", "bot_token"],
                    "env_credentials": ["环境变量", ".env"],
                }
                for sensitive_type, keywords in type_keyword_map.items():
                    if entry.sensitive_type.lower() == sensitive_type.lower():
                        for kw in keywords:
                            if kw.lower() in instruction_lower:
                                is_matched = True
                                break
                        if is_matched:
                            break

            if is_matched:
                matched_ids.append(pid)

        return list(dict.fromkeys(matched_ids))

    def parse_forget_targets(
        self,
        instruction: str,
        candidates: list[Any],
    ) -> ForgetResolution:
        """面向普通记忆/偏好快照的自然语言目标解析（P0 修复）。

        原 ``forget`` 只匹配脱敏索引，非敏感内容恒解析为空。本方法把遗忘
        指令映射到候选记忆（MemoryItem 快照：short/mid/long/preference）
        上，输出带置信度、删除范围与安全级联说明的目标列表。

        Args:
            instruction: 自然语言遗忘指令，如「忘掉下载路径偏好」。
            candidates: 候选记忆快照，元素需含 id/content/memory_type/layer。

        Returns:
            ForgetResolution：命中的目标 + 删除范围 + 说明。不执行删除。
        """
        return self._forget_parser.parse(instruction, candidates)

    def plan_knowledge_forget(self, instruction: str) -> "KnowledgeForgetPlan":
        """解析知识库遗忘指令（按自然语言条件批量定位遗忘）。

        返回 ``KnowledgeForgetPlan``，包含是否命中知识意图、定向召回查询串、
        是否删除全部、目标知识类型与批量删除过滤条件。解析阶段不执行删除，
        正式级联删除由 Orchestrator 统一编排。
        """
        return self._knowledge_resolver.plan(instruction)

    def resolve_knowledge_forget_targets(
        self,
        instruction: str,
        candidates: list[Any],
    ) -> "ForgetResolution":
        """对知识候选做 NL 遗忘目标解析（返回命中目标，不删除）。"""
        return self._knowledge_resolver.resolve(instruction, candidates)

    def _extract_keywords(self, text: str) -> list[str]:
        """从 NL 指令中提取关键词（去停用词）。"""
        # 简单停用词表
        stop_words = {
            "忘记", "忘掉", "删除", "移除", "不要", "取消", "偏好", "my", "me", "my ", "我的",
            "了", "的", "是", "在", "有", "和", "与", "或", "一个", "这个", "那个",
            "it", "the", "a", "an", "this", "that", "of", "to", "for", "on", "at",
        }
        # 提取中文词语和英文单词
        import re
        chinese_chars = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
        english_words = re.findall(r'[a-z]{2,}', text.lower())
        keywords = chinese_chars + english_words
        return [kw for kw in keywords if kw not in stop_words]

    def detect_only(self, text: str) -> list[str]:
        """仅检测不替换，用于快速扫描或调试。"""

        _, detected, _ = self.filter_sensitive(text)
        return detected

    def delete_sensitive_entries(self, user_id: str, placeholder_ids: list[str]) -> list[str]:
        """删除索引中的敏感条目（软删除）。
        
        Args:
            user_id: 用户ID
            placeholder_ids: 要删除的占位符ID列表

        Returns:
            成功删除的占位符ID列表
        """
        deleted = []
        for pid in placeholder_ids:
            entry = self._index.entries.get(pid)
            if entry is None or entry.user_id != user_id:
                continue
            if self._index.remove_entry(pid):
                deleted.append(pid)
        
        # 自动保存
        self.save_index()
        return deleted

    def get_sensitive_stats(self, user_id: str) -> dict:
        """获取敏感信息统计信息。"""
        entries = [e for e in self._index.entries.values() if e.user_id == user_id]
        total = len(entries)
        deleted = sum(1 for e in entries if e.is_deleted)
        type_breakdown: dict[str, int] = {}
        for entry in entries:
            if entry.is_deleted:
                continue
            type_breakdown[entry.sensitive_type] = (
                type_breakdown.get(entry.sensitive_type, 0) + 1
            )
        return {
            "total_count": total,
            "active_count": total - deleted,
            "deleted_count": deleted,
            "type_breakdown": type_breakdown,
        }

    def save_index(self) -> None:
        """持久化索引到文件。"""
        self._index.save_to_file(self.index_path)

    def load_index(self, filepath: str) -> None:
        """从文件加载索引。"""
        self.index_path = filepath
        self._index = SensitiveIndex.load_from_file(filepath, self.user_id)

    def persist_index(self) -> None:
        """保存当前索引到文件。"""
        self.save_index()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SecurityAndForgetService":
        """从配置字典构造实例。

        支持的可选配置项：
        - semantic_detection: dict，语义层配置（enabled/min_confidence）；
          端点/密钥/模型名等继承顶层 llm 段，见 src/common/config.py。
        """

        custom_rules: list[SensitivePattern] = []
        for rule_cfg in config.get("custom_rules", []):
            custom_rules.append(
                SensitivePattern(
                    name=rule_cfg["name"],
                    pattern=rule_cfg["pattern"],
                    replacement=rule_cfg.get("replacement", "[REDACTED]"),
                    case_insensitive=rule_cfg.get("case_insensitive", True),
                )
            )
        user_id = config.get("user_id", "default")
        index_path = config.get("index_path")

        semantic_detector: SemanticPiiDetector | None = None
        semantic_cfg = config.get("semantic_detection") or {}
        if semantic_cfg.get("enabled", False):
            semantic_detector = SemanticPiiDetector.from_config(semantic_cfg)

        return cls(
            rules=custom_rules if custom_rules else None,
            user_id=user_id,
            index_path=index_path,
            semantic_detector=semantic_detector,
        )
