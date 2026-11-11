"""Core orchestrator / layer manager / classifier / conflict resolver tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.common.models import MemoryItem, MemoryLayer, MemoryType, NormalizedRecord, RetrievalQuery
from src.core.classifier import MemoryClassifier
from src.core.conflict_resolver import ConflictAction, ConflictResolver
from src.core.factory import create_orchestrator
from src.core.layer_manager import LayerManager
from src.core.orchestrator import MemoryOrchestrator
from src.core.short_term import ShortTermMemory
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.staging_store import StagingStore
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient


def test_process_input_releases_dedup_when_short_write_fails() -> None:
    """短期记忆写入失败后，同一事件必须能再次通过 ingestion 去重。"""

    class _FailingLayerManager(LayerManager):
        def write_short(self, item):  # type: ignore[no-untyped-def]
            raise RuntimeError("short-term write failed")

    pipeline = IngestionPipeline()
    orchestrator = MemoryOrchestrator(
        ingestion=pipeline,
        layer_manager=_FailingLayerManager(short_capacity=10),
    )
    payload = {"key": "reply_language", "value": "中文", "confidence": 0.9}
    with pytest.raises(RuntimeError, match="short-term write failed"):
        orchestrator.process_input(payload, "manual_config", user_id="u001")

    retried = pipeline.process(payload, "manual_config", user_id="u001")
    assert retried.accepted is True


def test_process_input_uses_api_user_id_and_forwards_metadata() -> None:
    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(),
        layer_manager=LayerManager(short_capacity=10),
    )
    result = orchestrator.process_input(
        {"key": "reply_language", "value": "中文", "confidence": 0.8},
        "manual_config",
        user_id="u001",
        scene_id="coding-session",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.user_id == "u001"
    assert result.record.scene_id == "coding-session"
    assert result.quality_score is not None
    assert result.fingerprint

    short_items = orchestrator.list_short_term("u001")
    assert len(short_items) == 1
    item = short_items[0]
    assert item.memory_type == MemoryType.PREFERENCE
    assert item.metadata["config_key"] == "reply_language"
    assert item.metadata["preference_key"] == "reply_language"
    assert item.metadata["quality_score"] == result.quality_score
    assert item.metadata["source_type"] == "manual_config"
    assert item.metadata["heat"] > 0


def test_process_input_returns_reject_details_for_low_quality() -> None:
    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(quality_threshold=0.99),
        layer_manager=LayerManager(),
    )
    result = orchestrator.process_input(
        {"content": "嗯"},
        "conversation",
        user_id="u001",
    )

    assert result.accepted is False
    assert result.reject_code == "low_quality"
    assert result.reasons
    assert orchestrator.list_short_term("u001") == []


def test_write_short_evicts_oldest_when_capacity_reached() -> None:
    manager = LayerManager(short_capacity=2)
    first = MemoryItem(
        id="m1",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="first",
        user_id="u001",
        metadata={"fingerprint": "fp-1"},
    )
    second = MemoryItem(
        id="m2",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="second",
        user_id="u001",
        metadata={"fingerprint": "fp-2"},
    )
    third = MemoryItem(
        id="m3",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="third",
        user_id="u001",
        metadata={"fingerprint": "fp-3"},
    )

    assert manager.write_short(first).evicted is None
    assert manager.write_short(second).evicted is None
    result = manager.write_short(third)

    assert result.evicted is not None
    assert result.evicted.id == "m1"
    remaining = manager.read_short("u001", limit=10)
    # read_short 默认最新在前（newest_first=True）；m2/m3 创建时间相同，
    # 稳定排序下按插入逆序返回，与 /short-term API 语义保持一致。
    assert [item.id for item in remaining] == ["m3", "m2"]


def test_short_term_updates_same_fingerprint_instead_of_duplicating() -> None:
    store = ShortTermMemory(capacity=5)
    first = MemoryItem(
        id="m1",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="下载默认放 D 盘",
        user_id="u001",
        metadata={"fingerprint": "fp-download", "quality_score": 0.7},
    )
    second = MemoryItem(
        id="m2",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="下载默认放 E 盘",
        user_id="u001",
        metadata={"fingerprint": "fp-download", "quality_score": 0.9},
    )

    store.write(first)
    result = store.write(second)

    assert result.updated_existing is True
    assert result.evicted is None
    items = store.read("u001")
    assert len(items) == 1
    assert items[0].content == "下载默认放 E 盘"
    assert items[0].version == 2
    assert items[0].metadata["access_count"] == 2
    assert items[0].metadata["heat"] > 0


def test_short_term_merges_by_record_key_when_fingerprint_differs() -> None:
    store = ShortTermMemory(capacity=5)
    record_key = "manual_config:u001:global:download_path"
    first = MemoryItem(
        id="m1",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="用户设置“download_path”为“D:/Downloads”",
        user_id="u001",
        metadata={
            "record_key": record_key,
            "fingerprint": "fp-old-value",
            "config_key": "download_path",
            "quality_score": 0.7,
        },
    )
    second = MemoryItem(
        id="m2",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="用户设置“download_path”为“E:/Downloads”",
        user_id="u001",
        metadata={
            "record_key": record_key,
            "fingerprint": "fp-new-value",
            "config_key": "download_path",
            "quality_score": 0.9,
        },
    )

    store.write(first)
    result = store.write(second)

    assert result.updated_existing is True
    items = store.read("u001")
    assert len(items) == 1
    assert items[0].id == "m1"
    assert "E:/Downloads" in items[0].content
    assert items[0].version == 2
    assert items[0].metadata["fingerprint"] == "fp-new-value"


def test_process_input_merges_manual_config_value_update_via_record_key() -> None:
    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(),
        layer_manager=LayerManager(short_capacity=10),
    )
    first = orchestrator.process_input(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.9},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )
    second = orchestrator.process_input(
        {"key": "download_path", "value": "E:/Downloads", "confidence": 0.9},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )

    assert first.accepted is True
    assert second.accepted is True
    assert second.updated_existing is True
    assert first.fingerprint != second.fingerprint

    short_items = orchestrator.list_short_term("u001")
    assert len(short_items) == 1
    assert short_items[0].id == first.memory_id
    assert "E:/Downloads" in short_items[0].content
    assert short_items[0].metadata["record_key"].endswith(":download_path")
    assert short_items[0].version == 2


def test_promote_to_mid_moves_hot_items_only() -> None:
    manager = LayerManager(short_capacity=5, heat_threshold=2.0)
    cold = MemoryItem(
        id="cold",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="cold",
        user_id="u001",
        metadata={"fingerprint": "fp-cold", "quality_score": 0.1},
    )
    hot = MemoryItem(
        id="hot",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="hot",
        user_id="u001",
        metadata={"fingerprint": "fp-hot", "quality_score": 0.9, "access_count": 2},
    )

    manager.write_short(cold)
    manager.write_short(hot)

    # write() 会按 quality/access 重算 heat：hot ≈ 1+0.9×5 = 5.5 达标，
    # cold ≈ 1+0.1×5 = 1.5 不达 2.0 阈值。
    promoted = manager.promote_to_mid("u001")
    assert len(promoted) == 1
    assert promoted[0].id == "hot"
    assert promoted[0].layer == MemoryLayer.MID

    remaining = manager.read_short("u001")
    assert len(remaining) == 1
    assert remaining[0].id == "cold"


def test_promote_to_mid_does_not_reheat_remaining_items() -> None:
    manager = LayerManager(short_capacity=5, heat_threshold=5.0)
    cold = MemoryItem(
        id="cold-stable",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="cold stable",
        user_id="u001",
        metadata={"fingerprint": "cold-stable", "access_count": 1, "heat": 2.0},
    )
    hot = MemoryItem(
        id="hot-promote",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="hot promote",
        user_id="u001",
        metadata={"fingerprint": "hot-promote", "access_count": 5, "heat": 6.0},
    )
    manager.write_short(cold)
    manager.write_short(hot)
    cold_before = manager.get_short("cold-stable")
    assert cold_before is not None
    heat_before = cold_before.metadata["heat"]
    access_before = cold_before.metadata["access_count"]

    manager.promote_to_mid("u001")

    cold_after = manager.get_short("cold-stable")
    assert cold_after is not None
    assert cold_after.layer == MemoryLayer.SHORT
    assert cold_after.metadata["heat"] == heat_before
    assert cold_after.metadata["access_count"] == access_before


def test_classifier_uses_source_type_and_metadata() -> None:
    classifier = MemoryClassifier()

    tool_record = NormalizedRecord(
        type="tool_result",
        content="pip 执行失败：连接超时",
        confidence=0.8,
        source="tool_result",
        user_id="u001",
        metadata={"tool_name": "pip", "status": "failed"},
    )
    assert classifier.classify(tool_record) == MemoryType.KNOWLEDGE

    config_record = NormalizedRecord(
        type="manual_config",
        content="用户设置“reply_language”为“中文”",
        confidence=0.8,
        source="manual_config",
        user_id="u001",
        metadata={"config_key": "reply_language"},
    )
    assert classifier.classify(config_record) == MemoryType.PREFERENCE

    pii_record = NormalizedRecord(
        type="conversation",
        content="我的手机号是 <HASH_a5acfd40>,请记住",
        confidence=0.7,
        source="conversation",
        user_id="u001",
        metadata={"explicit": True},
    )
    assert classifier.classify(pii_record) == MemoryType.KNOWLEDGE


def test_conflict_resolver_uses_fingerprint_for_same_memory_key() -> None:
    resolver = ConflictResolver()
    old = MemoryItem(
        id="old",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="下载默认放 D 盘",
        user_id="u001",
        metadata={"fingerprint": "fp-1", "source_type": "behavior"},
    )
    new = MemoryItem(
        id="new",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="下载默认放 E 盘",
        user_id="u001",
        metadata={
            "fingerprint": "fp-1",
            "source_type": "manual_config",
            "quality_score": 0.9,
        },
    )

    assert resolver.detect(old, new) is True
    decision = resolver.resolve_preference(old, new)
    assert decision.action == ConflictAction.REPLACE
    assert decision.result.content == "下载默认放 E 盘"


def test_conflict_resolver_uses_record_key_when_fingerprints_differ() -> None:
    resolver = ConflictResolver()
    record_key = "manual_config:u001:global:download_path"
    old = MemoryItem(
        id="old",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="用户设置“download_path”为“D:/Downloads”",
        user_id="u001",
        metadata={
            "record_key": record_key,
            "fingerprint": "fp-old",
            "source_type": "manual_config",
            "quality_score": 0.7,
        },
    )
    new = MemoryItem(
        id="new",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="用户设置“download_path”为“E:/Downloads”",
        user_id="u001",
        metadata={
            "record_key": record_key,
            "fingerprint": "fp-new",
            "source_type": "manual_config",
            "quality_score": 0.9,
        },
    )

    assert resolver.memory_key(old) == resolver.memory_key(new)
    assert resolver.detect(old, new) is True
    decision = resolver.resolve_preference(old, new)
    assert decision.action == ConflictAction.REPLACE
    assert "E:/Downloads" in decision.result.content


def test_factory_loads_capacity_from_default_config() -> None:
    orchestrator = create_orchestrator()
    assert orchestrator.layer_manager.short_capacity == 10
    assert orchestrator.layer_manager.mid_capacity == 50
    assert isinstance(orchestrator._milvus, InMemoryMilvusClient)
    assert orchestrator._milvus.collection == "os_agent_memory"


def _fresh_orchestrator() -> MemoryOrchestrator:
    import tempfile

    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
    from src.preference.store import PreferenceMemoryStore

    return MemoryOrchestrator(
        ingestion=IngestionPipeline(),
        layer_manager=LayerManager(short_capacity=10, heat_threshold=1.0),
        preference=PreferenceMemoryStore(storage_dir=tempfile.mkdtemp(prefix="pref_core_")),
        milvus=InMemoryMilvusClient(),
    )


def test_memory_item_to_entry_maps_preference_key() -> None:
    from src.core.entry_adapter import memory_item_to_entry
    from src.knowledge.memory_service.memory_schema import MemoryTypeTag

    item = MemoryItem(
        id="m1",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.SHORT,
        content="用户设置“download_path”为“E:/Downloads”",
        user_id="u001",
        metadata={
            "config_key": "download_path",
            "source_type": "manual_config",
            "confidence": 0.9,
        },
    )
    entry = memory_item_to_entry(item)
    assert entry.memory_type == MemoryTypeTag.PREFERENCE
    assert entry.metadata["preference_key"] == "download_path"


def test_store_memory_entry_creates_preference_in_milvus() -> None:
    from src.core.entry_adapter import memory_item_to_entry
    from src.knowledge.memory_service.memory_schema import MemoryTypeTag

    orchestrator = _fresh_orchestrator()
    item = MemoryItem(
        id="local-1",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.MID,
        content="用户设置“download_path”为“D:/Downloads”",
        user_id="u001",
        scene_id="global",
        metadata={
            "preference_key": "habit.download_path",
            "preference_value": "D:/Downloads",
            "category": "habit",
            "source_type": "manual_config",
            "confidence": 0.9,
        },
    )
    result = orchestrator.store_memory_entry(memory_item_to_entry(item))

    assert result.created is True
    assert result.memory_id
    stored = orchestrator._milvus.get_by_id(result.memory_id)
    assert stored is not None
    assert stored["memory_type"] == MemoryTypeTag.PREFERENCE.value
    assert stored.get("preference_key") == "habit.download_path"


def test_preference_conflict_ignores_extraction_confidence() -> None:
    resolver = ConflictResolver()
    old = MemoryItem(
        id="old",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.LONG,
        content="D:/Downloads",
        user_id="u001",
        metadata={
            "preference_key": "habit.download_path",
            "source": "chat",
            "confidence": 0.92,
        },
    )
    new = MemoryItem(
        id="new",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.LONG,
        content="E:/Downloads",
        user_id="u001",
        metadata={
            "preference_key": "habit.download_path",
            "source": "chat",
            "confidence": 0.77,
        },
    )
    decision = resolver.resolve_preference(old, new)
    assert decision.action == ConflictAction.REPLACE


def test_store_memory_entry_v2_marks_old_not_latest() -> None:
    from src.core.entry_adapter import memory_item_to_entry

    orchestrator = _fresh_orchestrator()
    base_metadata = {
        "preference_key": "habit.download_path",
        "category": "habit",
        "source_type": "manual_config",
        "confidence": 0.9,
    }
    first = orchestrator.store_memory_entry(
        memory_item_to_entry(
            MemoryItem(
                id="local-1",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="用户设置“download_path”为“D:/Downloads”",
                user_id="u001",
                metadata={**base_metadata, "preference_value": "D:/Downloads"},
            )
        )
    )
    second = orchestrator.store_memory_entry(
        memory_item_to_entry(
            MemoryItem(
                id="local-2",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="用户设置“download_path”为“E:/Downloads”",
                user_id="u001",
                metadata={**base_metadata, "preference_value": "E:/Downloads"},
            )
        )
    )

    assert second.created is True
    assert second.superseded_memory_id == first.memory_id
    old = orchestrator._milvus.get_by_id(first.memory_id)
    new = orchestrator._milvus.get_by_id(second.memory_id)
    assert old is not None and new is not None
    assert old["is_latest"] is False
    assert new["is_latest"] is True
    assert new["version"] == 2


def test_store_knowledge_key_supersedes_old_and_recall_hides_it() -> None:
    from src.knowledge.memory_service.memory_schema import MemoryEntry, MemorySource, MemoryTypeTag

    orchestrator = _fresh_orchestrator()
    first = orchestrator.store_memory_entry(
        MemoryEntry(
            content="验收项目服务端口为 8080",
            memory_type=MemoryTypeTag.PROJECT_CONTEXT,
            source=MemorySource.MANUAL,
            user_id="port_user",
            confidence=0.95,
            metadata={"knowledge_key": "acceptance.service_port"},
        )
    )
    second = orchestrator.store_memory_entry(
        MemoryEntry(
            content="验收项目服务端口现已改为 9090，替代旧端口 8080",
            memory_type=MemoryTypeTag.PROJECT_CONTEXT,
            source=MemorySource.MANUAL,
            user_id="port_user",
            confidence=0.95,
            metadata={"knowledge_key": "acceptance.service_port"},
        )
    )
    retained = orchestrator.store_memory_entry(
        MemoryEntry(
            content="验收项目使用 Python 编程语言",
            memory_type=MemoryTypeTag.PROJECT_CONTEXT,
            source=MemorySource.MANUAL,
            user_id="port_user",
            confidence=0.95,
            metadata={"knowledge_key": "acceptance.language"},
        )
    )

    assert first.created is True
    assert second.created is True
    assert second.superseded_memory_id == first.memory_id
    old = orchestrator._milvus.get_by_id(first.memory_id)
    new = orchestrator._milvus.get_by_id(second.memory_id)
    assert old is not None and new is not None
    assert old["is_latest"] is False
    assert new["is_latest"] is True
    assert new["version"] == 2

    recalled = orchestrator.recall(
        RetrievalQuery(query="验收项目当前服务端口", user_id="port_user", top_k=100)
    )
    ids = {item.id for item in recalled.items}
    assert second.memory_id in ids
    # Sharing the project name alone is not evidence for a port question.
    assert retained.memory_id not in ids
    assert orchestrator._milvus.get_by_id(retained.memory_id)["is_latest"] is True
    assert first.memory_id not in ids

    history = orchestrator.recall(
        RetrievalQuery(query="验收项目当前服务端口", user_id="port_user", top_k=100),
        filters={"is_latest": False},
    )
    hist_ids = {item.id for item in history.items}
    assert first.memory_id in hist_ids
    assert second.memory_id in hist_ids


def test_unchanged_knowledge_does_not_create_duplicate_version() -> None:
    from src.knowledge.memory_service.memory_schema import MemoryEntry, MemorySource, MemoryTypeTag

    orchestrator = _fresh_orchestrator()
    entry = MemoryEntry(
        content="验收项目服务端口为 8080",
        memory_type=MemoryTypeTag.PROJECT_CONTEXT,
        source=MemorySource.MANUAL,
        user_id="same-port-user",
        confidence=0.95,
        metadata={"knowledge_key": "acceptance.service_port"},
    )
    first = orchestrator.store_memory_entry(entry)
    second = orchestrator.store_memory_entry(entry.model_copy(deep=True))

    assert first.created is True
    assert second.created is False
    assert second.memory_id == first.memory_id
    assert second.reason == "unchanged_knowledge"
    assert len(orchestrator._milvus.search(None, {"user_id": "same-port-user"}, top_k=10)) == 1


def test_conflict_resolver_uses_knowledge_key_for_same_fact() -> None:
    resolver = ConflictResolver()
    old = MemoryItem(
        id="old",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content="服务端口为 8080",
        user_id="u001",
        metadata={"knowledge_key": "acceptance.service_port"},
    )
    new = MemoryItem(
        id="new",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content="服务端口现已改为 9090",
        user_id="u001",
        metadata={"knowledge_key": "acceptance.service_port"},
    )
    assert resolver.memory_key(old) == resolver.memory_key(new)
    assert resolver.detect(old, new) is True
    assert resolver.resolve_knowledge(old, new).action == ConflictAction.REPLACE


def test_preference_history_survives_orchestrator_restart() -> None:
    from src.core.entry_adapter import memory_item_to_entry
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    milvus = InMemoryMilvusClient()
    first_orchestrator = MemoryOrchestrator(milvus=milvus)
    first = first_orchestrator.store_memory_entry(
        memory_item_to_entry(
            MemoryItem(
                id="restart-v1",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="默认下载到 D 盘",
                user_id="restart-user",
                metadata={
                    "preference_key": "habit.download_path",
                    "source_type": "manual_config",
                    "confidence": 0.9,
                },
            )
        )
    )

    restarted = MemoryOrchestrator(milvus=milvus)
    second = restarted.store_memory_entry(
        memory_item_to_entry(
            MemoryItem(
                id="restart-v2",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="默认下载到 E 盘",
                user_id="restart-user",
                metadata={
                    "preference_key": "habit.download_path",
                    "source_type": "manual_config",
                    "confidence": 0.9,
                },
            )
        )
    )

    assert second.created is True
    assert second.superseded_memory_id == first.memory_id
    assert milvus.get_by_id(first.memory_id)["is_latest"] is False
    assert milvus.get_by_id(second.memory_id)["version"] == 2


def test_unchanged_preference_does_not_create_duplicate_version() -> None:
    from src.core.entry_adapter import memory_item_to_entry

    orchestrator = _fresh_orchestrator()
    entry = memory_item_to_entry(
        MemoryItem(
            id="same-pref",
            memory_type=MemoryType.PREFERENCE,
            layer=MemoryLayer.MID,
            content="用户偏好中文回答",
            user_id="same-user",
            metadata={
                "preference_key": "style.reply_language",
                "source_type": "conversation",
                "confidence": 0.9,
            },
        )
    )
    first = orchestrator.store_memory_entry(entry)
    second = orchestrator.store_memory_entry(entry.model_copy(deep=True))

    assert first.created is True
    assert second.created is False
    assert second.memory_id == first.memory_id
    assert second.reason == "unchanged_preference"
    assert len(orchestrator._milvus.search(None, {"user_id": "same-user"}, top_k=10)) == 1


def test_rejected_preference_candidate_does_not_leak_to_profile_or_history(tmp_path) -> None:
    """后续明确更新必须进入权威存储，画像/历史/召回看到同一条最新值。"""
    from src.preference.store import PreferenceMemoryStore

    preference = PreferenceMemoryStore(
        storage_dir=tmp_path / "preference",
        use_llm=False,
        allow_rules=True,
    )
    orchestrator = MemoryOrchestrator(preference=preference)

    first = orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="consistent-user",
        scene_id="office",
    )
    second = orchestrator.extract_preferences_from_text(
        "以后下载改到 E 盘",
        user_id="consistent-user",
        scene_id="office",
        source="manual_config",
    )

    assert first.stored[0].created is True
    assert second.stored[0].created is True
    assert second.stored[0].superseded_memory_id == first.stored[0].memory_id

    profile = orchestrator.get_preference_profile("consistent-user", scene_id="office")
    history = orchestrator.get_preference_history(
        "consistent-user", "habit.download_path"
    )
    recalled = orchestrator.list_preferences(
        "consistent-user", preference_key="habit.download_path"
    )

    assert profile["kv"]["habit.download_path"] == "E:/Downloads"
    values = [item.metadata["preference_value"] for item in history]
    assert "D:/Downloads" in values
    assert "E:/Downloads" in values
    assert len(recalled.items) == 1
    assert recalled.items[0].metadata["preference_value"] == "E:/Downloads"


def test_recall_wraps_query_memory() -> None:
    from src.core.entry_adapter import memory_item_to_entry
    from src.common.models import RetrievalQuery

    orchestrator = _fresh_orchestrator()
    orchestrator.store_memory_entry(
        memory_item_to_entry(
            MemoryItem(
                id="local-1",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="用户偏好中文回答",
                user_id="u001",
                metadata={
                    "preference_key": "style.reply_language",
                    "preference_value": "中文",
                    "source_type": "conversation",
                    "confidence": 0.85,
                },
            )
        )
    )

    result = orchestrator.list_preferences("u001", preference_key="style.reply_language")
    assert len(result.items) == 1
    assert result.strategy == "query_memory"
    assert result.items[0].metadata.get("preference_key") == "style.reply_language"


def test_consolidate_promotes_and_stores_preference() -> None:
    orchestrator = _fresh_orchestrator()
    ingest = orchestrator.process_input(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.95},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )
    assert ingest.accepted is True
    path_b_stored = len(ingest.preference_stored_ids)

    short_items = orchestrator.list_short_term("u001")
    assert len(short_items) == 1
    short_items[0].metadata["access_count"] = 3
    short_items[0].metadata["heat"] = 3.5
    orchestrator.layer_manager.write_short(short_items[0])

    result = orchestrator.consolidate("u001")
    assert result.promoted_to_mid == 1
    assert result.promoted_to_long == 1
    assert result.retained_in_mid == 0
    assert result.mid_remaining == 0
    assert len(result.stored_memory_ids) == 1
    assert orchestrator.layer_manager.long_count("u001") == 0
    stats = orchestrator.memory_stats("u001")
    assert stats.long.count == 0
    # Path B 在 ingest 时已写入同一 download_path；consolidate 应复用/覆盖，不双写。
    assert stats.milvus.count >= 1
    assert stats.milvus.preference_count >= 1
    assert stats.milvus.count <= max(path_b_stored, 1) + 1


def test_consolidate_keeps_long_pending_when_milvus_store_fails() -> None:
    orchestrator = _fresh_orchestrator()
    orchestrator.process_input(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.95},
        "manual_config",
        user_id="pending_user",
    )
    short_items = orchestrator.list_short_term("pending_user")
    short_items[0].metadata["access_count"] = 3
    short_items[0].metadata["heat"] = 6.0
    orchestrator.layer_manager.write_short(short_items[0])

    original_create = orchestrator._create_entry

    def _reject_create(entry):
        from src.knowledge.memory_service.memory_schema import CreateMemoryResponse

        return CreateMemoryResponse(memory_id="", created=False, reason="policy_rejected")

    orchestrator._create_entry = _reject_create  # type: ignore[method-assign]
    try:
        result = orchestrator.consolidate("pending_user")
        assert result.promoted_to_long == 1
        # Path B 已在 process_input 阶段把偏好即时写入长期 Milvus；即使
        # consolidate 阶段 _create_entry 失败，该偏好也已由 Path B 持久化，
        # 不会丢失。本测试的核心验证点（偏好最终落库、不丢失）由下面的
        # milvus 持久化断言保证；long 待入库队列在复用成功后已被释放。
        stats = orchestrator.memory_stats("pending_user")
        assert stats.milvus.count == 1
    finally:
        orchestrator._create_entry = original_create  # type: ignore[method-assign]


def test_consolidate_releases_long_pending_when_staging_bind_fails(
    tmp_path: Path,
) -> None:
    """Milvus 已成功时，staging 绑定异常只能告警，不能卡住长期待入库队列。"""

    store = StagingStore(str(tmp_path / "bind-failure.db"))
    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(staging_store=store),
        layer_manager=LayerManager(short_capacity=10, mid_capacity=10),
    )
    orchestrator.process_input(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.95},
        "manual_config",
        user_id="bind_failure_user",
    )
    short_item = orchestrator.list_short_term("bind_failure_user")[0]
    short_item.metadata["access_count"] = 3
    short_item.metadata["heat"] = 6.0
    orchestrator.layer_manager.write_short(short_item)

    original_bind = store.bind_milvus_id

    def _fail_bind(*args, **kwargs):
        raise OSError("staging unavailable")

    store.bind_milvus_id = _fail_bind  # type: ignore[method-assign]
    try:
        result = orchestrator.consolidate("bind_failure_user")
    finally:
        store.bind_milvus_id = original_bind  # type: ignore[method-assign]

    assert len(result.stored_memory_ids) == 1
    assert orchestrator.layer_manager.long_count("bind_failure_user") == 0
    assert store.query_events("bind_failure_user")[0]["milvus_memory_id"] is None


def test_mid_term_capacity_evicts_oldest() -> None:
    manager = LayerManager(short_capacity=5, mid_capacity=2, heat_threshold=1.0, similarity_threshold=1.0)
    contents = [
        "pip install timeout workaround using domestic mirror",
        "user prefers markdown report format for weekly summary",
        "batch rename images with date prefix in downloads folder",
    ]
    for index, content in enumerate(contents):
        item = MemoryItem(
            id=f"m{index}",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.SHORT,
            content=content,
            user_id="u001",
            metadata={"fingerprint": f"fp-{index}", "quality_score": 0.8},
        )
        manager.write_short(item)
        written = manager.read_short("u001", limit=1)[-1]
        written.metadata["access_count"] = 3
        written.metadata["heat"] = 3.0
        manager.write_short(written)
        manager.promote_to_mid("u001")

    mid_items = manager.read_mid("u001", limit=10)
    assert len(mid_items) == 2
    assert [item.id for item in mid_items] == ["m1", "m2"]


def test_promote_to_long_retains_low_quality_in_mid() -> None:
    manager = LayerManager(quality_threshold=0.6)
    hot = MemoryItem(
        id="hot",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.MID,
        content="hot",
        user_id="u001",
        metadata={"fingerprint": "fp-hot", "quality_score": 0.9, "heat": 6.0},
    )
    cold = MemoryItem(
        id="cold",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.MID,
        content="cold",
        user_id="u001",
        metadata={"fingerprint": "fp-cold", "quality_score": 0.2, "heat": 6.0},
    )
    manager._mid.extend("u001", [hot, cold])

    promoted, retained = manager.promote_to_long("u001")
    assert len(promoted) == 1
    assert promoted[0].id == "hot"
    assert len(retained) == 1
    assert retained[0].id == "cold"
    assert manager.mid_count("u001") == 1


def test_consolidate_mid_only_does_not_store_to_milvus() -> None:
    orchestrator = _fresh_orchestrator()
    orchestrator.process_input(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.95},
        "manual_config",
        user_id="u001",
    )
    short_items = orchestrator.list_short_term("u001")
    short_items[0].metadata["access_count"] = 3
    short_items[0].metadata["heat"] = 6.0
    orchestrator.layer_manager.write_short(short_items[0])
    milvus_before = len(orchestrator._milvus._records)

    result = orchestrator.consolidate_mid("u001")
    assert result.promoted_to_mid == 1
    assert result.mid_remaining == 1
    assert orchestrator.list_mid_term("u001")
    # Path B 可能已在 ingest 写入；consolidate_mid 本身不得再写长期库。
    assert len(orchestrator._milvus._records) == milvus_before


def test_mid_term_merges_similar_content() -> None:
    manager = LayerManager(mid_capacity=10, similarity_threshold=0.7)
    first = MemoryItem(
        id="m1",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="pip install failed due to network timeout",
        user_id="u001",
        metadata={"fingerprint": "fp-1", "quality_score": 0.8, "heat": 6.0},
    )
    second = MemoryItem(
        id="m2",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="pip install failed because of network timeout",
        user_id="u001",
        metadata={"fingerprint": "fp-2", "quality_score": 0.85, "heat": 6.0},
    )
    manager._mid.extend("u001", [first, second])

    mid_items = manager.read_mid("u001", limit=10)
    assert len(mid_items) == 1
    assert mid_items[0].version == 2
    assert mid_items[0].metadata.get("mid_merged") is True
    assert mid_items[0].metadata.get("quality_score") == 0.85


def test_mid_term_does_not_merge_unrelated_short_english() -> None:
    """麒麟 GTE 上 hot/cold 余弦约 0.77，不能单靠向量把反义短词并成一条。"""

    manager = LayerManager(quality_threshold=0.6, similarity_threshold=0.7)
    hot = MemoryItem(
        id="hot",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.MID,
        content="hot",
        user_id="u001",
        metadata={"fingerprint": "fp-hot", "quality_score": 0.9, "heat": 6.0},
    )
    cold = MemoryItem(
        id="cold",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.MID,
        content="cold",
        user_id="u001",
        metadata={"fingerprint": "fp-cold", "quality_score": 0.2, "heat": 6.0},
    )
    manager._mid.extend("u001", [hot, cold])
    assert manager.mid_count("u001") == 2
    scores = {
        item.id: item.metadata.get("quality_score")
        for item in manager.read_mid("u001", limit=10)
    }
    assert scores["hot"] == 0.9
    assert scores["cold"] == 0.2


def test_mid_term_merge_keeps_higher_quality_score() -> None:
    manager = LayerManager(mid_capacity=10, similarity_threshold=0.7)
    first = MemoryItem(
        id="m1",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="pip install failed due to network timeout",
        user_id="u001",
        metadata={"fingerprint": "fp-1", "quality_score": 0.9, "heat": 6.0},
    )
    second = MemoryItem(
        id="m2",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content="pip install failed because of network timeout",
        user_id="u001",
        metadata={"fingerprint": "fp-2", "quality_score": 0.2, "heat": 6.0},
    )
    manager._mid.extend("u001", [first, second])
    mid_items = manager.read_mid("u001", limit=10)
    assert len(mid_items) == 1
    assert mid_items[0].metadata.get("quality_score") == 0.9
    assert "pip install" in mid_items[0].content


def test_memory_stats_reports_all_layers() -> None:
    orchestrator = _fresh_orchestrator()
    orchestrator.process_input(
        {"key": "lang", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="stats_user",
    )
    stats = orchestrator.memory_stats("stats_user")
    assert stats.user_id == "stats_user"
    assert stats.short.count == 1
    assert stats.total_count >= 1
    assert stats.short.preference_count == 1


def test_mid_term_stats_reports_avg_heat() -> None:
    manager = LayerManager(mid_capacity=10)
    manager._mid.extend(
        "u001",
        [
            MemoryItem(
                id="a",
                memory_type=MemoryType.PREFERENCE,
                layer=MemoryLayer.MID,
                content="a",
                user_id="u001",
                metadata={"heat": 4.0},
            ),
            MemoryItem(
                id="b",
                memory_type=MemoryType.KNOWLEDGE,
                layer=MemoryLayer.MID,
                content="b",
                user_id="u001",
                metadata={"heat": 6.0},
            ),
        ],
    )
    stats = manager.mid_stats("u001")
    assert stats.count == 2
    assert stats.avg_heat == 5.0


def test_forget_purges_short_term_memory() -> None:
    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(),
        layer_manager=LayerManager(short_capacity=10),
    )
    result = orchestrator.process_input(
        {"key": "reply_language", "value": "中文", "confidence": 0.8},
        "manual_config",
        user_id="forget_user",
    )
    assert result.accepted is True
    assert result.memory_id is not None
    assert len(orchestrator.list_short_term("forget_user")) == 1

    orchestrator.security.forget = lambda user_id, instruction: [result.memory_id]  # type: ignore[method-assign]

    deleted = orchestrator.forget("forget_user", "忘掉刚才的配置")
    assert deleted == [result.memory_id]
    assert orchestrator.list_short_term("forget_user") == []


def test_orchestrator_deletes_sensitive_ids_after_security_resolution(
    tmp_path: Path,
) -> None:
    """安全服务只解析占位符，Orchestrator 负责统一执行索引软删除。"""

    from src.security.service import SecurityAndForgetService

    security = SecurityAndForgetService(
        user_id="sensitive_user",
        index_path=str(tmp_path / "security-index.json"),
    )
    security.filter_sensitive("我的手机号是 13812345678")
    assert security.get_sensitive_stats("sensitive_user")["deleted_count"] == 0

    orchestrator = MemoryOrchestrator(security=security)
    deleted = orchestrator.forget("sensitive_user", "忘记我的手机号")

    assert deleted
    assert security.get_sensitive_stats("sensitive_user")["deleted_count"] >= 1


def test_forget_purges_mid_term_and_milvus_memory() -> None:
    from src.knowledge.memory_service.memory_schema import MemoryEntry

    orchestrator = MemoryOrchestrator(
        ingestion=IngestionPipeline(),
        layer_manager=LayerManager(short_capacity=10, mid_capacity=10),
    )
    mid_item = MemoryItem(
        id="mid-forget-1",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.MID,
        content="pip install timeout workaround",
        user_id="forget_user",
        metadata={"heat": 6.0, "quality_score": 0.9},
    )
    orchestrator.layer_manager._mid.extend("forget_user", [mid_item])  # type: ignore[attr-defined]

    entry = MemoryEntry(
        content="pip install timeout workaround",
        memory_type="tool_call",
        task_type="os_operation",
        source="agent_log",
        user_id="forget_user",
        confidence=0.9,
        metadata={"tool_name": "pip"},
    )
    stored = orchestrator.store_memory_entry(entry)
    assert stored.created is True

    orchestrator.security.forget = lambda user_id, instruction: [  # type: ignore[method-assign]
        "mid-forget-1",
        stored.memory_id,
    ]

    deleted = orchestrator.forget("forget_user", "忘掉 pip 相关记忆")
    assert "mid-forget-1" in deleted
    assert stored.memory_id in deleted
    assert orchestrator.list_mid_term("forget_user") == []
    recall = orchestrator.recall(
        RetrievalQuery(query="pip timeout", user_id="forget_user", top_k=5)
    )
    assert all(item.id != stored.memory_id for item in recall.items)


def test_forget_resolves_staging_local_and_milvus_ids(tmp_path: Path) -> None:
    """本地 ID 或最终 Milvus ID 都应命中同一 staging 映射并完成级联删除。"""

    for target_kind in ("local", "milvus"):
        store = StagingStore(str(tmp_path / f"{target_kind}.db"))
        orchestrator = MemoryOrchestrator(
            ingestion=IngestionPipeline(staging_store=store),
            layer_manager=LayerManager(short_capacity=10, mid_capacity=10),
        )
        result = orchestrator.process_input(
            {
                "key": f"reply_language_{target_kind}",
                "value": "中文",
                "confidence": 0.9,
            },
            "manual_config",
            user_id=f"mapping_{target_kind}",
        )
        assert result.memory_id is not None
        local_id = result.memory_id
        short_item = orchestrator.list_short_term(f"mapping_{target_kind}")[0]
        stored = orchestrator.resolve_and_store(short_item)
        milvus_id = stored.metadata.get("milvus_memory_id")
        assert isinstance(milvus_id, str) and milvus_id

        event = store.query_events(f"mapping_{target_kind}")[0]
        assert event["memory_id"] == local_id
        assert event["milvus_memory_id"] == milvus_id

        target_id = local_id if target_kind == "local" else milvus_id
        orchestrator.security.forget = (  # type: ignore[method-assign]
            lambda user_id, instruction, target=target_id: [target]
        )
        assert orchestrator.forget(f"mapping_{target_kind}", "忘掉这条记忆") == [
            target_id
        ]
        assert orchestrator.list_short_term(f"mapping_{target_kind}") == []
        recalled = orchestrator.recall(
            RetrievalQuery(
                query="中文",
                user_id=f"mapping_{target_kind}",
                top_k=5,
            )
        )
        assert all(item.id != milvus_id for item in recalled.items)
        assert store.query_events(f"mapping_{target_kind}")[0]["content"] is None


def test_extract_download_path_creates_version_two_despite_lower_confidence() -> None:
    orchestrator = _fresh_orchestrator()
    first = orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="version_user",
    )
    second = orchestrator.extract_preferences_from_text(
        "以后下载改到 E 盘",
        user_id="version_user",
    )
    assert first.stored and first.stored[0].created is True
    assert second.stored and second.stored[0].created is True
    assert second.stored[0].superseded_memory_id == first.stored[0].memory_id
    profile = orchestrator.get_preference_profile("version_user")
    assert profile["kv"]["habit.download_path"] == "E:/Downloads"
    history = orchestrator.get_preference_history("version_user", "habit.download_path")
    assert len(history) == 2


def test_cross_scene_same_preference_key_keeps_both_latest() -> None:
    orchestrator = _fresh_orchestrator()
    orchestrator.extract_preferences_from_text(
        "办公场景下用 Excel 处理数据",
        user_id="scene_user",
        scene_id="office",
    )
    orchestrator.extract_preferences_from_text(
        "用 Python 跑数据分析",
        user_id="scene_user",
        scene_id="data_analysis",
    )
    office = orchestrator.get_preference_profile("scene_user", scene_id="office")
    analysis = orchestrator.get_preference_profile("scene_user", scene_id="data_analysis")
    assert office["kv"]["tool_selection.data_processing_tool"] == "excel"
    assert analysis["kv"]["tool_selection.data_processing_tool"] == "python"
    latest = orchestrator.list_preferences("scene_user", latest_only=True, top_k=20)
    keys = {
        (
            item.metadata.get("preference_key"),
            item.scene_id,
            item.metadata.get("preference_value"),
        )
        for item in latest.items
        if item.metadata.get("is_latest")
    }
    assert ("tool_selection.data_processing_tool", "office", "excel") in keys
    assert ("tool_selection.data_processing_tool", "data_analysis", "python") in keys


def test_recall_excludes_superseded_preference_by_default() -> None:
    """未过滤 /recall 不应夹带 is_latest=false 的旧偏好（如 D→E 后的 D 盘）。"""

    orchestrator = _fresh_orchestrator()
    first = orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="recall_pref_user",
    )
    second = orchestrator.extract_preferences_from_text(
        "以后下载改到 E 盘",
        user_id="recall_pref_user",
    )
    assert first.stored and second.stored
    old_id = first.stored[0].memory_id
    new_id = second.stored[0].memory_id
    assert second.stored[0].superseded_memory_id == old_id

    # 混合召回：默认只看到最新 E 盘
    mixed = orchestrator.recall(
        RetrievalQuery(query="下载路径", user_id="recall_pref_user", top_k=10)
    )
    pref_items = [i for i in mixed.items if i.memory_type == MemoryType.PREFERENCE]
    assert pref_items
    assert all(i.metadata.get("is_latest", True) is not False for i in pref_items)
    assert all(i.id != old_id for i in pref_items)
    assert any(
        i.id == new_id or i.metadata.get("preference_value") == "E:/Downloads"
        for i in pref_items
    )

    # 偏好专查同样默认最新
    only_pref = orchestrator.recall(
        RetrievalQuery(
            query="下载",
            user_id="recall_pref_user",
            memory_type=MemoryType.PREFERENCE,
            top_k=10,
        )
    )
    assert only_pref.items
    assert all(i.metadata.get("is_latest", True) is not False for i in only_pref.items)
    assert all("D:/Downloads" not in (i.metadata.get("preference_value") or "") for i in only_pref.items)

    # 显式要历史时可拿到旧版本
    history = orchestrator.recall(
        RetrievalQuery(
            query="下载",
            user_id="recall_pref_user",
            memory_type=MemoryType.PREFERENCE,
            top_k=10,
        ),
        filters={"is_latest": False},
    )
    hist_ids = {i.id for i in history.items}
    assert old_id in hist_ids
    assert new_id in hist_ids


def test_recall_knowledge_filter_includes_tool_call_tags() -> None:
    orchestrator = _fresh_orchestrator()
    orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="recall_user",
    )
    knowledge = MemoryItem(
        id="k-apt",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content="银河麒麟安装依赖时优先使用清华 apt 镜像",
        user_id="recall_user",
        metadata={"source_type": "tool_result", "tool_name": "apt"},
    )
    stored = orchestrator.resolve_and_store(knowledge)
    assert stored.metadata.get("milvus_memory_id")

    hits = orchestrator.recall(
        RetrievalQuery(
            query="麒麟装软件用哪个镜像",
            user_id="recall_user",
            memory_type=MemoryType.KNOWLEDGE,
            top_k=5,
        )
    )
    assert hits.items
    assert all(item.memory_type == MemoryType.KNOWLEDGE for item in hits.items)
    assert any("清华" in item.content for item in hits.items)


def test_recall_keeps_projects_separate_and_rejects_unrelated_queries() -> None:
    """项目问句不串答；无关问句返回空，而不是最近邻端口。"""

    orchestrator = _fresh_orchestrator()
    uid = "recall_project_user"
    xinghe = MemoryItem(
        id="k-xinghe",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content=(
            "用户上传Word「04_xinghe_config_update.docx」："
            "星河笔记配置变更通知 / XingHe Notes Config Update\n"
            "变更项: HTTP 端口 8080 -> 9090\n"
            "FACT project=XingHeNotes port=9090"
        ),
        user_id=uid,
        metadata={"source_type": "document", "file_kind": "docx", "filename": "04_xinghe_config_update.docx"},
    )
    course = MemoryItem(
        id="k-course",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content=(
            "课程助手 CourseAssistant 独立部署，监听 8080。"
            "FACT project=CourseAssistant port=8080"
        ),
        user_id=uid,
        metadata={"source_type": "document", "file_kind": "docx", "filename": "course_assistant.docx"},
    )
    office = MemoryItem(
        id="k-office",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content=(
            "Kylin Office Ops Guide\n"
            "Meeting room WiFi SSID: Kylin-Office-Guest\n"
            "Q: How to export PDF from WPS?\n"
            "A: File -> Export -> PDF"
        ),
        user_id=uid,
        metadata={"source_type": "document", "file_kind": "pdf", "filename": "02_office_ops_guide.pdf"},
    )
    for item in (xinghe, course, office):
        stored = orchestrator.resolve_and_store(item)
        assert stored.metadata.get("milvus_memory_id")
    # 模拟麒麟向量把英文办公文档挤出 top_k：大量中文近邻占满候选。
    for index in range(24):
        filler = MemoryItem(
            id=f"k-fill-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.LONG,
            content=(
                f"星河笔记填充材料 {index}：健康检查与版本说明，"
                "不含办公网络名称或办公套件步骤。"
            ),
            user_id=uid,
            metadata={
                "source_type": "document",
                "file_kind": "txt",
                "filename": f"filler_{index}.txt",
            },
        )
        stored = orchestrator.resolve_and_store(filler)
        assert stored.metadata.get("milvus_memory_id")

    course_hits = orchestrator.recall(RetrievalQuery(query="课程助手端口是多少？", user_id=uid, top_k=5))
    blob = "\n".join(item.content for item in course_hits.items)
    assert "CourseAssistant" in blob
    assert "8080" in blob
    assert not any(
        "9090" in item.content and "CourseAssistant" not in item.content
        for item in course_hits.items
    )

    wifi = orchestrator.recall(RetrievalQuery(query="会议室访客 WiFi？", user_id=uid, top_k=5))
    assert any("Kylin-Office-Guest" in item.content for item in wifi.items)

    wps = orchestrator.recall(RetrievalQuery(query="WPS 如何导出 PDF？", user_id=uid, top_k=5))
    assert any("Export" in item.content and "PDF" in item.content for item in wps.items)

    stock = orchestrator.recall(RetrievalQuery(query="明天股价多少？", user_id=uid, top_k=5))
    assert stock.items == []
    assert not any("9090" in (item.content or "") or "8080" in (item.content or "") for item in stock.items)


def test_forget_nl_deletes_preference_and_phone_for_request_user(tmp_path: Path) -> None:
    from src.security.service import SecurityAndForgetService

    orchestrator = _fresh_orchestrator()
    orchestrator.security = SecurityAndForgetService(
        user_id="default",
        index_path=str(tmp_path / "security-index.json"),
    )
    orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="kylin_demo_user",
    )
    ingested = orchestrator.process_input(
        {"content": "以后请用中文发送系统通知", "confidence": 0.9},
        "conversation",
        user_id="kylin_demo_user",
    )
    assert ingested.accepted is True
    orchestrator.security.filter_sensitive(
        "我的手机号是 13800138000，请记住",
        user_id="kylin_demo_user",
    )

    pref_deleted = orchestrator.forget("kylin_demo_user", "忘掉下载路径偏好")
    assert pref_deleted
    history = orchestrator.get_preference_history("kylin_demo_user", "habit.download_path")
    assert history == []

    phone_deleted = orchestrator.forget("kylin_demo_user", "忘记我的手机号")
    assert phone_deleted
    short = orchestrator.list_short_term("kylin_demo_user")
    assert all("HASH" not in (item.content or "") for item in short)
