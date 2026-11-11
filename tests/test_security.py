"""Tests for security service — Phase 1 PII regex detection and redaction.

Uses placeholder-based redaction: matched PII is replaced with
`<HASH_8hexchars>` tokens. Custom rules retain their own replacement strings.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.security.forget_parser import ForgetTargetParser
from src.security.service import SecurityAndForgetService, SensitivePattern


HASH_PATTERN = re.compile(r"<HASH_[0-9a-f]{8}>")


def create_service() -> SecurityAndForgetService:
    """每个测试使用独立的临时索引文件，避免跨测试状态污染。"""
    return SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json"))


def _count_placeholders(text: str) -> int:
    """Count the number of HASH placeholders in *text*."""
    return len(HASH_PATTERN.findall(text))


# ---------------------------------------------------------------------------
# 基础接口契约
# ---------------------------------------------------------------------------


def test_filter_sensitive_returns_tuple() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("test")
    assert isinstance(result, str)
    assert isinstance(detected, list)


def test_filter_sensitive_empty_text() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("")
    assert result == ""
    assert detected == []


def test_filter_sensitive_no_pii() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("今天天气真好，写了一段 Python 代码")
    assert result == "今天天气真好，写了一段 Python 代码"
    assert detected == []


# ---------------------------------------------------------------------------
# 手机号检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_phone_basic() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("联系我 13812345678")
    assert HASH_PATTERN.search(result) is not None
    assert "phone" in detected


def test_filter_sensitive_phone_with_country_code() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("电话 +86 13812345678")
    assert HASH_PATTERN.search(result) is not None
    assert "phone_with_country_code" in detected or "phone" in detected


def test_filter_sensitive_multiple_phones() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("13811111111 和 13922222222")
    assert _count_placeholders(result) == 2
    assert "phone" in detected


def test_filter_sensitive_phone_edge_non_phone() -> None:
    """11 位但不以 1[3-9] 开头的数字不应匹配。"""
    service = create_service()
    result, detected, _ = service.filter_sensitive("编号 20241234567")
    assert "20241234567" in result
    assert "phone" not in detected


# ---------------------------------------------------------------------------
# 身份证号检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_id_card() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "我的身份证 110101199001011234"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "id_card" in detected


def test_filter_sensitive_id_card_upper_x() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "身份证 32010619880101123X"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "id_card" in detected


# ---------------------------------------------------------------------------
# 邮箱检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_email() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("邮箱 user@example.com")
    assert HASH_PATTERN.search(result) is not None
    assert "email" in detected


def test_filter_sensitive_email_subdomain() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("邮箱 test+tag@sub.domain.co.uk")
    assert HASH_PATTERN.search(result) is not None
    assert "email" in detected


def test_filter_sensitive_not_email() -> None:
    """不匹配无 @ 的字符串。"""
    service = create_service()
    result, detected, _ = service.filter_sensitive("我的主页 example.com")
    assert "example.com" in result
    assert "email" not in detected


# ---------------------------------------------------------------------------
# 银行卡号检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_bank_card_continuous() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("卡号 6222021234567890")
    assert HASH_PATTERN.search(result) is not None
    assert "bank_card" in detected


def test_filter_sensitive_bank_card_formatted() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("卡号 6222 0212 3456 7890")
    assert HASH_PATTERN.search(result) is not None
    assert "bank_card" in detected


# ---------------------------------------------------------------------------
# 组合场景
# ---------------------------------------------------------------------------


def test_filter_sensitive_multiple_types() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "我叫张三，手机 13812345678，邮箱 zhangsan@test.com"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "phone" in detected
    assert "email" in detected


def test_filter_sensitive_preserves_context() -> None:
    """脱敏应保留上下文结构。"""
    service = create_service()
    result, _, _ = service.filter_sensitive(
        "前缀 13812345678 中缀 user@test.com 后缀"
    )
    assert result.startswith("前缀")
    assert result.endswith("后缀")
    assert "前缀" in result and "中缀" in result and "后缀" in result


def test_filter_sensitive_detect_only() -> None:
    service = create_service()
    detected = service.detect_only("电话 13812345678 邮箱 a@b.com")
    assert "phone" in detected
    assert "email" in detected
    # Confirm no other types are falsely detected
    assert len(detected) == 2


def test_filter_sensitive_custom_rules() -> None:
    rules = [
        SensitivePattern("custom_ip", r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "[IP_REDACTED]"),
    ]
    service = SecurityAndForgetService(rules=rules)
    result, detected, _ = service.filter_sensitive("IP 192.168.1.100")
    # All rules (including custom) use HASH placeholder for restoration support
    assert HASH_PATTERN.search(result) is not None
    assert "custom_ip" in detected


# ---------------------------------------------------------------------------
# 护照检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_passport() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("护照号 E12345678")
    assert HASH_PATTERN.search(result) is not None
    assert "passport" in detected


def test_filter_sensitive_passport_no_false_positive() -> None:
    """普通单词 E12345 不应触发护照规则（8位数字要求）。"""
    service = create_service()
    result, detected, _ = service.filter_sensitive("E12345 test")
    assert "E12345" in result
    assert "passport" not in detected


# ---------------------------------------------------------------------------
# 统一社会信用代码检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_unified_credit_code() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "统一社会信用代码 91110108MA01ABCDEF"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "unified_social_credit_code" in detected


# ---------------------------------------------------------------------------
# 固定电话检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_landline() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("座机 010-12345678")
    assert HASH_PATTERN.search(result) is not None
    assert "landline" in detected


def test_filter_sensitive_landline_no_hyphen() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("区号电话 02112345678")
    assert HASH_PATTERN.search(result) is not None
    assert "landline" in detected


# ---------------------------------------------------------------------------
# IPv4 地址检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_ipv4() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("服务器 192.168.1.100")
    assert HASH_PATTERN.search(result) is not None
    assert "ipv4" in detected


def test_filter_sensitive_ipv4_local() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("localhost 127.0.0.1")
    assert HASH_PATTERN.search(result) is not None
    assert "ipv4" in detected


# ---------------------------------------------------------------------------
# SSH 私钥检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_ssh_key() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpA..."
    )
    assert HASH_PATTERN.search(result) is not None
    assert "ssh_private_key" in detected


def test_filter_sensitive_ssh_key_no_rsa() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "-----BEGIN PRIVATE KEY-----"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "ssh_private_key" in detected


# ---------------------------------------------------------------------------
# JWT Token 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_jwt_token() -> None:
    service = create_service()
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWV9.TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"
    result, detected, _ = service.filter_sensitive(f"token: {token}")
    assert HASH_PATTERN.search(result) is not None
    assert "jwt_token" in detected


# ---------------------------------------------------------------------------
# API Key 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_api_key() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "api_key: abcdefghijklmnop12345678"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "api_key" in detected


# ---------------------------------------------------------------------------
# 出生日期检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_birth_date() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("出生日期：1990-01-01")
    assert HASH_PATTERN.search(result) is not None
    assert "birth_date" in detected


def test_filter_sensitive_birth_date_slash() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("出生 1990/01/01")
    assert HASH_PATTERN.search(result) is not None
    assert "birth_date" in detected


# ---------------------------------------------------------------------------
# 车牌号检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_license_plate() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("车牌京A12345")
    assert HASH_PATTERN.search(result) is not None
    assert "license_plate" in detected


def test_filter_sensitive_license_plate_green() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("新能源车京AD12345")
    assert HASH_PATTERN.search(result) is not None
    assert "license_plate" in detected


# ---------------------------------------------------------------------------
# 综合场景：包含多种敏感信息
# ---------------------------------------------------------------------------


def test_filter_sensitive_comprehensive() -> None:
    """模拟真实场景：一句话包含身份证、手机、邮箱、IP。"""
    service = create_service()
    text = (
        "用户张三，身份证110101199001011234，"
        "手机13812345678，邮箱zhangsan@test.com，"
        "服务器192.168.1.100"
    )
    result, detected, _ = service.filter_sensitive(text)
    assert HASH_PATTERN.search(result) is not None
    for t in ("id_card", "phone", "email", "ipv4"):
        assert t in detected
    # 确认中文名字和上下文保留
    assert "张三" in result


def test_filter_sensitive_key_priority() -> None:
    """密钥类规则应优先于普通信息匹配。"""
    service = create_service()
    text = "ssh私钥-----BEGIN RSA PRIVATE KEY-----"
    result, detected, _ = service.filter_sensitive(text)
    assert HASH_PATTERN.search(result) is not None
    assert "ssh_private_key" in detected


# ---------------------------------------------------------------------------
# 港澳通行证检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_hk_pass_w() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("通行证W12345678")
    assert HASH_PATTERN.search(result) is not None
    assert "hk_macau_pass" in detected


def test_filter_sensitive_hk_pass_h() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("通行证 H12345678")
    assert HASH_PATTERN.search(result) is not None
    assert "hk_macau_pass" in detected


# ---------------------------------------------------------------------------
# 英国 NINO 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_uk_nino() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("NINO: AB123456C")
    assert HASH_PATTERN.search(result) is not None
    assert "uk_nino" in detected


# ---------------------------------------------------------------------------
# 意大利税号检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_italian_tax_code() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "意大利税号 RSSMRA80M05H606Z"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "italian_tax_code" in detected


# ---------------------------------------------------------------------------
# OpenAI Key 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_openai_key() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "openai_key" in detected


# ---------------------------------------------------------------------------
# GitHub PAT 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_github_pat() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "github_pat" in detected


def test_filter_sensitive_github_pat_long() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "github_pat_11ABCD1AB0ABCD1EFG2HIJ_ABCDEFGHIJKLMNOPQRSTUVwxyz"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "github_pat" in detected


# ---------------------------------------------------------------------------
# Stripe 密钥检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_stripe_live() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "sk_live_abc123def456ghi789"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "stripe_key" in detected


def test_filter_sensitive_stripe_test() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "pk_test_abc123def456ghi789"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "stripe_key" in detected


# ---------------------------------------------------------------------------
# Bot Token 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_discord_bot_token() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "xoxb-1-placeholder"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "bot_token" in detected


# ---------------------------------------------------------------------------
# Bearer Token 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_bearer_token() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ.abc123signature"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "bearer_token" in detected


# ---------------------------------------------------------------------------
# AWS Access Key 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_aws_key() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("AKIAIOSFODNN7EXAMPLE")
    assert HASH_PATTERN.search(result) is not None
    assert "aws_access_key" in detected


# ---------------------------------------------------------------------------
# 数据库连接串检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_db_connection_mysql() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "mysql://admin:secret123@db.example.com:3306/mydb"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "db_connection_string" in detected


def test_filter_sensitive_db_connection_mongo() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "mongodb+srv://user:mypass@cluster0.example.net/db"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "db_connection_string" in detected


# ---------------------------------------------------------------------------
# Git URL with credentials 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_git_creds() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "https://user:password@github.com/org/repo.git"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "git_url_with_creds" in detected


# ---------------------------------------------------------------------------
# X.509 证书检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_certificate() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "-----BEGIN CERTIFICATE-----\nMIICpDCCAZ..."
    )
    assert HASH_PATTERN.search(result) is not None
    assert "x509_certificate" in detected


# ---------------------------------------------------------------------------
# MAC 地址检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_mac_address_colon() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("设备 MAC: AA:BB:CC:DD:EE:FF")
    assert HASH_PATTERN.search(result) is not None
    assert "mac_address" in detected


def test_filter_sensitive_mac_address_dash() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("MAC AA-BB-CC-DD-EE-FF")
    assert HASH_PATTERN.search(result) is not None
    assert "mac_address" in detected


# ---------------------------------------------------------------------------
# UUID 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_uuid() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "ID: 550e8400-e29b-41d4-a716-446655440000"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "uuid" in detected


# ---------------------------------------------------------------------------
# 短信验证码检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_sms_code() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("验证码：123456")
    assert HASH_PATTERN.search(result) is not None
    assert "sms_verification_code" in detected


def test_filter_sensitive_otp() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("OTP: 654321")
    assert HASH_PATTERN.search(result) is not None
    assert "sms_verification_code" in detected


# ---------------------------------------------------------------------------
# 加密货币钱包检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_btc_address() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "BTC: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "crypto_wallet" in detected


def test_filter_sensitive_eth_address() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "ETH: 0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "crypto_wallet" in detected


# ---------------------------------------------------------------------------
# 加密压缩包密码检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_archive_password() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("password: MySecretPassword123")
    assert HASH_PATTERN.search(result) is not None
    assert "archive_password" in detected


def test_filter_sensitive_archive_password_cn() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("密码：secret123456")
    assert HASH_PATTERN.search(result) is not None
    assert "archive_password" in detected


# ---------------------------------------------------------------------------
# Wi-Fi 密码检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_wifi_password() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "ssid: MyNetwork WiFi密码: WifiPass123!"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "wifi_password" in detected
    assert "MyNetwork" in result
    assert "WifiPass123!" not in result


def test_filter_sensitive_keeps_public_wifi_ssid() -> None:
    service = create_service()
    text = "Meeting room WiFi SSID: Kylin-Office-Guest"
    result, detected, _ = service.filter_sensitive(text)
    assert result == text
    assert "wifi_password" not in detected
    assert "session_id" not in detected


# ---------------------------------------------------------------------------
# 登机牌/车票检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_boarding_pass() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "登机牌 CZ3101 座位12A"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "boarding_pass_ticket" in detected


# ---------------------------------------------------------------------------
# 密保问题检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_security_answer() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "密保问题：我的第一只宠物是什么？答案：Buddy"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "security_answer" in detected


# ---------------------------------------------------------------------------
# AADHAAR 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_aadhaar() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("AADHAAR: 1234 5678 9012")
    assert HASH_PATTERN.search(result) is not None
    assert "aadhaar_number" in detected


# ---------------------------------------------------------------------------
# Tor Onion 地址检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_onion_address() -> None:
    # 使用合法的56字符.onion地址
    service = create_service()
    result, detected, _ = service.filter_sensitive(
        "暗网地址：aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion"
    )
    assert HASH_PATTERN.search(result) is not None
    assert "onion_address" in detected


# ---------------------------------------------------------------------------
# US SSN 检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_us_ssn() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("SSN: 123-45-6789")
    assert HASH_PATTERN.search(result) is not None
    assert "us_ssn" in detected


# ---------------------------------------------------------------------------
# 银行卡有效期检测
# ---------------------------------------------------------------------------


def test_filter_sensitive_bank_card_expiry() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("有效期 12/26")
    assert HASH_PATTERN.search(result) is not None
    assert "bank_card_expiry" in detected


def test_filter_sensitive_bank_card_expiry_cn() -> None:
    service = create_service()
    result, detected, _ = service.filter_sensitive("到期：03/25")
    assert HASH_PATTERN.search(result) is not None
    assert "bank_card_expiry" in detected


# ---------------------------------------------------------------------------
# 综合：新技术密钥类型
# ---------------------------------------------------------------------------


def test_filter_sensitive_all_cloud_keys() -> None:
    """同时检测多种云厂商密钥。"""
    service = create_service()
    text = (
        "AWS: AKIAIOSFODNN7EXAMPLE "
        "OpenAI: sk-proj-abc123def456ghi789jkl012mno345pqr "
        "Stripe: sk_live_abc123def456"
    )
    result, detected, _ = service.filter_sensitive(text)
    assert _count_placeholders(result) == 3
    for t in ("aws_access_key", "openai_key", "stripe_key"):
        assert t in detected


def test_filter_sensitive_all_credential_types() -> None:
    """同时检测 JWT、Bearer Token、SSH 密钥。"""
    service = create_service()
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "SYNTHETIC_KEY_BODY\n-----END RSA PRIVATE KEY-----\n"
        "eyJhbGciOiJIUzI1NiJ9.eyJ0eXAiOiJKV1Qi.eyJzdWIiOiIxMjM0NTY3ODkwIn0\n"
        "Bearer: eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ.abc123signature"
    )
    result, detected, _ = service.filter_sensitive(text)
    # SSH key + JWT + Bearer = 3 hashes (Bearer may or may not match depending on regex)
    assert _count_placeholders(result) >= 2
    for t in ("ssh_private_key", "jwt_token"):
        assert t in detected


# ---------------------------------------------------------------------------
# NL 遗忘功能测试
# ---------------------------------------------------------------------------


def test_forget_by_type_phone() -> None:
    """forget 只解析手机号占位符，删除由 Orchestrator 后续执行。"""
    service = create_service()
    service.filter_sensitive("我的手机是13812345678")
    # 确保有数据
    stats = service.get_sensitive_stats("default")
    assert stats["active_count"] > 0
    
    # 用"手机"关键词遗忘
    matched = service.forget("default", "忘记我的手机")
    assert len(matched) > 0
    # 解析阶段不能提前修改索引。
    stats_after = service.get_sensitive_stats("default")
    assert stats_after["deleted_count"] == 0

    deleted = service.delete_sensitive_entries("default", matched)
    assert deleted == matched
    final_stats = service.get_sensitive_stats("default")
    assert final_stats["deleted_count"] >= 1


def test_forget_by_type_email() -> None:
    """通过类型关键词遗忘邮箱。"""
    service = create_service()
    service.filter_sensitive("邮箱 user@test.com")
    matched = service.forget("default", "删除我的邮箱")
    assert len(matched) > 0


def test_forget_by_keyword_match() -> None:
    """通过关键词精确匹配 original_content。"""
    service = create_service()
    service.filter_sensitive("密码：MySecret123")
    matched = service.forget("default", "忘记密码MySecret123")
    assert len(matched) > 0


def test_forget_no_match() -> None:
    """无匹配时返回空列表。"""
    service = create_service()
    service.filter_sensitive("我的手机是13812345678")
    matched = service.forget("default", "忘记我的信用卡号9999999999999999")
    assert matched == []


def test_forget_wrong_user() -> None:
    """不同用户返回空列表。"""
    service = create_service()
    service._index.user_id = "user_a"
    service.filter_sensitive("电话 13812345678")
    matched = service.forget("user_b", "忘记手机号")
    assert matched == []


def test_forget_empty_instruction() -> None:
    """空指令返回空列表。"""
    service = create_service()
    service.filter_sensitive("电话 13812345678")
    matched = service.forget("default", "")
    assert matched == []


def test_forget_clears_all_phone() -> None:
    """遗忘指令应匹配所有该类型的敏感条目。"""
    service = create_service()
    service.filter_sensitive("手机13811111111和13922222222")
    matched = service.forget("default", "忘记手机")
    # 两个手机号都被匹配
    assert len(matched) >= 2


# ---------------------------------------------------------------------------
# 通用 NL 遗忘目标解析器测试（P0：普通记忆/偏好）
# ---------------------------------------------------------------------------


def _mem(id: str, content: str, mtype: str = "preference", layer: str = "long") -> Any:
    """构造候选记忆快照（兼容 MemoryItem 结构的最小对象）。"""
    return MemoryItem(
        id=id,
        memory_type=MemoryType(mtype),
        layer=MemoryLayer(layer),
        content=content,
        user_id="default",
    )


def test_parser_hits_chinese_reply_language() -> None:
    """口语『忘掉用中文回答这件事』应命中语言偏好，不误删下载路径。"""
    parser = ForgetTargetParser()
    lang = _mem("p1", "[preference] style.reply_language=zh-CN | 中文回复偏好")
    lang.metadata["preference_key"] = "style.reply_language"
    dl = _mem("p2", "[preference] habit.download_path=E:/Downloads | 下载默认放 E 盘")
    dl.metadata["preference_key"] = "habit.download_path"
    res = parser.parse("忘掉用中文回答这件事", [lang, dl])
    assert [t.memory_id for t in res.targets] == ["p1"]
    assert res.targets[0].preference_key == "style.reply_language"


def test_parser_hits_download_path_preference() -> None:
    """『忘掉下载路径偏好』能命中普通偏好（P0 主场景）。"""
    from src.security.service import SecurityAndForgetService

    service = SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json"))
    candidates = [
        _mem("m1", "下载路径 = /data/downloads"),
        _mem("m2", "喜欢用 Python 编写代码"),
    ]
    res = service.parse_forget_targets("忘掉下载路径偏好", candidates)
    ids = [t.memory_id for t in res.targets]
    assert "m1" in ids
    assert "m2" not in ids


def test_parser_type_constraint() -> None:
    """指令点名『偏好』时只命中偏好，不误删知识。"""
    parser = ForgetTargetParser()
    candidates = [
        _mem("p1", "下载路径偏好：/data/downloads", mtype="preference", layer="long"),
        _mem("k1", "用户下载路径安装说明", mtype="knowledge", layer="mid"),
    ]
    res = parser.parse("忘掉所有偏好", candidates)
    assert [t.memory_id for t in res.targets] == ["p1"]


def test_parser_java_preference_precise() -> None:
    """『忘掉关于 Java 的偏好』精确命中 Java，不误删其他偏好。"""
    parser = ForgetTargetParser()
    candidates = [
        _mem("j1", "用户偏好 Java 语言"),
        _mem("py1", "用户偏好 Python 语言"),
    ]
    res = parser.parse("忘掉关于 Java 的偏好", candidates)
    assert [t.memory_id for t in res.targets] == ["j1"]


def test_parser_confidence_gate_blocks_vague() -> None:
    """过模糊的指令不产生目标（安全级联：防误删）。"""
    parser = ForgetTargetParser()
    candidates = [_mem("p1", "下载路径 = /tmp")]
    res = parser.parse("帮我把那个东西删掉", candidates)
    assert res.targets == []


def test_parser_all_type_with_explicit_scope() -> None:
    """『忘掉所有偏好』显式限定类型时放行全量删除。"""
    parser = ForgetTargetParser()
    candidates = [
        _mem("p1", "下载路径 = /tmp"),
        _mem("p2", "喜欢中文"),
    ]
    res = parser.parse("忘掉所有偏好", candidates)
    assert {t.memory_id for t in res.targets} == {"p1", "p2"}


def test_parser_empty_candidates() -> None:
    """无候选记忆时返回空。"""
    parser = ForgetTargetParser()
    res = parser.parse("忘掉下载路径偏好", [])
    assert res.targets == []


def test_parser_empty_instruction() -> None:
    """空指令不产生目标。"""
    parser = ForgetTargetParser()
    res = parser.parse("", [_mem("p1", "下载路径")])
    assert res.targets == []
    assert res.note


# ---------------------------------------------------------------------------
# 语义级敏感识别测试
# ---------------------------------------------------------------------------

from unittest import mock  # noqa: E402

from src.security.semantic_detector import SemanticMatch, SemanticPiiDetector  # noqa: E402


def _make_semantic_service(matches: list[SemanticMatch]) -> SecurityAndForgetService:
    """构造注入 mock 语义检测器的服务，避免依赖真实 Ollama。"""
    detector = mock.Mock(spec=SemanticPiiDetector)
    detector.enabled = True
    detector.detect.return_value = matches
    return SecurityAndForgetService(
        index_path=tempfile.mktemp(suffix=".json"),
        semantic_detector=detector,
    )


def test_semantic_detects_regex_missed_pii() -> None:
    """语义层应替换正则规则无法识别的敏感片段（如工号 NO.9527）。"""
    service = _make_semantic_service(
        [SemanticMatch(type="employee_id", text="NO.9527", confidence=0.95)]
    )
    result, detected, mapping = service.filter_sensitive("我的工号是NO.9527")
    assert "NO.9527" not in result
    assert HASH_PATTERN.search(result) is not None
    assert "employee_id" in detected
    assert mapping["NO.9527"].startswith("<HASH_")


def test_semantic_skips_already_regexed_content() -> None:
    """正则已替换的内容，语义层重复匹配应被跳过。"""
    service = _make_semantic_service(
        [SemanticMatch(type="phone", text="13812345678", confidence=0.9)]
    )
    result, detected, _ = service.filter_sensitive("手机 13812345678")
    # 正则已处理，占位符数量保持 1 个（不会被语义层重复替换）
    assert _count_placeholders(result) == 1
    assert "phone" in detected


def test_semantic_restore_roundtrip() -> None:
    """语义层替换的内容应能通过 restore_sensitive 还原。"""
    service = _make_semantic_service(
        [SemanticMatch(type="employee_id", text="NO.9527", confidence=0.95)]
    )
    filtered, _, _ = service.filter_sensitive("工号NO.9527，手机13812345678")
    restored = service.restore_sensitive(filtered)
    assert restored == "工号NO.9527，手机13812345678"


def test_semantic_empty_matches_noop() -> None:
    """语义层无匹配时行为与纯正则一致。"""
    service = _make_semantic_service([])
    result, detected, _ = service.filter_sensitive("今天天气不错")
    assert result == "今天天气不错"
    assert detected == []


def test_semantic_disabled_service_works() -> None:
    """未配置语义层时（默认），filter_sensitive 仅走正则。"""
    service = create_service()
    assert service.semantic_detector is None
    result, detected, _ = service.filter_sensitive("手机 13812345678")
    assert "13812345678" not in result
    assert "phone" in detected


# ---------------------------------------------------------------------------
# SemanticPiiDetector 解析测试
# ---------------------------------------------------------------------------


def test_detector_parse_valid_json() -> None:
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '{"matches":[{"type":"phone","text":"13812345678","confidence":0.95}]}'
    )
    assert len(matches) == 1
    assert matches[0].type == "phone"
    assert matches[0].text == "13812345678"


def test_detector_filters_low_confidence() -> None:
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.8)
    matches = detector._parse_response(
        '{"matches":[{"type":"phone","text":"13812345678","confidence":0.5}]}'
    )
    assert matches == []


def test_detector_maps_chinese_type() -> None:
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '{"matches":[{"type":"银行卡号","text":"6222021234567890","confidence":0.95}]}'
    )
    assert matches[0].type == "bank_card"


def test_detector_invalid_json_returns_empty() -> None:
    detector = SemanticPiiDetector(enabled=True)
    assert detector._parse_response("不是 JSON") == []


def test_detector_disabled_returns_empty() -> None:
    detector = SemanticPiiDetector(enabled=False)
    assert detector.detect("任何文本") == []


def test_detector_llm_unreachable_degrades_gracefully() -> None:
    """LM Studio 不可达时 detect 降级返回空列表，不抛异常。"""
    detector = SemanticPiiDetector(
        enabled=True,
        base_url="http://127.0.0.1:1",  # 必然连接失败
        timeout_seconds=0.5,
    )
    assert detector.detect("测试文本") == []


def test_detector_lmstudio_http_call() -> None:
    """模拟 LM Studio 原生 /api/v1/chat 响应，验证完整请求链路。

    校验：端点 /api/v1/chat、鉴权头、reasoning 参数、响应解析。
    """
    detector = SemanticPiiDetector(
        enabled=True,
        base_url="http://127.0.0.1:1234",
        api_key="sk-lm-test",
        model="qwen3.5-0.8b",
        min_confidence=0.5,
        reasoning="off",
    )
    content = _json.dumps(
        {
            "matches": [
                {
                    "type": "employee_id",
                    "text": "NO.9527",
                    "confidence": 0.95,
                }
            ]
        }
    )
    with mock.patch("httpx.post") as mock_post:
        mock_response = mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "output": [{"type": "message", "content": content}]
        }
        mock_post.return_value = mock_response

        matches = detector.detect("我的工号是NO.9527")

    # 请求端点与参数校验
    assert mock_post.called
    call_args = mock_post.call_args
    assert call_args.args[0] == "http://127.0.0.1:1234/api/v1/chat"
    payload = call_args.kwargs["json"]
    assert payload["model"] == "qwen3.5-0.8b"
    # reasoning=off 关闭思考，保证 content 有值（见 semantic_detector.py）
    assert payload["reasoning"] == "off"
    assert payload["store"] is False
    assert "max_output_tokens" in payload
    # 鉴权头校验
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer sk-lm-test"
    # 响应解析校验
    assert len(matches) == 1
    assert matches[0].type == "employee_id"
    assert matches[0].text == "NO.9527"


def test_detector_lmstudio_reasoning_param() -> None:
    """reasoning 参数应透传到请求体，未指定时回退配置默认值。"""
    detector = SemanticPiiDetector(
        enabled=True,
        base_url="http://127.0.0.1:1234",
        reasoning="low",
    )
    with mock.patch("httpx.post") as mock_post:
        mock_response = mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "output": [{"type": "message", "content": "{}"}]
        }
        mock_post.return_value = mock_response

        detector.detect("测试文本")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["reasoning"] == "low"


def test_detector_base_url_keeps_root() -> None:
    """LM Studio 根地址不应被补全 /v1，原生端点由 _call_llm 拼接。"""
    detector = SemanticPiiDetector(
        enabled=True,
        base_url="http://127.0.0.1:1234",
    )
    assert detector._base_url == "http://127.0.0.1:1234"


def test_detector_lmstudio_ignores_reasoning_output() -> None:
    """响应含 reasoning 项时应只取 message 正文。"""
    detector = SemanticPiiDetector(
        enabled=True,
        base_url="http://127.0.0.1:1234",
        reasoning="on",
    )
    content = '{"matches":[]}'
    with mock.patch("httpx.post") as mock_post:
        mock_response = mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "output": [
                {"type": "reasoning", "content": "思考过程..."},
                {"type": "message", "content": content},
            ]
        }
        mock_post.return_value = mock_response

        assert detector.detect("测试文本") == []


def test_detector_parse_json_with_code_fence() -> None:
    """模型以 markdown 代码块包裹 JSON 时应能正确解析。"""
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '```json\n{"matches":[{"type":"employee_id","text":"NO.9527","confidence":0.95}]}\n```'
    )
    assert len(matches) == 1
    assert matches[0].type == "employee_id"
    assert matches[0].text == "NO.9527"


def test_detector_parse_json_no_fence() -> None:
    """不带代码块的纯 JSON 输出应正常解析。"""
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '{"matches":[{"type":"phone","text":"13812345678","confidence":0.9}]}'
    )
    assert len(matches) == 1
    assert matches[0].type == "phone"


def test_detector_parse_bare_list_payload() -> None:
    """qwen 系列偶尔直接输出裸数组而非 {"matches":[...]}，应同样解析。"""
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '```json\n[{"type":"employee_id","text":"NO.9527","confidence":0.9}]\n```'
    )
    assert len(matches) == 1
    assert matches[0].type == "employee_id"
    assert matches[0].text == "NO.9527"


def test_detector_parse_trailing_garbage_quote() -> None:
    """模型偶发在 JSON 尾部多输出引号（{"matches":[]}"），应修复解析而非整体丢弃。"""
    detector = SemanticPiiDetector(enabled=True, min_confidence=0.5)
    matches = detector._parse_response(
        '{"matches":[{"type":"phone","text":"13812345678","confidence":0.9}]}"'
    )
    assert len(matches) == 1
    assert matches[0].text == "13812345678"


