#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump sampled git-dataset rows (input) using the same RNG sequence as eval scripts."""
from __future__ import annotations

import argparse
import json
import random
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from benchmarks.run_dataset_eval import (  # noqa: E402
    load_bpmn,
    load_dailydialog,
    load_dialogbench,
    load_multiwoz,
    load_personachat,
    load_toolbench,
)

GIT = ROOT / "git"
MAX_TEXT = 8000


def _clip(text: str) -> tuple[str, bool]:
    s = str(text or "")
    if len(s) <= MAX_TEXT:
        return s, False
    return s[:MAX_TEXT], True


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def dump_pairs(name: str, items: list, idx: list[int], out_dir: Path) -> dict:
    rows = []
    for i, (content, meta) in zip(idx, items):
        text, trunc = _clip(content)
        extra = {}
        if isinstance(meta, dict):
            extra = {
                k: meta[k]
                for k in (
                    "id",
                    "task",
                    "dialogue_id",
                    "conv_id",
                    "file",
                    "last_user",
                    "content_truncated",
                )
                if k in meta
            }
        rows.append(
            {
                "sampled_index": i,
                "input": text,
                "input_truncated": trunc,
                **extra,
            }
        )
    path = out_dir / f"{name}.jsonl"
    _write_jsonl(path, rows)
    return {
        "file": str(path),
        "n": len(rows),
        "sampled_indices": idx,
    }


