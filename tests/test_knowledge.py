"""Knowledge memory service tests — 记忆库 CRUD 与策略.

Owner: 2abac5a6 | Ref: src/knowledge/memory_service/ + src/core/entry_adapter.py

覆盖:
  - MemoryPolicy 多维评分（隐私风险/临时性/反馈信号）
  - update_memory / delete_memory
  - MilvusClient mock（insert/search/update/delete/soft delete）
  - entry_adapter 双向转换
"""

from __future__ import annotations

import pytest

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.entry_adapter import memory_entry_to_item, memory_item_to_entry
from src.knowledge.memory_service.delete_memory import delete_memory
from src.knowledge.memory_service.memory_policy import MemoryPolicy
from src.knowledge.memory_service.memory_schema import (
    DeleteMemoryRequest,
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    TaskType,
    UpdateMemoryRequest,
)
from src.knowledge.memory_service.in_memory_client import (
    InMemoryMilvusClient as MilvusClient,
)
from src.knowledge.memory_service.update_memory import update_memory


# ---------------------------------------------------------------------------
# MemoryPolicy 评分测试
# ---------------------------------------------------------------------------


def _entry(
    content: str = "用户偏好中文回复",
    memory_type: MemoryTypeTag = MemoryTypeTag.PREFERENCE,
    confidence: float = 0.7,
    source: MemorySource = MemorySource.CHAT,
    **metadata,
) -> MemoryEntry:
    return MemoryEntry(
        content=content,
        memory_type=memory_type,
        source=source,
        confidence=confidence,
        metadata=metadata,
    )


def test_policy_preference_bonus() -> None:
    policy = MemoryPolicy()
    score = policy.score_save(_entry())
    # 0.7 + 0.15(preference) + 0.1(chat)
    assert score == pytest.approx(0.95)


def test_policy_privacy_risk_reduces_score() -> None:
    policy = MemoryPolicy()
    score = policy.score_save(_entry(privacy_risk=True))
    assert score < policy.SAVE_THRESHOLD


def test_policy_temporary_reduces_score() -> None:
    policy = MemoryPolicy()
    score = policy.score_save(_entry(temporary=True))
    assert score == pytest.approx(0.7 + 0.15 + 0.1 - 0.4)


def test_policy_feedback_negative_signal_should_save() -> None:
    policy = MemoryPolicy()
    entry = _entry(
        content="刚才只是测试，不应保存这个文件路径",
        memory_type=MemoryTypeTag.FEEDBACK,
    )
    should_save, score, reason = policy.should_save(entry)
    assert should_save is True
    assert "反馈" in reason


def test_policy_low_confidence_not_saved() -> None:
    policy = MemoryPolicy()
    entry = _entry(confidence=0.1, source=MemorySource.PDF)
    should_save, score, _ = policy.should_save(entry)
    assert should_save is False
    assert score < policy.SAVE_THRESHOLD


def test_policy_score_clamped_to_unit() -> None:
    policy = MemoryPolicy()
    entry = _entry(confidence=1.0)
    score = policy.score_save(entry)
    assert 0.0 <= score <= 1.0


def test_policy_should_update_only_on_change() -> None:
    policy = MemoryPolicy()
    assert policy.should_update("相同内容", "相同内容") is False
    assert policy.should_update("旧内容", "新内容") is True


def test_policy_same_type_should_merge() -> None:
    policy = MemoryPolicy()
    assert policy.should_merge("preference", "preference") is True
    assert policy.should_merge("preference", "tool_call") is False


# ---------------------------------------------------------------------------
# MilvusClient mock 测试
# ---------------------------------------------------------------------------


def test_milvus_insert_and_get() -> None:
    client = MilvusClient()
    assert client.insert("m1", "内容", [1.0, 0.0], {"user_id": "u1"}) is True
    record = client.get_by_id("m1")
    assert record is not None
    assert record["content"] == "内容"
    assert record["user_id"] == "u1"


def test_milvus_search_filters_by_user() -> None:
    client = MilvusClient()
    client.insert("m1", "a", [1.0, 0.0], {"user_id": "u1"})
    client.insert("m2", "b", [1.0, 0.0], {"user_id": "u2"})
    hits = client.search(None, filters={"user_id": "u1"}, top_k=5)
    assert [hit["memory_id"] for hit in hits] == ["m1"]


def test_milvus_search_ranks_by_cosine() -> None:
    client = MilvusClient()
    client.insert("m1", "a", [1.0, 0.0], {"user_id": "u1"})
    client.insert("m2", "b", [0.0, 1.0], {"user_id": "u1"})
    hits = client.search([0.9, 0.1], filters={"user_id": "u1"}, top_k=2)
    assert hits[0]["memory_id"] == "m1"  # 与查询向量夹角更小
    assert hits[0]["score"] > hits[1]["score"]


def test_milvus_soft_delete_hidden_from_search() -> None:
    client = MilvusClient()
    client.insert("m1", "a", [1.0, 0.0], {"user_id": "u1"})
    assert client.delete("m1", soft=True) is True
    assert client.search(None, filters={}, top_k=5) == []
    # Match the public real-Milvus contract: soft-deleted rows are hidden from
    # both search and get_by_id, while remaining available to storage internals.
    assert client.get_by_id("m1") is None


def test_milvus_physical_delete_removes_record() -> None:
    client = MilvusClient()
    client.insert("m1", "a", [1.0, 0.0], {"user_id": "u1"})
    assert client.delete("m1", soft=False) is True
    assert client.get_by_id("m1") is None
    assert client.delete("nonexistent", soft=False) is False