def test_detector_reasoning_boolean_normalized() -> None:
    """reasoning 传入布尔值（YAML 未加引号的 off/on）应归一化为合法字符串。"""
    assert SemanticPiiDetector(reasoning=False)._reasoning == "off"
    assert SemanticPiiDetector(reasoning=True)._reasoning == "on"
    # 非法值回退 off，非法字符串同样回退
    assert SemanticPiiDetector(reasoning="invalid")._reasoning == "off"
    assert SemanticPiiDetector(reasoning="")._reasoning == "off"


# ---------------------------------------------------------------------------
# 索引加密测试
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402

from src.security.index_crypto import reset_key_cache  # noqa: E402


def _use_key(key: str) -> None:
    """设置测试密钥并重置缓存。"""
    os.environ["MOKOA_SECURITY_KEY"] = key
    reset_key_cache()


def _clear_key() -> None:
    """清除测试密钥并重置缓存。"""
    os.environ.pop("MOKOA_SECURITY_KEY", None)
    reset_key_cache()


def test_index_saved_file_contains_no_plaintext() -> None:
    """落盘索引不得包含明文原文，original_content 应带加密前缀。"""
    _use_key("unit-test-key")
    try:
        index_path = tempfile.mktemp(suffix=".json")
        service = SecurityAndForgetService(index_path=index_path)
        service.filter_sensitive("手机 13812345678")
        service.save_index()

        with open(index_path, encoding="utf-8") as handle:
            data = _json.load(handle)
        serialized = _json.dumps(data, ensure_ascii=False)
        assert "13812345678" not in serialized
        entry = next(iter(data["entries"].values()))
        assert entry["original_content"].startswith("MOKOA1:")
    finally:
        _clear_key()


