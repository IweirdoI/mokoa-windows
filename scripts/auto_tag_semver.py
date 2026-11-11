#!/usr/bin/env python3
"""按语义化版本（SemVer）规范为 master 历史提交批量打 tag。

打标策略（经团队确认）：
- 仅处理 master 分支，**跳过 merge 提交**（其内容已由被并入的提交覆盖）；
- 最早的提交标记为 ``0.0.1``，之后按提交类型递增；
- ``feat`` -> MINOR；``fix`` / ``security`` -> PATCH；
  含 ``!`` 或 ``BREAKING CHANGE`` -> MAJOR；
- ``docs`` / ``chore`` / ``test`` / ``refactor`` / ``perf`` 等**不改变对外功能**的提交
  不推进版本号，改用构建元数据（如 ``0.3.0+build.12``），保证每个提交都有唯一 tag
  且不污染版本语义；版本号变化时 ``build`` 序号重置。

用法::

    python scripts/auto_tag_semver.py                     # dry-run，仅打印映射
    python scripts/auto_tag_semver.py --apply             # 实际创建 annotated tag
    python scripts/auto_tag_semver.py --branch master     # 指定分支（默认 master）

退出码：0 成功；1 参数/仓库状态异常。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CONVENTIONAL = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:")
BREAKING_SUBJECT = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!:")
# 仅识别 Conventional Commits 的页脚写法（行首 BREAKING CHANGE:），
# 避免正文里只是“提到”这个词就被误判为破坏性变更。
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

FEATURE_TYPES = frozenset({"feat"})
PATCH_TYPES = frozenset({"fix", "security"})
NON_VERSION_TYPES = frozenset(
    {"docs", "chore", "test", "refactor", "perf", "style", "ci", "build", "revert"}
)

# 早期提交没有 Conventional Commits 前缀，按改动内容人工判定（保持可复核）。
# 键为短哈希，值为判定类型：feat / fix / docs / refactor / init。
MANUAL_CLASSIFICATION = {
    "be7bc7e": "init",       # 首次上传 MOKOA 项目
    "c2a779a": "feat",       # wmy_ingestion_1
    "68e4a80": "feat",       # SQLite ingestion 中转库任务初步
    "d0eed25": "feat",       # 敏感信息监测与拦截 + 70 个用例
    "1d02106": "docs",       # ingestion 更新优化（以文档为主）
    "f14ad3f": "feat",       # 添加 Milvus 版代码
    "ee49092": "refactor",   # 接口核验（大量删除/收敛）
    "e91d6dc": "docs",       # 注释优化
    "1eea3d0": "feat",       # wire preference profile and conflict api
}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


@dataclass(frozen=True)
class TagPlan:
    sha: str
    tag: str
    kind: str
    subject: str


def git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=True
    )
    return result.stdout.decode("utf-8", "replace")


def read_commits(branch: str) -> list[Commit]:
    """读取分支上“最早的在前”的非 merge 提交（拓扑序，父提交先于子提交）。"""

    raw = git(
        "log", "--no-merges", "--reverse", "--topo-order",
        "--format=%H%x1f%s%x1f%b%x1e", branch,
    )
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 3:
            continue
        commits.append(Commit(sha=parts[0].strip(), subject=parts[1], body=parts[2]))
    return commits


def classify(commit: Commit) -> str:
    """把提交映射为 init / feat / fix / breaking / none 之一。"""

    short = commit.sha[:7]
    if short in MANUAL_CLASSIFICATION:
        manual = MANUAL_CLASSIFICATION[short]
        return "init" if manual == "init" else ("none" if manual in NON_VERSION_TYPES else manual)

    if BREAKING_SUBJECT.match(commit.subject) or BREAKING_FOOTER.search(commit.body):
        return "breaking"

    match = CONVENTIONAL.match(commit.subject)
    if not match:
        return "none"
    type_ = match.group("type").lower()
    if match.group("breaking"):
        return "breaking"
    if type_ in FEATURE_TYPES:
        return "feat"
    if type_ in PATCH_TYPES:
        return "fix"
    return "none"


def build_plan(
    commits: list[Commit], prefix: str, existing: dict[str, str] | None = None
) -> list[TagPlan]:
    """按 SemVer 规则推进版本号，生成 (提交 -> tag) 的完整映射。

    ``existing`` 是已存在 tag 到 commit 的映射。若算出的构建元数据 tag 已被
    **其他**提交占用（旁支提交在拓扑序里靠前时会导致编号错位），就继续递增
    build 序号直到空闲，避免该提交因同名冲突被静默跳过、永远打不上标签。
    """

    existing = existing or {}
    major = minor = patch = 0
    build = 0
    plan: list[TagPlan] = []

    for index, commit in enumerate(commits):
        kind = classify(commit)
        if index == 0:
            # 首个提交固定为起点版本 0.0.1
            major, minor, patch, build = 0, 0, 1, 0
            tag = f"{major}.{minor}.{patch}"
        elif kind == "breaking":
            major, minor, patch, build = major + 1, 0, 0, 0
            tag = f"{major}.{minor}.{patch}"
        elif kind == "feat":
            minor, patch, build = minor + 1, 0, 0
            tag = f"{major}.{minor}.{patch}"
        elif kind == "fix":
            patch, build = patch + 1, 0
            tag = f"{major}.{minor}.{patch}"
        else:
            # 非功能提交：版本号不变，用构建元数据保证 tag 唯一
            build += 1
            tag = f"{major}.{minor}.{patch}+build.{build}"
            # 该编号已被其他提交占用时继续递增，直到拿到空闲编号
            while existing.get(f"{prefix}{tag}", commit.sha) != commit.sha:
                build += 1
                tag = f"{major}.{minor}.{patch}+build.{build}"
        plan.append(TagPlan(sha=commit.sha, tag=f"{prefix}{tag}", kind=kind, subject=commit.subject))

    return plan


def read_existing_tags() -> dict[str, str]:
    """返回 ``{tag 名: 指向的 commit sha}``（annotated tag 会解引用到提交）。"""

    raw = git(
        "for-each-ref",
        "--format=%(refname:short)%09%(objecttype)%09%(objectname)%09%(*objectname)",
        "refs/tags",
    )
    mapping: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _objtype, objname, deref = (line.split("\t") + ["", "", "", ""])[:4]
        mapping[name] = deref or objname
    return mapping


def apply_tags(plan: list[TagPlan]) -> int:
    created = 0
    for item in plan:
        subject = item.subject.replace("\n", " ").strip()
        git("tag", "-a", item.tag, item.sha, "-m", f"{item.tag} {subject}")
        created += 1
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default="master", help="要处理的分支（默认 master）")
    parser.add_argument("--prefix", default="", help="tag 前缀，例如 v")
    parser.add_argument("--apply", action="store_true", help="实际创建 tag（默认仅 dry-run）")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="已默认启用（保留以兼容旧命令）：跳过已打标的提交，只补标新提交",
    )
    args = parser.parse_args(argv)

    commits = read_commits(args.branch)
    if not commits:
        print(f"分支 {args.branch} 没有可处理的提交。", file=sys.stderr)
        return 1

    existing = read_existing_tags()
    plan = build_plan(commits, args.prefix, existing)
    # 已有任意 tag 指向该提交 = 该提交已打过标，跳过；其余为待补标的新提交
    tagged = set(existing.values())
    already = [item for item in plan if item.sha in tagged]
    if already:
        print(f"跳过 {len(already)} 个已打标的提交，仅补标新提交。")
        plan = [item for item in plan if item.sha not in tagged]
        if not plan:
            print("tag 已覆盖分支全部提交，无需操作。")
            return 0

    if not args.apply:
        print(f"[dry-run] 分支 {args.branch}：{len(plan)} 个非 merge 提交待打标\n")
        for item in plan:
            print(f"{item.tag:<24} {item.sha[:9]} {item.kind:<8} {item.subject}")
        versioned = sum(1 for item in plan if "+build." not in item.tag)
        print(
            f"\n合计 {len(plan)} 个 tag：版本推进 {versioned} 个，"
            f"构建元数据 {len(plan) - versioned} 个；最终版本 {plan[-1].tag}"
        )
        print("确认无误后加 --apply 实际创建。")
        return 0

    created = apply_tags(plan)
    print(f"已创建 {created} 个 annotated tag，最终版本 {plan[-1].tag}")
    print("推送到远端：git push origin --tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
