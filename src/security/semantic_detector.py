"""LLM 语义级敏感信息检测器。

Owner: 2abac5a6
Ref: 主方案（语义识别）→ 回退方案（正则规则），确保模型不可用时不崩溃。

通过本地 LLM（默认 LM Studio 原生 REST API /api/v1/chat）识别正则规则难以
覆盖的敏感信息，如上下文中的工号、昵称、隐式地址等。生产调用使用 strict=True，
模型不可用、超时或结果不确定时抛出安全异常；兼容调用仍可返回空列表。

思考模式控制：使用 LM Studio 原生 ``reasoning`` 参数（off/low/medium/high/on）。
语义识别场景使用 ``off`` 关闭思考 —— qwen3.5 等推理模型在思考模式会把输出
写入 reasoning_content，导致 content 为空、检测结果丢失（见 lmstudio 官方
REST 文档 https://lmstudio.ai/docs/developer/rest/chat）。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.common.config import get_llm_config
from src.security.outbound import is_loopback_service

logger = logging.getLogger(__name__)


class SemanticDetectionUnavailable(RuntimeError):
    """Enabled privacy inspection could not establish a valid result."""

# 语义识别提示词：要求输出 JSON 且只返回原文中的精确片段。
# 0.8B 级小模型需要少样本示例才能稳定遵守 JSON 格式，示例使用与任务同构的文本。
_DETECTION_PROMPT = """你是敏感信息识别专家。请从用户提供的文本中找出所有个人敏感信息（PII），
包括但不限于：手机号(phone)、身份证号(id_card)、邮箱(email)、银行卡号(bank_card)、
家庭住址(address)、姓名(name)、工号(employee_id)、密码(password)、
出生日期(birth_date)、车牌号(license_plate)、社交账号(account_id) 等。

示例：
文本：我的工号是NO.9527，地址在北京海淀区中关村大街1号
输出：{"matches":[{"type":"employee_id","text":"NO.9527","confidence":0.95},{"type":"address","text":"北京海淀区中关村大街1号","confidence":0.9}]}

要求：
1. 只输出原文中出现的精确片段，不要改写、不要翻译、不要补充额外信息
2. 敏感片段与上下文无关时也要识别（如单独的编号、证件号）
3. 普通数字、日期、会议安排等不属于敏感信息，不要输出
4. 以 JSON 格式输出（禁止输出 JSON 以外的任何内容），type 必须是下面之一：
   phone / id_card / email / bank_card / address / name / employee_id /
   password / birth_date / license_plate / account_id / passport /
   sms_verification_code / api_key
