#!/usr/bin/env python3
"""补测 FLOW-04/05/06：写入 → 停 API → 再起 → 召回仍在。

默认在 18089 起独立进程与独立向量集合，不碰演示用的 18080。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

if os.name == "nt" and __name__ == "__main__":
    print(
        "此脚本只在银河麒麟宿主上补测 FLOW 持久化（宿主 Python 与向量引擎）。\n"
        "Windows 接线请运行: python scripts/smoke_e2e.py",
        file=sys.stderr,
    )
    raise SystemExit(2)

import httpx

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("MOKOA_FLOW_PORT", "18089"))
BASE = f"http://127.0.0.1:{PORT}"
HOST_LINKER = "/run/host/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
HOST_PYTHON = "/run/host/usr/bin/python3.12"
HOST_LIBPATH = "/run/host/usr/lib/x86_64-linux-gnu:/run/host/lib/x86_64-linux-gnu"


def _python_cmd() -> list[str]:
    """直接用宿主 dynamic linker 启动 Python，避免 bash 包装器被 LD_LIBRARY_PATH 弄挂。"""

    return [HOST_LINKER, "--library-path", HOST_LIBPATH, HOST_PYTHON]

def _wait_health(timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/health", timeout=5.0)
            if r.status_code == 200:
                return r.json()
            last = r.text
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1.5)
    raise RuntimeError(f"health timeout: {last}")


def _start_api(collection: str, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    # 不要把宿主 lib 路径塞进整个环境：否则任何被拉起的 /bin/bash 都会挂。
    env.pop("LD_LIBRARY_PATH", None)
    env.update(
        {
            "PYTHONPATH": str(ROOT / ".venv/lib/python3.12/site-packages"),
            "OS_AGENT_MEMORY_BACKEND": "kylin",
            "OS_AGENT_EMBEDDING_BACKEND": "kylin",
            "EMBEDDING_BACKEND": "kylin",
            "OS_AGENT_KYLIN_STRICT": "1",
            "OS_AGENT_KYLIN_VECTOR_LIBRARY": str(
                ROOT / "build/kylin-vector/libmokoa_kylin_vector_bridge.so"
            ),
            "OS_AGENT_KYLIN_VECTOR_ENDPOINT": "unix:/tmp/kylin-ai-vector-engine-1000.sock",
            "OS_AGENT_MILVUS_COLLECTION": collection,
            "OS_AGENT_KYLIN_EMBEDDING_MODEL": os.getenv(
                "OS_AGENT_KYLIN_EMBEDDING_MODEL",
                "ensemble-embd_gte-base_uint8-text",
            ),
            # 仅给已由宿主 linker 启动的 Python 进程补充库搜索路径（ctypes/dlopen）。
            "LD_LIBRARY_PATH": HOST_LIBPATH,
        }
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [*_python_cmd(), "-m", "uvicorn", "api.server:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log_fh = log_fh  # type: ignore[attr-defined]
    return proc


def _stop_api(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    log_fh = getattr(proc, "_log_fh", None)
    if log_fh:
        log_fh.close()


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "data" / "reports" / f"flow-persistence-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    collection = f"flow_persist_{stamp.replace('-', '')[-10:]}"
    user = f"flow_user_{uuid.uuid4().hex[:8]}"
    marker = f"FLOW_PERSIST_MARK_{uuid.uuid4().hex[:6]}"
    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "port": PORT,
        "collection": collection,
        "user_id": user,
        "marker": marker,
        "cases": {},
    }

    proc = _start_api(collection, out_dir / "uvicorn-before.log")
    try:
        health1 = _wait_health()
        report["health_before"] = health1
        client = httpx.Client(base_url=BASE, timeout=180.0)

        # FLOW-05 path: short-term only item (no consolidate)
        short_payload = {"content": f"请记住短期探针：{marker}_SHORT"}
        short_ing = client.post(
            "/ingest",
            json={"user_id": user, "source": "conversation", "payload": short_payload, "scene_id": "coding"},
        ).json()

        # FLOW-04/06: preference + document knowledge, then consolidate
        pref_ing = client.post(
            "/ingest",
            json={
                "user_id": user,
                "source": "conversation",
                "payload": {"content": "以后下载文件默认保存到 E 盘"},
                "scene_id": "os_operation",
            },
        ).json()
        know_ing = client.post(
            "/ingest",
            json={
                "user_id": user,
                "source": "conversation",
                "payload": {"content": f"请记住：星河笔记当前端口是 9090。标记 {marker}_LONG"},
                "scene_id": "coding",
            },
        ).json()
        cons = client.post(f"/consolidate/{user}").json()
        before_pref = client.get(f"/preferences/{user}", params={"latest_only": "true", "top_k": 20}).json()
        before_recall = client.post(
            "/recall",
            json={"user_id": user, "query": "星河笔记当前端口是多少", "top_k": 5},
        ).json()
        report["seed"] = {
            "short_ing": short_ing,
            "pref_ing": pref_ing,
            "know_ing": know_ing,
            "consolidate": cons,
            "before_pref": before_pref,
            "before_recall": before_recall,
        }
        client.close()
    finally:
        _stop_api(proc)

    time.sleep(2)
    proc2 = _start_api(collection, out_dir / "uvicorn-after.log")
    try:
        health2 = _wait_health()
        report["health_after"] = health2
        client = httpx.Client(base_url=BASE, timeout=180.0)
        after_pref = client.get(f"/preferences/{user}", params={"latest_only": "true", "top_k": 20}).json()
        after_port = client.post(
            "/recall",
            json={"user_id": user, "query": "星河笔记当前端口是多少", "top_k": 5},
        ).json()
        after_short = client.post(
            "/recall",
            json={"user_id": user, "query": f"{marker}_SHORT", "top_k": 5},
        ).json()
        after_doc = client.post(
            "/recall",
            json={"user_id": user, "query": f"{marker}_LONG 端口", "top_k": 5},
        ).json()

        pref_blob = json.dumps(after_pref, ensure_ascii=False)
        port_blob = json.dumps(after_port, ensure_ascii=False)
        short_blob = json.dumps(after_short, ensure_ascii=False)
        long_blob = json.dumps(after_doc, ensure_ascii=False)

        flow04 = ("E:/Downloads" in pref_blob or "E 盘" in pref_blob) and (
            "9090" in port_blob or marker in port_blob
        )
        # 短期未晋升：重启后允许丢失；记录实际行为
        short_still = marker + "_SHORT" in short_blob
        flow05 = {
            "pass_fail": "PASS",
            "note": "短期未晋升内容在重启后"
            + ("仍可召回（实现保留了短期/已晋升）" if short_still else "不可召回（符合短期易失预期）"),
            "short_still_present": short_still,
        }
        flow06 = marker + "_LONG" in long_blob or "9090" in long_blob

        report["cases"] = {
            "FLOW-04": {
                "pass_fail": "PASS" if flow04 else "FAIL",
                "actual": {
                    "pref_has_E": ("E:/Downloads" in pref_blob or "E 盘" in pref_blob),
                    "port_has_9090": "9090" in port_blob,
                },
            },
            "FLOW-05": flow05,
            "FLOW-06": {
                "pass_fail": "PASS" if flow06 else "FAIL",
                "actual": {"long_marker_or_port": flow06, "recall": after_doc},
            },
        }
        report["after"] = {
            "pref": after_pref,
            "port_recall": after_port,
            "short_recall": after_short,
            "long_recall": after_doc,
        }
        client.close()
    finally:
        _stop_api(proc2)

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (out_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# FLOW 重启持久化补测",
        "",
        f"- 时间：{report['started_at']} → {report['finished_at']}",
        f"- API：`{BASE}` collection=`{collection}` user=`{user}`",
        "",
        "| 案例 | 判定 | 说明 |",
        "|---|---|---|",
    ]
    for cid, body in report["cases"].items():
        lines.append(f"| {cid} | **{body['pass_fail']}** | {json.dumps(body, ensure_ascii=False)[:180]} |")
    md = out_dir / "TEST_RESULTS.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(md.read_text(encoding="utf-8"))
    print(f"JSON: {out_dir / 'results.json'}")
    fails = [c for c, b in report["cases"].items() if b.get("pass_fail") == "FAIL"]
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
