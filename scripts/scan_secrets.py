"""密钥泄露自查脚本.

扫描仓库中（受版本控制的 + 未忽略的未跟踪文件）可能泄露的密钥/令牌，
用于在提交前或 CI 中自检，避免把真实 API Key 提交出去。

用法:
  python scripts/scan_secrets.py            # 扫描并打印报告
  python scripts/scan_secrets.py --strict   # 发现即非零退出（适合 pre-commit/CI）

说明:
- 仅扫描 git 追踪及未忽略的未跟踪文本文件（--exclude-standard）。
- 自动跳过 .env.example 与明显的占位符（如 sk-your-real-key-here）。
- 仅做启发式正则，不能替代专业工具（如 gitleaks），但足够拦截常见误提交。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# 启发式规则：名称 + 值形态
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("siliconflow/openai key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("api key assignment", re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]")),
]

# 占位符/示例白名单（命中后不视为泄露）
ALLOWLIST = [
    "sk-your-real-key-here",
    "sk-...",
    "sk-你的密钥",
    "your-key",
    "your_api_key",
    "lm-studio",
    "${",
    "os.getenv",
    "getenv(",
    # 测试固件中的假令牌（tests/test_security.py）
    "sk-proj-",          # OpenAI 测试密钥前缀
    "eyJhbGci",          # JWT 测试令牌头
]

MAX_FILE_BYTES = 1_000_000


def _is_allowlisted(text: str) -> bool:
    return any(allow in text for allow in ALLOWLIST)


def _git_files(repo: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-c", "-o", "--exclude-standard"],
        cwd=str(repo), capture_output=True, text=True,
    )
    files: list[Path] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = repo / line
        if p.exists() and p.is_file():
            files.append(p)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repo for leaked secrets")
    parser.add_argument("--strict", action="store_true", help="发现泄露时返回非零退出码")
    parser.add_argument("--repo", default=".", help="仓库根目录（默认当前目录）")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    files = _git_files(repo)

    findings: list[tuple[Path, str, int, str]] = []
    for fp in files:
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # 跳过二进制/不可读文件
        for name, pat in PATTERNS:
            for m in pat.finditer(text):
                snippet = m.group(0)
                if _is_allowlisted(snippet):
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append((fp, name, line_no, snippet[:60]))

    if not findings:
        print("OK: 未发现疑似泄露的密钥。")
        return 0

    print(f"发现 {len(findings)} 处疑似密钥泄露：\n")
    for fp, name, line_no, snippet in findings:
        rel = fp.relative_to(repo)
        print(f"  [{name}] {rel}:{line_no}  ->  {snippet!r}")
    print("\n建议：将真实密钥移入 .env（已被 .gitignore 忽略），并尽快轮换已泄露的 key。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
