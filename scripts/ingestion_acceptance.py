#!/usr/bin/env python3
"""3fba60e1 · ingestion 准上线验收脚本。

重放 ``benchmarks/datasets/ingestion_acceptance.jsonl``，再生成一批合成事件
测量吞吐与 p95。可在开发机或麒麟真机上运行：

    python scripts/ingestion_acceptance.py
    python scripts/ingestion_acceptance.py --repeat 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import IngestionPipeline, StagingStore
DATASET = PROJECT_ROOT / "benchmarks" / "datasets" / "ingestion_acceptance.jsonl"


def _load_samples() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _replay(pipeline: IngestionPipeline, samples: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for sample in samples:
        result = pipeline.process(
            sample["payload"],
            sample["source"],
            user_id=sample["user_id"],
            scene_id=sample.get("scene_id"),
        )
        expected = sample["expected"]
        ok = result.accepted is expected["accepted"]
        if expected.get("reject_code") is not None:
            actual_code = result.reject_code.value if result.reject_code else None
            ok = ok and actual_code == expected["reject_code"]
        if "privacy_risk" in expected and result.record is not None:
            flagged = bool(result.record.metadata.get("privacy_risk"))
            ok = ok and flagged is expected["privacy_risk"]
        rows.append(
            {
                "id": sample["id"],
                "ok": ok,
                "accepted": result.accepted,
                "reject_code": (
                    result.reject_code.value if result.reject_code else None
                ),
                "processing_ms": result.processing_ms,
            }
        )
    return rows


def _throughput(pipeline: IngestionPipeline, repeat: int) -> list[float]:
    latencies: list[float] = []
    for index in range(repeat):
        started = time.perf_counter()
        pipeline.process(
            {
                "action_id": f"bench-{index}",
                "tool_name": "pip",
                "status": "failed",
                "stderr": "连接超时，建议切换国内镜像",
                "confidence": 0.85,
            },
            "tool_result",
            user_id="bench_user",
            scene_id="os_operation",
        )
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


def main() -> int:
    parser = argparse.ArgumentParser(description="ingestion 准上线验收")
    parser.add_argument("--repeat", type=int, default=80, help="合成吞吐样本数")
    args = parser.parse_args()

    samples = _load_samples()
    with tempfile.TemporaryDirectory() as tmp:
        store = StagingStore(
            str(Path(tmp) / "acceptance.db"),
            auto_purge_interval_seconds=0,
        )
        pipeline = IngestionPipeline(staging_store=store)
        replay = _replay(pipeline, samples)
        latencies = _throughput(pipeline, args.repeat)
        ops = store.operational_stats()
        store.close()

    failed = [row for row in replay if not row["ok"]]
    print(f"dataset: {DATASET}")
    print(f"replay: {len(replay) - len(failed)}/{len(replay)} passed")
    for row in failed:
        print(f"  FAIL {row['id']} accepted={row['accepted']} code={row['reject_code']}")
    print(
        "throughput_ms: "
        f"p50={statistics.median(latencies):.3f} "
        f"p95={sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]:.3f} "
        f"max={max(latencies):.3f} "
        f"n={len(latencies)}"
    )
    print(
        "staging: "
        f"total={ops['total']} accepted={ops['accepted']} "
        f"rejected={ops['rejected']} by_reject={ops['by_reject_code']}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
