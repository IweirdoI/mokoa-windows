"""API integration tests."""

from __future__ import annotations

import sqlite3
import tempfile

from fastapi.testclient import TestClient

from api.server import app, orchestrator
from src.core.mid_term import MidTermMemory
from src.core.short_term import ShortTermMemory
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.staging_store import StagingStore
from src.preference.store import PreferenceMemoryStore
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

client = TestClient(app)


def setup_function() -> None:
    # API 模块正式启动时使用配置中的持久化 SQLite；单元测试必须为每个用例换回
    # 全新的内存 ingestion，否则上一次 pytest 运行留下的 TTL 指纹会让固定样例
    # 被正确判定为 duplicate，进而破坏测试的可重复性。SQLite 接线由 staging
    # 专项测试单独覆盖。
    orchestrator.ingestion = IngestionPipeline()
    orchestrator.layer_manager._short = ShortTermMemory(
        capacity=orchestrator.layer_manager.short_capacity
    )
    orchestrator.layer_manager._mid = MidTermMemory(
        capacity=orchestrator.layer_manager.mid_capacity
    )
    orchestrator.layer_manager._long.clear()
    orchestrator._milvus = InMemoryMilvusClient()
    orchestrator.preference = PreferenceMemoryStore(
        storage_dir=tempfile.mkdtemp(prefix="pref_api_")
    )


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ingest_writes_to_short_term() -> None:
    ingest_response = client.post(
        "/ingest",
        json={
            "user_id": "demo_user",
            "source": "manual_config",
            "scene_id": "coding-session",
            "payload": {
                "key": "reply_language",
                "value": "中文",
                "confidence": 0.8,
            },
        },
    )
    assert ingest_response.status_code == 200
    body = ingest_response.json()
    assert body["status"] == "accepted"
    assert body["accepted"] is True
    assert body["user_id"] == "demo_user"
    assert body["memory_id"]
    assert body["memory_type"] == "preference"
    assert body["record"]["content"]

    list_response = client.get("/short-term/demo_user")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert listed["count"] == 1
    assert listed["capacity"] == 10
    assert listed["items"][0]["id"] == body["memory_id"]
    assert listed["items"][0]["memory_type"] == "preference"