def test_milvus_update_version_bump() -> None:
    client = MilvusClient()
    client.insert("m1", "旧", [1.0, 0.0], {"user_id": "u1", "version": 1})
    assert client.update("m1", {"content": "新", "version": 2}) is True
    assert client.get_by_id("m1")["content"] == "新"
    assert client.update("missing", {}) is False


# ---------------------------------------------------------------------------
# update_memory 测试
# ---------------------------------------------------------------------------


def test_update_memory_missing_id_returns_not_updated() -> None:
    response = update_memory(
        UpdateMemoryRequest(memory_id="missing"), milvus=MilvusClient()
    )
    assert response.updated is False


def test_update_memory_content_refresh() -> None:
    milvus = MilvusClient()
    milvus.insert("m1", "旧内容", [1.0, 0.0], {"user_id": "u1", "version": 1})
    response = update_memory(
        UpdateMemoryRequest(memory_id="m1", content="新内容"),
        milvus=milvus,
        embedding=_FakeEmbedder(),
    )
    assert response.updated is True
    assert response.old_content == "旧内容"
    assert response.new_content == "新内容"
    assert milvus.get_by_id("m1")["version"] == 2


def test_update_memory_unchanged_content_keeps_version() -> None:
    milvus = MilvusClient()
    milvus.insert("m1", "相同", [1.0, 0.0], {"user_id": "u1", "version": 3})
    response = update_memory(
        UpdateMemoryRequest(memory_id="m1", content="相同"),
        milvus=milvus,
        embedding=_FakeEmbedder(),
    )
    assert response.updated is True
    assert milvus.get_by_id("m1")["version"] == 3


def test_update_memory_metadata_merge() -> None:
    milvus = MilvusClient()
    milvus.insert("m1", "内容", [1.0, 0.0], {"user_id": "u1"})
    response = update_memory(
        UpdateMemoryRequest(memory_id="m1", metadata={"is_latest": False}),
        milvus=milvus,
    )
    assert response.updated is True
    assert milvus.get_by_id("m1")["is_latest"] is False


class _FakeEmbedder:
    """update_memory 的测试替身：返回定长向量。"""

    def embed(self, text: str) -> list[float]:
        return [0.5, 0.5]


# ---------------------------------------------------------------------------
# delete_memory 测试
# ---------------------------------------------------------------------------


def test_delete_memory_by_id_soft() -> None:
    milvus = MilvusClient()
    milvus.insert("m1", "内容", [1.0, 0.0], {"user_id": "u1"})
    response = delete_memory(DeleteMemoryRequest(memory_id="m1"), milvus=milvus)
    assert response.deleted_ids == ["m1"]
    assert response.count == 1
    assert milvus.get_by_id("m1") is None


def test_delete_memory_missing_id_noop() -> None:
    response = delete_memory(
        DeleteMemoryRequest(memory_id="missing"), milvus=MilvusClient()
    )
    assert response.count == 0


# ---------------------------------------------------------------------------
# entry_adapter 双向转换测试
# ---------------------------------------------------------------------------


def test_memory_item_to_entry_maps_types() -> None:
    item = MemoryItem(
        id="i1",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="喜欢中文回答",
        user_id="u1",
        scene_id="coding-session",
        metadata={
            "source_type": "manual_config",
            "config_key": "reply_language",
            "confidence": 0.9,
        },
    )
    entry = memory_item_to_entry(item)
    assert entry.memory_type == MemoryTypeTag.PREFERENCE
    assert entry.source == MemorySource.MANUAL
    assert entry.task_type == TaskType.CODING  # scene_id 含 coding 提示
    assert entry.metadata["preference_key"] == "reply_language"


def test_memory_item_to_entry_tool_result_task() -> None:
    item = MemoryItem(
        id="i2",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content="pip 超时换源",
        user_id="u1",
        metadata={"source_type": "tool_result"},
    )
    entry = memory_item_to_entry(item)
    assert entry.memory_type == MemoryTypeTag.TOOL_CALL
    assert entry.task_type == TaskType.OS_OPERATION


def test_memory_item_to_entry_scene_task_hint() -> None:
    item = MemoryItem(
        id="i3",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content="论文排版技巧",
        user_id="u1",
        scene_id="paper-session",
        metadata={},
    )
    entry = memory_item_to_entry(item)
    assert entry.task_type == TaskType.PAPER_WRITING


def test_memory_entry_to_item_roundtrip() -> None:
    entry = MemoryEntry(
        content="用户偏好 D 盘下载",
        memory_type=MemoryTypeTag.PREFERENCE,
        source=MemorySource.MANUAL,
        user_id="u1",
        scene_id="os_operation",
        confidence=1.0,
        metadata={"version": 2},
    )
    item = memory_entry_to_item(entry, memory_id="m-42")
    assert item.id == "m-42"
    assert item.memory_type == MemoryType.PREFERENCE
    assert item.layer == MemoryLayer.LONG
    assert item.version == 2
    assert item.metadata["milvus_memory_id"] == "m-42"
    assert item.metadata["is_latest"] is True


def test_memory_entry_to_item_knowledge_type() -> None:
    entry = MemoryEntry(
        content="Ubuntu 安装 ROS2",
        memory_type=MemoryTypeTag.TOOL_CALL,
        source=MemorySource.AGENT_LOG,
        user_id="u1",
    )
    item = memory_entry_to_item(entry, memory_id="m-1")
    assert item.memory_type == MemoryType.KNOWLEDGE
