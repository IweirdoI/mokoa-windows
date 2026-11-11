"""Orchestrator preference wiring tests (no live Milvus)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.common.models import DataSource, RawRecord
from src.core.orchestrator import MemoryOrchestrator, StoreEntryResult
from src.preference.store import PreferenceMemoryStore


def test_extract_and_store_preferences_wires_to_store_memory_entry():
    with tempfile.TemporaryDirectory(prefix="pref_orch_") as tmp:
        preference = PreferenceMemoryStore(
            storage_dir=tmp,
            use_llm=False,
            allow_rules=True,
        )
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch.preference = preference
        orch.conflict_resolver = MagicMock()
        calls: list = []

        def _fake_store(entry):
            calls.append(entry)
            return StoreEntryResult(
                memory_id=f"mem_{len(calls)}",
                created=True,
                reason="ok",
            )

        orch.store_memory_entry = _fake_store  # type: ignore[method-assign]

        result = orch.extract_preferences_from_text(
            "把桌面上所有 .tmp 文件删了，直接删不用问我",
            user_id="u_wire",
            scene_id="os_operation",
        )
        assert result.mode == "rule"
        assert result.extracted_count >= 1
        assert len(result.stored) == result.extracted_count
        assert all(s.created for s in result.stored)
        keys = {e.metadata.get("preference_key") for e in result.entries}
        assert "security.confirm_before_rm" in keys
        hist = orch.preference.get_version_history("u_wire", "security.confirm_before_rm")
        assert hist
        assert hist[-1].metadata.get("preference_value") in ("false", False, "False") or str(
            hist[-1].metadata.get("preference_value")
        ).lower() == "false"


def test_version_chain_via_extract_path():
    with tempfile.TemporaryDirectory(prefix="pref_orch_v_") as tmp:
        preference = PreferenceMemoryStore(
            storage_dir=tmp,
            use_llm=False,
            allow_rules=True,
        )
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch.preference = preference
        orch.store_memory_entry = lambda entry: StoreEntryResult(  # type: ignore[method-assign]
            memory_id="x",
            created=True,
            reason="ok",
        )

        orch.extract_preferences_from_text(
            "下载路径设为 D:/Downloads。",
            user_id="demo_version_chain",
            scene_id="os_operation",
        )
        orch.extract_preferences_from_text(
            "下载路径改到 E:/Downloads。",
            user_id="demo_version_chain",
            scene_id="os_operation",
        )
        hist = orch.preference.get_version_history(
            "demo_version_chain", "habit.download_path"
        )
        assert len(hist) >= 2
        assert hist[-1].metadata.get("preference_value") == "E:/Downloads"
        assert hist[-1].metadata.get("is_latest") is True
        assert any(not h.metadata.get("is_latest") for h in hist[:-1]) or hist[0].version < hist[-1].version


def test_extract_and_store_from_raw_records():
    with tempfile.TemporaryDirectory(prefix="pref_orch_r_") as tmp:
        preference = PreferenceMemoryStore(
            storage_dir=Path(tmp),
            use_llm=False,
            allow_rules=True,
        )
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch.preference = preference
        orch.store_memory_entry = lambda entry: StoreEntryResult(  # type: ignore[method-assign]
            memory_id="raw1",
            created=True,
        )
        records = [
            RawRecord(
                id="r1",
                source=DataSource.CONVERSATION,
                user_id="u2",
                scene_id="coding",
                payload={"content": "写代码时请保证可直接运行，并附带必要测试命令。"},
            )
        ]
        result = orch.extract_and_store_preferences(records)
        assert result.user_id == "u2"
        assert any(
            e.metadata.get("preference_key") == "style.code_runnable" for e in result.entries
        )


def test_process_input_parallel_path_b_stores_long_term_preference():
    """聊天仅 ingest 时，规则命中应并行写入长期偏好（不依赖前端 /preferences/extract）。"""
    with tempfile.TemporaryDirectory(prefix="pref_orch_ingest_") as tmp:
        preference = PreferenceMemoryStore(
            storage_dir=tmp,
            use_llm=False,
            allow_rules=True,
        )
        orch = MemoryOrchestrator(preference=preference)
        result = orch.process_input(
            {"content": "以后回答请更加简洁", "user_id": "u_p2_chat"},
            "conversation",
            user_id="u_p2_chat",
        )
        assert result.accepted
        assert result.preference_extracted >= 1
        assert "style.response_tone" in result.preference_keys
        latest = orch.list_preferences("u_p2_chat", latest_only=True).items
        assert any(
            i.metadata.get("preference_key") == "style.response_tone"
            and str(i.metadata.get("preference_value")) == "concise"
            for i in latest
        )
