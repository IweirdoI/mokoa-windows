"""偏好抽取评测脚本并行化回归测试（不触发远程 LLM）。

覆盖本轮改动的核心路径：
- extract_batch 在 rule 模式（确定性、无 LLM）下，workers=1 与 workers=N 必须给出
  完全一致的逐样本结果与聚合指标，验证并行聚合逻辑没有破坏正确性。
- pref_eval_dataset.evaluate 在 rule 模式下，workers=1 与 workers=N 结果一致，
  且命中已知封闭键基线（precision=1.0 / recall≈0.9583 / sample_exact≈0.9429）。

LLM 路径（fusion / semantic）的语义指标已在手动端到端运行中验证
（见 docs/evidence/run-fusion-final），此处刻意只用 rule 路径以稳定、快速地
守住并行重构不引入回归。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.eval_preference_hf_dataset as ev  # noqa: E402
import scripts.pref_eval_dataset as pref_eval  # noqa: E402


def _sample_rows(n: int) -> list[dict]:
    """构造若干条 rule 可识别偏好的样本（无 gold，用于验证抽取稳定性）。"""
    rows = []
    for i in range(n):
        rows.append(
            {
                "index": i,
                "input": "我习惯用 Chrome 浏览器，平时喜欢用支付宝付款，下午一般喝美式咖啡。",
                "output": None,
                "gold_preferences": [],
            }
        )
    return rows


def test_extract_batch_rule_deterministic_across_workers():
    rows = _sample_rows(12)
    base = ev.extract_batch("rule", rows, semantic=False, workers=1)
    par = ev.extract_batch("rule", rows, semantic=False, workers=4)

    # extract_batch 返回聚合 dict，逐样本明细在 "details" 键
    assert len(base["details"]) == len(par["details"]) == 12
    base_sorted = sorted(base["details"], key=lambda d: d["index"])
    par_sorted = sorted(par["details"], key=lambda d: d["index"])
    for a, b in zip(base_sorted, par_sorted):
        assert a["index"] == b["index"]
        assert a["n_gold"] == b["n_gold"]
        assert a["n_got"] == b["n_got"]
        assert a["sample_ok"] == b["sample_ok"]
        assert a["detection_ok"] == b["detection_ok"]
        assert a["recall"] == b["recall"]
        assert a["got"] == b["got"]


def test_pref_eval_dataset_rule_deterministic_and_baseline():
    ds = ROOT / "benchmarks" / "datasets" / "preference_extraction.jsonl"
    assert ds.exists(), f"缺失封闭键数据集: {ds}"

    a = pref_eval.evaluate("rule", ds, workers=1)
    b = pref_eval.evaluate("rule", ds, workers=4)

    for k in ("tp", "fp", "fn", "precision", "recall", "sample_exact_match_rate"):
        assert a[k] == b[k], f"worker 数改变导致 {k} 不一致: {a[k]} vs {b[k]}"

    # 封闭键 rule 口径已知基线（与历史手工运行一致）
    assert a["precision"] == 1.0
    assert a["recall"] == pytest.approx(0.9583, abs=1e-3)
    assert a["sample_exact_match_rate"] == pytest.approx(0.9429, abs=1e-3)
    assert a["negative_samples_with_any_extract"] == 0
    assert a["samples"] == 35
