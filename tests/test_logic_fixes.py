"""逻辑修复回归测试：覆盖验收报告中 P1 逻辑问题与现实使用场景。

这些测试不加载真实 Embedding 模型，统一走确定性 hash 后端（autouse fixture）。
重点验证三处逻辑修复：
  1. recall 的 filters 不得覆盖可信 user_id（跨用户泄露）；
  2. /store-entry 直写入库必经脱敏；
  3. 知识类按 record_key 实体维度做冲突消解（替换过期事实）。
并补充多用户隔离、偏好版本链、ingest→consolidate→recall 等真实链路。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.common.models import RetrievalQuery
from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.memory_schema import (
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
)
from api.server import app as api_app


@pytest.fixture(autouse=True)
def _hash_embedding(monkeypatch) -> None:
    """避免测试加载真实 BGE 模型，统一走确定性 hash 后端。"""

    monkeypatch.setenv("OS_AGENT_EMBEDDING_BACKEND", "hash")


def _knowledge_entry(
    content: str,
    *,
    user_id: str,
    record_key: str,
    source=MemorySource.MANUAL,
    scene_id=None,
) -> MemoryEntry:
    return MemoryEntry(
        content=content,
        memory_type=MemoryTypeTag.DOCUMENT_CHUNK,
        source=source,
        user_id=user_id,
        scene_id=scene_id,
        metadata={"record_key": record_key},
    )


def _recall(orch: MemoryOrchestrator, user_id: str, *, top_k: int = 20):
    return orch.recall(RetrievalQuery(query="", user_id=user_id, top_k=top_k))


# --------------------------------------------------------------------------- #
# 1. recall 的 filters 不得覆盖可信 user_id（跨用户泄露）
# --------------------------------------------------------------------------- #
def test_recall_filters_cannot_override_user_id() -> None:
    orch = MemoryOrchestrator()
    orch.store_memory_entry(_knowledge_entry("A 的专属知识", user_id="alice", record_key="a_secret"))
    orch.store_memory_entry(_knowledge_entry("B 的专属知识", user_id="bob", record_key="b_secret"))

    # bob 试图用 filters 覆盖 user_id 读取 alice 数据
    result = orch.recall(
        RetrievalQuery(query="", user_id="bob", top_k=20),
        filters={"user_id": "alice"},
    )
    assert result.items, "bob 至少应看到自己的数据"
    assert all(item.user_id == "bob" for item in result.items)
    assert not any("A 的专属知识" in item.content for item in result.items)


def test_recall_other_user_is_empty() -> None:
    orch = MemoryOrchestrator()
    orch.store_memory_entry(_knowledge_entry("A 数据", user_id="alice", record_key="k_a"))
    result = _recall(orch, "bob")
    assert result.items == []


def test_multi_user_isolation_on_recall() -> None:
    orch = MemoryOrchestrator()
    orch.store_memory_entry(_knowledge_entry("A 数据", user_id="alice", record_key="k_a"))
    orch.store_memory_entry(_knowledge_entry("B 数据", user_id="bob", record_key="k_b"))
    a = _recall(orch, "alice")
    b = _recall(orch, "bob")
    assert all(i.user_id == "alice" for i in a.items)
    assert all(i.user_id == "bob" for i in b.items)
    assert "B 数据" not in [i.content for i in a.items]


# --------------------------------------------------------------------------- #
# 2. /store-entry 直写入库必经脱敏
# --------------------------------------------------------------------------- #
def test_store_entry_applies_sensitive_filtering() -> None:
    orch = MemoryOrchestrator()
    entry = MemoryEntry(
        content="我的手机号是 13800138000",
        memory_type=MemoryTypeTag.DOCUMENT_CHUNK,
        source=MemorySource.MANUAL,
        user_id="u1",
        metadata={"record_key": "phone_note"},
    )
    orch.store_memory_entry(entry)
    result = _recall(orch, "u1")
    assert len(result.items) == 1
    content = result.items[0].content
    assert "13800138000" not in content
    assert "<HASH_" in content


def test_store_entry_sensitive_text_not_recallable_in_plaintext() -> None:
    """即使经非 /ingest 入口写入，召回也不应返回明文手机号。"""
    orch = MemoryOrchestrator()
    orch.store_memory_entry(
        MemoryEntry(
            content="紧急联系人 13912345678",
            memory_type=MemoryTypeTag.DOCUMENT_CHUNK,
            source=MemorySource.MANUAL,
            user_id="u2",
            metadata={"record_key": "contact"},
        )
    )
    result = _recall(orch, "u2")
    assert all("13912345678" not in item.content for item in result.items)


# --------------------------------------------------------------------------- #
# 3. 知识类按 record_key 实体维度冲突消解
# --------------------------------------------------------------------------- #
def test_knowledge_conflict_replaces_stale_fact() -> None:
    orch = MemoryOrchestrator()
    r1 = orch.store_memory_entry(_knowledge_entry("服务端口为 8080", user_id="u1", record_key="help_service_port"))
    assert r1.created is True
    r2 = orch.store_memory_entry(
        _knowledge_entry("服务端口已改为 9090，旧端口不再使用", user_id="u1", record_key="help_service_port")
    )
    assert r2.created is True
    assert r2.reason == "knowledge_replaced"

    result = _recall(orch, "u1")
    contents = [item.content for item in result.items]
    assert "服务端口为 8080" not in contents
    assert "服务端口已改为 9090，旧端口不再使用" in contents
    # 旧事实被软删除，召回只返回最新一条
    assert len(result.items) == 1


def test_knowledge_conflict_unchanged_when_same_content() -> None:
    orch = MemoryOrchestrator()
    orch.store_memory_entry(_knowledge_entry("端口 8080", user_id="u1", record_key="port"))
    r2 = orch.store_memory_entry(_knowledge_entry("端口 8080", user_id="u1", record_key="port"))
    assert r2.created is False
    assert r2.reason == "unchanged_knowledge"
    assert len(_recall(orch, "u1").items) == 1


def test_knowledge_conflict_keeps_more_trusted_old() -> None:
    orch = MemoryOrchestrator()
    old = _knowledge_entry("端口 8080", user_id="u1", record_key="port")
    old.metadata["source_type"] = "manual_config"  # 高信任
    orch.store_memory_entry(old)

    new = _knowledge_entry("端口 9090", user_id="u1", record_key="port")
    new.metadata["source_type"] = "conversation"  # 低信任
    r2 = orch.store_memory_entry(new)
    assert r2.created is False  # 低信任新值不覆盖高信任旧值
    assert "端口 8080" in _recall(orch, "u1").items[0].content


def test_knowledge_without_record_key_still_written() -> None:
    """无 record_key 视为不可消歧的独立知识，直接写入（保持原行为）。"""
    orch = MemoryOrchestrator()
    r = orch.store_memory_entry(
        MemoryEntry(
            content="一段无法消歧的零散知识",
            memory_type=MemoryTypeTag.DOCUMENT_CHUNK,
            source=MemorySource.MANUAL,
            user_id="u1",
        )
    )
    assert r.created is True
    assert len(_recall(orch, "u1").items) == 1


# --------------------------------------------------------------------------- #
# 4. 偏好版本链（真实使用场景）
# --------------------------------------------------------------------------- #
def test_preference_version_chain_keeps_latest() -> None:
    orch = MemoryOrchestrator()
    orch.store_memory_entry(
        MemoryEntry(
            content="喜欢中文",
            memory_type=MemoryTypeTag.PREFERENCE,
            source=MemorySource.CHAT,
            user_id="u1",
            metadata={"preference_key": "reply.lang", "confidence": 0.9, "preference_value": "中文"},
        )
    )
    orch.store_memory_entry(
        MemoryEntry(
            content="喜欢英文",
            memory_type=MemoryTypeTag.PREFERENCE,
            source=MemorySource.CHAT,
            user_id="u1",
            metadata={"preference_key": "reply.lang", "confidence": 0.9, "preference_value": "英文"},
        )
    )
    profile = orch.get_preference_profile("u1")
    assert profile["kv"]["reply.lang"] == "英文"

    history = orch.get_preference_history("u1", "reply.lang")
    assert len(history) == 2
    latest = [h for h in history if h.metadata.get("is_latest")][0]
    assert latest.content == "喜欢英文"


# --------------------------------------------------------------------------- #
# 5. 端到端 ingest → consolidate → recall
# --------------------------------------------------------------------------- #
def test_ingest_consolidate_recall_knowledge() -> None:
    from src.core.factory import create_orchestrator
    from src.ingestion.pipeline import IngestionPipeline
    from src.core.layer_manager import LayerManager

    orch = create_orchestrator()
    orch.ingestion = IngestionPipeline()
    orch.layer_manager = LayerManager()

    res = orch.process_input(
        {"key": "service_port", "value": "8080", "confidence": 0.9},
        "manual_config",
        user_id="u1",
    )
    assert res.accepted
    orch.consolidate("u1")
    result = _recall(orch, "u1")
    assert any("8080" in item.content for item in result.items)


# --------------------------------------------------------------------------- #
# 6. API 层端到端验证（/store-entry 脱敏 + /recall 用户隔离）
# --------------------------------------------------------------------------- #
def test_api_store_entry_desensitizes() -> None:
    client = TestClient(api_app)
    resp = client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "我的手机号是 13800138000",
                "memory_type": "document_chunk",
                "source": "manual",
                "user_id": "api_u1",
                "metadata": {"record_key": "api_phone"},
            }
        },
    )
    assert resp.status_code == 200
    rec = client.post("/recall", json={"user_id": "api_u1", "query": "", "top_k": 10})
    items = rec.json()["items"]
    assert items and "13800138000" not in items[0]["content"]


def test_api_recall_cannot_override_user_via_filters() -> None:
    client = TestClient(api_app)
    client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "alice 数据",
                "memory_type": "document_chunk",
                "source": "manual",
                "user_id": "api_alice",
                "metadata": {"record_key": "ka"},
            }
        },
    )
    client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "bob 数据",
                "memory_type": "document_chunk",
                "source": "manual",
                "user_id": "api_bob",
                "metadata": {"record_key": "kb"},
            }
        },
    )
    rec = client.post(
        "/recall",
        json={"user_id": "api_bob", "query": "", "top_k": 10, "filters": {"user_id": "api_alice"}},
    )
    items = rec.json()["items"]
    assert all(i["user_id"] == "api_bob" for i in items)
    assert not any("alice 数据" in i["content"] for i in items)
