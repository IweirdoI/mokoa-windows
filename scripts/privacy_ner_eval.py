"""Offline CPU candidate evaluation. This script does not enable a production detector.

All supplied examples must be synthetic. Output contains only sample IDs and metrics.
Use --runtime-dir for an isolated dependency directory; no downloads occur here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def decode_bio(text, offsets, labels):
    """Return source spans; O/special tokens end a span and a new B starts one."""
    spans = []
    current = None
    for (start, end), label in zip(offsets, labels):
        if start == end or label == "O":
            if current:
                spans.append(current)
                current = None
            continue
        prefix, kind = label.split("-", 1)
        if prefix == "I" and current and current[2] == kind:
            current = (current[0], end, kind)
        else:
            if current:
                spans.append(current)
            current = (start, end, kind)
    if current:
        spans.append(current)
    return spans


def positions(spans):
    return {i for start, end, *_ in spans for i in range(start, end)}


def score_cases(cases, predictions):
    if len(cases) != len(predictions):
        raise ValueError("Every case requires a prediction")
    total = caught = false_chars = clean_total = clean_altered = 0
    failures = []
    for case, predicted in zip(cases, predictions):
        expected = []
        for secret in case["secrets"]:
            if not secret or case["text"].count(secret) != 1:
                raise ValueError("Diagnostic secrets must be nonempty and occur exactly once")
            start = case["text"].index(secret)
            expected.append((start, start + len(secret)))
        expected_positions = positions(expected)
        covered = sum(set(range(start, end)) <= predicted for start, end in expected)
        total += len(expected)
        caught += covered
        excess = len(predicted - expected_positions)
        false_chars += excess
        if not expected:
            clean_total += 1
            clean_altered += bool(predicted)
        if covered < len(expected) or excess:
            failures.append({"id": case["id"], "missed_spans": len(expected) - covered, "extra_masked_chars": excess})
    return {"sensitive_spans": total, "fully_covered_spans": caught,
            "full_span_recall": caught / total if total else None,
            "clean_cases": clean_total, "clean_cases_altered": clean_altered,
            "extra_masked_chars": false_chars, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--rules-file", type=Path, help="Optional frozen regex snapshot for a standalone probe")
    parser.add_argument("--threads", type=int, choices=[1, 2, 4], default=2)
    parser.add_argument("--corpus", type=Path, default=ROOT / "tests/fixtures/privacy_ner_cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runtime_dir:
        sys.path.insert(0, str(args.runtime_dir.resolve()))
    import psutil
    process = psutil.Process()
    initial_rss = process.memory_info().rss
    start = perf_counter()
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    if args.rules_file:
        rule_specs = json.loads(args.rules_file.read_text(encoding="utf-8"))
        rule_patterns = [re.compile(item["pattern"], item["flags"]) for item in rule_specs]
    else:
        from src.security.service import _RULES
        rule_patterns = [rule._regex for rule in _RULES]

    config = json.loads((args.model_dir / "config.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(args.model_dir / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    model_file = args.model_dir / "onnx/model_quantized.onnx"
    session = ort.InferenceSession(str(model_file), sess_options=options, providers=["CPUExecutionProvider"])
    load_ms = (perf_counter() - start) * 1000
    names = {item.name for item in session.get_inputs()}
    cases = json.loads(args.corpus.read_text(encoding="utf-8"))

    def predict(text):
        encoded = tokenizer.encode(text)
        if len(encoded.ids) > int(config.get("max_position_embeddings", 512)):
            raise ValueError("Overlength input: no silent truncation in the evaluation")
        features = {"input_ids": encoded.ids, "attention_mask": encoded.attention_mask, "token_type_ids": encoded.type_ids}
        logits = session.run(None, {key: np.asarray([features[key]], dtype=np.int64) for key in names})[0][0]
        labels = [config["id2label"][str(int(i))] for i in logits.argmax(axis=-1)]
        return decode_bio(text, encoded.offsets, labels)

    predictions, rules, timings = [], [], []
    for case in cases:
        spans = predict(case["text"])
        # Deliberately naive candidate policy; public places are included so
        # the report exposes why generic NER is not equivalent to PII detection.
        predictions.append(positions([s for s in spans if s[2] in {"PER", "LOC"}]))
        rules.append(positions([(m.start(), m.end()) for pattern in rule_patterns for m in pattern.finditer(case["text"])]))
    for _ in range(5):
        for case in cases:
            start = perf_counter()
            predict(case["text"])
            timings.append((perf_counter() - start) * 1000)
    memory = process.memory_info()
    report = {"scope": "synthetic diagnostic corpus, Windows/Linux CPU candidate test; not Kylin acceptance",
              "platform": platform.platform(), "python": platform.python_version(),
              "onnxruntime": ort.__version__, "threads": args.threads,
              "model_sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
              "model_bytes": model_file.stat().st_size, "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
              "case_count": len(cases), "load_including_imports_ms": round(load_ms, 2),
              "rules_sha256": hashlib.sha256(json.dumps([(p.pattern, p.flags) for p in rule_patterns], ensure_ascii=False).encode("utf-8")).hexdigest(),
              "rules_metric": "union of raw regex matches, not a full ingestion or API test",
              "process_rss_mib": round(memory.rss / 2**20, 2),
              "rss_increase_mib": round((memory.rss - initial_rss) / 2**20, 2),
              "sampled_end_rss_only": True,
              "warm_calls": len(timings), "median_ms": round(float(np.median(timings)), 3),
              "p95_ms": round(sorted(timings)[math.ceil(len(timings) * .95) - 1], 3),
              "max_ms": round(max(timings), 3),
              "rules": score_cases(cases, rules), "ner_per_loc": score_cases(cases, predictions),
              "rules_plus_ner": score_cases(cases, [a | b for a, b in zip(rules, predictions)])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"rules", "ner_per_loc", "rules_plus_ner"}}, ensure_ascii=False, indent=2))
    for mode in ("rules", "ner_per_loc", "rules_plus_ner"):
        print(mode, json.dumps({k:v for k,v in report[mode].items() if k != "failures"}))


if __name__ == "__main__":
    main()
