"""Evaluate conflict resolution correctness on conflict_resolution.jsonl.

Measures the 赛题要求 (4) 指标：知识冲突处理正确率 >= 88%.

Each dataset row defines an (old, new) memory pair plus the expected outcome:
  - "replace"      : detect == True  and resolver decides REPLACE
  - "keep_old"     : detect == True  and resolver decides KEEP_OLD
  - "no_conflict"  : detect == False (correctly not flagged as a conflict)

The resolver splits preference vs knowledge: preferences use source-trust
hierarchy (manual_config > conversation > behavior), knowledge uses
"latest fact wins" with a low-trust guard. This dataset encodes those
product-intent expectations; it is an independent gold standard, not the
resolver's own construction.

Usage:
  python scripts/conflict_eval_dataset.py
  python scripts/conflict_eval_dataset.py --dataset benchmarks/datasets/conflict_resolution.jsonl --out docs/evidence/conflict_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.models import MemoryItem, MemoryType, MemoryLayer
from src.core.conflict_resolver import ConflictResolver, ConflictAction

CONFLICT_CORRECTNESS_TARGET = 0.88


def _build(spec: dict, base: datetime) -> MemoryItem:
    mt = MemoryType(spec.get("type", "knowledge"))
    updated = base + timedelta(days=spec.get("age_days", 0))
    meta: dict = {"source_type": spec["source_type"]}
    key = spec.get("key")
    if key:
        field = "preference_key" if mt == MemoryType.PREFERENCE else "knowledge_key"
        meta[field] = key
    if spec.get("quality_score") is not None:
        meta["quality_score"] = spec["quality_score"]
    scene = spec.get("scene_id")
    return MemoryItem(
        id="old" if spec.get("_role") == "old" else "new",
        memory_type=mt,
        layer=MemoryLayer.LONG,
        content=spec["content"],
        user_id="bench",
        scene_id=scene,
        metadata=meta,
        updated_at=updated,
    )


def evaluate(dataset: Path) -> dict:
    resolver = ConflictResolver()
    base = datetime.utcnow()
    rows: list[dict] = []
    correct = 0
    per_case: list[dict] = []
    with dataset.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    for row in rows:
        old = _build({**row["old"], "_role": "old"}, base)
        new = _build({**row["new"], "_role": "new"}, base)
        detected = resolver.detect(old, new)
        expected = row["expected"]
        if expected == "no_conflict":
            ok = not detected
            got = "no_conflict" if not detected else "false_conflict"
        else:
            action = (
                resolver.resolve_preference(old, new)
                if old.memory_type == MemoryType.PREFERENCE
                else resolver.resolve_knowledge(old, new)
            )
            ok = detected and action.action.value == expected
            got = action.action.value if detected else "not_detected"
        correct += int(ok)
        per_case.append(
            {
                "id": row["id"],
                "category": row.get("category"),
                "expected": expected,
                "got": got,
                "correct": ok,
                "notes": row.get("notes"),
            }
        )

    total = len(rows)
    correctness = correct / total if total else 0.0
    return {
        "target": CONFLICT_CORRECTNESS_TARGET,
        "samples": total,
        "correct": correct,
        "correctness": round(correctness, 4),
        "pass": correctness >= CONFLICT_CORRECTNESS_TARGET,
        "details": per_case,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "benchmarks" / "datasets" / "conflict_resolution.jsonl",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    report = evaluate(args.dataset)
    summary = {k: v for k, v in report.items() if k != "details"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    for c in report["details"]:
        flag = "OK " if c["correct"] else "XX "
        print(f"  {flag}{c['id']:5} exp={c['expected']:11} got={str(c['got']):13} {c['category']}")
    if not report["pass"]:
        print(f"\n未达到 {CONFLICT_CORRECTNESS_TARGET:.0%} 目标（实际 {report['correctness']:.0%}）")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
