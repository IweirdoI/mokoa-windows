#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话（MultiWOZ）与工具调用（ToolBench）专项评测.

在麒麟宿主上：
    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_dialog_tool_datasets.py --seed 42 --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import atexit
import shutil
import subprocess
import tempfile
from collections import Counter
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

from benchmarks.run_dataset_eval import load_multiwoz, load_toolbench  # noqa: E402
from src.core.classifier import MemoryClassifier  # noqa: E402
from src.core.dialogue_query import task_query  # noqa: E402
from src.core.factory import create_orchestrator  # noqa: E402
from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402
from src.knowledge.memory_service.embedding_client import EmbeddingClient  # noqa: E402
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient  # noqa: E402
from src.knowledge.memory_service.memory_schema import (  # noqa: E402
    CreateMemoryRequest,
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    QueryMemoryRequest,
)
from src.knowledge.memory_service.create_memory import create_memory  # noqa: E402
from src.knowledge.memory_service.query_memory import query_memory  # noqa: E402


_STAGING_TMPDIR = None


def _ensure_isolated_staging() -> None:
    """将 staging 指向进程级临时 sqlite，避免污染共享库 ./data/ingestion_staging.db。

    共享库的 300s dedup TTL 会让同 seed 复跑的 process_input 全部被判重，
    使编排接受率变 0/50、跑分失真（见 docs/测试结果.md 第 15.2 节）。
    参考 scripts/ingestion_acceptance.py，评测应在独立临时库上进行。
    """
    global _STAGING_TMPDIR
    if _STAGING_TMPDIR is not None:
        return
    _STAGING_TMPDIR = tempfile.mkdtemp(prefix="mokoa_eval_staging_")
    atexit.register(shutil.rmtree, _STAGING_TMPDIR, ignore_errors=True)
    from src.common.config import load_default_config

    cfg = load_default_config()
    cfg.setdefault("storage", {})["staging_sqlite"] = os.path.join(
        _STAGING_TMPDIR, "staging.db"
    )


class _Rec:
    def __init__(self, content: str, rtype: str, metadata: dict | None = None):
        self.content = content
        self.type = rtype
        self.metadata = metadata or {}


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (k - lo)) + s[hi] * (k - lo)


def classify_batch(name: str, source: str, items: list) -> dict:
    clf = MemoryClassifier()
    types, reasons, empty = Counter(), Counter(), 0
    for content, _meta in items:
        if not (content or "").strip():
            empty += 1
        decision = clf.explain(_Rec(content, source, _meta))
        types[decision["memory_type"]] += 1
        reasons[decision["reason"]] += 1
    n = len(items)
    return {
        "n": n,
        "empty_content": empty,
        "source": source,
        "type_distribution": dict(types),
        "reason_distribution": dict(reasons),
        "knowledge_rate": round(types["knowledge"] / n, 4) if n else 0.0,
        "preference_rate": round(types["preference"] / n, 4) if n else 0.0,
    }


def orchestrate(name: str, source: str, items: list, user_id: str) -> dict:
    _ensure_isolated_staging()
    orch = create_orchestrator()
    accepted = 0
    types: Counter = Counter()
    reject: Counter = Counter()
    scores: list[float] = []
    samples: list[dict] = []
    for content, meta in items:
        if source == "conversation":
            raw = {
                "content": task_query(meta.get("user_turns", [])),
                "role": "user",
                "user_id": user_id,
            }
        else:
            raw = {
                "content": content[:800],
                "tool_name": "toolbench",
                "status": "completed",
                "result": content[:800],
                "user_id": user_id,
            }
        res = orch.process_input(raw, source, user_id=user_id)
        if res.quality_score is not None:
            scores.append(float(res.quality_score))
        if res.accepted:
            accepted += 1
            types[str(res.memory_type)] += 1
        else:
            # 记录全部拒绝原因，不再只取 reasons[0]，避免后续强原因（如隐私拦截）被吞掉
            for r in (res.reasons or ["?"]):
                reject[r[:80]] += 1
        if len(samples) < 5:
            samples.append({
                "accepted": res.accepted,
                "memory_type": str(getattr(res, "memory_type", None)),
                "quality_score": res.quality_score,
                "reasons": (res.reasons or [])[:2],
                "preview": (raw.get("content") or "")[:120],
            })
    n = len(items)
    return {
        "n": n,
        "accepted": accepted,
        "accept_rate": round(accepted / n, 4) if n else 0.0,
        "memory_type_distribution": dict(types),
        "reject_reasons": dict(reject),
        "quality_avg": round(statistics.fmean(scores), 4) if scores else None,
        "samples": samples,
    }