def test_index_save_load_roundtrip() -> None:
    """加密索引保存后加载，restore 应能完整还原。"""
    _use_key("unit-test-key")
    try:
        index_path = tempfile.mktemp(suffix=".json")
        service = SecurityAndForgetService(index_path=index_path)
        filtered, _, _ = service.filter_sensitive("邮箱 user@test.com")
        service.save_index()

        loaded = SecurityAndForgetService(index_path=index_path)
        assert loaded.restore_sensitive(filtered) == "邮箱 user@test.com"
    finally:
        _clear_key()


def test_index_load_legacy_plaintext_compatible() -> None:
    """历史明文索引（无加密前缀）应能原样加载。"""
    _use_key("unit-test-key")
    try:
        index_path = tempfile.mktemp(suffix=".json")
        legacy = {
            "user_id": "default",
            "entries": {
                "<HASH_abc12345>": {
                    "placeholder_id": "<HASH_abc12345>",
                    "original_content": "13812345678",
                    "sensitive_type": "phone",
                    "created_at": None,
                    "is_deleted": False,
                }
            },
        }
        with open(index_path, "w", encoding="utf-8") as handle:
            _json.dump(legacy, handle, ensure_ascii=False)

        service = SecurityAndForgetService(index_path=index_path)
        assert service.restore_sensitive("手机 <HASH_abc12345>") == "手机 13812345678"
    finally:
        _clear_key()


