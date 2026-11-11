"""Strict smoke/latency test for the real Kylin CoreAI Embedding SDK."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.embedding.kylin_adapter import KylinEmbeddingAdapter


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    ln = math.sqrt(sum(value * value for value in left))
    rn = math.sqrt(sum(value * value for value in right))
    return dot / (ln * rn) if ln and rn else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    provider = KylinEmbeddingAdapter(allow_fallback=False)
    texts = ["pip 安装超时时使用国内镜像", "pip 下载失败时更换镜像源", "今天天气很好"]
    latencies: list[float] = []
    try:
        vectors = provider.embed(texts)
        for _ in range(max(1, args.runs)):
            started = time.perf_counter()
            provider.embed([texts[0]])
            latencies.append((time.perf_counter() - started) * 1000)
        report = {
            "passed": True,
            "backend": provider.backend_name,
            "using_fallback": provider.using_fallback,
            "dimension": len(vectors[0]),
            "non_zero": all(any(value != 0.0 for value in vector) for vector in vectors),
            "related_similarity": _cosine(vectors[0], vectors[1]),
            "unrelated_similarity": _cosine(vectors[0], vectors[2]),
            "latency_ms": {
                "runs": len(latencies),
                "min": min(latencies),
                "median": statistics.median(latencies),
                "max": max(latencies),
            },
        }
        if provider.using_fallback or not report["non_zero"]:
            report["passed"] = False
    except Exception as exc:
        report = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        provider.close()

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
