#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识冲突处理正确率抽测：LongMemEval oracle（git/longmemeval_oracle.json）。

主集是 question_type=knowledge-update（旧事实 → 新事实，金标准答案是更新后的事实）。
补 22 条同文再写入作为 no_conflict 对照，凑满 n=100。

口径：
  1) 解析器：同一 knowledge_key 的新旧事实应 REPLACE；同文应 no_conflict
  2) 端到端：按 key 写入后，用问题做 semantic 召回，top-5 应含更新后答案、且默认不回旧条

    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_conflict_longmemeval.py --seed 42 --n 100
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

from src.common.models import MemoryItem, MemoryLayer, MemoryType, RetrievalQuery  # noqa: E402
from src.core.conflict_resolver import ConflictResolver  # noqa: E402
from src.core.factory import create_orchestrator  # noqa: E402
from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402
from src.knowledge.memory_service.memory_schema import (  # noqa: E402
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    TaskType,
)

GIT = ROOT / "git"
DEFAULT_PATH = GIT / "longmemeval_oracle.json"
CONFLICT_TARGET = 0.88

_STAGING_TMPDIR = None


def _ensure_isolated_staging() -> None:
    global _STAGING_TMPDIR
    if _STAGING_TMPDIR is not None:
        return
    _STAGING_TMPDIR = tempfile.mkdtemp(prefix="mokoa_lme_staging_")
    atexit.register(shutil.rmtree, _STAGING_TMPDIR, ignore_errors=True)
    from src.common.config import load_default_config

    cfg = load_default_config()
    cfg.setdefault("storage", {})["staging_sqlite"] = os.path.join(
        _STAGING_TMPDIR, "staging.db"
    )


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (k - lo)) + s[hi] * (k - lo)


def parse_lme_date(raw: str | None) -> datetime:
    text = str(raw or "").strip()
    match = re.search(r"(\d{4}/\d{2}/\d{2}).*?(\d{2}:\d{2})", text)
    if match:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y/%m/%d %H:%M")
    return datetime.utcnow()


def answer_aliases(answer: str) -> list[str]:
    text = str(answer or "").strip()
    aliases = [text]
    paren = re.search(r"\((?:or\s+)?([^)]+)\)", text, re.I)
    if paren:
        aliases.append(paren.group(1).strip())
        aliases.append(re.sub(r"\s*\([^)]*\)", "", text).strip())
    extra: list[str] = []
    for alias in aliases:
        extra.extend(
            [p.strip() for p in re.split(r"\s+or\s+", alias, flags=re.I) if p.strip()]
        )
    seen: set[str] = set()
    out: list[str] = []
    for alias in aliases + extra:
        key = alias.lower()
        if len(alias) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return out


def contains_answer(text: str, answer: str) -> bool:
    blob = (text or "").lower()
    for alias in answer_aliases(answer):
        if alias.lower() in blob:
            return True
    return False


def extract_answer_turns(item: dict) -> list[dict]:
    turns: list[dict] = []
    sessions = item.get("haystack_sessions") or []
    dates = item.get("haystack_dates") or []
    sids = item.get("haystack_session_ids") or []
    for i, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        date = dates[i] if i < len(dates) else ""
        sid = sids[i] if i < len(sids) else ""
        for turn in session:
            if not isinstance(turn, dict):
                continue
            if turn.get("role") != "user" or not turn.get("has_answer"):
                continue
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            turns.append(
                {
                    "session_index": i,
                    "session_id": sid,
                    "date": date,
                    "content": content,
                }
            )
    return turns


def _memory_item(content: str, key: str, when: datetime) -> MemoryItem:
    return MemoryItem(
        id=f"{key}-{int(when.timestamp())}",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.LONG,
        content=content,
        user_id="lme",
        metadata={"knowledge_key": key, "source_type": "conversation"},
        updated_at=when,
    )