def test_index_key_change_degrades() -> None:
    """密钥变更后旧条目无法解密，降级为空串且不中断加载。"""
    _use_key("key-a")
    try:
        index_path = tempfile.mktemp(suffix=".json")
        service = SecurityAndForgetService(index_path=index_path)
        service.filter_sensitive("银行卡 6222021234567890")
        service.save_index()
    finally:
        _clear_key()

    _use_key("key-b")
    try:
        service = SecurityAndForgetService(index_path=index_path)
        entry = next(iter(service._index.entries.values()))
        assert entry.original_content == ""
    finally:
        _clear_key()


# ---------------------------------------------------------------------------
# 多用户隔离测试
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from src.security.index_crypto import reset_key_cache  # noqa: E402
from src.security.service import SecurityAndForgetService  # noqa: E402


def test_multi_user_index_per_file() -> None:
    """不同用户应使用不同的索引文件，数据不会互相干扰。"""
    user_a_path = tempfile.mktemp(suffix="_user_a.json")
    user_b_path = tempfile.mktemp(suffix="_user_b.json")
    try:
        service_a = SecurityAndForgetService(
            index_path=user_a_path, user_id="user_a"
        )
        service_b = SecurityAndForgetService(
            index_path=user_b_path, user_id="user_b"
        )
        service_a.filter_sensitive("用户A手机号 13811111111")
        service_b.filter_sensitive("用户B手机号 13822222222")
        service_a.save_index()
        service_b.save_index()

        # A 不能恢复 B 的数据
        b_entry = next(iter(service_b._index.entries.values()))
        assert "13822222222" not in service_a.restore_sensitive(
            f"手机{b_entry.placeholder_id}"
        )
        # B 不能恢复 A 的数据
        a_entry = next(iter(service_a._index.entries.values()))
        assert "13811111111" not in service_b.restore_sensitive(
            f"手机{a_entry.placeholder_id}"
        )
    finally:
        for p in (user_a_path, user_b_path):
            if os.path.exists(p):
                os.unlink(p)


