#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户行为（PersonaChat）与知识文档（BPMN）专项评测.

在麒麟宿主上：
    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_behavior_knowledge_datasets.py --seed 42 --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import random
import atexit
import shutil
import subprocess
import sys
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

from benchmarks.run_dataset_eval import load_bpmn, load_personachat  # noqa: E402
from scripts.eval_dialog_tool_datasets import classify_batch, retrieve  # noqa: E402
from src.core.factory import create_orchestrator  # noqa: E402
from src.core.persona_query import persona_query  # noqa: E402
from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402
from src.ingestion.bpmn_text import bpmn_task_query  # noqa: E402


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


def orchestrate(items: list, user_id: str, source: str, raw_builder) -> dict:
    _ensure_isolated_staging()
    orch = create_orchestrator()
    accepted = 0
    types: Counter = Counter()
    reject: Counter = Counter()
    scores: list[float] = []
    pref_extracted = 0
    samples: list[dict] = []
    for content, meta in items:
        raw = raw_builder(content, meta)
        raw.setdefault("user_id", user_id)
        res = orch.process_input(raw, source, user_id=user_id)
        if res.quality_score is not None:
            scores.append(float(res.quality_score))
        pref_extracted += int(res.preference_extracted or 0)
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
                "preference_extracted": res.preference_extracted,
                "preview": (raw.get("content") or "")[:120],
            })
    n = len(items)
    return {
        "n": n,
        "accepted": accepted,
        "accept_rate": round(accepted / n, 4) if n else 0.0,
        "memory_type_distribution": dict(types),
        "reject_reasons": dict(reject),
        "quality_avg": round(sum(scores) / len(scores), 4) if scores else None,
        "preference_extracted_total": pref_extracted,
        "samples": samples,
    }


def _persona_concat_raw(content: str, _meta: dict) -> dict:
    return {"content": content[:1500], "role": "user"}


def _persona_facts_raw(_content: str, meta: dict) -> dict:
    text = str(meta.get("persona_text") or "")[:800]
    return {"content": text, "role": "user"}


def _behavior_once_raw(_content: str, meta: dict) -> dict:
    facts = meta.get("persona_facts") or []
    fact = str(facts[0] if facts else meta.get("persona_text") or "")[:240]
    return {
        "action": "stated_persona",
        "target": fact[:80],
        "content": fact,
        "occurrence_count": 1,
    }


def _behavior_repeat_raw(_content: str, meta: dict) -> dict:
    raw = _behavior_once_raw("", meta)
    raw["occurrence_count"] = 5
    return raw


def _bpmn_labels_raw(content: str, meta: dict) -> dict:
    return {
        "filename": meta.get("file") or "process.bpmn",
        "content": content[:1500],
    }


