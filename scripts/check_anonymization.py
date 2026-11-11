#!/usr/bin/env python3
"""交付前匿名化自检：扫描仓库中残留的本机用户名路径与（可选）已知主机名。

背景：提交/评审材料中不应出现成员本机用户名、主机名或绝对路径。既往匿名化只覆盖了
成员姓名，本脚本用于防止此类残留复发。

用法::

    python scripts/check_anonymization.py
    MOKOA_FORBIDDEN_HOSTS=host-a,host-b python scripts/check_anonymization.py

退出码：0 = 通过；1 = 发现残留。

设计说明：
- 只按“路径形态”匹配（用户目录路径），因此**不需要**把具体用户名写进仓库；
- 主机名无法用形态表达，故通过环境变量 ``MOKOA_FORBIDDEN_HOSTS`` 在本地临时注入，
  同样避免把真实主机名固化进代码；
- 模板占位符（如 ``C:/Users/{user}/Downloads``）不算残留，已在正则中排除。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Windows 用户目录绝对路径，排除模板占位符 {user}
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/](?!\{)", re.IGNORECASE)
# 类 Unix 家目录路径（/home/<name> 或 /Users/<name>），排除 <占位符>
UNIX_HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9][A-Za-z0-9._-]*")

SKIP_DIRS = frozenset(
    {".git", "node_modules", "dist", "__pycache__", ".venv", "venv",
     "data", "tmp", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build"}
)
MAX_FILE_BYTES = 4 * 1024 * 1024

# 规则测试夹具：验证本脚本检测能力时必然包含示例路径，不应计入结果。
ALLOWLIST = frozenset({"tests/test_anonymization_check.py"})


def scan_text(text: str, hosts: tuple[str, ...] = ()) -> list[str]:
    """返回文本中命中的可疑片段描述（纯函数，便于单测）。"""

    hits: list[str] = []
    hits.extend(f"windows-user-path: {m.group(0)}" for m in WINDOWS_USER_PATH.finditer(text))
    hits.extend(f"unix-home-path: {m.group(0)}" for m in UNIX_HOME_PATH.finditer(text))
    hits.extend(f"forbidden-host: {host}" for host in hosts if host and host in text)
    return hits


def _git_file_list() -> list[str]:
    """列出被追踪文件 + 未追踪（非忽略）文件，避免遗漏即将提交的新文件。"""

    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in result.stdout.decode("utf-8", "replace").split("\0") if name]


def iter_candidate_files() -> list[Path]:
    files: list[Path] = []
    for name in _git_file_list():
        path = ROOT / name
        if any(part in SKIP_DIRS for part in Path(name).parts):
            continue
        files.append(path)
    return files


def _read_text(path: Path) -> str | None:
    """读取可解码的文本文件；二进制或超限文件返回 None。"""

    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    del argv  # 预留：后续可支持命令行传入主机名
    hosts = tuple(
        host.strip()
        for host in os.getenv("MOKOA_FORBIDDEN_HOSTS", "").split(",")
        if host.strip()
    )

    findings: list[tuple[str, str]] = []
    for path in iter_candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        text = _read_text(path)
        if text is None:
            continue
        findings.extend((rel, hit) for hit in scan_text(text, hosts))

    if findings:
        print(f"发现 {len(findings)} 处可能的个人信息残留：", file=sys.stderr)
        for rel, hit in findings:
            print(f"  {rel}: {hit}", file=sys.stderr)
        print(
            "\n请改为仓库相对路径或占位符；主机名请用 MOKOA_FORBIDDEN_HOSTS 本地注入，不要写入仓库。",
            file=sys.stderr,
        )
        return 1

    print("匿名化自检通过：未发现本机用户名路径残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