def test_multi_user_forget_isolated() -> None:
    """forget 应只影响调用方所属用户的索引。"""
    _use_key("isolation-test")
    try:
        user_a_path = tempfile.mktemp(suffix="_isolation_a.json")
        user_b_path = tempfile.mktemp(suffix="_isolation_b.json")
        try:
            service_a = SecurityAndForgetService(
                index_path=user_a_path, user_id="user_a"
            )
            service_b = SecurityAndForgetService(
                index_path=user_b_path, user_id="user_b"
            )
            service_a.filter_sensitive("手机号 13811111111")
            service_b.filter_sensitive("手机号 13822222222")

            # user_a 遗忘，不应影响 user_b 的索引
            matched_a = service_a.forget("user_a", "忘记手机")
            assert len(matched_a) >= 1
            service_a.delete_sensitive_entries("user_a", matched_a)

            # user_b 的索引条目应仍完整
            stats_b = service_b.get_sensitive_stats("user_b")
            assert stats_b["active_count"] == 1
        finally:
            for p in (user_a_path, user_b_path):
                if os.path.exists(p):
                    os.unlink(p)
    finally:
        _clear_key()


def test_parser_maps_download_path_preference_key() -> None:
    """『忘掉下载路径偏好』应将 target 映射到 habit.download_path 标准键。"""
    from src.security.service import SecurityAndForgetService

    service = SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json"))
    dl = _mem("m1", "下载路径 = /data/downloads")
    dl.metadata["preference_key"] = "habit.download_path"
    res = service.parse_forget_targets("忘掉下载路径偏好", [dl])
    assert res.targets and res.targets[0].memory_id == "m1"
    assert res.targets[0].preference_key == "habit.download_path"