def _bpmn_xml_raw(_content: str, meta: dict) -> dict:
    return {
        "filename": meta.get("file") or "process.bpmn",
        "content": str(meta.get("raw_prefix") or "")[:1500],
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
    get_shared_embedding_backend(backend)

    print(f"[1/4] 加载 PersonaChat n={args.n} ...", flush=True)
    pc_items, pc_idx = load_personachat(args.n, rng)
    print(
        f"      loaded={len(pc_items)} nonempty={sum(1 for c, _ in pc_items if c.strip())} "
        f"persona_text={sum(1 for _, m in pc_items if str(m.get('persona_text') or '').strip())}",
        flush=True,
    )
    print(f"[2/4] 加载 BPMN n={args.n} ...", flush=True)
    bpmn_items, bpmn_idx = load_bpmn(args.n, rng)
    print(
        f"      loaded={len(bpmn_items)} labels={sum(1 for c, _ in bpmn_items if c.strip())} "
        f"parse_ok={sum(1 for _, m in bpmn_items if m.get('parse_ok'))}",
        flush=True,
    )
    if not pc_items:
        raise RuntimeError("PersonaChat has no samples; check git/PersonaChat.")
    skip_bpmn = not bpmn_items
    if skip_bpmn:
        print("      BPMN 无可用样本，跳过该库", flush=True)

    print("[3/4] 分类 + 编排器 ...", flush=True)
    persona_text_items = [(str(m.get("persona_text") or ""), m) for _, m in pc_items]
    bpmn_xml_items = [(str(m.get("raw_prefix") or ""), m) for _, m in bpmn_items]
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
            "query_strategy": (
                "persona_facts_v1"
                if skip_bpmn
                else "persona_facts_v1 + bpmn_task_labels_v1"
            ),
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "sampling_fn": "benchmarks.run_dataset_eval.sample_indices",
                "algorithm": "one Random(seed); load_personachat then load_bpmn (shared rng)",
            },
            "skipped": {"bpmn": "git/BPMN missing or empty"} if skip_bpmn else {},
        },
        "personachat": {
            "sampled_indices": pc_idx,
            "classify_concat_conversation": classify_batch(
                "PersonaChat", "conversation", pc_items
            ),
            "classify_persona_conversation": classify_batch(
                "PersonaChatPersona", "conversation", persona_text_items
            ),
            "classify_persona_behavior": classify_batch(
                "PersonaChatBehavior", "behavior", persona_text_items
            ),
            "orchestrator_concat_conversation": orchestrate(
                pc_items, "beh_pc_concat", "conversation", _persona_concat_raw
            ),
            "orchestrator_persona_conversation": orchestrate(
                pc_items, "beh_pc_facts", "conversation", _persona_facts_raw
            ),
            "orchestrator_behavior_once": orchestrate(
                pc_items, "beh_pc_once", "user_behavior", _behavior_once_raw
            ),
            "orchestrator_behavior_repeat5": orchestrate(
                pc_items, "beh_pc_rep5", "user_behavior", _behavior_repeat_raw
            ),
        },
        "bpmn": (
            {"skipped": True, "reason": "git/BPMN missing or empty", "sampled_indices": []}
            if skip_bpmn
            else {
                "sampled_indices": bpmn_idx,
                "classify_labels_document": classify_batch("BPMN", "document", bpmn_items),
                "classify_xml_document": classify_batch("BPMNxml", "document", bpmn_xml_items),
                "classify_labels_as_conversation": classify_batch(
                    "BPMNconv", "conversation", bpmn_items
                ),
                "orchestrator_labels_document": orchestrate(
                    bpmn_items, "kn_bpmn_lab", "document", _bpmn_labels_raw
                ),
                "orchestrator_xml_document": orchestrate(
                    bpmn_items, "kn_bpmn_xml", "document", _bpmn_xml_raw
                ),
            }
        ),
    }

    print("[4/4] 自检索 Hit@5 ...", flush=True)
    pc_fact_q = [persona_query(m.get("persona_facts") or []) for _, m in pc_items]
    pc_last_q = [str(m.get("last_user") or "") for _, m in pc_items]

    results["personachat"]["retrieval_persona_query_on_facts"] = retrieve(
        "PersonaChat", persona_text_items, pc_fact_q, "beh_pc_ret_facts", backend
    )
    results["personachat"]["retrieval_persona_query_on_concat"] = retrieve(
        "PersonaChat", pc_items, pc_fact_q, "beh_pc_ret_concat", backend
    )
    results["personachat"]["last_user_control_on_concat"] = retrieve(
        "PersonaChat", pc_items, pc_last_q, "beh_pc_ret_last", backend
    )
    if not skip_bpmn:
        bpmn_task_q = [str(m.get("task_query") or bpmn_task_query(m)) for _, m in bpmn_items]
        xml_prefix_q = [str(m.get("raw_prefix") or "")[:80] for _, m in bpmn_items]
        results["bpmn"]["retrieval_task_query_on_labels"] = retrieve(
            "BPMN", bpmn_items, bpmn_task_q, "kn_bpmn_ret_lab", backend
        )
        results["bpmn"]["retrieval_task_query_on_xml"] = retrieve(
            "BPMN", bpmn_xml_items, bpmn_task_q, "kn_bpmn_ret_xml", backend
        )
        results["bpmn"]["xml_prefix_control_on_labels"] = retrieve(
            "BPMN", bpmn_items, xml_prefix_q, "kn_bpmn_ret_prefix", backend
        )

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"behavior_knowledge_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(json_path),
        "personachat": {
            "classify_concat": results["personachat"]["classify_concat_conversation"],
            "accept_concat": results["personachat"]["orchestrator_concat_conversation"]["accept_rate"],
            "accept_persona": results["personachat"]["orchestrator_persona_conversation"]["accept_rate"],
            "accept_behavior_once": results["personachat"]["orchestrator_behavior_once"]["accept_rate"],
            "accept_behavior_repeat5": results["personachat"]["orchestrator_behavior_repeat5"]["accept_rate"],
            "hit_facts": results["personachat"]["retrieval_persona_query_on_facts"]["hit_at_5"],
            "hit_concat_persona_q": results["personachat"]["retrieval_persona_query_on_concat"]["hit_at_5"],
            "hit_concat_last_user": results["personachat"]["last_user_control_on_concat"]["hit_at_5"],
        },
        "bpmn": (
            {"skipped": True, "reason": "git/BPMN missing or empty"}
            if skip_bpmn
            else {
                "classify_labels": results["bpmn"]["classify_labels_document"],
                "accept_labels": results["bpmn"]["orchestrator_labels_document"]["accept_rate"],
                "accept_xml": results["bpmn"]["orchestrator_xml_document"]["accept_rate"],
                "hit_labels": results["bpmn"]["retrieval_task_query_on_labels"]["hit_at_5"],
                "hit_xml": results["bpmn"]["retrieval_task_query_on_xml"]["hit_at_5"],
                "hit_xml_prefix_control": results["bpmn"]["xml_prefix_control_on_labels"]["hit_at_5"],
            }
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
