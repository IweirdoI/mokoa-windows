#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record every controllable variable for a contest git-dataset eval run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, cwd=ROOT, text=True).strip()
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__}: {exc})"


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_llm() -> dict:
    """Tiny remote completion so model_version / fingerprint are on disk. No secrets."""
    try:
        from src.common.env import load_env

        load_env()
    except Exception:
        pass
    out = {
        "ok": False,
        "model_requested": os.getenv("SILICONFLOW_LLM_MODEL"),
        "base_url": os.getenv("SILICONFLOW_BASE_URL"),
        "env_file_model": None,
    }
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("SILICONFLOW_LLM_MODEL="):
                out["env_file_model"] = line.split("=", 1)[1].strip()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=os.getenv("SILICONFLOW_API_KEY") or "missing",
            base_url=os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1",
        )
        model = os.getenv("SILICONFLOW_LLM_MODEL") or "deepseek-ai/DeepSeek-V4-Flash"
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single token PING."}],
            temperature=0.0,
            max_tokens=8,
        )
        choice = resp.choices[0]
        out.update(
            {
                "ok": True,
                "model_version": getattr(resp, "model", None),
                "system_fingerprint": getattr(resp, "system_fingerprint", None),
                "id": getattr(resp, "id", None),
                "finish_reason": getattr(choice, "finish_reason", None),
                "content_preview": (choice.message.content or "")[:80],
                "usage": (
                    {
                        "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                        "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                    }
                    if resp.usage
                    else None
                ),
                "temperature": 0.0,
                "top_p": None,
                "seed": None,
                "seed_note": "chat.completions.create is not passed seed; temperature=0.0",
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--stamp", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--n-pos", type=int, default=120)
    ap.add_argument("--n-neg", type=int, default=30)
    ap.add_argument("--nq", type=int, default=150)
    ap.add_argument("--rc", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    prompts = out / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)

    prompt_src = ROOT / "src" / "preference" / "prompts.py"
    if prompt_src.exists():
        dest = prompts / "preference_prompts.py"
        dest.write_text(prompt_src.read_text(encoding="utf-8"), encoding="utf-8")

    judge = ROOT / "scripts" / "eval_preference_hf_dataset.py"
    if judge.exists():
        (prompts / "semantic_judge_source.py").write_text(
            judge.read_text(encoding="utf-8"), encoding="utf-8"
        )

    llm = _probe_llm()
    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stamp": args.stamp,
        "project": {
            "workspace_head": _git(["git", "rev-parse", "HEAD"]),
            "workspace_head_short": _git(["git", "rev-parse", "--short", "HEAD"]),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "eval_code_note": (
                "preference parallel workers + restrict_llm_to_known_keys for closed-set fusion"
            ),
            "origin_master": _git(["git", "rev-parse", "origin/master"]),
            "status_short": _git(["git", "status", "--short"]),
            "extractor_sha256": _sha256(ROOT / "src" / "preference" / "extractor.py"),
            "store_sha256": _sha256(ROOT / "src" / "preference" / "store.py"),
            "prompts_sha256": _sha256(prompt_src),
            "eval_preference_sha256": _sha256(ROOT / "scripts" / "eval_preference_hf_dataset.py"),
            "pref_eval_sha256": _sha256(ROOT / "scripts" / "pref_eval_dataset.py"),
        },
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "uname": platform.uname()._asdict(),
        },
        "sampling": {
            "seed": args.seed,
            "n": args.n,
            "n_pos": args.n_pos,
            "n_neg": args.n_neg,
            "nq": args.nq,
            "rc": args.rc,
            "floor_script_default_n": 50,
            "floor_reporting_n": 100,
            "this_run_multiplier_vs_50": round(args.n / 50, 4),
            "this_run_multiplier_vs_100": round(args.n / 100, 4),
            "band": "1.5x–5x of the reporting floor 100 → 150–500; n=150 = 1.5×",
            "rng_implementation": "Python random.Random (CPython Mersenne Twister / MT19937)",
            "preference_sampling_fn": "scripts.eval_preference_hf_dataset.sample_hf_rows",
            "preference_algorithm": (
                "independent Vitter reservoir: nonempty gold → n_pos, empty gold → n_neg"
            ),
            "dialog_sampling_fn": "benchmarks.run_dataset_eval.sample_indices",
            "dialog_algorithm": "sorted(rng.sample(range(total), n)) without replacement",
        },
        "llm": {
            **llm,
            "provider": "SiliconFlow OpenAI-compatible",
            "endpoint_flag": "remote",
            "extract_temperature": 0.0,
            "extract_max_tokens": 2000,
            "judge_temperature": 0.0,
            "judge_max_tokens": 200,
            "top_p": None,
            "top_p_note": "not passed; provider default",
            "seed": None,
            "workers": args.workers,
            "prompt_files": [
                "src/preference/prompts.py",
                "scripts/eval_preference_hf_dataset.py::_semantic_cover",
            ],
        },
        "embedding": {
            "OS_AGENT_EMBEDDING_BACKEND": os.environ.get("OS_AGENT_EMBEDDING_BACKEND"),
            "OS_AGENT_MEMORY_BACKEND": os.environ.get("OS_AGENT_MEMORY_BACKEND"),
            "OS_AGENT_KYLIN_STRICT": os.environ.get("OS_AGENT_KYLIN_STRICT"),
            "OS_AGENT_KYLIN_VECTOR_LIBRARY": os.environ.get("OS_AGENT_KYLIN_VECTOR_LIBRARY"),
            "OS_AGENT_KYLIN_VECTOR_ENDPOINT": os.environ.get("OS_AGENT_KYLIN_VECTOR_ENDPOINT"),
            "kylin_so_sha256": _sha256(
                Path(
                    os.environ.get(
                        "OS_AGENT_KYLIN_VECTOR_LIBRARY",
                        str(ROOT / "build/kylin-vector/libmokoa_kylin_vector_bridge.so"),
                    )
                )
            ),
        },
        "contest_thresholds": {
            "preference_acc": 0.85,
            "recall": 0.85,
            "latency_ms": 500,
            "conflict_acc": 0.88,
        },
    }
    path = out / "reproducibility.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"snapshot": str(path), "llm_ok": llm.get("ok")}, indent=2))
    if not llm.get("ok"):
        print("WARN: LLM probe failed; preference fusion may also fail", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