def test_acceptance_forget_preference_syncs_all_layers() -> None:
    """P0 验收：忘掉下载路径偏好 → 返回真实 ID，版本索引/画像/向量同步删除。"""
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
    from src.preference.store import PreferenceMemoryStore

    storage_dir = tempfile.mkdtemp(suffix="_accept_forget")
    try:
        # 用户 A / B 各自独立的偏好存储与向量库。
        pref_a = PreferenceMemoryStore(storage_dir=storage_dir, use_llm=False, allow_rules=True)
        pref_b = PreferenceMemoryStore(storage_dir=storage_dir, use_llm=False, allow_rules=True)
        orch_a = MemoryOrchestrator(preference=pref_a, milvus=InMemoryMilvusClient())
        orch_b = MemoryOrchestrator(preference=pref_b, milvus=InMemoryMilvusClient())

        res_a = orch_a.extract_preferences_from_text(
            "下载文件默认保存到 C:/data/downloads", user_id="u_a", scene_id="general"
        )
        res_b = orch_b.extract_preferences_from_text(
            "下载文件默认保存到 D:/data/downloads", user_id="u_b", scene_id="general"
        )
        # 规则提取应产出 habit.download_path。
        assert any(
            e.metadata.get("preference_key") == "habit.download_path" for e in res_a.entries
        )
        assert any(
            e.metadata.get("preference_key") == "habit.download_path" for e in res_b.entries
        )
        # 遗忘前：向量可检索、版本索引存在。
        assert any(
            i.metadata.get("preference_key") == "habit.download_path"
            for i in orch_a.list_preferences("u_a", latest_only=False, top_k=10).items
        )
        assert any(r.preference_key == "habit.download_path" for r in pref_a.index.all_records("u_a"))

        # ② 返回真实删除 ID。
        deleted = orch_a.forget("u_a", "忘掉下载路径偏好")
        assert deleted, "forget 应返回真实删除 ID"

        # ③ 版本索引 / 画像 / 向量同步删除。
        assert not any(
            r.preference_key == "habit.download_path" for r in pref_a.index.all_records("u_a")
        )
        assert "habit.download_path" not in pref_a.get_feature_profile("u_a")
        # ④ 删除后再查询返回 0 条。
        after = [
            i
            for i in orch_a.list_preferences("u_a", latest_only=False, top_k=10).items
            if i.metadata.get("preference_key") == "habit.download_path"
        ]
        assert after == []

        # ⑤ 用户 A 遗忘不删用户 B。
        assert any(r.preference_key == "habit.download_path" for r in pref_b.index.all_records("u_b"))
    finally:
        import shutil

        shutil.rmtree(storage_dir, ignore_errors=True)


