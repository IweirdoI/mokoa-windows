"""Ingestion SQLite 中转库专项测试。

所有数据库和导出文件都写入 pytest 提供的 ``tmp_path``，确保重复运行测试不会
读取上一次运行留下的 TTL 指纹，也不会污染项目的 ``data/`` 目录。
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.common.models import DataSource, NormalizedRecord
from src.core import factory
from src.ingestion import IngestionPipeline, RejectCode, StagingStore
from src.ingestion.models import IngestionResult, PipelineStage


def _count_rows(db_path: Path, table: str) -> int:
    """读取测试库表行数；table 只由测试常量传入，不接收外部输入。"""

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_behavior_occurrence_accumulates_across_staging_instances(
    tmp_path: Path,
) -> None:
    """SQLite 审计应跨 pipeline 实例累计同指纹行为次数。"""

    db_path = tmp_path / "staging.db"
    payload = {
        "content": "用户打开了 VSCode",
        "occurrence_count": 1,
        "confidence": 0.7,
    }

    first_store = StagingStore(str(db_path))
    # 显式 0.6 仅作全局兜底; 按类型分阈值下 behavior 真实日志约 0.36, 同指纹累加至 count>=2
    # 即越过阈值被接受。本用例专注验证"跨 pipeline 实例的同指纹频次累积"。
    first_pipeline = IngestionPipeline(quality_threshold=0.6, staging_store=first_store)
    early = [
        first_pipeline.process(payload, "user_behavior", user_id="u001")
        for _ in range(4)
    ]
    assert first_store.count_events(
        "u001",
        dedup_fingerprint=str(early[0].dedup_fingerprint),
    ) == 4
    second = IngestionPipeline(
        quality_threshold=0.6,
        staging_store=StagingStore(str(db_path)),
    ).process(
        payload,
        "user_behavior",
        user_id="u001",
    )

    # 首次(低频次)应被质量门拒; 后续因频次累积越过按类型阈值被接受属正常去重。
    assert early[0].accepted is False
    assert early[0].reject_code == RejectCode.LOW_QUALITY
    # 跨实例累积: 第二个实例读到已累积 4 次, 去重阶段命中既有记录并累加为 5 次。
    # (命中既存记录返回 DUPLICATE 属正常去重, 记录 metadata 即证明跨实例频次累积生效)
    assert second.reject_code == RejectCode.DUPLICATE
    assert second.record is not None
    assert second.record.metadata["occurrence_count"] == 5
    assert second.record.metadata["occurrence_prior_count"] == 4
    assert second.record.metadata["occurrence_accumulated"] is True


def test_identity_defaulted_is_kept_and_tokenizer_is_not_redacted(
    tmp_path: Path,
) -> None:
    """identity_defaulted 必须保留；仅子串命中 token 的键不能整值清空。"""

    store = StagingStore(str(tmp_path / "staging.db"))
    pipeline = IngestionPipeline(staging_store=store)
    result = pipeline.process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        metadata={"tokenizer": "bert-base", "api_token": "should-redact"},
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.user_id == "default"
    assert result.record.metadata["identity_defaulted"] is True
    event = store.query_events("default")[0]
    assert event["metadata"]["identity_defaulted"] is True
    assert event["metadata"]["tokenizer"] == "bert-base"
    assert event["metadata"]["api_token"] == "[REDACTED]"


def test_process_with_staging_includes_stage_timings(tmp_path: Path) -> None:
    """注入 staging 后，结果应包含 staging 阶段耗时并写入审计 metadata。"""

    store = StagingStore(str(tmp_path / "staging.db"))
    result = IngestionPipeline(staging_store=store).process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="u001",
    )

    assert result.accepted is True
    assert set(result.stage_ms) >= {
        "collect",
        "clean",
        "normalize",
        "quality",
        "deduplicate",
        "staging",
    }
    assert result.processing_ms >= result.stage_ms["staging"]
    event = store.query_events("u001")[0]
    assert "collect" in event["metadata"]["stage_ms"]
    assert "staging" not in event["metadata"]["stage_ms"]


def test_staging_persists_accepted_event(tmp_path: Path) -> None:
    """成功记录应同时写入事件表、去重表和原始载荷表。"""

    db_path = tmp_path / "staging.db"
    store = StagingStore(str(db_path))
    pipeline = IngestionPipeline(staging_store=store)

    result = pipeline.process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )

    assert result.accepted is True
    events = store.query_events("u001")
    assert len(events) == 1
    event = events[0]
    assert event["accepted"] is True
    assert event["stage"] == "completed"
    assert event["source"] == "manual_config"
    assert event["content"] == result.record.content
    assert event["quality_score"] == result.quality_score
    assert event["dedup_fingerprint"] == result.dedup_fingerprint
    assert event["record_key"] == result.record.metadata["record_key"]
    assert event["quality_components"]
    assert event["reasons"] == result.reasons
    assert _count_rows(db_path, "dedup_cache") == 1
    assert _count_rows(db_path, "raw_payload_archive") == 1


def test_staging_duplicate_survives_new_pipeline_instance(tmp_path: Path) -> None:
    """创建新的 store/pipeline 后，未过期指纹仍必须被识别为重复。"""

    db_path = tmp_path / "staging.db"
    payload = {
        "action_id": "action-001",
        "tool_name": "pip",
        "status": "failed",
        "stderr": "连接超时",
    }

    first = IngestionPipeline(
        staging_store=StagingStore(str(db_path), dedup_ttl_seconds=300)
    ).process(payload, "tool_result", user_id="u001")
    second_store = StagingStore(str(db_path), dedup_ttl_seconds=300)
    second = IngestionPipeline(staging_store=second_store).process(
        payload,
        "tool_result",
        user_id="u001",
    )

    assert first.accepted is True
    assert second.accepted is False
    assert second.reject_code == RejectCode.DUPLICATE
    assert second.dedup_fingerprint == first.dedup_fingerprint
    events = second_store.query_events("u001")
    assert [event["accepted"] for event in events] == [False, True]
    assert events[0]["reject_code"] == "duplicate"


def test_low_quality_does_not_register_dedup_cache(tmp_path: Path) -> None:
    """质量门控拒绝项需要审计，但不能占用 TTL 去重位置。"""

    db_path = tmp_path / "staging.db"
    store = StagingStore(str(db_path))
    pipeline = IngestionPipeline(staging_store=store)

    result = pipeline.process(
        {"content": "嗯"},
        "conversation",
        user_id="u001",
    )

    assert result.accepted is False
    assert result.reject_code == RejectCode.LOW_QUALITY
    assert _count_rows(db_path, "dedup_cache") == 0
    events = store.query_events("u001")
    assert len(events) == 1
    assert events[0]["stage"] == "quality"
    assert events[0]["reject_code"] == "low_quality"


def test_expired_fingerprint_can_be_registered_again(tmp_path: Path) -> None:
    """过期行应在下次检查时清理，同一指纹随后可重新被接收。"""

    db_path = tmp_path / "staging.db"
    store = StagingStore(str(db_path), dedup_ttl_seconds=300)
    assert store.register_if_new("fingerprint-001", "u001") is True

    # 直接把过期时间改到过去，避免测试依赖 sleep 和机器调度精度。
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE dedup_cache SET expires_at = ? WHERE dedup_fingerprint = ?",
            ("2000-01-01T00:00:00.000000+00:00", "fingerprint-001"),
        )

    assert store.is_duplicate("fingerprint-001") is False
    assert store.register_if_new("fingerprint-001", "u001") is True
    assert _count_rows(db_path, "dedup_cache") == 1


def test_collect_failure_is_also_audited(tmp_path: Path) -> None:
    """即使来源枚举非法、没有 NormalizedRecord，也必须留下拒绝审计。"""

    store = StagingStore(str(tmp_path / "staging.db"))
    pipeline = IngestionPipeline(staging_store=store)
    result = pipeline.process(
        {"action_id": "bad-source-event", "content": "待处理数据"},
        "unknown_source",
        user_id="u001",
    )

    assert result.reject_code == RejectCode.INVALID_SOURCE
    event = store.query_events("u001")[0]
    assert event["stage"] == "collect"
    assert event["source"] == "unknown_source"
    assert event["content"] is None
    assert event["source_event_id"] == "bad-source-event"
    assert event["reject_code"] == "invalid_source"


def test_query_stats_and_export_jsonl(tmp_path: Path) -> None:
    """查询过滤、拒绝统计和 JSONL 导出应使用同一套结构化字段。"""

    store = StagingStore(str(tmp_path / "staging.db"))
    pipeline = IngestionPipeline(staging_store=store)
    accepted = pipeline.process(
        {"key": "reply_language", "value": "中文"},
        "manual_config",
        user_id="u001",
    )
    rejected = pipeline.process(
        {"content": "嗯"},
        "conversation",
        user_id="u001",
    )
    pipeline.process(
        {"key": "theme", "value": "dark"},
        "manual_config",
        user_id="u002",
    )
    assert accepted.accepted is True
    assert rejected.accepted is False

    accepted_events = store.query_events("u001", accepted=True)
    assert len(accepted_events) == 1
    assert accepted_events[0]["source"] == "manual_config"
    assert store.stats("u001") == {
        "total": 2,
        "accepted": 1,
        "rejected": 1,
        "by_reject_code": {"low_quality": 1},
    }

    output_path = tmp_path / "exports" / "u001.jsonl"
    assert store.export_jsonl(str(output_path), user_id="u001") == 2
    exported = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(exported) == 2
    assert all(isinstance(event["accepted"], bool) for event in exported)
    assert all(isinstance(event["metadata"], dict) for event in exported)
    assert all(isinstance(event["reasons"], list) for event in exported)


def test_archived_json_fields_remain_valid_when_truncated(tmp_path: Path) -> None:
    """超限 metadata/raw 必须保持合法 JSON，且实际 UTF-8 字节数不越界。"""

    db_path = tmp_path / "staging.db"
    pipeline = IngestionPipeline(
        staging_store=StagingStore(str(db_path)),
        max_text_length=8_000,
    )
    result = pipeline.process(
        {"content": "以后请记住" + "中文" * 10_000},
        "conversation",
        user_id="u001",
        metadata={"large_audit_field": "值" * 5_000},
    )
    assert result.accepted is True

    with sqlite3.connect(db_path) as connection:
        event_row = connection.execute(
            "SELECT metadata_json FROM ingestion_events"
        ).fetchone()
        raw_row = connection.execute(
            "SELECT raw_json FROM raw_payload_archive"
        ).fetchone()

    assert event_row is not None
    assert raw_row is not None
    metadata_json = str(event_row[0])
    raw_json = str(raw_row[0])
    assert len(metadata_json.encode("utf-8")) <= 4 * 1024
    assert len(raw_json.encode("utf-8")) <= 16 * 1024
    assert json.loads(metadata_json)["_truncated"] is True
    assert json.loads(raw_json)["_truncated"] is True


def test_raw_archive_is_redacted_by_default(tmp_path: Path) -> None:
    """raw 默认脱敏归档，嵌套敏感字段和正文中的 PII 都不能明文落盘。"""

    db_path = tmp_path / "staging.db"
    pipeline = IngestionPipeline(staging_store=StagingStore(str(db_path)))
    result = pipeline.process(
        {
            "content": "请联系 13800138000 或 user@example.com",
            "password": "dev-secret-123",
            "nested": {"access_token": "token-value-should-not-leak"},
        },
        "conversation",
        user_id="u001",
        metadata={"debug_token": "metadata-secret-value"},
    )
    # PII 风险会被质量门控拒绝，但拒绝事件同样必须安全归档。
    assert result.staging_event_id is not None

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT raw_json, envelope_json FROM raw_payload_archive"
        ).fetchone()
        event_row = connection.execute(
            "SELECT content, metadata_json FROM ingestion_events"
        ).fetchone()
    assert row is not None
    assert event_row is not None
    raw_json = str(row[0])
    archived = json.loads(raw_json)
    assert "13800138000" not in raw_json
    assert "user@example.com" not in raw_json
    assert "dev-secret-123" not in raw_json
    assert "token-value-should-not-leak" not in raw_json
    assert "[PHONE_REDACTED]" in archived["content"]
    assert archived["password"] == "[REDACTED]"
    assert archived["nested"]["access_token"] == "[REDACTED]"
    assert json.loads(str(row[1]))["archive_mode"] == "redacted"
    assert "13800138000" not in str(event_row[0])
    assert "user@example.com" not in str(event_row[0])
    assert "metadata-secret-value" not in str(event_row[1])
    assert json.loads(str(event_row[1]))["debug_token"] == "[REDACTED]"


def test_raw_archive_can_be_disabled_or_explicitly_plaintext_in_dev(
    tmp_path: Path,
) -> None:
    """off 不写 raw；plaintext 必须经过显式二次确认。"""

    off_db = tmp_path / "off.db"
    off_store = StagingStore(str(off_db), raw_archive_mode="off")
    IngestionPipeline(staging_store=off_store).process(
        {"key": "lang", "value": "中文"},
        "manual_config",
        user_id="u001",
    )
    assert _count_rows(off_db, "ingestion_events") == 1
    assert _count_rows(off_db, "raw_payload_archive") == 0

    with pytest.raises(ValueError, match="仅允许开发环境显式启用"):
        StagingStore(str(tmp_path / "unsafe.db"), raw_archive_mode="plaintext")

    dev_db = tmp_path / "dev.db"
    dev_store = StagingStore(
        str(dev_db),
        raw_archive_mode="plaintext",
        allow_plaintext_raw_archive=True,
    )
    IngestionPipeline(staging_store=dev_store).process(
        {"content": "测试手机号 13800138000"},
        "conversation",
        user_id="u001",
    )
    with sqlite3.connect(dev_db) as connection:
        row = connection.execute("SELECT raw_json FROM raw_payload_archive").fetchone()
    assert row is not None
    assert "13800138000" in str(row[0])


def test_forget_memory_scrubs_content_and_raw_but_keeps_audit_skeleton(
    tmp_path: Path,
) -> None:
    """精确遗忘只清目标内容，保留阶段、结果、时间和质量审计。"""

    db_path = tmp_path / "staging.db"
    store = StagingStore(str(db_path))
    result = IngestionPipeline(staging_store=store).process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="u001",
    )
    assert result.staging_event_id is not None
    assert store.bind_memory_id(result.staging_event_id, "memory-short-001") is True
    assert store.bind_milvus_id(
        "u001",
        "memory-short-001",
        "memory-long-001",
    ) == 1
    assert store.resolve_memory_ids("u001", "memory-short-001") == {
        "memory_ids": ["memory-short-001"],
        "milvus_memory_ids": ["memory-long-001"],
    }
    assert store.resolve_memory_ids("u001", "memory-long-001") == {
        "memory_ids": ["memory-short-001"],
        "milvus_memory_ids": ["memory-long-001"],
    }

    forgotten = store.forget_memory("u001", "memory-long-001")
    assert forgotten == {
        "events_scrubbed": 1,
        "raw_deleted": 1,
        "dedup_deleted": 1,
    }
    event = store.query_events("u001")[0]
    assert event["memory_id"] == "memory-short-001"
    assert event["milvus_memory_id"] == "memory-long-001"
    assert event["content"] is None
    assert event["dedup_fingerprint"] is None
    assert event["metadata"] == {}
    assert event["reasons"] == []
    assert event["stage"] == "completed"
    assert event["accepted"] is True
    assert event["quality_score"] == result.quality_score
    assert event["created_at"]
    assert _count_rows(db_path, "raw_payload_archive") == 0
    assert store.forget_memory("u001", "memory-short-001") == {
        "events_scrubbed": 0,
        "raw_deleted": 0,
        "dedup_deleted": 0,
    }


def test_manual_purge_uses_seven_day_raw_and_ninety_day_audit_defaults(
    tmp_path: Path,
) -> None:
    """手动 purge 按 7d raw / 90d audit 清理，并保留 90 天内审计骨架。"""

    db_path = tmp_path / "staging.db"
    store = StagingStore(str(db_path))
    pipeline = IngestionPipeline(staging_store=store)
    for index in range(3):
        pipeline.process(
            {"key": f"setting_{index}", "value": f"value_{index}"},
            "manual_config",
            user_id="u001",
        )

    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    ten_days_ago = (now - timedelta(days=10)).isoformat(timespec="microseconds")
    hundred_days_ago = (now - timedelta(days=100)).isoformat(timespec="microseconds")
    with sqlite3.connect(db_path) as connection:
        event_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM ingestion_events ORDER BY created_at, id"
            ).fetchall()
        ]
        connection.execute(
            "UPDATE ingestion_events SET created_at = ? WHERE id = ?",
            (hundred_days_ago, event_ids[0]),
        )
        connection.execute(
            "UPDATE ingestion_events SET created_at = ? WHERE id = ?",
            (ten_days_ago, event_ids[1]),
        )
        connection.execute(
            """
            UPDATE dedup_cache
            SET expires_at = ?
            WHERE dedup_fingerprint = (
                SELECT dedup_fingerprint FROM dedup_cache ORDER BY dedup_fingerprint LIMIT 1
            )
            """,
            (hundred_days_ago,),
        )

    purged = store.purge_expired(now=now)
    assert purged["raw_deleted"] == 2
    assert purged["audit_deleted"] == 1
    assert purged["dedup_deleted"] == 1
    assert _count_rows(db_path, "raw_payload_archive") == 1
    assert _count_rows(db_path, "ingestion_events") == 2


def test_register_if_new_is_atomic_across_store_instances(tmp_path: Path) -> None:
    """多个连接并发登记同一指纹时，只允许一个请求成为首次写入。"""

    db_path = tmp_path / "staging.db"
    stores = [StagingStore(str(db_path)) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        results = list(
            executor.map(
                lambda store: store.register_if_new("shared-fingerprint", "u001"),
                stores,
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == len(stores) - 1
    assert _count_rows(db_path, "dedup_cache") == 1


def test_factory_wires_sqlite_staging_from_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """正式工厂应读取 storage 配置，并把 SQLite store 注入 ingestion。"""

    db_path = tmp_path / "factory-staging.db"
    monkeypatch.setattr(
        factory,
        "_load_default_config",
        lambda: {
            "memory": {"quality_threshold": 0.6},
            "storage": {
                "staging_backend": "sqlite",
                "staging_sqlite": str(db_path),
                "dedup_ttl_seconds": 123,
            },
        },
    )

    orchestrator = factory.create_orchestrator()
    assert isinstance(orchestrator.ingestion, IngestionPipeline)
    assert isinstance(orchestrator.ingestion.staging, StagingStore)
    assert orchestrator.ingestion.staging.db_path == db_path
    assert orchestrator.ingestion.staging.dedup_ttl_seconds == 123
    assert orchestrator.ingestion.staging.raw_archive_mode == "redacted"
    assert orchestrator.ingestion.staging.raw_retention_days == 7
    assert orchestrator.ingestion.staging.audit_retention_days == 90
    assert orchestrator.ingestion.staging.auto_purge_interval_seconds == 0


def test_release_fingerprint_allows_same_event_after_downstream_failure(
    tmp_path: Path,
) -> None:
    """SQLite 去重释放后，新 pipeline 实例也应再次接收同一事件。"""

    db_path = tmp_path / "staging.db"
    payload = {"key": "reply_language", "value": "中文", "confidence": 0.9}
    first = IngestionPipeline(staging_store=StagingStore(str(db_path))).process(
        payload, "manual_config", user_id="u001"
    )
    assert first.accepted is True
    released = IngestionPipeline(staging_store=StagingStore(str(db_path)))
    assert released.release_dedup(first.dedup_fingerprint) is True
    retried = released.process(payload, "manual_config", user_id="u001")
    assert retried.accepted is True


def test_auto_purge_thread_starts_and_stops(tmp_path: Path) -> None:
    """间隔大于 0 时应启动守护线程，close 后线程退出。"""

    store = StagingStore(
        str(tmp_path / "auto-purge.db"),
        auto_purge_interval_seconds=60,
    )
    try:
        assert store._purge_thread is not None
        assert store._purge_thread.is_alive()
    finally:
        store.close()
    assert store._purge_thread is not None
    assert store._purge_thread.is_alive() is False


def test_operational_stats_include_latency_and_reject_codes(tmp_path: Path) -> None:
    """运维统计应同时给出拒绝分布和耗时分位数。"""

    store = StagingStore(str(tmp_path / "ops.db"))
    pipeline = IngestionPipeline(staging_store=store)
    pipeline.process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="u001",
    )
    pipeline.process({"content": "嗯"}, "conversation", user_id="u001")

    stats = store.operational_stats("u001")
    assert stats["total"] == 2
    assert stats["accepted"] == 1
    assert stats["by_reject_code"] == {"low_quality": 1}
    assert stats["sample_size"] == 2
    assert stats["latency_ms"]["max"] >= 0
    assert "collect" in stats["by_stage"]


def test_staging_store_close_is_idempotent_and_releases_connection(
    tmp_path: Path,
) -> None:
    """显式 close 和上下文退出都应真正释放连接，且可以重复调用。"""
    store = StagingStore(str(tmp_path / "close.db"))
    connection = store._connection

    store.close()
    store.close()

    assert store._closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with StagingStore(str(tmp_path / "context.db")) as context_store:
        context_connection = context_store._connection

    assert context_store._closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        context_connection.execute("SELECT 1")


def test_semantic_redact_applied_to_audit_content_and_raw_archive(
    tmp_path: Path,
) -> None:
    """注入语义级脱敏回调后，审计落库的 content 与原始归档都应走该回调（修 #8）。

    回归点：此前 staging_store 对 ingestion_events.content 与 raw_payload_archive
    只用规则级 redact_sensitive_text，语义级 PII 会明文落库。修复后只要注入了
    semantic_redact 回调（factory 在 SecurityAndForgetService 就绪后注入
    redact_for_export），两处都改用语义级脱敏。
    """

    calls: list[str] = []

    def semantic_redact(text: str) -> str:
        calls.append(text)
        return f"<SEM:{text}>"

    store = StagingStore(
        str(tmp_path / "semantic.db"),
        raw_archive_mode="redacted",
        semantic_redact=semantic_redact,
    )

    record = NormalizedRecord(
        type="manual_config",
        content="我的手机号13800138000",
        source=DataSource.MANUAL_CONFIG,
        user_id="u1",
    )
    result = IngestionResult(
        accepted=True,
        stage=PipelineStage.COMPLETED,
        record=record,
        reject_code=None,
        quality_score=0.9,
        dedup_fingerprint="fp1",
        processing_ms=1.0,
    )
    store.append_event(
        result,
        user_id="u1",
        scene_id=None,
        source="manual_config",
        raw={"note": "secret-token-abc", "password": "x"},
    )

    # 审计 content 应经过语义回调脱敏，而非规则级明文。
    with sqlite3.connect(store.db_path) as connection:
        content_row = connection.execute(
            "SELECT content FROM ingestion_events LIMIT 1"
        ).fetchone()
        raw_row = connection.execute(
            "SELECT raw_json FROM raw_payload_archive LIMIT 1"
        ).fetchone()

    assert content_row is not None
    assert content_row[0] == "<SEM:我的手机号13800138000>"
    assert "我的手机号13800138000" in calls

    # 原始归档：password 命中敏感 key 整值 [REDACTED]；note 走语义回调。
    assert raw_row is not None
    raw_payload = json.loads(raw_row[0])
    assert raw_payload.get("password") == "[REDACTED]"
    assert raw_payload.get("note") == "<SEM:secret-token-abc>"
    assert "secret-token-abc" in calls


def test_default_staging_store_uses_rule_based_redaction(tmp_path: Path) -> None:
    """未注入语义回调时，staging 回落到规则级脱敏，保持既有行为（向后兼容）。"""

    store = StagingStore(str(tmp_path / "rule.db"), raw_archive_mode="redacted")
    record = NormalizedRecord(
        type="manual_config",
        content="我的手机号13800138000",
        source=DataSource.MANUAL_CONFIG,
        user_id="u1",
    )
    result = IngestionResult(
        accepted=True,
        stage=PipelineStage.COMPLETED,
        record=record,
        reject_code=None,
        quality_score=0.9,
        dedup_fingerprint="fp2",
        processing_ms=1.0,
    )
    store.append_event(
        result,
        user_id="u1",
        scene_id=None,
        source="manual_config",
    )

    with sqlite3.connect(store.db_path) as connection:
        content_row = connection.execute(
            "SELECT content FROM ingestion_events LIMIT 1"
        ).fetchone()

    assert content_row is not None
    # 手机号命中规则，被规则级脱敏处理（不应是裸号）。
    assert "13800138000" not in content_row[0]
