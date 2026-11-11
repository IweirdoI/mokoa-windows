"""Strict host-only acceptance: exit 0 complete, 1 failed, 2 incomplete."""
import json
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_health(health, pid, run_id):
    expected = {"status": "ok", "memory_backend": "kylin", "embedding_backend": "kylin",
                "kylin_strict": "1", "using_fallback": False, "pid": pid, "run_id": run_id}
    for key, value in expected.items():
        if health.get(key) != value:
            raise RuntimeError(f"health mismatch: {key}: {health.get(key)!r}")
    if health.get("vector_sdk", {}).get("backend") != "kylin-vector-sdk":
        raise RuntimeError("System vector SDK evidence missing")
    if health.get("vector_dim") != 768:
        raise RuntimeError("Expected V11 measured embedding dimension 768")


def final_status(phases):
    if any(p["status"] == "failed" for p in phases.values()):
        return "failed", 1
    if any(p["status"] == "pending" for p in phases.values()):
        return "incomplete", 2
    return "passed", 0


def main():
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    out = ROOT / "data/reports" / ("kylin-v11-full-" + run_id)
    out.mkdir(parents=True)
    phases = {name: {"status": "pending"} for name in
              ("health", "preference_rule", "conflict_component", "joint_diagnostic", "http_assertions", "scale_latency",
               "independent_large_recall", "browser")}
    report = {"run_id": run_id, "hostname": socket.gethostname(), "platform": platform.platform(), "phases": phases}
    process = log = None

    def save():
        report["status"], code = final_status(phases)
        (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return code

    def phase(name, action):
        try:
            phases[name] = {"status": "passed", "detail": action()}
        except Exception as exc:
            phases[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        save()
        print(name, phases[name]["status"], flush=True)
        return phases[name]["status"] == "passed"

    def command(name, args):
        with (out / (name + ".log")).open("w", encoding="utf-8") as handle:
            result = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, timeout=3600)
        if result.returncode:
            raise RuntimeError(f"Exit {result.returncode}; see {name}.log")

    def preflight():
        from src.common.kylin_runtime import enforce_acceptance, host_can_load_kylin_sdk
        expected_host = os.getenv("MOKOA_KYLIN_HOST", "").strip()
        if platform.system() != "Linux" or (expected_host and socket.gethostname() != expected_host):
            raise RuntimeError(
                "Run on the Galaxy Kylin V11 host (set MOKOA_KYLIN_HOST to pin a specific host)"
            )
        release = platform.freedesktop_os_release()
        report["os_release"] = release
        if "kylin" not in str(release).lower() or str(release.get("VERSION_ID", "")).lower().lstrip("v").split(".")[0] != "11":
            raise RuntimeError("Galaxy Kylin V11 required")
        if not host_can_load_kylin_sdk():
            raise RuntimeError("Host SDK glibc unavailable; do not run inside KARE")
        os.environ["OS_AGENT_KYLIN_ACCEPTANCE"] = "1"
        enforce_acceptance()
        library = Path(os.getenv("OS_AGENT_KYLIN_VECTOR_LIBRARY", str(ROOT / "build/kylin-vector/libmokoa_kylin_vector_bridge.so")))
        if not library.is_file():
            raise RuntimeError(f"Bridge missing: {library}")
        os.environ.update(OS_AGENT_KYLIN_VECTOR_LIBRARY=str(library.resolve()), OS_AGENT_ACCEPTANCE_RUN_ID=run_id,
                          OS_AGENT_ACCEPTANCE_STATE=str(out / "state"), OS_AGENT_MILVUS_COLLECTION="mokoa_accept_" + uuid.uuid4().hex,
                          PYTHONPATH=str(ROOT) + os.pathsep + os.getenv("PYTHONPATH", ""))
        report["collection"] = os.environ["OS_AGENT_MILVUS_COLLECTION"]
        report["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["working_tree"] = subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 18085))
        return {"library": str(library), "host_checks": "passed"}

    try:
        if not phase("preflight", preflight):
            return save()
        import httpx
        log = (out / "uvicorn.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.kylin_acceptance_server:app",
                                    "--host", "127.0.0.1", "--port", "18085"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

        def health():
            last = None
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("API exited; see uvicorn.log")
                try:
                    response = httpx.get("http://127.0.0.1:18085/health", timeout=5, trust_env=False)
                    response.raise_for_status()
                    evidence = response.json()
                    check_health(evidence, process.pid, run_id)
                    (out / "health.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                    return evidence
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
                    time.sleep(1)
            raise RuntimeError(f"API health unavailable: {last}")
        if not phase("health", health):
            return save()

        def preference(mode):
            target = out / ("preference-" + mode + ".json")
            command("preference-" + mode, [sys.executable, "scripts/pref_eval_dataset.py", "--mode", mode, "--out", str(target)])
            result = json.loads(target.read_text(encoding="utf-8"))
            if mode == "fusion" and (result["llm_calls"]["succeeded"] == 0 or result["llm_calls"]["failed"]):
                raise RuntimeError("Fusion LLM failed/unavailable; rule fallback must not disguise an LLM failure")
            if result["samples"] <= 0 or result["sample_exact_match_rate"] < .85:
                raise RuntimeError("Preference sample exact match below 85%; see JSON details")
            return {"sample_exact_match_rate": result["sample_exact_match_rate"], "scope": "extraction component"}
        phase("preference_rule", lambda: preference("rule"))
        from src.preference.llm_client import SiliconFlowClient
        if SiliconFlowClient().available:
            phase("preference_fusion", lambda: preference("fusion"))
        else:
            phases["preference_fusion"] = {"status": "not_applicable", "reason": "No LLM credentials"}
        phase("conflict_component", lambda: command("conflict-component", [sys.executable, "-m", "pytest", "-q", "-s",
              "tests/test_metrics.py::test_conflict_resolution_correctness", "tests/test_metrics.py::test_conflict_resolution_reports_accuracy"]))
        phase("joint_diagnostic", lambda: command("joint", [sys.executable, "scripts/eval_kylin_joint.py", "--output", str(out / "joint.json")]))

        def http_cases():
            from scripts.kylin_http_acceptance import evaluate
            return evaluate(out, run_id)
        phase("http_assertions", http_cases)

        def scale():
            command("scale", [sys.executable, "scripts/windows_scale_acceptance.py", str(out)])
            data = json.loads((out / "scale.json").read_text(encoding="utf-8"))
            check_health(data["health"], process.pid, run_id)
            if [r["documents"] for r in data["rows"]] != [100, 1000] or any(r["max_ms"] > 500 for r in data["rows"]):
                raise RuntimeError("Scale HTTP latency exceeds 500ms or rows missing")
            return {"scope": "synthetic size/HTTP latency only; no recall score"}
        phase("scale_latency", scale)

        def scale_recall():
            target = out / "joint-scale-1000.json"
            command("joint-scale-1000", [sys.executable, "scripts/eval_kylin_joint.py",
                                         "--dataset", str(ROOT / "benchmarks/datasets/kylin_scale_recall_1000.json"),
                                         "--output", str(target), "--top-k", "5", "--repeats", "3"])
            result = json.loads(target.read_text(encoding="utf-8"))
            metrics = result.get("metrics") or {}
            if result.get("documents") != 1000:
                raise RuntimeError("Scale recall dataset must contain 1000 documents")
            if not result.get("passed") or metrics.get("macro_recall_at_k", 0) < .85 or metrics.get("max_ms", 1e9) > 500:
                raise RuntimeError("Labeled 1000-doc recall below 85% or latency over 500ms")
            return {
                "scope": "labeled 1000-doc Recall@5 + embed/search latency; not HTTP",
                "macro_recall_at_k": metrics["macro_recall_at_k"],
                "hit_at_1": metrics["hit_at_1"],
                "max_ms": metrics["max_ms"],
                "unique_queries": metrics["unique_queries"],
            }
        phase("independent_large_recall", scale_recall)
        phases["browser"]["reason"] = "Verify conversation and memory pages in host browser; retain screenshots and steps"
    except Exception as exc:
        phases["runner"] = {"status": "failed", "error": str(exc)}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log:
            log.close()
        save()
        print("Evidence:", out, flush=True)
    return save()


if __name__ == "__main__":
    raise SystemExit(main())
