"""Preflight checks to run before and after moving the project to Kylin OS."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _command(*args: str) -> dict[str, Any]:
    executable = shutil.which(args[0])
    if not executable:
        return {"available": False, "output": "command not found"}
    completed = subprocess.run(
        [executable, *args[1:]],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = (completed.stdout or completed.stderr).strip()
    return {
        "available": True,
        "returncode": completed.returncode,
        "output": output[:2000],
    }


def collect() -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine().lower()
    is_linux = system == "Linux"
    supported_arch = machine in {"x86_64", "amd64", "aarch64", "arm64"}
    packages = {
        name: importlib.util.find_spec(name) is not None
        for name in ("pymilvus", "yaml", "fastapi", "cryptography")
    }

    report: dict[str, Any] = {
        "ready_for_kylin_runtime": False,
        "host": {
            "system": system,
            "release": platform.release(),
            "machine": machine,
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "project": {
            "root": str(ROOT),
            "memory_backend": os.getenv("OS_AGENT_MEMORY_BACKEND", "memory"),
            "embedding_backend": os.getenv("OS_AGENT_EMBEDDING_BACKEND", "auto"),
            "milvus_uri": os.getenv("OS_AGENT_MILVUS_URI"),
        },
        "python_packages": packages,
        "commands": {},
        "checks": {
            "linux": is_linux,
            "supported_arch": supported_arch,
            "python_3_10_plus": sys.version_info >= (3, 10),
        },
        "next_actions": [],
    }

    if is_linux:
        report["commands"]["os_release"] = _command("sh", "-c", "cat /etc/os-release")
        report["commands"]["pkg_config"] = _command(
            "pkg-config", "--modversion", "kysdk-coreai-embedding"
        )
        report["commands"]["ldconfig"] = _command(
            "sh", "-c", "ldconfig -p | grep -i 'coreai.*embedding'"
        )
        report["commands"]["dpkg"] = _command(
            "dpkg-query", "-W", "libkylin-coreai-embedding-dev"
        )
        sdk_ready = any(
            command.get("returncode") == 0 and bool(command.get("output"))
            for key, command in report["commands"].items()
            if key in {"pkg_config", "ldconfig", "dpkg"}
        )
    else:
        sdk_ready = False
        report["next_actions"].append("在银河麒麟 2503 上重新运行本脚本")

    report["checks"]["kylin_embedding_sdk_detected"] = sdk_ready
    if not sdk_ready:
        report["next_actions"].append(
            "在设置→AI模块管理安装AI子系统，并安装 libkylin-coreai-embedding-dev"
        )
    if not packages["pymilvus"]:
        report["next_actions"].append('安装依赖: pip install "pymilvus>=2.4,<3"')
    if not supported_arch:
        report["next_actions"].append("确认目标CPU是否有对应Milvus/AI SDK依赖")

    report["ready_for_kylin_runtime"] = bool(
        is_linux
        and supported_arch
        and sys.version_info >= (3, 10)
        and sdk_ready
        and packages["pymilvus"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    parser.add_argument(
        "--require-kylin",
        action="store_true",
        help="return non-zero unless the Kylin runtime prerequisites are present",
    )
    args = parser.parse_args()
    report = collect()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 2 if args.require_kylin and not report["ready_for_kylin_runtime"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
