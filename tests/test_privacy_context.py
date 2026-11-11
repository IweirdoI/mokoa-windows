"""Additional synthetic contexts beyond the original candidate diagnostic corpus."""
import pytest

from src.security.service import SecurityAndForgetService, detect_privacy_risk, redact_sensitive_text


@pytest.mark.parametrize("text, secret", [
    ("联系号码：159 2468 3579", "159 2468 3579"),
    ("请联系176-2345-6789。", "176-2345-6789"),
    ("手机：一七六二三四五六七八九", "一七六二三四五六七八九"),
    ("电话为幺五九二四六八三五七九", "幺五九二四六八三五七九"),
    ("生日是1997-08-23", "1997-08-23"),
    ("出生日期为2002年6月9日", "2002年6月9日"),
    ("date of birth: 1986/11/03", "1986/11/03"),
    ("QQ号：65432", "65432"),
])
def test_context_sensitive_roundtrip(text, secret, tmp_path):
    service = SecurityAndForgetService(index_path=str(tmp_path / "index.json"))
    result, _, _ = service.filter_sensitive(text)
    assert secret not in result
    assert secret not in redact_sensitive_text(text)
    assert service.restore_sensitive(result) == text


@pytest.mark.parametrize("text", [
    "会议时间：2027-03-16，上午九点。",
    "2028/02/21 是计划发布日期",
    "生日会时间：2027-03-16",  # Event date, not an asserted date of birth.
    "端口：54321", "计数：65432", "总字数 76543",
    "念数字一七六二三四五六七八九",  # No telephone context.
    "版本号1590-2468-3579",  # Twelve digits, not the mobile layout.
    "拨打159-2468-35790",  # Do not partially mask a longer number.
    "北京的天气怎么样", "清华大学镜像源", "文件位于E:/Downloads",
])
def test_benign_context_is_preserved(text):
    assert redact_sensitive_text(text) == text


def test_spoken_phone_also_reaches_ingestion_privacy_gate():
    assert detect_privacy_risk("手机是幺五九二四六八三五七九")
