"""端到端真实验证：跨对话记忆固化（26a9835a方向）。

跑真实代码路径：IngestionPipeline → 短期层 → consolidate(短→中语义合并 / 中→长抽象)
→ 结构化偏好画像(LPM) 回写。向量存储用 InMemoryMilvusClient（与默认工厂一致），
embedding 用本地真实适配器，LLM 抽象在 SILICONFLOW_API_KEY 就绪时启用真实模型，
否则走确定性规则回退（不影响主流程）。

用法：
    python scripts/validate_consolidation.py
"""

from __future__ import annotations

import os
import sys

from src.core.factory import create_orchestrator
from src.common.models import DataSource


def _dump(item) -> dict:
    return {
        "content": item.content,
        "type": item.memory_type.value,
        "meta": {k: item.metadata.get(k) for k in (
            "preference_key", "preference_value", "mid_merged",
            "consolidated_from", "abstracted", "scene_id",
        ) if k in item.metadata},
    }


def main() -> int:
    orch = create_orchestrator()

    # 真实验证：使用放宽后的真实默认阈值（heat_threshold=3.0, quality_threshold=0.46），
    # 不再强制覆盖，以验证新默认下碎片能正常进入固化。
    lm = orch.layer_manager
    print(f"  [setup] 真实默认阈值 heat={lm.heat_threshold} quality={lm.quality_threshold} "
          f"similarity={lm.similarity_threshold}")

    # 探测真实 LLM 抽象能力
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if api_key:
        print("[setup] 检测到 SILICONFLOW_API_KEY → 启用中期→长期真实 LLM 抽象")
        lm._consolidator.use_llm = True
        lm._consolidator.llm_client = orch.preference.llm_client
    else:
        print("[setup] 未检测到 SILICONFLOW_API_KEY → LLM 抽象关闭（走最新值回退）")

    # 跨多个伪会话摄入（conversation 源 → 触发分类 + 提取 + 短期写入）
    # run_tag 避免 StagingStore 跨进程持久化去重把重复文本判为重复（真实行为，验证时需绕过）。
    import uuid
    run_tag = uuid.uuid4().hex[:8]
    # 每轮独立 uid：偏好索引持久化到磁盘，独立 uid 避免跨轮版本号累积干扰判读。
    uid = f"validate_{run_tag}"
    sessions = [
        # 会话1：语言偏好 + 数据处理工具（规则可命中 data_processing_tool:python）
        f"[{run_tag}] 我平时用 Python 做数据分析",
        f"[{run_tag}] 请用英文回复我",
        # 会话2（跨会话同主题）：应被语义合并进同一条中期记忆
        f"[{run_tag}] 数据分析我更喜欢用 Python 脚本处理",
        # 会话3：偏好漂移（reply_language 由 en-US 改为 zh-CN，应被近因覆盖）
        f"[{run_tag}] 还是用中文回复吧",
        # 会话4：一条事实型知识（无偏好键，走知识路径）
        f"[{run_tag}] 我们公司总部在北京",
    ]

    print("\n=== 1) 摄入 (process_input) ===")
    for i, text in enumerate(sessions, 1):
        try:
            res = orch.process_input(
                {"content": text},
                "conversation",
                user_id=uid,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [会话{i}] 摄入异常: {exc!r}")
            return 2
        pk = res.preference_keys
        print(f"  [会话{i}] accepted={res.accepted} type={res.memory_type} "
              f"quality={res.quality_score} pref_keys={pk} "
              f"evicted={res.evicted_memory_id}")

    short = orch.layer_manager.read_short(uid)
    print(f"\n  短期层条数: {len(short)}")
    print("  [debug] 短期 bucket 键:", list(orch.layer_manager._short._buckets.keys()))
    for it in short:
        print("   -", "uid=", it.user_id, _dump(it))

    print("\n=== 2) consolidate (短→中→长) ===")
    print("  [debug] heat_threshold=", lm.heat_threshold,
          " quality_threshold=", lm.quality_threshold)
    cands = lm._short.candidates_for_mid(uid, lm.heat_threshold)
    print("  [debug] candidates_for_mid 数量=", len(cands),
          " 各自 heat=", [round(float(c.metadata.get('heat', 0)), 3) for c in cands])
    pm = lm.promote_to_mid(uid)
    print("  [debug] promote_to_mid 返回条数=", len(pm),
          " short_count=", lm.short_count(uid), " mid_count=", lm.mid_count(uid))
    plong, retained = lm.promote_to_long(uid)
    print("  [debug] promote_to_long 返回=", len(plong),
          " retained=", len(retained), " mid_count=", lm.mid_count(uid),
          " long_count=", lm.long_count(uid))
    for it in plong:
        print("   [长期项] content=", repr(it.content[:80]),
              " key=", it.metadata.get("preference_key"),
              " abstracted=", it.metadata.get("abstracted"),
              " mid_merged=", it.metadata.get("mid_merged"))
    # 复刻 consolidate 的持久化（仅用于验证画像回写）
    stored_ids = []
    for item in plong:
        if item.memory_type.value == "preference" and not str(item.metadata.get("preference_key") or "").strip():
            continue
        stored = orch.resolve_and_store(item)
        mid = stored.metadata.get("milvus_memory_id")
        if isinstance(mid, str) and mid:
            stored_ids.append(mid)
            lm.release_long_pending(uid, item.id)
    result = type("R", (), {
        "user_id": uid, "promoted_to_mid": len(pm), "promoted_to_long": len(plong),
        "retained_in_mid": len(retained), "short_remaining": lm.short_count(uid),
        "mid_remaining": lm.mid_count(uid), "stored_memory_ids": stored_ids,
    })()
    print("  ", vars(result))

    mid = orch.layer_manager.read_mid(uid)
    print(f"\n  中期层条数: {len(mid)}")
    for it in mid:
        print("   -", _dump(it))

    print("\n=== 3) 结构化偏好画像 (LPM) 回读 ===")
    try:
        recs = orch.preference.index.all_records(uid)
    except Exception as exc:  # noqa: BLE001
        print(f"  [画像读取异常] {exc!r}")
        recs = []
    if not recs:
        print("  (空) — 偏好未回写，需排查 extract/store 路径")
    for r in recs:
        print(f"   - key={r.preference_key} value={r.value} "
              f"scope={getattr(r, 'scope', None)} "
              f"ver={getattr(r, 'current_version', None)} "
              f"latest={getattr(r, 'is_latest', None)} "
              f"conf={getattr(r, 'confidence', None)}")

    print("\n=== 4) 真值断言 ===")
    problems = []
    keys = {r.preference_key for r in recs}

    def _has_key(suffix: str) -> bool:
        return any(k == suffix or k.endswith("." + suffix) for k in keys)

    def _record_for(suffix: str):
        return next(
            (r for r in recs if r.preference_key == suffix or str(r.preference_key).endswith("." + suffix)),
            None,
        )

    if not _has_key("data_processing_tool"):
        problems.append("未回写 data_processing_tool:python（同主题跨会话合并链路断裂）")
    lang = _record_for("reply_language")
    if lang is None:
        problems.append("未回写 reply_language（漂移覆盖链路断裂）")
    elif str(lang.value) != "zh-CN":
        problems.append(f"reply_language 漂移未生效，期望 zh-CN 实为 {lang.value!r}")

    # 晋升后中期池已空；合并标记在长期代表项或晋升前的 mid_merged 上。
    merged = any(it.metadata.get("mid_merged") for it in plong) or any(
        it.metadata.get("mid_merged") for it in mid
    )
    if not merged:
        problems.append("晋升结果未出现 mid_merged（跨会话语义合并未触发）")

    if problems:
        print("  ❌ 发现问题:")
        for p in problems:
            print("   -", p)
        return 1
    print("  ✅ 核心链路正常：跨对话合并 + 偏好漂移覆盖 + LPM 回写 均成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
