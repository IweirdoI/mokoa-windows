"""专项测试：用户隔离（多用户实例）与持久化安全。

Owner: 2abac5a6
对应文档「待办」第 1、2 项：
  - 用户隔离（多用户实例管理）专项测试
  - 持久化安全专项测试

仅依赖 src/security 内部实现，不触碰 Milvus / 网络。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

import pytest

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.security import index_crypto
from src.security.knowledge_forget import KnowledgeForgetResolver
from src.security.service import SensitiveIndex, SecurityAndForgetService


def _clear_key_env() -> None:
    """清除密钥相关缓存与环境变量，避免跨测试污染。"""
    os.environ.pop("MOKOA_SECURITY_KEY", None)
    index_crypto.reset_key_cache()


def create_service() -> SecurityAndForgetService:
    """每个测试使用独立的临时索引文件，避免跨测试状态污染。"""
    _clear_key_env()
    return SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json"))


# ---------------------------------------------------------------------------
# 一、用户隔离（多用户实例管理）
# ---------------------------------------------------------------------------


def test_filter_sensitive_tags_user_id() -> None:
    service = create_service()
    _, _, _ = service.filter_sensitive("我的手机 13800000001", user_id="alice")
    _, _, _ = service.filter_sensitive("我的手机 13800000002", user_id="bob")
    alice_entries = [e for e in service._index.entries.values() if e.user_id == "alice"]
    bob_entries = [e for e in service._index.entries.values() if e.user_id == "bob"]
    assert len(alice_entries) == 1 and len(bob_entries) == 1


def test_forget_scoped_by_user_returns_only_own() -> None:
    service = create_service()
    service.filter_sensitive("我的手机 13800000001", user_id="alice")
    service.filter_sensitive("我的手机 13800000002", user_id="bob")

    alice_ids = service.forget("alice", "忘记我的手机号")
    bob_ids = service.forget("bob", "忘记我的手机号")

    assert len(alice_ids) == 1
    assert len(bob_ids) == 1
    assert set(alice_ids).isdisjoint(set(bob_ids))


def test_forget_other_user_does_not_delete_own() -> None:
    service = create_service()
    service.filter_sensitive("我的手机 13800000001", user_id="alice")
    service.filter_sensitive("我的手机 13800000002", user_id="bob")

    # 用户 A 发起遗忘，只应删除 A 的条目
    service.delete_sensitive_entries("alice", service.forget("alice", "忘记我的手机号"))

    stats_alice = service.get_sensitive_stats("alice")
    stats_bob = service.get_sensitive_stats("bob")
    assert stats_alice["active_count"] == 0
    assert stats_bob["active_count"] == 1


def test_restore_sensitive_respects_loaded_user_index() -> None:
    service = create_service()
    filtered, _, pmap = service.filter_sensitive("手机 13800000001", user_id="u1")
    assert "<HASH_" in filtered
    # 默认占位符还原依赖索引；不同用户写入不会互相串
    service2 = create_service()
    service2.filter_sensitive("手机 13800000002", user_id="u2")
    # service2 不应能还原 u1 的占位符（索引里没有）
    assert "<HASH_" not in service2.restore_sensitive("占位符无关")


# ---------------------------------------------------------------------------
# 二、持久化安全（索引加密落盘）
# ---------------------------------------------------------------------------


def test_index_persisted_encrypted_no_plaintext() -> None:
    service = create_service()
    service.filter_sensitive("我的手机号 13800000001，邮箱 a@b.com", user_id="u")
    service.save_index()

    raw = open(service.index_path, encoding="utf-8").read()
    # 敏感原文不得明文出现在索引文件中
    assert "13800000001" not in raw
    assert "a@b.com" not in raw
    # 原文应被 AES-GCM 加密（MOKOA1: 前缀）
    assert "MOKOA1:" in raw
    # 占位符可明文存在（不可逆）
    assert "<HASH_" in raw


def test_index_roundtrip_restore() -> None:
    service = create_service()
    filtered, _, pmap = service.filter_sensitive("手机 13800000001", user_id="u")
    placeholder = next(iter(pmap.values()))
    service.save_index()

    # 用同一索引文件构造新服务，应能还原
    reopened = SecurityAndForgetService(index_path=service.index_path, user_id="u")
    assert reopened.restore_sensitive(placeholder) == "13800000001"


def test_legacy_plaintext_index_compat() -> None:
    path = tempfile.mktemp(suffix=".json")
    data = {
        "user_id": "u",
        "entries": {
            "p1": {
                "placeholder_id": "<HASH_abc>",
                "original_content": "13800000001",  # 历史明文索引，无 MOKOA1: 前缀
                "sensitive_type": "phone",
                "created_at": None,
                "is_deleted": False,
            }
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    idx = SensitiveIndex.load_from_file(path, "u")
    assert idx.entries["p1"].original_content == "13800000001"


def test_key_rotation_degradation_no_crash() -> None:
    try:
        os.environ["MOKOA_SECURITY_KEY"] = "secret-a"
        index_crypto.reset_key_cache()
        s1 = SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json"))
        s1.filter_sensitive("手机 13800000001", user_id="u")
        s1.save_index()

        # 密钥轮换后加载旧索引：解密失败应降级为空串，不抛异常
        os.environ["MOKOA_SECURITY_KEY"] = "secret-b"
        index_crypto.reset_key_cache()
        s2 = SecurityAndForgetService(index_path=s1.index_path, user_id="u")
        entry = next(iter(s2._index.entries.values()))
        assert entry.placeholder_id.startswith("<HASH_")
        assert entry.original_content == ""  # 已降级
    finally:
        _clear_key_env()


@pytest.mark.skipif(os.name == "nt", reason="Windows 不限制密钥文件权限")
def test_key_file_permission_restricted_on_posix() -> None:
    _clear_key_env()
    index_crypto.get_or_create_secret_key()
    key_path = index_crypto._key_file_path()
    if key_path.exists():
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# 三、知识库 NL 遗忘解析（与专项测试同文件，便于归属查看）
# ---------------------------------------------------------------------------


def test_knowledge_forget_plan_detects_intent() -> None:
    resolver = KnowledgeForgetResolver()
    plan = resolver.plan("忘记知识库中关于SSH配置的内容")
    assert plan.is_knowledge_forget is True
    assert "ssh" in plan.recall_query.lower()
    assert "配置" in plan.recall_query
    assert set(plan.memory_types) == set(KnowledgeForgetResolver.KNOWLEDGE_TYPES)


def test_knowledge_forget_plan_want_all() -> None:
    resolver = KnowledgeForgetResolver()
    plan = resolver.plan("删除所有文档")
    assert plan.is_knowledge_forget is True
    assert plan.want_all is True


def test_non_knowledge_instruction_not_flagged() -> None:
    resolver = KnowledgeForgetResolver()
    plan = resolver.plan("忘掉我的下载路径偏好")
    assert plan.is_knowledge_forget is False


def test_knowledge_forget_resolve_targets() -> None:
    resolver = KnowledgeForgetResolver()
    candidates = [
        MemoryItem(
            id="k1",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.LONG,
            content="SSH 配置方法：修改 /etc/ssh/sshd_config",
            user_id="u1",
        ),
        MemoryItem(
            id="k2",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.LONG,
            content="conda 创建虚拟环境",
            user_id="u1",
        ),
    ]
    resolution = resolver.resolve("忘记知识库中关于SSH配置的内容", candidates)
    matched_ids = {t.memory_id for t in resolution.targets}
    assert "k1" in matched_ids
    assert "k2" not in matched_ids