def resolver_case(old: dict, new: dict | None, key: str, expected: str) -> dict:
    resolver = ConflictResolver()
    old_item = _memory_item(old["content"], key, parse_lme_date(old.get("date")))
    if expected == "no_conflict":
        new_item = _memory_item(
            (new or old)["content"],
            key,
            parse_lme_date((new or old).get("date")) if new else datetime.utcnow(),
        )
        # 对照：同文再写
        if new is None:
            new_item = _memory_item(old["content"], key, datetime.utcnow())
        detected = resolver.detect(old_item, new_item)
        ok = not detected
        got = "no_conflict" if not detected else "false_conflict"
        return {"expected": expected, "got": got, "correct": ok, "detected": detected}

    new_item = _memory_item(new["content"], key, parse_lme_date(new.get("date")))
    detected = resolver.detect(old_item, new_item)
    if not detected:
        return {
            "expected": expected,
            "got": "not_detected",
            "correct": False,
            "detected": False,
        }
    decision = resolver.resolve_knowledge(old_item, new_item)
    got = decision.action.value
    return {
        "expected": expected,
        "got": got,
        "correct": got == expected,
        "detected": True,
        "reason": decision.reason,
    }


def store_turn(orch, user_id: str, key: str, turn: dict) -> dict:
    entry = MemoryEntry(
        content=turn["content"],
        memory_type=MemoryTypeTag.PROJECT_CONTEXT,
        task_type=TaskType.GLOBAL,
        source=MemorySource.CHAT,
        user_id=user_id,
        confidence=0.9,
        metadata={
            "knowledge_key": key,
            "source_type": "conversation",
            "session_id": turn.get("session_id") or "",
            "haystack_date": turn.get("date") or "",
        },
    )
    t0 = time.perf_counter()
    result = orch.store_memory_entry(entry)
    return {
        "created": bool(result.created),
        "reason": result.reason,
        "memory_id": result.memory_id,
        "superseded_memory_id": getattr(result, "superseded_memory_id", None),
        "store_ms": round((time.perf_counter() - t0) * 1000, 3),
    }


def _same_turn(left: str, right: str) -> bool:
    a = (left or "").strip()
    b = (right or "").strip()
    if not a or not b:
        return False
    return a == b or a[:80] in b or b[:80] in a