def retrieve(name: str, items: list, queries: list[str], user_id: str, backend: str) -> dict:
    if len(items) != len(queries):
        raise ValueError("Each sampled item must have exactly one query, possibly empty.")
    client = InMemoryMilvusClient()
    embedding = EmbeddingClient(backend)
    ids = {}
    t_in = []
    for i, (content, meta) in enumerate(items):
        if not content.strip():
            continue
        t0 = time.perf_counter()
        resp = create_memory(
            CreateMemoryRequest(
                entry=MemoryEntry(
                    content=content,
                    memory_type=MemoryTypeTag.TOOL_CALL if name == "ToolBench" else MemoryTypeTag.PROJECT_CONTEXT,
                    task_type="os_operation",
                    source=MemorySource.AGENT_LOG if name == "ToolBench" else MemorySource.CHAT,
                    user_id=user_id,
                    confidence=0.9,
                    metadata={"src": name, **{k: meta[k] for k in (
                        "id", "task", "dialogue_id", "line", "source_line", "content_truncated"
                    ) if k in meta}},
                )
            ),
            milvus=client,
            embedding=embedding,
        )
        t_in.append((time.perf_counter() - t0) * 1000)
        if resp.created:
            ids[i] = resp.memory_id
    hits, lats = [], []
    details = []
    evaluated = 0
    for i, (content, meta) in enumerate(items):
        q = queries[i].strip()
        if not q or i not in ids:
            continue
        evaluated += 1
        t0 = time.perf_counter()
        resp = query_memory(
            QueryMemoryRequest(user_id=user_id, query=q, top_k=5),
            milvus=client,
            embedding=embedding,
        )
        lat = (time.perf_counter() - t0) * 1000
        lats.append(lat)
        ranked = [str(it.get("memory_id") or it.get("id") or "") for it in resp.items]
        hit = int(str(ids[i]) in ranked)
        hits.append(hit)
        details.append(
            {
                "item_i": i,
                "query": q,
                "gold_memory_id": ids[i],
                "ranked_ids": ranked,
                "hit_at_5": hit,
                "latency_ms": round(lat, 3),
            }
        )
    return {
        "n_indexed": len(ids),
        "n_queries": evaluated,
        "n_input": len(items),
        "n_index_rejected": len(items) - len(ids),
        "n_query_skipped": len(items) - evaluated,
        "n_empty_queries": sum(not q.strip() for q in queries),
        "n_content_truncated": sum(bool(m.get("content_truncated")) for _, m in items),
        "n_user_turns_outside_window": sum(int(m.get("n_user_turns_outside_window", 0)) for _, m in items),
        "metric": "exact_memory_id_self_retrieval",
        "scope": "create_memory/query_memory component; not Orchestrator.recall or semantic QA accuracy",
        "hit_at_5": round(sum(hits) / evaluated, 4) if evaluated else 0.0,
        "ingest_avg_ms": round(statistics.fmean(t_in), 3) if t_in else None,
        "query_avg_ms": round(statistics.fmean(lats), 3) if lats else None,
        "query_p95_ms": round(_pct(lats, 95), 3) if lats else None,
        "latency_passed": bool(lats) and _pct(lats, 95) <= 500,
        "details": details,
    }


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
    get_shared_embedding_backend(backend)  # 预热 SDK

    print(f"[1/4] 加载 MultiWOZ n={args.n} ...", flush=True)
    mwoz_items, mwoz_idx = load_multiwoz(args.n, rng)
    print(f"      loaded={len(mwoz_items)} nonempty={sum(1 for c,_ in mwoz_items if c.strip())}", flush=True)
    print(f"[2/4] 加载 ToolBench n={args.n}（扫描 jsonl）...", flush=True)
    tb_items, tb_idx = load_toolbench(args.n, rng)
    if not mwoz_items or not tb_items:
        raise RuntimeError("MultiWOZ or ToolBench has no samples; check dataset files before evaluation.")
    print(f"      loaded={len(tb_items)} nonempty={sum(1 for c,_ in tb_items if c.strip())}", flush=True)

    print("[3/4] 分类 + 编排器 ...", flush=True)
    results = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "n": args.n,
            "embedding_backend": backend,
            "memory_backend": os.environ.get("OS_AGENT_MEMORY_BACKEND", "memory"),
            "commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True).strip(),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "query_strategy": "initial_task_and_recent_user_constraints_v1",
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "sampling_fn": "benchmarks.run_dataset_eval.sample_indices",
                "algorithm": "one Random(seed); load_multiwoz then load_toolbench (shared rng state)",
            },
        },
        "multiwoz": {
            "sampled_indices": mwoz_idx,
            "classify": classify_batch("MultiWOZ", "conversation", mwoz_items),
            "orchestrator": orchestrate("MultiWOZ", "conversation", mwoz_items, "dlg_mwoz"),
        },
        "toolbench": {
            "sampled_indices": tb_idx,
            "classify": classify_batch("ToolBench", "tool_result", tb_items),
            "orchestrator": orchestrate("ToolBench", "tool_result", tb_items, "dlg_tool"),
        },
    }

    print("[4/4] 自检索 Hit@5 ...", flush=True)
    results["multiwoz"]["retrieval"] = retrieve(
        "MultiWOZ",
        mwoz_items,
        [task_query(m.get("user_turns", [])) for _, m in mwoz_items],
        "dlg_mwoz_ret",
        backend,
    )
    results["multiwoz"]["last_user_control"] = retrieve(
        "MultiWOZ", mwoz_items,
        [str(m.get("last_user") or "") for _, m in mwoz_items],
        "dlg_mwoz_control", backend,
    )
    results["toolbench"]["retrieval"] = retrieve(
        "ToolBench",
        tb_items,
        [c.split("[TOOLS]")[0].replace("[TASK]", "").strip()[:180] for c, _ in tb_items],
        "dlg_tool_ret",
        backend,
    )

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"dialog_tool_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(json_path),
        "multiwoz": {
            "classify": results["multiwoz"]["classify"],
            "accept_rate": results["multiwoz"]["orchestrator"]["accept_rate"],
            "hit_at_5": results["multiwoz"]["retrieval"]["hit_at_5"],
            "query_p95_ms": results["multiwoz"]["retrieval"]["query_p95_ms"],
        },
        "toolbench": {
            "classify": results["toolbench"]["classify"],
            "accept_rate": results["toolbench"]["orchestrator"]["accept_rate"],
            "hit_at_5": results["toolbench"]["retrieval"]["hit_at_5"],
            "query_p95_ms": results["toolbench"]["retrieval"]["query_p95_ms"],
        },
    }, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
