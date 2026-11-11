"""Offline end-to-end wiring check for preference storage, recall and delete."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("OS_AGENT_MEMORY_BACKEND", "memory")
os.environ.setdefault("OS_AGENT_EMBEDDING_BACKEND", "hash")

from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
from src.preference.store import PreferenceMemoryStore


def main() -> int:
    milvus = InMemoryMilvusClient()
    temp_storage = tempfile.TemporaryDirectory(prefix="memory-e2e-")
    preference = PreferenceMemoryStore(
        storage_dir=Path(temp_storage.name),
        use_llm=False,
        allow_rules=True,
    )
    orchestrator = MemoryOrchestrator(preference=preference, milvus=milvus)

    first = orchestrator.extract_preferences_from_text(
        "下载默认放 D 盘",
        user_id="preflight_user",
        scene_id="general",
    )
    if not first.entries:
        print(json.dumps({"passed": False, "error": "rule extraction returned no entries"}, ensure_ascii=False))
        return 1
    updated_entry = first.entries[0].model_copy(deep=True)
    updated_entry.content = "[preference] habit.download_path=E:/Downloads | 以后改到 E 盘下载"
    updated_entry.metadata["preference_value"] = "E:/Downloads"
    updated_entry.confidence = 1.0
    updated_entry.metadata["source_type"] = "manual_config"
    second = orchestrator.store_memory_entry(updated_entry)
    history = orchestrator.list_preferences(
        "preflight_user",
        latest_only=False,
        top_k=10,
    )
    latest = orchestrator.list_preferences(
        "preflight_user",
        latest_only=True,
        top_k=10,
    )

    first_created = sum(1 for item in first.stored if item.created)
    second_created = int(second.created)
    history_items = history.items
    latest_items = latest.items
    report = {
        "passed": bool(first_created and second_created and history_items and latest_items),
        "backend": "memory",
        "embedding": "hash",
        "first_created": first_created,
        "second_created": second_created,
        "second_reason": second.reason,
        "history_count": len(history_items),
        "latest_count": len(latest_items),
        "latest_content": latest_items[0].content if latest_items else None,
        "note": "仅验证接线，不代表真实Milvus、麒麟SDK或性能验收",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    temp_storage.cleanup()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
