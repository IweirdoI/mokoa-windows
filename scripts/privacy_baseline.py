"""Run the synthetic rule corpus without a model, network, or database."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.security.service import redact_sensitive_text


def main():
    cases = json.loads((ROOT / "tests/fixtures/privacy_baseline.json").read_text(encoding="utf-8"))
    failed, timings = [], []
    for case in cases:
        output = redact_sensitive_text(case["text"])
        passed = case["secret"] not in output if "secret" in case else output == case["text"]
        if not passed:
            failed.append(case["id"])
    for _ in range(100):
        for case in cases:
            start = perf_counter()
            redact_sensitive_text(case["text"])
            timings.append((perf_counter() - start) * 1000)
    report = {
        "scope": "synthetic short-text rule regression; not real-world accuracy or model performance",
        "platform": platform.system(), "python": platform.python_version(),
        "cases": len(cases), "passed": len(cases) - len(failed), "failed_ids": failed,
        "warm_calls": len(timings), "median_ms": round(statistics.median(timings), 4),
        "p95_ms": round(sorted(timings)[int(len(timings) * .95) - 1], 4),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
