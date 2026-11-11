"""Multi-dimensional save/update/delete policy. Owner: cb0aeabe (rules) + 26a9835a/2abac5a6 (input signals)."""

from __future__ import annotations

from src.knowledge.memory_service.memory_schema import MemoryEntry, MemoryTypeTag


class MemoryPolicy:
    """
    是否保存 = 长期价值 + 任务相关性 + 用户明确性 + 复用概率 + 置信度
              - 隐私风险 - 过期风险

    上下游分工:
      - 3fba60e1 ingestion/quality.py: 初筛
      - 26a9835a preference: 产出候选 + confidence
      - 2abac5a6 security: 隐私风险、显式删除信号
      - cb0aeabe memory_policy: 入库前最终评价（本模块）
    """

    # 入库/持久化决策阈值：score_save 综合评分达到该值才写入长期记忆。
    # 取值来源：离线评估校准 —— 偏好/反馈等高价值信号叠加后，普通置信度条目约
    # 0.46 以上才值得长期占用向量库空间；低于则仅留短期，避免噪声沉淀。
    SAVE_THRESHOLD = 0.46

    # 各信号对长期保存评分的相对权重（仅语义权重，非绝对概率；最终裁剪到 [0,1]）。
    _PREF_BONUS = 0.15              # 偏好类：用户明确表达的稳定意图，优先保留
    _FEEDBACK_BONUS = 0.1           # 反馈类：用户纠偏/确认，高价值信号
    _EXPLICIT_SOURCE_BONUS = 0.1    # 来源 chat/manual：用户主动、明确提供
    _PRIVACY_PENALTY = 0.5          # 隐私风险：宁可错删也不长期留存敏感内容
    _TEMPORARY_PENALTY = 0.4        # 临时内容：一次性，不占长期容量
    _SCORE_FLOOR = 0.0              # 评分下限
    _SCORE_CEIL = 1.0               # 评分上限

    def score_save(self, entry: MemoryEntry) -> float:
        score = entry.confidence  # 初值：条目自身置信度
        if entry.memory_type == MemoryTypeTag.PREFERENCE:
            score += self._PREF_BONUS
        if entry.memory_type == MemoryTypeTag.FEEDBACK:
            score += self._FEEDBACK_BONUS  # 反馈类高价值
        if entry.source.value in ("chat", "manual"):
            score += self._EXPLICIT_SOURCE_BONUS  # 用户明确性
        if entry.metadata.get("privacy_risk"):
            score -= self._PRIVACY_PENALTY
        if entry.metadata.get("temporary"):
            score -= self._TEMPORARY_PENALTY
        # 裁剪到 [0,1]，避免多信号叠加后越界
        return max(self._SCORE_FLOOR, min(self._SCORE_CEIL, score))

    def should_save(self, entry: MemoryEntry) -> tuple[bool, float, str]:
        s = self.score_save(entry)
        if entry.memory_type == MemoryTypeTag.FEEDBACK and "不应" in entry.content:
            # 用户明确否定/修正信号，直接保留，后续由删除链或修正规则处理。
            return True, s, "反馈类：需写入并触发下游删除/修正规则"

        if s >= self.SAVE_THRESHOLD:
            return True, s, "评分达阈值"
        return False, s, "长期价值/置信度不足"

    def should_update(self, old_content: str, new_content: str) -> bool:
        return old_content.strip() != new_content.strip()

    def should_merge(self, existing_type: str, new_type: str) -> bool:
        """同类合并：同一 user + memory_type + task_type 偏好不存十条."""
        return existing_type == new_type