def test_multi_user_stats_isolated() -> None:
    """get_sensitive_stats 应只统计当前用户的数据。"""
    _use_key("stats-test")
    try:
        path_a = tempfile.mktemp(suffix="_stats_a.json")
        try:
            service_a = SecurityAndForgetService(
                index_path=path_a, user_id="user_a"
            )
            for i in range(3):
                service_a.filter_sensitive(f"手机138{i}0000000{i}")
            stats = service_a.get_sensitive_stats("user_a")
            assert stats["active_count"] == 3
            assert stats["deleted_count"] == 0
        finally:
            if os.path.exists(path_a):
                os.unlink(path_a)
    finally:
        _clear_key()


# ---------------------------------------------------------------------------
# 持久化安全专项测试
# ---------------------------------------------------------------------------


def test_concurrent_index_write() -> None:
    """多线程同时写入同一索引文件不应崩溃。"""
    _use_key("concurrent-test")
    try:
        path = tempfile.mktemp(suffix="_concurrent.json")
        service = SecurityAndForgetService(
            index_path=path, user_id="test_user"
        )

        def write_task(phone: str) -> None:
            service.filter_sensitive(f"手机{phone}")

        threads = [
            threading.Thread(target=write_task, args=(f"138{str(i).zfill(8)}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = service.get_sensitive_stats("test_user")
        assert stats["active_count"] >= 10
    finally:
        _clear_key()


def test_key_file_created(tmp_path: Path, monkeypatch) -> None:
    """首次使用时应自动创建密钥文件。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    key_path = tmp_path / ".mokoa" / "security.key"
    # 确保环境变量不被干扰
    env_backup = os.environ.pop("MOKOA_SECURITY_KEY", None)
    reset_key_cache()
    backup = None
    try:
        # 确保 .mokoa 目录存在，防止权限问题
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # 备份已存在的密钥文件
        backup = None
        if key_path.exists():
            backup = key_path.with_suffix(".bak")
            shutil.move(str(key_path), str(backup))
        # 确保目录存在
        key_path.parent.mkdir(parents=True, exist_ok=True)
        
        service = SecurityAndForgetService(
            index_path=str(tmp_path / "sensitive-index.json")
        )
        service.filter_sensitive("手机 13812345678")
        # 注意：加密是在 save_index() 时触发的，所以要保存才会生成密钥文件
        service.save_index()
        assert key_path.exists(), "密钥文件应被自动创建"
    finally:
        if key_path.exists():
            key_path.unlink()
        # 恢复备份
        if backup and os.path.exists(backup):
            shutil.move(backup, str(key_path))
        if env_backup:
            os.environ["MOKOA_SECURITY_KEY"] = env_backup
        reset_key_cache()


def test_concurrent_index_save() -> None:
    """多线程并发落盘：最终文件应为合法 JSON 且不丢失已提交条目。

    当前 save_index 直接 json.dump 写目标文件（无原子写/无锁），
    本测试用于暴露并发落盘的竞态；若失败说明需要为落盘加保护。
    """
    _use_key("concurrent-save-test")
    try:
        path = tempfile.mktemp(suffix="_concurrent_save.json")
        service = SecurityAndForgetService(index_path=path, user_id="test_user")
        # 先稳定写入 20 条
        for i in range(20):
            service.filter_sensitive(f"手机138{i:08d}")

        def save_task() -> None:
            for _ in range(5):
                service.save_index()

        threads = [threading.Thread(target=save_task) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(path, encoding="utf-8") as handle:
            data = _json.load(handle)
        assert len(data["entries"]) == 20, "并发落盘不应丢失已提交条目"
    finally:
        _clear_key()


def test_index_corrupted_file_degrades() -> None:
    """索引文件损坏（非法 JSON）时加载应回退空索引，不崩溃且可继续工作。"""
    _use_key("corrupt-test")
    try:
        path = tempfile.mktemp(suffix="_corrupt.json")
        Path(path).write_text("{ not valid json !!!", encoding="utf-8")

        service = SecurityAndForgetService(index_path=path)
        assert service._index.get_stats()["total_count"] == 0
        # 仍能正常脱敏
        filtered, detected, _ = service.filter_sensitive("手机 13812345678")
        assert "phone" in detected
    finally:
        _clear_key()


def test_placeholder_not_rematched_by_numeric_rules() -> None:
    """回归测试：占位符 <HASH_xxxxxxxx> 中的 hex 数字段不能被数字类规则二次脱敏。

    曾出现 qq_number 把 <HASH_04709e77> 中的 "04709" 当 QQ 号再次替换，
    产生伪阳性条目并破坏占位符链（见 src/security/service.py 的哨兵保护）。
    """
    _use_key("placeholder-protect")
    try:
        # 场景 A：真实脱敏流程 —— 13800000006 的占位符哈希含纯数字段（04709e77）
        service = SecurityAndForgetService(
            index_path=tempfile.mktemp(suffix=".json")
        )
        out, detected, _ = service.filter_sensitive("手机 13800000006")
        # 占位符完整保留，不再被嵌套脱敏
        assert out == "手机 " + HASH_PATTERN.search(out).group()
        assert HASH_PATTERN.fullmatch(HASH_PATTERN.search(out).group())
        assert "qq_number" not in detected, "占位符数字段不应触发 qq_number 规则"
        assert "phone" in detected
        # 索引中不应出现占位符内部片段作为独立条目
        types = {e.sensitive_type for e in service._index.entries.values()}
        assert types == {"phone"}
        assert service.restore_sensitive(out) == "手机 13800000006"

        # 场景 B：文本中已存在的占位符（纯数字段）应原样保留
        out2, detected2, _ = service.filter_sensitive(
            "编号 <HASH_12345678> 正常内容"
        )
        assert out2 == "编号 <HASH_12345678> 正常内容"
        assert detected2 == []
    finally:
        _clear_key()


def test_placeholder_deterministic_and_idempotent() -> None:
    """同一原文同类型应生成相同占位符，重复过滤不新增索引条目（幂等）。"""
    _use_key("deterministic")
    try:
        service = SecurityAndForgetService(
            index_path=tempfile.mktemp(suffix=".json")
        )
        text = "手机 13812345678，邮箱 user@test.com"

        out1, detected1, _ = service.filter_sensitive(text)
        stats_after_first = service._index.get_stats()["total_count"]

        out2, detected2, _ = service.filter_sensitive(text)
        stats_after_second = service._index.get_stats()["total_count"]

        assert out1 == out2, "同一文本脱敏结果应一致"
        assert detected1 == detected2
        assert stats_after_first == stats_after_second, "重复过滤不应膨胀索引"
        # 还原两次一致
        assert service.restore_sensitive(out1) == text
    finally:
        _clear_key()


def test_multi_user_delete_isolated() -> None:
    """delete_sensitive_entries 应只影响当前用户：A 不能删除 B 的条目。"""
    _use_key("delete-isolation")
    try:
        path_a = tempfile.mktemp(suffix="_del_a.json")
        path_b = tempfile.mktemp(suffix="_del_b.json")
        try:
            service_a = SecurityAndForgetService(
                index_path=path_a, user_id="user_a"
            )
            service_b = SecurityAndForgetService(
                index_path=path_b, user_id="user_b"
            )
            service_a.filter_sensitive("手机 13811111111")
            service_b.filter_sensitive("手机 13822222222")

            b_entry = next(iter(service_b._index.entries.values()))

            # A 尝试删除 B 的条目：user_id 不匹配，应返回空且 B 不受影响
            deleted = service_a.delete_sensitive_entries("user_a", [b_entry.placeholder_id])
            assert deleted == []
            stats_b = service_b.get_sensitive_stats("user_b")
            assert stats_b["active_count"] == 1

            # A 删除自己的条目：只影响 A
            a_entry = next(iter(service_a._index.entries.values()))
            deleted = service_a.delete_sensitive_entries("user_a", [a_entry.placeholder_id])
            assert deleted == [a_entry.placeholder_id]
            stats_a = service_a.get_sensitive_stats("user_a")
            assert stats_a["active_count"] == 0
            # B 的条目仍完整
            assert service_b.get_sensitive_stats("user_b")["active_count"] == 1
        finally:
            for p in (path_a, path_b):
                if os.path.exists(p):
                    os.unlink(p)
    finally:
        _clear_key()


def test_bulk_index_save_load_roundtrip() -> None:
    """批量（50 条混合类型）脱敏 → 落盘 → 重载 → 全部还原，无丢失。"""
    _use_key("bulk-roundtrip")
    try:
        path = tempfile.mktemp(suffix="_bulk.json")
        service = SecurityAndForgetService(index_path=path, user_id="bulk_user")

        originals: dict[str, str] = {}  # 脱敏文本 -> 原文
        for i in range(20):
            text = f"记录{i} 手机138{i:08d}"
            out, _, _ = service.filter_sensitive(text)
            originals[out] = text
        for i in range(20):
            text = f"邮箱 user{i}@test.com"
            out, _, _ = service.filter_sensitive(text)
            originals[out] = text
        for i in range(10):
            text = f"证件 1101011990010112{i:02d}"
            out, _, _ = service.filter_sensitive(text)
            originals[out] = text
        assert len(originals) == 50

        service.save_index()
        loaded = SecurityAndForgetService(index_path=path, user_id="bulk_user")
        stats = loaded.get_sensitive_stats("bulk_user")
        assert stats["active_count"] == 50
        # 每条脱敏文本都能完整还原
        for out, original in originals.items():
            assert loaded.restore_sensitive(out) == original
    finally:
        _clear_key()