def test_ingest_file_accepts_docx() -> None:
    from io import BytesIO

    from docx import Document

    document = Document()
    document.add_paragraph("麒麟办公助手：优先使用国内软件源安装依赖。")
    buffer = BytesIO()
    document.save(buffer)

    response = client.post(
        "/ingest/file",
        data={"user_id": "file_user", "scene_id": "os_operation"},
        files={
            "file": (
                "guide.docx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["filename"] == "guide.docx"
    assert body["memory_type"] == "knowledge"
    assert "国内软件源" in body["record"]["content"]


def test_ingest_rejection_returns_reasons() -> None:
    response = client.post(
        "/ingest",
        json={
            "user_id": "demo_user",
            "source": "conversation",
            "payload": {"content": "嗯"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["accepted"] is False
    assert body["reject_code"] == "low_quality"


def test_short_term_filters_by_memory_type() -> None:
    client.post(
        "/ingest",
        json={
            "user_id": "filter_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.8},
        },
    )
    client.post(
        "/ingest",
        json={
            "user_id": "filter_user",
            "source": "tool_result",
            "payload": {
                "tool_name": "pip",
                "status": "failed",
                "stderr": "连接超时",
                "confidence": 0.8,
            },
        },
    )

    response = client.get("/short-term/filter_user", params={"memory_type": "knowledge"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert all(item["memory_type"] == "knowledge" for item in body["items"])


def test_store_entry_creates_preference() -> None:
    response = client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "[preference] style.code_runnable=true | 代码要可运行",
                "memory_type": "preference",
                "task_type": "coding",
                "source": "chat",
                "user_id": "demo_user",
                "scene_id": "coding",
                "confidence": 0.9,
                "metadata": {
                    "preference_key": "style.code_runnable",
                    "preference_value": "true",
                    "category": "style",
                    "version": 1,
                    "is_latest": True,
                },
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["memory_id"]


def test_recall_returns_stored_preference() -> None:
    client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "[preference] habit.download_path=D:/Downloads | 下载目录",
                "memory_type": "preference",
                "task_type": "os_operation",
                "source": "manual",
                "user_id": "demo_user",
                "confidence": 1.0,
                "metadata": {
                    "preference_key": "habit.download_path",
                    "preference_value": "D:/Downloads",
                    "category": "habit",
                    "version": 1,
                    "is_latest": True,
                },
            }
        },
    )
    response = client.get(
        "/preferences/demo_user",
        params={"preference_key": "habit.download_path"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["metadata"]["preference_key"] == "habit.download_path"


def test_recall_endpoint_supports_query() -> None:
    client.post(
        "/store-entry",
        json={
            "entry": {
                "content": "pip 安装失败应使用国内镜像",
                "memory_type": "tool_call",
                "task_type": "os_operation",
                "source": "agent_log",
                "user_id": "demo_user",
                "confidence": 0.8,
                "metadata": {"tool_name": "pip"},
            }
        },
    )
    response = client.post(
        "/recall",
        json={"user_id": "demo_user", "query": "pip 镜像", "top_k": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["strategy"] in {"query_memory", "query_memory+lexical"}
    assert body["count"] >= 1


def test_consolidate_mid_moves_hot_items_to_mid_term_api() -> None:
    ingest = client.post(
        "/ingest",
        json={
            "user_id": "mid_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.95},
        },
    )
    assert ingest.status_code == 200

    short_item = orchestrator.list_short_term("mid_user")[0]
    short_item.metadata["access_count"] = 4
    short_item.metadata["heat"] = 6.0
    orchestrator.layer_manager.write_short(short_item)

    consolidate = client.post("/consolidate/mid_user/mid")
    assert consolidate.status_code == 200
    body = consolidate.json()
    assert body["promoted_to_mid"] == 1
    assert body["mid_remaining"] == 1

    mid_list = client.get("/mid-term/mid_user")
    assert mid_list.status_code == 200
    listed = mid_list.json()
    assert listed["count"] == 1
    assert listed["avg_heat"] > 0
    assert listed["items"][0]["layer"] == "mid"


def test_touch_endpoint_makes_short_memory_promotable() -> None:
    response = client.post(
        "/ingest",
        json={
            "user_id": "touch_api_user",
            "source": "manual_config",
            "payload": {"key": "reply_language", "value": "中文", "confidence": 0.95},
        },
    )
    assert response.status_code == 200
    memory_id = response.json()["memory_id"]

    touch_body = None
    for _ in range(4):
        touched = client.post(f"/short-term/touch_api_user/{memory_id}/touch")
        assert touched.status_code == 200
        touch_body = touched.json()
        assert touch_body["touched"] is True
    assert touch_body is not None and touch_body["heat"] >= 5.0

    consolidated = client.post("/consolidate/touch_api_user/mid")
    assert consolidated.status_code == 200
    assert consolidated.json()["promoted_to_mid"] == 1


def test_stats_endpoint_reports_layer_counts() -> None:
    client.post(
        "/ingest",
        json={
            "user_id": "stats_api_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.9},
        },
    )

    response = client.get("/stats/stats_api_user")
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "stats_api_user"
    assert body["total_count"] >= 1
    assert body["layers"]["short"]["count"] == 1
    assert body["layers"]["short"]["capacity"] == 10
    assert body["layers"]["mid"]["capacity"] == 50


def test_forget_endpoint_deletes_short_term_memory_and_staging_content(
    tmp_path,
) -> None:
    db_path = tmp_path / "forget-staging.db"
    staging = StagingStore(str(db_path))
    orchestrator.ingestion = IngestionPipeline(staging_store=staging)
    ingest = client.post(
        "/ingest",
        json={
            "user_id": "forget_api_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.9},
        },
    )
    assert ingest.status_code == 200
    memory_id = ingest.json()["memory_id"]
    assert memory_id is not None
    event_before = staging.query_events("forget_api_user")[0]
    assert event_before["memory_id"] == memory_id
    assert event_before["content"]

    short_before = client.get("/short-term/forget_api_user")
    assert short_before.json()["count"] == 1

    orchestrator.security.forget = lambda user_id, instruction: [memory_id]  # type: ignore[method-assign]

    response = client.post(
        "/forget",
        json={
            "user_id": "forget_api_user",
            "instruction": "忘掉刚才的配置",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["deleted_ids"] == [memory_id]

    short_after = client.get("/short-term/forget_api_user")
    assert short_after.json()["count"] == 0

    event_after = staging.query_events("forget_api_user")[0]
    assert event_after["content"] is None
    assert event_after["stage"] == "completed"
    assert event_after["accepted"] is True
    assert event_after["quality_score"] is not None
    with sqlite3.connect(db_path) as connection:
        raw_count = connection.execute(
            "SELECT COUNT(*) FROM raw_payload_archive"
        ).fetchone()
    assert raw_count is not None
    assert int(raw_count[0]) == 0


# ---------------------------------------------------------------------------
# Batch H：边界安全复查修复（#1/#5/#6/#9/#10）回归测试
# ---------------------------------------------------------------------------


def test_default_cors_is_not_wildcard_with_credentials() -> None:
    """CORS 不允许 “*” 与 credentials 同时出现（#1）。"""

    import api.server as srv

    assert srv._allow_origins != ["*"]
    assert srv._allow_credentials is False


def test_api_requires_token_when_configured(monkeypatch) -> None:
    """配置 API_AUTH_TOKEN 后非豁免端点必须带令牌，否则 401（#1）。"""

    import api.server as srv

    monkeypatch.setattr(srv, "_API_AUTH_TOKEN", "secret-token")
    try:
        # /health 显式豁免鉴权
        assert client.get("/health").status_code == 200
        # 其余端点缺令牌应被拒
        assert client.get("/stats/someuser").status_code == 401
        # 带正确令牌应通过鉴权（后续由业务逻辑决定最终状态码，但不应再是 401）
        resp = client.get(
            "/stats/someuser", headers={"Authorization": "Bearer secret-token"}
        )
        assert resp.status_code != 401
        # 错误令牌应被拒
        assert (
            client.get(
                "/stats/someuser", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
    finally:
        monkeypatch.undo()


def test_health_does_not_leak_internal_details() -> None:
    """/health 不应回显 collection / vector_dim / kylin_strict 等内部细节（#10）。"""

    body = client.get("/health").json()
    assert "collection" not in body
    assert "vector_dim" not in body
    assert "kylin_strict" not in body


def test_outbound_ssrf_guard_blocks_internal() -> None:
    """出站 URL 安全校验应阻断回环/私有/链路本地，放行公网 IP（#5）。"""

    from src.security.outbound import is_external_url_safe

    assert is_external_url_safe("http://127.0.0.1/") is False
    assert is_external_url_safe("http://localhost/") is False
    assert is_external_url_safe("http://169.254.169.254/") is False
    assert is_external_url_safe("http://192.168.1.1/") is False
    assert is_external_url_safe("http://10.0.0.5/") is False
    # 带 userinfo 的 URL 视为不安全
    assert is_external_url_safe("http://user:pass@example.com/") is False
    # 非 http/https 协议被拒
    assert is_external_url_safe("ftp://example.com/") is False
    # 公网 IP 字面量应放行（无需联网）
    assert is_external_url_safe("http://1.1.1.1/") is True


def test_web_fetch_blocks_internal_address() -> None:
    """web 抓取工具在入口处拦截内网/回环地址（#5）。"""

    from src.agent.service import _web_fetch

    ok, msg = _web_fetch("http://169.254.169.254/latest/meta-data/")
    assert ok is False
    assert "SSRF" in msg


class _FakeUpload:
    """模拟 UploadFile，按固定分块返回数据，便于离线测试有界读取。"""

    def __init__(self, data: bytes, chunk: int = 4) -> None:
        self._parts = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
        self._i = 0

    async def read(self, n: int = -1) -> bytes:
        if self._i >= len(self._parts):
            return b""
        part = self._parts[self._i]
        self._i += 1
        return part


def test_read_upload_bounded_rejects_over_limit() -> None:
    import asyncio

    from api.server import _read_upload_bounded

    fake = _FakeUpload(b"x" * 10, chunk=4)
    try:
        asyncio.run(_read_upload_bounded(fake, 5))
        raise AssertionError("应因超出上限而抛出 413")
    except Exception as exc:  # noqa: BLE001
        from fastapi import HTTPException

        assert isinstance(exc, HTTPException)
        assert exc.status_code == 413


def test_read_upload_bounded_returns_full_data() -> None:
    import asyncio

    from api.server import _read_upload_bounded

    fake = _FakeUpload(b"hello world", chunk=4)
    assert asyncio.run(_read_upload_bounded(fake, 100)) == b"hello world"


def test_defense_middleware_rejects_oversized_body(monkeypatch) -> None:
    import api.server as srv

    monkeypatch.setattr(srv, "MAX_BODY_BYTES", 100)
    big = "x" * 200
    response = client.post(
        "/ingest",
        json={"user_id": "u", "source": "manual_config", "payload": {"big": big}},
    )
    assert response.status_code == 413


def test_defense_middleware_rate_limits_per_ip(monkeypatch) -> None:
    import api.server as srv

    srv._rate_state.clear()
    monkeypatch.setattr(srv, "RATE_LIMIT_MAX", 3)
    try:
        codes = [client.get("/health").status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429
    finally:
        monkeypatch.undo()
        srv._rate_state.clear()


def test_defense_middleware_allows_normal_request() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_milvus_client_does_not_connect_at_init() -> None:
    from src.knowledge.memory_service.milvus_client import MilvusClient

    # 构造时不应立即连接/建集合（修复 #4：Milvus 不可达也不应让服务起不来）
    store = MilvusClient()
    assert store.client is None


def test_memory_backend_unavailable_maps_to_503() -> None:
    import asyncio

    from src.knowledge.memory_service.milvus_client import MemoryBackendUnavailable
    from api.server import backend_unavailable

    response = asyncio.run(backend_unavailable(None, MemoryBackendUnavailable("boom")))
    assert response.status_code == 503


def test_staging_stats_endpoint_returns_reject_and_latency(tmp_path) -> None:
    db_path = tmp_path / "stats-staging.db"
    staging = StagingStore(str(db_path))
    orchestrator.ingestion = IngestionPipeline(staging_store=staging)
    client.post(
        "/ingest",
        json={
            "user_id": "stats_staging_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.9},
        },
    )
    client.post(
        "/ingest",
        json={
            "user_id": "stats_staging_user",
            "source": "conversation",
            "payload": {"content": "嗯"},
        },
    )

    response = client.get("/staging/stats", params={"user_id": "stats_staging_user"})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["user_id"] == "stats_staging_user"
    assert body["total"] == 2
    assert body["accepted"] == 1
    assert body["by_reject_code"] == {"low_quality": 1}
    assert "p95" in body["latency_ms"]


def test_staging_purge_endpoint_uses_configured_defaults(tmp_path) -> None:
    db_path = tmp_path / "purge-staging.db"
    staging = StagingStore(
        str(db_path),
        raw_retention_days=0,
        audit_retention_days=0,
    )
    orchestrator.ingestion = IngestionPipeline(staging_store=staging)
    client.post(
        "/ingest",
        json={
            "user_id": "purge_user",
            "source": "manual_config",
            "payload": {"key": "lang", "value": "中文", "confidence": 0.9},
        },
    )

    response = client.post("/staging/purge", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["raw_deleted"] == 1
    assert body["audit_deleted"] == 1
    assert "raw_cutoff" in body
    assert "audit_cutoff" in body
