#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化评测类对话数据集：DailyDialog + DIALOGBENCH.

在麒麟宿主上：
    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_dialog_quant_datasets.py --seed 42 --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OS_AGENT_EMBEDDING_BACKEND", "local")
os.environ.setdefault("OS_AGENT_MEMORY_BACKEND", "memory")
try:
    from src.common.env import load_env
    load_env()
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.run_dataset_eval import load_dailydialog, load_dialogbench  # noqa: E402
from scripts.eval_dialog_tool_datasets import classify_batch, orchestrate, retrieve  # noqa: E402
from src.core.dialogue_query import task_query  # noqa: E402
from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", type=str, default="benchmarks/results")
    args = ap.parse_args()
    if args.n <= 0:
        ap.error("--n must be positive")
    rng = random.Random(args.seed)
    backend = os.environ.get("OS_AGENT_EMBEDDING_BACKEND", "local")
    get_shared_embedding_backend(backend)

    print(f"[1/4] 加载 DailyDialog n={args.n} ...", flush=True)
    dd_items, dd_idx = load_dailydialog(args.n, rng)
    print(
        f"      loaded={len(dd_items)} nonempty={sum(1 for c, _ in dd_items if c.strip())}",
        flush=True,
    )
    print(f"[2/4] 加载 DIALOGBENCH JSONL n={args.n}（坏行跳过）...", flush=True)
    parse_diagnostics = {}
    db_items, db_idx = load_dialogbench(args.n, rng, diagnostics=parse_diagnostics)
    print(
        f"      loaded={len(db_items)} nonempty={sum(1 for c, _ in db_items if c.strip())} "
        f"tasks={sorted({str(m.get('task') or '') for _, m in db_items})}",
        flush=True,
    )
    if not dd_items:
        raise RuntimeError("DailyDialog has no samples; check git/DailyDialog/train.zip")
    if not db_items:
        print("      DIALOGBENCH 无可用样本，跳过该库", flush=True)

    print("[3/4] 分类 + 编排器 ...", flush=True)
    results = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "n": args.n,
            "embedding_backend": backend,
            "memory_backend": os.environ.get("OS_AGENT_MEMORY_BACKEND", "memory"),
            "commit": subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True
            ).strip(),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "query_strategy": "task_query_on_user_turns_v1",
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "sampling_fn": "benchmarks.run_dataset_eval.sample_indices",
                "algorithm": "one Random(seed); load_dailydialog then load_dialogbench (shared rng)",
            },
            "dialogbench_parse": "jsonl_skip_bad_lines",
            "query_window": "complete_user_turns_within_indexed_1500_chars",
            "metric_scope": "memory_id_self_retrieval_only; not BLEU, option accuracy or official task (7) score",
        },
        "dailydialog": {
            "sampled_indices": dd_idx,
            "classify": classify_batch("DailyDialog", "conversation", dd_items),
            "orchestrator": orchestrate("DailyDialog", "conversation", dd_items, "dlg_dd"),
        },
    }
    if db_items:
        results["dialogbench"] = {
            "parse_diagnostics": parse_diagnostics,
            "sampled_indices": db_idx,
            "classify_dialogue": classify_batch("DIALOGBENCH", "conversation", db_items),
            "orchestrator": orchestrate("DIALOGBENCH", "conversation", db_items, "dlg_db"),
        }
    else:
        results["dialogbench"] = {"skipped": True, "reason": "no_jsonl_samples", "parse_diagnostics": parse_diagnostics}

    print("[4/4] 自检索 Hit@5 ...", flush=True)
    dd_task_q = [task_query(m.get("user_turns") or []) for _, m in dd_items]
    dd_last_q = [str(m.get("last_user") or "") for _, m in dd_items]
    results["dailydialog"]["retrieval"] = retrieve(
        "DailyDialog", dd_items, dd_task_q, "dlg_dd_ret", backend
    )
    results["dailydialog"]["last_user_control"] = retrieve(
        "DailyDialog", dd_items, dd_last_q, "dlg_dd_last", backend
    )
    if db_items:
        db_task_q = [task_query(m.get("user_turns") or []) for _, m in db_items]
        db_last_q = [str(m.get("last_user") or "") for _, m in db_items]
        results["dialogbench"]["retrieval"] = retrieve(
            "DIALOGBENCH", db_items, db_task_q, "dlg_db_ret", backend
        )
        results["dialogbench"]["last_user_control"] = retrieve(
            "DIALOGBENCH", db_items, db_last_q, "dlg_db_last", backend
        )

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"dialog_quant_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(json_path),
        "metric_scope": results["meta"]["metric_scope"],
        "dailydialog": {
            "classify": results["dailydialog"]["classify"],
            "accept_rate": results["dailydialog"]["orchestrator"]["accept_rate"],
            "hit_at_5": results["dailydialog"]["retrieval"]["hit_at_5"],
            "last_user_hit_at_5": results["dailydialog"]["last_user_control"]["hit_at_5"],
            "query_p95_ms": results["dailydialog"]["retrieval"]["query_p95_ms"],
        },
        "dialogbench": (
            {
                "classify_dialogue": results["dialogbench"]["classify_dialogue"],
                "accept_rate": results["dialogbench"]["orchestrator"]["accept_rate"],
                "hit_at_5": results["dialogbench"]["retrieval"]["hit_at_5"],
                "last_user_hit_at_5": results["dialogbench"]["last_user_control"]["hit_at_5"],
                "query_p95_ms": results["dialogbench"]["retrieval"]["query_p95_ms"],
            }
            if db_items
            else results["dialogbench"]
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
