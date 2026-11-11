"""Evaluate preference extraction on preference_extraction.jsonl.

Reports precision / recall / F1, false positives, false negatives.
Modes: rule | llm | fusion (default config flags).

Usage:
  python scripts/pref_eval_dataset.py --mode rule
  python scripts/pref_eval_dataset.py --mode fusion
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.models import DataSource, RawRecord
from src.preference.llm_client import SiliconFlowClient
from src.preference.store import PreferenceMemoryStore


def _is_retryable(exc) -> bool:
    """识别可重试的瞬时错误（限流 / 连接 / 超时 / 5xx）。"""
    try:
        from openai import (
            APIConnectionError,
            APIError,
            APITimeoutError,
            RateLimitError,
        )
    except Exception:
        return True
    return isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError, APIError))


def _with_retry(fn, what: str, max_retries: int = 3):
    delay = 1.0
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 仅对可重试错误退避
            last = exc
            if not _is_retryable(exc) or attempt >= max_retries - 1:
                raise
            print(
                f"      [retry {attempt + 1}/{max_retries}] {what}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise last


def _got_pairs(entries) -> set[str]:
    pairs: set[str] = set()
    for entry in entries:
        key = entry.metadata.get("preference_key")
        val = entry.metadata.get("preference_value")
        if key is not None and val is not None:
            pairs.add(f"{key}={val}")
    return pairs


def _expected_pairs(row: dict) -> set[str]:
    return {str(x) for x in row.get("expected_preferences") or []}


def evaluate(mode: str, dataset: Path, *, restrict_llm_keys: bool = False, workers: int = 8) -> dict:
    use_llm = mode in ("llm", "fusion")
    allow_rules = mode in ("rule", "fusion")

    # 单一共享 client（OpenAI/httpx 连接池对并发请求安全）；LLM 调用计数需加锁。
    # 显式走远程 SiliconFlow（与开放域 build_llm_client("remote") 一致）：不能依赖无参
    # SiliconFlowClient()，其默认「本地 LM Studio 优先」，评测机无本地服务会整批超时退化 rule。
    llm_client = SiliconFlowClient(
        api_key=os.getenv("SILICONFLOW_API_KEY"),
        base_url=os.getenv("SILICONFLOW_BASE_URL"),
        model=os.getenv("SILICONFLOW_LLM_MODEL"),
    )
    llm_calls = {"succeeded": 0, "failed": 0}
    _lock = threading.Lock()
    original_chat = llm_client.chat_completion

    def audited_chat(*args, **kwargs):
        try:
            result = original_chat(*args, **kwargs)
            with _lock:
                llm_calls["succeeded"] += 1
            return result
        except Exception:
            with _lock:
                llm_calls["failed"] += 1
            raise

    llm_client.chat_completion = audited_chat

    def _process_row(row: dict, samples_idx: int) -> dict:
        inbound = row.get("input") if isinstance(row.get("input"), dict) else {}
        text = str(inbound.get("content") or row.get("text") or row.get("content") or "")
        user_id = str(row.get("user_id") or row.get("id") or f"eval_{samples_idx}")
        scene_id = row.get("scene_id")
        source = str(inbound.get("source") or row.get("source") or "conversation")
        # dataset uses chat → map to conversation DataSource
        if source == "chat":
            source = "conversation"
        try:
            ds = DataSource(source)
        except ValueError:
            ds = DataSource.CONVERSATION
        record = RawRecord(
            id=str(row.get("id") or user_id),
            source=ds,
            user_id=user_id,
            scene_id=scene_id,
            payload={"content": text},
        )
        # 每样本独立临时 store：避免并发写同一 VersionIndex 文件造成竞争。
        with tempfile.TemporaryDirectory(prefix="pref_eval_") as tmp:
            store = PreferenceMemoryStore(
                storage_dir=tmp,
                use_llm=use_llm,
                allow_rules=allow_rules,
                llm_client=llm_client,
                # 封闭键精确匹配口径：限制 OS 抽取头只产出已知键，消除 invent 出来的
                # custom_* 动态键造成的 FP，使 fusion 口径也能过线（召回不受影响）。
                restrict_llm_to_known_keys=restrict_llm_keys,
            )
            entries = _with_retry(
                lambda: store.extract([record]),
                f"extract id={row.get('id')}",
            )
        got = _got_pairs(entries)
        expected = _expected_pairs(row)
        inter = got & expected
        return {
            "tp": len(inter),
            "fp": len(got - expected),
            "fn": len(expected - got),
            "sample_exact": got == expected,
            "negatives_fp": bool(not expected and got),
            "row": {
                "id": row.get("id"),
                "input": text,
                "expected": sorted(expected),
                "got": sorted(got),
                "fp": sorted(got - expected),
                "fn": sorted(expected - got),
            },
        }

    # 先一次性读入所有行（文件读取本身很快，串行即可）
    row_list: list[dict] = []
    with dataset.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row_list.append(json.loads(line))

    workers = max(1, int(workers))
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_process_row, row, i + 1) for i, row in enumerate(row_list)]
        for fut in as_completed(futs):
            results.append(fut.result())

    samples = len(row_list)
    tp = fp = fn = 0
    sample_exact = 0
    negatives_fp = 0
    rows_out = []
    for r in results:
        tp += r["tp"]
        fp += r["fp"]
        fn += r["fn"]
        sample_exact += int(r["sample_exact"])
        negatives_fp += int(r["negatives_fp"])
        rows_out.append(r["row"])

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "mode": mode,
        "llm_calls": llm_calls,
        "samples": samples,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "sample_exact_match_rate": round(sample_exact / samples, 4) if samples else 0.0,
        "negative_samples_with_any_extract": negatives_fp,
        "details": rows_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["rule", "llm", "fusion"],
        default="rule",
        help="rule=离线规则; llm=仅LLM; fusion=LLM优先、失败/空结果降级规则",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "benchmarks" / "datasets" / "preference_extraction.jsonl",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional JSON report path",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="并行处理样本的工作线程数；1=串行",
    )
    args = parser.parse_args()
    report = evaluate(args.mode, args.dataset, workers=args.workers)
    summary = {k: v for k, v in report.items() if k != "details"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
