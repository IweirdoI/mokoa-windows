"""Evaluate preference extraction on HuggingFace dataset blackhao0426/user-preference-564k.

数据集每行: {input: 用户查询文本, output: {"preferences":[{condition,action,confidence}]}, source}
金标准是 LLM 从查询中推断出的开放文本偏好；本系统偏好抽取输出为闭式分类
(style/habit/tool_selection/... = 归一化短值)。两者范式不同，故采用双层指标:

  1) 存在级召回/精确率: 有金标准的样本里，本系统是否至少抽到 1 条偏好；以及
     本系统抽到偏好的样本里，是否确实该抽(负样本不被误抽)。
  2) 动作级覆盖(F1): 金标准 action 与抽取 content 做 token 重叠(贪心 1:1 匹配)，
     衡量具体动作是否被覆盖。

用法:
  python scripts/pref_eval_hf_dataset.py --limit 200
  python scripts/pref_eval_hf_dataset.py --mode fusion --limit 500 --out docs/evidence/pref_eval_hf_dataset.json

默认走 HF 镜像站 (HF_ENDPOINT=https://hf-mirror.com)。LLM 模式需 SILICONFLOW_API_KEY，
缺失时自动回退 rule 并提示。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 使用镜像站加速访问 HuggingFace
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from datasets import load_dataset
from src.common.models import DataSource, RawRecord
from src.preference.store import PreferenceMemoryStore

DATASET_ID = "blackhao0426/user-preference-564k"


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    toks = re.findall(r"[a-z0-9_]+", text)
    toks += re.findall(r"[一-鿿]", text)
    return {t for t in toks if len(t) > 1 or re.fullmatch(r"[a-z0-9]{2,}", t)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _gold_prefs(output: str) -> list[dict]:
    try:
        data = json.loads(output)
    except Exception:
        return []
    prefs = data.get("preferences") if isinstance(data, dict) else None
    if not isinstance(prefs, list):
        return []
    return [p for p in prefs if isinstance(p, dict)]


def _match(gold_items: list[dict], ext_texts: list[str], thr: float) -> tuple[int, int, int]:
    """贪心 1:1 匹配 (gold vs extracted)，返回 (tp, fp, fn) 动作级计数。"""
    gold_tokens = []
    for g in gold_items:
        action = str(g.get("action") or "")
        cond = str(g.get("condition") or "")
        gold_tokens.append(_tokens(f"{cond} {action}"))
    ext_tokens = [_tokens(t) for t in ext_texts]

    used_ext = [False] * len(ext_tokens)
    tp = 0
    for gset in gold_tokens:
        best, best_j = -1, thr
        for i, eset in enumerate(ext_tokens):
            if used_ext[i]:
                continue
            j = _jaccard(gset, eset)
            if j > best_j:
                best, best_j = i, j
        if best >= 0:
            used_ext[best] = True
            tp += 1
    fn = len(gold_items) - tp
    fp = sum(1 for u in used_ext if not u)
    return tp, fp, fn


def evaluate(mode: str, limit: int, sample_examples: int) -> dict:
    use_llm = mode in ("llm", "fusion")
    allow_rules = mode in ("rule", "fusion")
    store = PreferenceMemoryStore(use_llm=use_llm, allow_rules=allow_rules)

    rows_with_gold = 0
    rows_extracted = 0
    gold_and_extracted = 0
    gold_but_empty_ext = 0
    empty_gold_but_extracted = 0
    tp = fp = fn = 0
    per_source = defaultdict(lambda: {"rows": 0, "rows_with_gold": 0, "rows_extracted": 0})

    examples: list[dict] = []
    processed = 0

    ds = load_dataset(DATASET_ID, split="train", streaming=True)
    for idx, row in enumerate(ds):
        if limit and processed >= limit:
            break
        processed += 1
        inp = str(row.get("input") or "")
        gold = _gold_prefs(row.get("output") or "")
        src = str(row.get("source") or "unknown")
        per_source[src]["rows"] += 1

        record = RawRecord(
            id=f"hf_{idx}",
            source=DataSource.CONVERSATION,
            user_id="hf_bench",
            scene_id=None,
            payload={"content": inp},
        )
        entries = store.extract([record])
        ext_texts = [
            (e.metadata.get("preference_key", "") + "="
             + str(e.metadata.get("preference_value", "")) + " " + e.content)
            for e in entries
        ]

        has_gold = len(gold) > 0
        has_ext = len(entries) > 0
        if has_gold:
            rows_with_gold += 1
            per_source[src]["rows_with_gold"] += 1
        if has_ext:
            rows_extracted += 1
            per_source[src]["rows_extracted"] += 1
        if has_gold and has_ext:
            gold_and_extracted += 1
        if has_gold and not has_ext:
            gold_but_empty_ext += 1
        if (not has_gold) and has_ext:
            empty_gold_but_extracted += 1

        t, f, n = _match(gold, ext_texts, thr=0.15)
        tp += t
        fp += f
        fn += n

        if len(examples) < sample_examples:
            examples.append({
                "idx": idx,
                "source": src,
                "input": inp[:200],
                "gold": [f"{g.get('condition')}: {g.get('action')}" for g in gold][:5],
                "extracted": [e.content[:160] for e in entries][:5],
            })

    presence_recall = gold_and_extracted / rows_with_gold if rows_with_gold else 0.0
    presence_prec = gold_and_extracted / rows_extracted if rows_extracted else 0.0
    presence_f1 = (
        2 * presence_prec * presence_recall / (presence_prec + presence_recall)
        if (presence_prec + presence_recall)
        else 0.0
    )
    action_prec = tp / (tp + fp) if (tp + fp) else 0.0
    action_rec = tp / (tp + fn) if (tp + fn) else 0.0
    action_f1 = (
        2 * action_prec * action_rec / (action_prec + action_rec)
        if (action_prec + action_rec)
        else 0.0
    )

    return {
        "dataset": DATASET_ID,
        "mode": mode,
        "hf_endpoint": os.environ.get("HF_ENDPOINT"),
        "llm_available": use_llm,
        "sampled_rows": processed,
        "rows_with_gold": rows_with_gold,
        "rows_extracted": rows_extracted,
        "gold_and_extracted": gold_and_extracted,
        "gold_but_empty_ext": gold_but_empty_ext,
        "empty_gold_but_extracted": empty_gold_but_extracted,
        "presence_recall": round(presence_recall, 4),
        "presence_precision": round(presence_prec, 4),
        "presence_f1": round(presence_f1, 4),
        "action_tp": tp,
        "action_fp": fp,
        "action_fn": fn,
        "action_precision": round(action_prec, 4),
        "action_recall": round(action_rec, 4),
        "action_f1": round(action_f1, 4),
        "per_source": {k: dict(v) for k, v in per_source.items()},
        "examples": examples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rule", "llm", "fusion"], default="rule")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--examples", type=int, default=20)
    parser.add_argument("--thr", type=float, default=0.15, help="动作级 token 重叠阈值")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    report = evaluate(args.mode, args.limit, args.examples)
    summary = {k: v for k, v in report.items() if k != "examples" and k != "per_source"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
