"""Shared last-mile rules for external text and local-only raw-image/PII calls."""
import ipaddress
import re
import socket
from urllib.parse import urlsplit


_SECRET_FIELD = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|"
    r"access[_-]?token|private[_-]?token|personal[_-]?access[_-]?token|"
    r"codeup[_-]?token|client[_-]?secret|authorization|cookie)", re.I
)


def is_loopback_service(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"http", "https"} and not parsed.username
                and not parsed.password and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
    except ValueError:
        return False


def _is_public_ip(ip: str) -> bool:
    """地址字面量是否可路由的公网地址（非回环/私有/链路本地/保留/未指定）。"""

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        return False
    return True


def is_external_url_safe(url: str) -> bool:
    """仅允许指向公网可路由地址的出站 URL，阻断 SSRF 常见目标。

    覆盖：回环（127.0.0.1/::1/localhost）、私有网段（10/8、172.16/12、
    192.168/16、100.64/10）、链路本地（169.254/16 含云元数据 169.254.169.254、
    fe80::/10）、未指定/保留地址，以及带 userinfo 的 URL（可能夹带凭据）。
    对主机名做 DNS 解析并校验所有解析结果均为公网地址，降低内网主机名绕过风险。
    """

    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return False
    if not infos:
        return False
    return all(_is_public_ip(info[4][0]) for info in infos)


def protect_payload(value):
    """Redact strings, including metadata keys, without recording raw values."""
    from src.security.service import redact_sensitive_text
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, (list, tuple)):
        return [protect_payload(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "input_image", "input_audio", "file", "input_file"}:
            raise ValueError("未经本地脱敏的附件不能通过文本模型接口外发。")
        return {
            protect_payload(key): (
                "[CREDENTIAL_REDACTED]"
                if isinstance(key, str) and _SECRET_FIELD.fullmatch(key)
                else protect_payload(item)
            )
            for key, item in value.items()
        }
    return value
