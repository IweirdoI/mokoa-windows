#!/usr/bin/env python3
"""一键量化评测入口 — 4 项硬指标 + 报告输出.

Owner: da0f41a1 | Requirement: (7) 量化评测

复刻 tests/test_metrics.py 的组件级基线，并在此之上输出更完整的指标
（Recall@5 / Hit@1 / P50 / P95）与可直接汇入效果验证报告的 JSON / Markdown。

两种运行模式：
  1) 默认（组件级基线）：确定性 hash embedding + 进程内向量客户端，无需真机，
     用于日常回归与 CI；结果标记为 baseline。
  2) --real：按 configs/default.yaml 的 storage/embedding 后端（真机应为 kylin）
     通过 create_memory / query_memory 原生接口播种并检索，得到真实召回/延迟。
     在麒麟 V11 宿主上先按 docs/测试结果.md 第 9 节导出 OS_AGENT_* 环境变量再运行。

方法学声明：组件级基线用确定性向量，只验证「链路与指标口径正确」，
不代表真实语义检索质量；真机数字以 --real 跑分为准。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

# 确保仓库根在 sys.path，便于以 `python benchmarks/run_benchmarks.py` 直接运行
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 硬指标阈值（赛题要求；与 tests/test_metrics.py 保持一致）
PREFERENCE_ACCURACY_TARGET = 0.85
RETRIEVAL_RECALL_TARGET = 0.85
LATENCY_BUDGET_MS = 500
CONFLICT_CORRECTNESS_TARGET = 0.88

ROOT = _ROOT
RESULTS_DIR = ROOT / "benchmarks" / "results"
DATASETS_DIR = ROOT / "benchmarks" / "datasets"


# ---------------------------------------------------------------------------
# 工具：确定性向量（组件级基线，仅用于可重复回归，不代表真实语义）
# ---------------------------------------------------------------------------


def _hash_embedding(text: str, dim: int = 48) -> list[float]:
    vec = [0.0] * dim
    normalized = text.lower()
    for i in range(len(normalized) - 1):
        pair = normalized[i : i + 2]
        vec[abs(hash(pair)) % dim] += 1.0
    return vec


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---------------------------------------------------------------------------
# 数据集（组件级基线，对齐 tests/test_metrics.py；可被 jsonl 扩展）
# ---------------------------------------------------------------------------


def _preference_cases() -> list[tuple[str, str, str]]:
    """(content, source_type, expected) — 中文偏好/知识标注。"""
    return [
        ("我喜欢用中文回答", "conversation", "preference"),
        ("用户偏好暗色主题界面", "manual_config", "preference"),
        ("她习惯把下载目录放 D 盘", "behavior", "preference"),
        ("不希望自动更新软件", "conversation", "preference"),
        ("我喜欢 Python，不喜欢 Java", "conversation", "preference"),
        ("我讨厌吃榴莲", "conversation", "preference"),
        ("Ubuntu 安装 ROS2 的方法", "conversation", "knowledge"),
        ("如何配置国内 pip 镜像", "conversation", "knowledge"),
        ("SSH 免密登录配置步骤", "tool_result", "knowledge"),
        ("什么是向量数据库", "conversation", "knowledge"),
        ("安装依赖失败时换国内源", "tool_result", "knowledge"),
        ("theme_dark=true", "manual_config", "preference"),
        ("exit_code=1", "tool_result", "knowledge"),
    ]


def _retrieval_corpus() -> tuple[list[str], list[tuple[str, str]]]:
    """返回 (documents, [(query, relevant_content)])。"""
    documents = [
        "pip 安装超时时应使用国内镜像源",
        "Ubuntu 系统使用 apt 安装 ROS2 的完整步骤",
        "SSH 免密登录需要将公钥追加到 authorized_keys",
        "Python 虚拟环境使用 conda 创建与激活",
        "Docker 容器端口映射使用 -p 参数",
        "Git 回退到上一版本使用 git reset",
        "麒麟系统开启防火墙需要配置 iptables",
        "Linux 查看磁盘占用使用 df -h",
        "NVIDIA 驱动安装后需要重启才能生效",
        "Kubernetes 部署应用需要编写 deployment.yaml",
    ]
    relevant = [
        ("pip 镜像怎么设置", "pip 安装超时时应使用国内镜像源"),
        ("ROS2 安装教程", "Ubuntu 系统使用 apt 安装 ROS2 的完整步骤"),
        ("SSH 免密配置", "SSH 免密登录需要将公钥追加到 authorized_keys"),
        ("conda 环境怎么创建", "Python 虚拟环境使用 conda 创建与激活"),
        ("Docker 端口映射", "Docker 容器端口映射使用 -p 参数"),
    ]
    # 扩展：若存在 knowledge_retrieval.jsonl，追加其条目作为文档与查询
    jsonl = DATASETS_DIR / "knowledge_retrieval.jsonl"
    if jsonl.exists():
        import json as _json

        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            content = row.get("relevant_content") or row.get("content")
            if content and content not in documents:
                documents.append(content)
            q = row.get("query")
            if q and content:
                relevant.append((q, content))
    return documents, relevant


def _memory_item(content, *, source_type="conversation", confidence=0.9, updated_at=None):
    from datetime import datetime as _dt, timedelta as _td

    from src.common.models import MemoryItem, MemoryLayer, MemoryType

    kwargs = {}
    if updated_at is not None:
        kwargs["updated_at"] = updated_at
    return MemoryItem(
        id=f"m-{abs(hash(content)) % 100000}",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.LONG,
        content=content,
        user_id="bench",
        metadata={"source_type": source_type, "confidence": confidence, "preference_key": "reply_language"},
        **kwargs,
    )


def _conflict_cases():
    from datetime import datetime as _dt

    from src.core.conflict_resolver import ConflictAction

    return [
        (
            _memory_item("喜欢中文", source_type="conversation", confidence=0.8),
            _memory_item("喜欢英文", source_type="manual_config", confidence=0.9),
            ConflictAction.REPLACE,
        ),
        (
            _memory_item("默认语言中文", source_type="manual_config", updated_at=_dt(2026, 1, 1)),
            _memory_item("默认语言英文", source_type="manual_config", updated_at=_dt(2026, 1, 2)),
            ConflictAction.REPLACE,
        ),
        (
            _memory_item("回复用中文", source_type="conversation", confidence=0.9),
            _memory_item("回复用英文", source_type="behavior", confidence=0.6),
            ConflictAction.KEEP_OLD,
        ),
        (
            _memory_item("字体大一点", source_type="conversation"),
            _memory_item("字体小一点", source_type="conversation"),
            ConflictAction.REPLACE,
        ),
        (
            _memory_item("主题暗色", source_type="conversation"),
            _memory_item("主题暗色", source_type="conversation"),
            ConflictAction.KEEP_OLD,
        ),
        (
            _memory_item("下载到 D 盘", source_type="conversation"),
            _memory_item("下载到 E 盘", source_type="manual_config", confidence=0.9),
            ConflictAction.REPLACE,
        ),
    ]


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


def _run_preference_accuracy() -> dict:
    from src.common.models import DataSource
    from src.core.classifier import MemoryClassifier

    classifier = MemoryClassifier()
    cases = _preference_cases()
    correct = 0
    for content, source_type, expected in cases:
        record = _make_normalized(content, source_type)
        if classifier.classify(record).value == expected:
            correct += 1
    acc = correct / len(cases)
    return {
        "name": "偏好提取准确率",
        "value": round(acc, 4),
        "target": PREFERENCE_ACCURACY_TARGET,
        "passed": acc >= PREFERENCE_ACCURACY_TARGET,
        "sample_size": len(cases),
        "detail": f"{correct}/{len(cases)}",
    }


def _make_normalized(content, source_type):
    from src.common.models import NormalizedRecord

    return NormalizedRecord(
        type=source_type,
        content=content,
        user_id="bench",
        source=__import__("src.common.models", fromlist=["DataSource"]).DataSource.CONVERSATION,
        metadata={},
    )


def _run_retrieval_recall(real: bool = False) -> dict:
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    documents, relevant = _retrieval_corpus()
    client = InMemoryMilvusClient()
    for idx, doc in enumerate(documents):
        client.insert(memory_id=f"k-{idx}", content=doc, embedding=_hash_embedding(doc), metadata={"user_id": "bench"})

    hits_at5 = 0
    hits_at1 = 0
    latencies: list[float] = []
    for query, relevant_content in relevant:
        start = time.perf_counter()
        results = client.search(_hash_embedding(query), filters={"user_id": "bench"}, top_k=5)
        latencies.append((time.perf_counter() - start) * 1000)
        top_contents = [hit.get("content") for hit in results]
        if relevant_content in top_contents:
            hits_at5 += 1
        if top_contents and top_contents[0] == relevant_content:
            hits_at1 += 1

    n = len(relevant)
    recall = hits_at5 / n
    hit1 = hits_at1 / n
    return {
        "name": "知识检索召回率",
        "recall_at_5": round(recall, 4),
        "hit_at_1": round(hit1, 4),
        "target": RETRIEVAL_RECALL_TARGET,
        "passed": recall >= RETRIEVAL_RECALL_TARGET,
        "sample_size": n,
        "latency_p50_ms": round(_percentile(latencies, 50), 3),
        "latency_p95_ms": round(_percentile(latencies, 95), 3),
        "latency_avg_ms": round(statistics.fmean(latencies), 3),
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "latency_passed": _percentile(latencies, 95) <= LATENCY_BUDGET_MS,
        "mode": "real" if real else "baseline",
    }


def _run_latency() -> dict:
    """纯检索端到端延迟（组件级基线，100 次平均）。"""
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    documents, _ = _retrieval_corpus()
    client = InMemoryMilvusClient()
    for idx, doc in enumerate(documents):
        client.insert(memory_id=f"l-{idx}", content=doc, embedding=_hash_embedding(doc), metadata={"user_id": "bench"})
    query_vec = _hash_embedding("pip 镜像")
    iters = 100
    start = time.perf_counter()
    for _ in range(iters):
        client.search(query_vec, filters={"user_id": "bench"}, top_k=5)
    avg = (time.perf_counter() - start) * 1000 / iters
    return {
        "name": "检索响应时间",
        "avg_ms": round(avg, 3),
        "budget_ms": LATENCY_BUDGET_MS,
        "passed": avg <= LATENCY_BUDGET_MS,
        "iterations": iters,
        "note": "组件级基线（内存向量）；真机 P95 见 _run_retrieval_recall(real=True)",
    }


def _run_conflict_correctness() -> dict:
    from src.core.conflict_resolver import ConflictResolver

    resolver = ConflictResolver()
    cases = _conflict_cases()
    correct = 0
    for old, new, expected in cases:
        if not resolver.detect(old, new):
            if expected is __import__("src.core.conflict_resolver", fromlist=["ConflictAction"]).ConflictAction.KEEP_OLD:
                correct += 1
            continue
        if resolver.resolve(old, new).action == expected:
            correct += 1
    acc = correct / len(cases)
    return {
        "name": "冲突处理正确率",
        "value": round(acc, 4),
        "target": CONFLICT_CORRECTNESS_TARGET,
        "passed": acc >= CONFLICT_CORRECTNESS_TARGET,
        "sample_size": len(cases),
        "detail": f"{correct}/{len(cases)}",
    }


# ---------------------------------------------------------------------------
# 真机模式（--real）：经原生 create_memory / query_memory 接口
# ---------------------------------------------------------------------------


def _run_real_retrieval() -> dict | None:
    """在配置的后端（真机应为 kylin）上播种并检索，产出真实召回/延迟。"""
    try:
        from src.knowledge.memory_service.memory_schema import (
            CreateMemoryRequest,
            MemoryEntry,
            MemorySource,
            MemoryTypeTag,
            QueryMemoryRequest,
            TaskType,
        )
        from src.knowledge.memory_service.create_memory import create_memory
        from src.knowledge.memory_service.query_memory import query_memory
    except Exception as exc:  # pragma: no cover - 真机依赖
        print(f"[warn] --real 模式依赖不可用，跳过真机检索：{exc}")
        return None

    documents, relevant = _retrieval_corpus()
    user_id = "bench_real"
    for idx, doc in enumerate(documents):
        entry = MemoryEntry(
            content=doc,
            memory_type=MemoryTypeTag.TOOL_CALL,
            task_type=TaskType.CODING,
            source=MemorySource.MANUAL,
            user_id=user_id,
            confidence=0.9,
        )
        create_memory(CreateMemoryRequest(entry=entry))

    hits_at5 = 0
    hits_at1 = 0
    latencies: list[float] = []
    for query, relevant_content in relevant:
        resp = query_memory(
            QueryMemoryRequest(user_id=user_id, query=query, top_k=5),
        )
        latencies.append(resp.latency_ms)
        contents = [it.get("content", "") for it in resp.items]
        if any(relevant_content in c for c in contents):
            hits_at5 += 1
        if contents and relevant_content in contents[0]:
            hits_at1 += 1
    n = len(relevant)
    recall = hits_at5 / n
    return {
        "name": "知识检索召回率(真机)",
        "recall_at_5": round(recall, 4),
        "hit_at_1": round(hits_at1 / n, 4),
        "target": RETRIEVAL_RECALL_TARGET,
        "passed": recall >= RETRIEVAL_RECALL_TARGET,
        "sample_size": n,
        "latency_p50_ms": round(_percentile(latencies, 50), 3),
        "latency_p95_ms": round(_percentile(latencies, 95), 3),
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "latency_passed": _percentile(latencies, 95) <= LATENCY_BUDGET_MS,
        "mode": "real",
    }


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def _build_report(real: bool) -> dict:
    metrics = [
        _run_preference_accuracy(),
        _run_retrieval_recall(real=real),
        _run_latency(),
        _run_conflict_correctness(),
    ]
    if real:
        real_retrieval = _run_real_retrieval()
        if real_retrieval:
            metrics.append(real_retrieval)
    return {
        "project": "麒忆智脑 / MOKOA",
        "requirement": "XA-202612 (7) 量化评测",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "real" if real else "baseline",
        "metrics": metrics,
        "thresholds": {
            "preference_accuracy": PREFERENCE_ACCURACY_TARGET,
            "retrieval_recall": RETRIEVAL_RECALL_TARGET,
            "latency_budget_ms": LATENCY_BUDGET_MS,
            "conflict_correctness": CONFLICT_CORRECTNESS_TARGET,
        },
    }


def _render_summary(report: dict) -> str:
    lines = ["# 量化评测结果摘要", "", f"- 生成时间：{report['generated_at']}", f"- 模式：{report['mode']}", ""]
    for m in report["metrics"]:
        name = m.get("name")
        passed = m.get("passed")
        mark = "[PASS]" if passed else "[FAIL]"
        if "recall_at_5" in m:
            lines.append(f"- **{name}**：Recall@5={m['recall_at_5']} / Hit@1={m['hit_at_1']} / "
                         f"P95={m.get('latency_p95_ms')}ms / 目标≥{m['target']} → {mark}")
        elif "avg_ms" in m:
            lines.append(f"- **{name}**：avg={m['avg_ms']}ms / 预算≤{m['budget_ms']}ms → {mark}")
        else:
            lines.append(f"- **{name}**：{m.get('value')} / 目标≥{m['target']} → {mark} （{m.get('detail','')}）")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="MOKOA 量化评测一键跑分")
    parser.add_argument("--real", action="store_true", help="在配置的后端（真机应为 kylin）上跑真实召回/延迟")
    args = parser.parse_args()

    report = _build_report(real=args.real)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = RESULTS_DIR / f"report_{ts}.json"
    md_path = RESULTS_DIR / f"summary_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_summary(report), encoding="utf-8")

    print(_render_summary(report))
    print(f"\n报告已写入：\n  {json_path}\n  {md_path}")
    all_passed = all(m.get("passed") for m in report["metrics"])
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
