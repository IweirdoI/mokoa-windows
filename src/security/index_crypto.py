"""敏感索引加密工具（AES-GCM）。

Owner: 2abac5a6

密钥来源：环境变量 MOKOA_SECURITY_KEY 优先，兜底本地密钥文件 ~/.mokoa/security.key
（首次自动生成，非 Windows 平台限制 0600 权限）。
加密范围：仅 original_content（敏感原文），placeholder_id/type 等留明文，
保证索引查询与统计性能不受影响。
密文格式：MOKOA1:<base64(nonce)>.<base64(ciphertext+tag)>，前缀用于区分明文
（兼容历史明文索引）与版本识别。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# 密文前缀：标识加密版本，同时用于区分明文（兼容旧索引）
CIPHER_PREFIX = "MOKOA1:"
ENV_KEY = "MOKOA_SECURITY_KEY"
_KEY_DIR = ".mokoa"
_KEY_FILENAME = "security.key"
_KEY_BYTES = 32  # AES-256

_key_cache: bytes | None = None


def _key_file_path() -> Path:
    """本地密钥文件路径。"""
    return Path.home() / _KEY_DIR / _KEY_FILENAME


def get_or_create_secret_key() -> bytes:
    """获取加密密钥：环境变量优先，兜底本地密钥文件（首次自动生成）。

    密钥文件不可写时退化为进程内随机密钥（仅当前进程可解密）。
    """
    global _key_cache
    if _key_cache is not None:
        return _key_cache

    env_key = os.environ.get(ENV_KEY)
    if env_key:
        # 任意长度口令 -> SHA256 派生 32 字节
        _key_cache = hashlib.sha256(env_key.encode("utf-8")).digest()
        return _key_cache

    key_path = _key_file_path()
    if key_path.exists():
        data = key_path.read_bytes()
        _key_cache = data if len(data) == _KEY_BYTES else hashlib.sha256(data).digest()
        return _key_cache

    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        raw = os.urandom(_KEY_BYTES)
        key_path.write_bytes(raw)
        if sys.platform != "win32":
            key_path.chmod(0o600)
        _key_cache = raw
        logger.info("已生成敏感索引加密密钥: %s", key_path)
    except OSError:
        # 密钥文件不可写时使用临时内存密钥（进程内可用，重启后旧索引无法解密）
        _key_cache = os.urandom(_KEY_BYTES)
        logger.warning("密钥文件创建失败，改用进程内随机密钥（重启后旧索引无法解密）")
    return _key_cache


def reset_key_cache() -> None:
    """重置密钥缓存（测试/密钥轮换场景使用）。"""
    global _key_cache
    _key_cache = None


def encrypt_original(text: str) -> str:
    """加密原文，返回带前缀的密文；空串不加密。"""
    if not text:
        return text
    key = get_or_create_secret_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    token = (
        base64.b64encode(nonce).decode("ascii")
        + "."
        + base64.b64encode(ciphertext).decode("ascii")
    )
    return CIPHER_PREFIX + token


def decrypt_original(token: str) -> str:
    """解密原文。

    无前缀视为明文（兼容历史索引）；解密失败（密钥变更/数据损坏）返回空串降级，
    由调用方决定如何处置，不抛出异常中断索引加载。
    """
    if not token or not token.startswith(CIPHER_PREFIX):
        return token
    try:
        payload = token[len(CIPHER_PREFIX):]
        nonce_b64, ciphertext_b64 = payload.split(".", 1)
        key = get_or_create_secret_key()
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(nonce_b64),
            base64.b64decode(ciphertext_b64),
            None,
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, ValueError, KeyError) as exc:
        logger.warning("敏感索引条目解密失败（密钥变更或数据损坏），该条无法还原: %s", exc)
        return ""