def recall_case(
    orch,
    user_id: str,
    question: str,
    gold_answer: str,
    old_content: str | None,
    new_content: str | None,
) -> dict:
    t0 = time.perf_counter()
    res = orch.recall(
        RetrievalQuery(
            query=question,
            user_id=user_id,
            top_k=5,
            memory_type=MemoryType.KNOWLEDGE,
            retrieval_mode="semantic",
        )
    )
    latency = (time.perf_counter() - t0) * 1000
    contents = [it.content for it in res.items]
    gold_ranks = [i for i, c in enumerate(contents, start=1) if contains_answer(c, gold_answer)]
    old_ranks = [
        i for i, c in enumerate(contents, start=1) if old_content and _same_turn(c, old_content)
    ]
    new_ranks = [
        i for i, c in enumerate(contents, start=1) if new_content and _same_turn(c, new_content)
    ]
    gold_hit = bool(gold_ranks)
    old_present = bool(old_ranks)
    if not contents:
        latest_wins = False
    elif old_content and new_content and not _same_turn(old_content, new_content):
        latest_wins = bool(new_ranks) and new_ranks[0] == 1 and not old_present
    else:
        latest_wins = True
    return {
        "latency_ms": round(res.latency_ms or latency, 3),
        "strategy": res.strategy,
        "n_items": len(contents),
        "gold_in_top5": gold_hit,
        "gold_rank": gold_ranks[0] if gold_ranks else None,
        "old_present": old_present,
        "new_rank": new_ranks[0] if new_ranks else None,
        "old_outranks_new": bool(old_ranks) and (not new_ranks or old_ranks[0] < new_ranks[0]),
        "latest_wins": latest_wins,
        "e2e_ok": latest_wins,
        "preview": [c[:160].replace("\n", " ") for c in contents[:3]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--dataset", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--out", type=str, default="docs/evidence/dataset-eval-20260914")
    ap.add_argument("--skip-e2e", action="store_true")
    args = ap.parse_args()

    if not args.dataset.exists():
        raise FileNotFoundError(f"missing {args.dataset}")

    print(f"[1/4] 加载 {args.dataset} ...", flush=True)
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("longmemeval_oracle.json 应为 list")
    type_counts = Counter(str(x.get("question_type") or "") for x in data)
    updates = [x for x in data if x.get("question_type") == "knowledge-update"]
    controls_pool = [
        x
        for x in data
        if x.get("question_type") in {"single-session-user", "single-session-assistant"}
    ]
    rng = random.Random(args.seed)
    n_update = min(len(updates), args.n)
    n_control = max(args.n - n_update, 0)
    if n_control:
        controls = rng.sample(controls_pool, min(n_control, len(controls_pool)))
    else:
        controls = []
    print(
        f"      total={len(data)} types={dict(type_counts)} "
        f"update={n_update} control={len(controls)}",
        flush=True,
    )

    cases: list[dict] = []
    skipped_update = 0
    for item in updates[:n_update]:
        turns = extract_answer_turns(item)
        if len(turns) < 2:
            skipped_update += 1
            continue
        cases.append(
            {
                "id": item.get("question_id"),
                "category": "knowledge-update",
                "expected": "replace",
                "question": item.get("question") or "",
                "answer": item.get("answer") or "",
                "old": turns[0],
                "new": turns[-1],
            }
        )
    for item in controls:
        turns = extract_answer_turns(item)
        if not turns:
            # 无 has_answer 时退回第一条 user 句
            sessions = item.get("haystack_sessions") or []
            content = ""
            if sessions and isinstance(sessions[0], list):
                for turn in sessions[0]:
                    if isinstance(turn, dict) and turn.get("role") == "user":
                        content = str(turn.get("content") or "")
                        break
            if not content:
                continue
            turns = [{"content": content, "date": (item.get("haystack_dates") or [""])[0], "session_id": ""}]
        cases.append(
            {
                "id": item.get("question_id"),
                "category": "no_conflict_repeat",
                "expected": "no_conflict",
                "question": item.get("question") or "",
                "answer": item.get("answer") or "",
                "old": turns[0],
                "new": None,
            }
        )

    print(f"[2/4] 冲突解析器 n={len(cases)} skipped_update={skipped_update} ...", flush=True)
    resolver_rows = []
    resolver_correct = 0
    for case in cases:
        key = str(case["id"])
        row = resolver_case(case["old"], case.get("new"), key, case["expected"])
        row.update({"id": case["id"], "category": case["category"]})
        resolver_rows.append(row)
        resolver_correct += int(row["correct"])
    resolver_acc = resolver_correct / len(cases) if cases else 0.0

    e2e = {"skipped": True, "reason": "--skip-e2e"}
    if not args.skip_e2e:
        print("[3/4] 端到端写入+召回 ...", flush=True)
        backend = os.environ.get("OS_AGENT_EMBEDDING_BACKEND", "local")
        get_shared_embedding_backend(backend)
        _ensure_isolated_staging()
        orch = create_orchestrator()
        e2e_rows = []
        e2e_ok = 0
        lats: list[float] = []
        for i, case in enumerate(cases, start=1):
            user_id = f"lme_{case['id']}"
            key = str(case["id"])
            first = store_turn(orch, user_id, key, case["old"])
            if case["expected"] == "replace":
                second = store_turn(orch, user_id, key, case["new"])
                old_content = case["old"]["content"]
            else:
                second = store_turn(orch, user_id, key, case["old"])
                old_content = None
            rec = recall_case(
                orch,
                user_id,
                case["question"],
                case["answer"],
                old_content,
                case["new"]["content"] if case["expected"] == "replace" else case["old"]["content"],
            )
            ok = rec["e2e_ok"]
            e2e_ok += int(ok)
            lats.append(rec["latency_ms"])
            e2e_rows.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "expected": case["expected"],
                    "store_first": first,
                    "store_second": second,
                    "recall": rec,
                    "e2e_ok": ok,
                    "question": case["question"],
                    "answer": str(case["answer"]),
                    "old_content": case["old"]["content"],
                    "new_content": (
                        case["new"]["content"] if case.get("new") else None
                    ),
                }
            )
            if i % 20 == 0:
                print(f"      e2e {i}/{len(cases)}", flush=True)
        e2e_acc = e2e_ok / len(cases) if cases else 0.0
        gold_qa = sum(int(r["recall"]["gold_in_top5"]) for r in e2e_rows)
        e2e = {
            "skipped": False,
            "samples": len(cases),
            "correct": e2e_ok,
            "correctness": round(e2e_acc, 4),
            "pass_88": e2e_acc >= CONFLICT_TARGET,
            "gold_qa_substring_hit": gold_qa,
            "gold_qa_substring_rate": round(gold_qa / len(cases), 4) if cases else 0.0,
            "query_p95_ms": round(_pct(lats, 95), 3) if lats else None,
            "query_avg_ms": round(sum(lats) / len(lats), 3) if lats else None,
            "latency_passed": bool(lats) and _pct(lats, 95) <= 500,
            "details": e2e_rows,
        }

    print("[4/4] 写报告 ...", flush=True)
    results = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "n_requested": args.n,
            "n_eval": len(cases),
            "n_update": sum(c["category"] == "knowledge-update" for c in cases),
            "n_control": sum(c["category"] != "knowledge-update" for c in cases),
            "skipped_update_lt2_turns": skipped_update,
            "dataset": str(args.dataset),
            "type_counts": dict(type_counts),
            "metric_scope": (
                "knowledge-update pairs share knowledge_key; resolver should REPLACE. "
                "E2E latest_wins: after ingest in date order, semantic recall returns the "
                "new turn and not the superseded old turn. gold_qa_substring is diagnostic "
                "only (gold '4' vs memory 'four'). Not LongMemEval official LLM-judge."
            ),
            "embedding_backend": os.environ.get("OS_AGENT_EMBEDDING_BACKEND", "local"),
            "memory_backend": os.environ.get("OS_AGENT_MEMORY_BACKEND", "memory"),
            "commit": subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True
            ).strip(),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "sampling_fn": (
                    "knowledge-update: file order first n_update (no rng); "
                    "controls: rng.sample(single-session-user|assistant, n_control)"
                ),
            },
            "sampled_ids": {
                "knowledge_update": [c["id"] for c in cases if c["category"] == "knowledge-update"],
                "controls": [c["id"] for c in cases if c["category"] != "knowledge-update"],
            },
        },
        "resolver": {
            "samples": len(cases),
            "correct": resolver_correct,
            "correctness": round(resolver_acc, 4),
            "pass_88": resolver_acc >= CONFLICT_TARGET,
            "by_category": {
                cat: {
                    "n": sum(1 for r in resolver_rows if r["category"] == cat),
                    "correct": sum(1 for r in resolver_rows if r["category"] == cat and r["correct"]),
                }
                for cat in sorted({r["category"] for r in resolver_rows})
            },
            "details": resolver_rows,
        },
        "e2e": e2e,
        "target": CONFLICT_TARGET,
    }

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"conflict_longmemeval_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(json_path),
        "n_eval": len(cases),
        "resolver": results["resolver"]["correctness"],
        "resolver_pass_88": results["resolver"]["pass_88"],
        "e2e": None
        if e2e.get("skipped")
        else {
            "correctness": e2e["correctness"],
            "pass_88": e2e["pass_88"],
            "gold_qa_substring_rate": e2e.get("gold_qa_substring_rate"),
            "query_p95_ms": e2e["query_p95_ms"],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