5. 格式：{"matches":[{"type":"英文类型","text":"原文精确片段","confidence":0到1之间的数字}]}
如果没有敏感信息，输出：{"matches":[]}"""

# 中文敏感类型 -> 英文类型映射（模型偶尔输出中文时的兜底）
_TYPE_ALIASES: dict[str, str] = {
    "手机号": "phone",
    "电话": "phone",
    "身份证": "id_card",
    "身份证号": "id_card",
    "邮箱": "email",
    "电子邮件": "email",
    "银行卡": "bank_card",
    "银行卡号": "bank_card",
    "信用卡": "bank_card",
    "地址": "address",
    "家庭住址": "address",
    "姓名": "name",
    "工号": "employee_id",
    "员工编号": "employee_id",
    "密码": "password",
    "出生日期": "birth_date",
    "生日": "birth_date",
    "车牌": "license_plate",
    "车牌号": "license_plate",
    "社交账号": "account_id",
    "账号": "account_id",
    "护照": "passport",
    "护照号": "passport",
    "验证码": "sms_verification_code",
    "密钥": "api_key",
    "token": "api_key",
}


def _strip_code_fence(content: str) -> str:
    """剥离模型输出中 markdown 代码块围栏（如 ```json ... ```）。

    reasoning 模型常将 JSON 输出包裹在代码块中，直接 json.loads 会失败，
    这里只做轻量剥离：去掉首尾围栏行与语言标注，不影响 JSON 主体。
    """
    text = content.strip()
    if text.startswith("```"):
        # 去掉开头的 ```lang 与结尾的 ```
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


@dataclass
class SemanticMatch:
    """语义层识别到的单条敏感信息。"""

    type: str  # 敏感类型（英文）
    text: str  # 原文精确片段
    confidence: float  # 置信度 0~1
    raw_type: str = field(default="")  # 模型原始输出的类型（可能为中文）


class SemanticPiiDetector:
    """基于本地 LLM（LM Studio）的语义敏感信息检测器。

    端点/密钥/模型名等参数默认从 ``configs/default.yaml`` 的顶层 ``llm`` 段读取
    （见 ``src/common/config.py::get_llm_config``），也可通过构造参数显式覆盖。

    调用 LM Studio 原生 REST 端点 ``/api/v1/chat``（而非 OpenAI 兼容
    ``/chat/completions``），以支持官方 ``reasoning`` 思考控制参数。

    Args:
        base_url: LM Studio 服务根地址，如 http://127.0.0.1:1234。
            默认读取配置 llm.base_url。
        api_key: LM Studio 鉴权密钥。默认读取配置 llm.api_key。
        model: 使用的模型名（LM Studio 中加载的模型名）。默认读取配置 llm.model。
        min_confidence: 置信度阈值，低于该值的匹配被丢弃
        timeout_seconds: 单次请求超时
        max_tokens: 回答 token 预算，默认读取配置 llm.max_tokens
        reasoning: 思考模式控制（"off"/"low"/"medium"/"high"/"on"），
            默认读取配置 llm.reasoning；语义识别推荐 "off" 关闭思考。
        enabled: 是否启用（False 时 detect 直接返回空列表）
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        min_confidence: float | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        reasoning: str | None = None,
        enabled: bool = True,
    ) -> None:
        # 端点/密钥/模型名及各类参数统一回退到顶层 llm 配置段，
        # 构造参数显式传入时优先覆盖配置。
        cfg = get_llm_config()
        self._base_url = (base_url or str(cfg.get("base_url", "http://127.0.0.1:1234"))).rstrip("/")
        self._api_key = api_key if api_key is not None else str(cfg.get("api_key", ""))
        self._model = model or str(cfg.get("model", "qwen3.5-0.8b"))
        self._min_confidence = (
            min_confidence if min_confidence is not None else float(cfg.get("min_confidence", 0.6))
        )
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else float(cfg.get("timeout_seconds", 30.0))
        )
        self._max_tokens = max_tokens if max_tokens is not None else int(cfg.get("max_tokens", 1024))
        # reasoning 规范化：YAML 把 off/on 解析为布尔时会走这里兜底，统一转回合法字符串
        raw_reasoning = reasoning if reasoning is not None else cfg.get("reasoning", "off")
        if isinstance(raw_reasoning, bool):
            raw_reasoning = "on" if raw_reasoning else "off"
        raw_reasoning = str(raw_reasoning).lower()
        if raw_reasoning not in ("off", "low", "medium", "high", "on"):
            raw_reasoning = "off"
        self._reasoning = raw_reasoning
        self._enabled = enabled

    @classmethod
    def from_config(cls, cfg: dict) -> "SemanticPiiDetector":
        """从配置字典构造检测器（支持模块覆盖段合并顶层 llm 段）。

        Args:
            cfg: 模块配置段（如 security.semantic_detection），
                端点/密钥/模型名缺失时回退顶层 llm 段。
        """
        merged = get_llm_config(cfg)
        return cls(
            base_url=str(merged.get("base_url", "")) or None,
            api_key=str(merged.get("api_key", "")) or None,
            model=str(merged.get("model", "")) or None,
            min_confidence=float(merged.get("min_confidence", 0.6)),
            timeout_seconds=float(merged.get("timeout_seconds", 30.0)),
            max_tokens=int(merged.get("max_tokens", 1024)),
            reasoning=str(merged.get("reasoning", "off")) or None,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def detect(self, text: str, *, strict: bool = False) -> list[SemanticMatch]:
        """对文本执行语义敏感检测。

        strict=True 用于入库和外发：失败或不确定时抛出安全异常。
        默认 False 保留旧调用的规则降级行为，不代表检测成功。
        """
        if not self._enabled or not text:
            return []

        try:
            raw = self._call_llm(text)
            matches = self._parse_response(raw, strict=strict)
            if strict and any(match.text not in text for match in matches):
                raise ValueError("non-source match")
            return matches
        except Exception as exc:  # noqa: BLE001 —— 语义层是增强能力，任何失败都应降级
            logger.warning("语义敏感检测失败 (%s)", type(exc).__name__)
            if strict:
                raise SemanticDetectionUnavailable("敏感信息检测未完成，请检查本地检测服务后重试。") from None
            return []

    def _call_llm(self, text: str) -> str:
        """调用 LM Studio 原生 /api/v1/chat，返回模型输出文本。

        使用官方 ``reasoning`` 参数控制思考模式（off/low/medium/high/on），
        ``max_output_tokens`` 限制输出预算；响应从 ``output`` 数组中提取
        ``type=message`` 的正文（reasoning 项不参与解析）。
        """
        if not is_loopback_service(self._base_url):
            raise ValueError("原始敏感文本仅允许交给本机检测服务")
        payload: dict[str, Any] = {
            "model": self._model,
            "input": f"{_DETECTION_PROMPT}\n\n文本：{text}",
            "max_output_tokens": self._max_tokens,
            "temperature": 0.0,
            "reasoning": self._reasoning,
            "store": False,
        }

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        response = httpx.post(
            f"{self._base_url}/api/v1/chat",
            json=payload,
            headers=headers,
            timeout=self._timeout,
            trust_env=False,
        )
        response.raise_for_status()
        data = response.json()
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                return str(item.get("content", ""))
        return ""

    def _parse_response(self, content: str, *, strict: bool = False) -> list[SemanticMatch]:
        """解析模型 JSON 输出为 SemanticMatch 列表。

        兼容模型以 markdown 代码块包裹 JSON 的情况（如 ```json {...} ```），
        剥离代码块围栏后解析；同时兼容两种顶层结构：
        - ``{"matches": [...]}``（约定格式）
        - 裸数组 ``[{...}, ...]``（qwen 系列偶尔直接输出列表）
        """
        matches: list[SemanticMatch] = []
        cleaned = _strip_code_fence(content)
        try:
            payload = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            # 模型偶发在 JSON 尾部多输出引号/杂散字符（如 {"matches":[]}"），
            # 提取首 { 到末 } 的子串后重试，避免带匹配时整体丢失。
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                try:
                    payload = json.loads(cleaned[start : end + 1])
                except (json.JSONDecodeError, TypeError):
                    if strict:
                        raise ValueError("invalid detection JSON") from None
                    logger.warning("语义检测返回非 JSON 内容，已忽略")
                    return matches
            else:
                if strict:
                    raise ValueError("invalid detection JSON") from None
                logger.warning("语义检测返回非 JSON 内容，已忽略")
                return matches

        if isinstance(payload, dict):
            if strict and "matches" not in payload:
                raise ValueError("missing matches")
            raw_matches = payload.get("matches", [])
        elif isinstance(payload, list):
            raw_matches = payload
        else:
            if strict:
                raise ValueError("invalid detection schema")
            return matches
        if not isinstance(raw_matches, list):
            if strict:
                raise ValueError("invalid matches")
            return matches

        for item in raw_matches:
            if not isinstance(item, dict):
                if strict:
                    raise ValueError("invalid match")
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                if strict:
                    raise ValueError("empty match")
                continue
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if strict and (not isinstance(item.get("text"), str)
                           or not isinstance(item.get("confidence"), (int, float))
                           or isinstance(item.get("confidence"), bool)
                           or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError("invalid match fields")
            if confidence < self._min_confidence:
                if strict:
                    raise ValueError("uncertain sensitive match")
                continue

            raw_type = str(item.get("type", "unknown")).strip().lower()
            if strict and _TYPE_ALIASES.get(raw_type, raw_type) not in set(_TYPE_ALIASES.values()):
                raise ValueError("unknown sensitive type")
            matches.append(
                SemanticMatch(
                    type=_TYPE_ALIASES.get(raw_type, raw_type),
                    text=text,
                    confidence=confidence,
                    raw_type=raw_type,
                )
            )
        return matches