def _latest(out: Path, prefix: str) -> Path | None:
    files = sorted(out.glob(f"{prefix}_*.json"))
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--nq", type=int, default=150)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()
    out = ROOT / args.out
    raw = out / "raw_io"
    raw.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "n": args.n,
        "nq": args.nq,
        "rng": {
            "implementation": "Python random.Random (CPython Mersenne Twister)",
            "note": "dialog dumps consume rng in the same order as the eval scripts",
        },
        "datasets": {},
    }

    pref_cache = GIT / "user-preference-564k" / f"sampled_seed{args.seed}_n{args.n}.jsonl"
    if not pref_cache.exists():
        pref_cache = GIT / "user-preference-564k" / "sampled_seed42_n100.jsonl"
    if pref_cache.exists():
        dest = raw / pref_cache.name
        shutil.copy2(pref_cache, dest)
        payload = json.loads(pref_cache.read_text(encoding="utf-8"))
        stats = payload.get("sample_stats") if isinstance(payload, dict) else {}
        manifest["datasets"]["user-preference-564k"] = {
            "file": str(dest),
            "sampled_indices": (stats or {}).get("sampled_indices"),
            "n_pos": (stats or {}).get("n_pos_sampled"),
            "n_neg": (stats or {}).get("n_neg_sampled"),
            "sampling_fn": "scripts.eval_preference_hf_dataset.sample_hf_rows",
            "algorithm": "independent Vitter reservoir on nonempty vs empty gold",
            "seed": (stats or {}).get("seed", args.seed),
        }

    closed = ROOT / "benchmarks" / "datasets" / "preference_extraction.jsonl"
    if closed.exists():
        dest = raw / "preference_extraction_closed.jsonl"
        shutil.copy2(closed, dest)
        manifest["datasets"]["preference_extraction_closed"] = {
            "file": str(dest),
            "n": sum(1 for line in dest.read_text(encoding="utf-8").splitlines() if line.strip()),
            "note": "full closed-key gold set; not sampled",
        }

    rng = random.Random(args.seed)
    mwoz_items, mwoz_idx = load_multiwoz(args.n, rng)
    tb_items, tb_idx = load_toolbench(args.n, rng)
    manifest["datasets"]["multiwoz"] = dump_pairs("multiwoz", mwoz_items, mwoz_idx, raw)
    manifest["datasets"]["toolbench"] = dump_pairs("toolbench", tb_items, tb_idx, raw)
    manifest["datasets"]["multiwoz"]["rng"] = "shared Random(seed) with toolbench; multiwoz first"
    manifest["datasets"]["toolbench"]["rng"] = "shared Random(seed) after multiwoz"

    rng = random.Random(args.seed)
    pc_items, pc_idx = load_personachat(args.n, rng)
    manifest["datasets"]["personachat"] = dump_pairs("personachat", pc_items, pc_idx, raw)
    try:
        bpmn_items, bpmn_idx = load_bpmn(args.n, rng)
        if bpmn_items:
            manifest["datasets"]["bpmn"] = dump_pairs("bpmn", bpmn_items, bpmn_idx, raw)
        else:
            manifest["datasets"]["bpmn"] = {"skipped": True, "reason": "empty or missing git/BPMN"}
    except Exception as exc:  # noqa: BLE001
        manifest["datasets"]["bpmn"] = {"skipped": True, "error": f"{type(exc).__name__}: {exc}"}

    rng = random.Random(args.seed)
    dd_items, dd_idx = load_dailydialog(args.n, rng)
    db_items, db_idx = load_dialogbench(args.n, rng)
    manifest["datasets"]["dailydialog"] = dump_pairs("dailydialog", dd_items, dd_idx, raw)
    if db_items:
        manifest["datasets"]["dialogbench"] = dump_pairs("dialogbench", db_items, db_idx, raw)
    else:
        manifest["datasets"]["dialogbench"] = {"skipped": True}

    try:
        from scripts.eval_retrieval_datasets import _sample_indices, load_msmarco_rows

        rows = load_msmarco_rows()
        q_idx = _sample_indices(random.Random(args.seed), len(rows), args.nq)
        marco_rows = []
        for i in q_idx:
            r = rows[i]
            q, qtrunc = _clip(str(r.get("query") or ""))
            passages = r.get("passages") or {}
            ptexts = passages.get("passage_text") or []
            psel = passages.get("is_selected") or []
            gold = []
            for t, s in zip(ptexts, psel):
                if s:
                    gt, gtrunc = _clip(str(t or ""))
                    gold.append({"text": gt, "truncated": gtrunc})
            marco_rows.append(
                {
                    "sampled_index": i,
                    "query_id": r.get("query_id") or r.get("queryid"),
                    "query": q,
                    "query_truncated": qtrunc,
                    "gold_passages": gold,
                }
            )
        path = raw / "msmarco_queries.jsonl"
        _write_jsonl(path, marco_rows)
        manifest["datasets"]["msmarco"] = {
            "file": str(path),
            "n": len(marco_rows),
            "sampled_indices": q_idx,
            "sampling_fn": "scripts.eval_retrieval_datasets._sample_indices",
            "pool_size": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        manifest["datasets"]["msmarco"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        from src.core.trec_qrels import load_trec_dl

        topics = load_trec_dl(GIT)
        trec_rows = []
        for qid in topics["judged_qids"]:
            q, qtrunc = _clip(topics["queries"].get(qid) or "")
            rels = topics["qrels"].get(qid) or {}
            trec_rows.append(
                {
                    "qid": qid,
                    "query": q,
                    "query_truncated": qtrunc,
                    "n_positive_pids": sum(1 for rel in rels.values() if rel >= 1),
                }
            )
        path = raw / "trec_judged_queries.jsonl"
        _write_jsonl(path, trec_rows)
        manifest["datasets"]["trec"] = {
            "file": str(path),
            "n": len(trec_rows),
            "eval_qids": topics["judged_qids"],
            "note": "trec_nq=0 → all judged queries, not rng-sampled",
        }
    except Exception as exc:  # noqa: BLE001
        manifest["datasets"]["trec"] = {"error": f"{type(exc).__name__}: {exc}"}

    lme = GIT / "longmemeval_oracle.json"
    if lme.exists():
        data = json.loads(lme.read_text(encoding="utf-8"))
        updates = [x for x in data if x.get("question_type") == "knowledge-update"]
        controls_pool = [
            x
            for x in data
            if x.get("question_type") in {"single-session-user", "single-session-assistant"}
        ]
        n_update = min(len(updates), args.n)
        n_control = max(args.n - n_update, 0)
        rng = random.Random(args.seed)
        controls = rng.sample(controls_pool, min(n_control, len(controls_pool))) if n_control else []
        rows = []
        for item in updates[:n_update] + controls:
            q, qtrunc = _clip(str(item.get("question") or ""))
            a, atrunc = _clip(str(item.get("answer") or ""))
            rows.append(
                {
                    "question_id": item.get("question_id"),
                    "question_type": item.get("question_type"),
                    "question": q,
                    "question_truncated": qtrunc,
                    "answer": a,
                    "answer_truncated": atrunc,
                }
            )
        path = raw / "longmemeval_sampled.jsonl"
        _write_jsonl(path, rows)
        manifest["datasets"]["longmemeval"] = {
            "file": str(path),
            "n": len(rows),
            "n_update": n_update,
            "n_control": len(controls),
            "update_ids": [x.get("question_id") for x in updates[:n_update]],
            "control_ids": [x.get("question_id") for x in controls],
            "sampling_fn": "file-order first n_update; rng.sample controls",
        }

    eval_out = raw / "eval_json_copied"
    eval_out.mkdir(parents=True, exist_ok=True)
    for prefix in (
        "preference",
        "conflict_longmemeval",
        "dialog_tool",
        "behavior_knowledge",
        "dialog_quant",
        "retrieval",
    ):
        src = _latest(out, prefix)
        if src:
            shutil.copy2(src, eval_out / src.name)
            manifest.setdefault("eval_json", {})[prefix] = str(src)

    (raw / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"raw_io": str(raw), "n_datasets": len(manifest["datasets"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
