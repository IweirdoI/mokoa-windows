"""Memory consolidation — cross-conversation 固化 (26a9835a/efca9634 协作方向). Owner: efca9634.

参考 MemoryOS memoryos-pypi/updater.py 与 TiMem 的时间分层固化思路：

- ``consolidate_short_to_mid``：对一批待晋升的短期记忆做 **语义聚类 + 合并**，把跨对话的
  同义碎片合成一条，减少中期冗余（替代逐条原样搬运）。
- ``abstract_mid_to_long``：对中期池中同 identity / 同偏好键的碎片做 **抽象提炼**，
  可选调用 LLM 生成一条稳定长期记忆（带版本/置信度）；无 LLM 时回退为保留最新值。

所有 LLM 调用均 try/except 包裹，失败不影响主链路；写入节流：仅在聚类规模 ≥ 2 时尝试
LLM 抽象，避免每轮对话高频调用模型。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.common.models import MemoryItem, MemoryLayer
from src.core.conflict_resolver import merge_identity_key, same_feature_identity
from src.core.mid_term import MidTermMemory
from src.core.short_term import ShortTermMemory


def _ts(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


class MemoryConsolidator:
    """把多次对话产生的碎片合并 / 抽象成稳定记忆。"""

    def __init__(
        self,
        similarity_threshold: float = 0.7,
        keyword_alpha: float = 0.3,
        use_llm: bool = False,
        llm_client: Any | None = None,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.keyword_alpha = keyword_alpha
        self.use_llm = use_llm
        self.llm_client = llm_client
        # 复用中期的语义相似度实现（embedding 余弦 + 关键词 Jaccard）。
        self._sim = MidTermMemory(
            capacity=1,
            similarity_threshold=similarity_threshold,
            keyword_alpha=keyword_alpha,
        )

    # ---------------------------------------------------------- short -> mid
    def consolidate_short_to_mid(
        self, items: list[MemoryItem]
    ) -> tuple[list[MemoryItem], list[str]]:
        """语义聚类合并。返回 ``(representatives, removed_ids)``。

        ``removed_ids`` 包含每个聚类内所有原始 id（含代表项），调用方据此从短期池删除。
        """

        clusters: list[list[MemoryItem]] = []
        for item in items:
            target = self._find_cluster(clusters, item)
            if target is None:
                clusters.append([item])
            else:
                target.append(item)

        representatives: list[MemoryItem] = []
        removed: list[str] = []
        for cluster in clusters:
            if len(cluster) == 1:
                representatives.append(cluster[0])
                removed.append(cluster[0].id)
            else:
                representatives.append(self._merge_cluster(cluster))
                removed.extend(c.id for c in cluster)
        return representatives, removed

    def _find_cluster(
        self, clusters: list[list[MemoryItem]], item: MemoryItem
    ) -> list[MemoryItem] | None:
        for cluster in clusters:
            rep = cluster[0]
            if self._same_identity(rep, item):
                return cluster
            if self._similar(rep, item) >= self.similarity_threshold:
                return cluster
        return None

    def _same_identity(self, a: MemoryItem, b: MemoryItem) -> bool:
        # 主题身份优先：指纹每次摄入都变，不能挡住同一 preference_key 的跨对话合并。
        if same_feature_identity(
            a.metadata,
            b.metadata,
            left_scene=a.scene_id,
            right_scene=b.scene_id,
        ):
            return True
        left = merge_identity_key(a.metadata)
        right = merge_identity_key(b.metadata)
        if left and right:
            return left == right
        return False

    def _similar(self, a: MemoryItem, b: MemoryItem) -> float:
        try:
            return self._sim.similarity_score(a, b)
        except Exception:
            from difflib import SequenceMatcher

            left = (a.content or "").lower()
            right = (b.content or "").lower()
            return SequenceMatcher(None, left, right).ratio()

    def _merge_cluster(self, cluster: list[MemoryItem]) -> MemoryItem:
        base = max(
            cluster,
            key=lambda it: (
                float(it.metadata.get("heat", 0)),
                float(it.metadata.get("quality_score", 0) or 0),
                _ts(it.created_at),
            ),
        )
        merged_metadata = dict(base.metadata)
        qualities = [
            float(c.metadata.get("quality_score"))
            for c in cluster
            if isinstance(c.metadata.get("quality_score"), (int, float))
        ]
        if qualities:
            merged_metadata["quality_score"] = max(qualities)
        merged_metadata["access_count"] = sum(
            int(c.metadata.get("access_count", 1)) for c in cluster
        )
        merged_metadata["mid_merged"] = True
        merged_metadata["consolidated_from"] = [
            c.id for c in cluster if c.id != base.id
        ]
        return base.model_copy(
            update={
                "layer": MemoryLayer.MID,
                "version": max(c.version for c in cluster) + 1,
                "metadata": merged_metadata,
                "updated_at": datetime.utcnow(),
            }
        )

    # ---------------------------------------------------------- mid -> long
    def abstract_mid_to_long(self, items: list[MemoryItem]) -> list[MemoryItem]:
        """对中期碎片按 identity / 偏好键聚类，可选 LLM 抽象为稳定长期记忆。

        返回晋升用的代表条目（内容可能已被 LLM 抽象）。无 LLM 或失败时保留最新值，
        不改变条目数量，因此不影响晋升计数类测试。
        """

        clusters: list[list[MemoryItem]] = []
        for item in items:
            target = None
            for cluster in clusters:
                if self._same_identity(cluster[0], item):
                    target = cluster
                    break
            if target is None:
                clusters.append([item])
            else:
                target.append(item)

        result: list[MemoryItem] = []
        for cluster in clusters:
            if len(cluster) == 1:
                item = cluster[0]
                # 已合并的中期碎片（跨对话固化产物）也值得用 LLM 提炼成稳定陈述，
                # 对应 MemoryOS 由热段分析更新长期画像。
                if item.metadata.get("mid_merged"):
                    abstracted = self._llm_abstract([item])
                    if abstracted:
                        # 安全：抽象结果只作为补充摘要，不覆盖规范正文（保留用户原话），
                        # 避免模型不稳定时把废话/提示词回显写进长期记忆。
                        merged_metadata = dict(item.metadata)
                        merged_metadata["abstracted"] = True
                        merged_metadata["abstracted_summary"] = abstracted
                        result.append(
                            item.model_copy(
                                update={
                                    "metadata": merged_metadata,
                                    "updated_at": datetime.utcnow(),
                                }
                            )
                        )
                        continue
                result.append(item)
                continue
            base = max(cluster, key=lambda it: _ts(it.created_at))
            abstracted = self._llm_abstract(cluster)
            if abstracted:
                merged_metadata = dict(base.metadata)
                merged_metadata["consolidated_from"] = [
                    c.id for c in cluster if c.id != base.id
                ]
                merged_metadata["abstracted"] = True
                merged_metadata["abstracted_summary"] = abstracted
                result.append(
                    base.model_copy(
                        update={
                            "metadata": merged_metadata,
                            "updated_at": datetime.utcnow(),
                        }
                    )
                )
            else:
                result.append(base)
        return result

    def _llm_abstract(self, cluster: list[MemoryItem]) -> str | None:
        if not self.use_llm or self.llm_client is None:
            return None
        texts = "\n".join(f"- {c.content}" for c in cluster if c.content)
        if not texts.strip():
            return None
        try:
            messages = [
                {
                    "role": "user",
                    "content": (
                        "你负责把用户的记忆片段归纳为长期偏好/事实。请只输出【一句话】稳定陈述，"
                        "必须严格保留原片段的核心含义，不得引入原片段没有的新信息、不得举例、"
                        "不得解释或发挥：\n" + texts
                    ),
                }
            ]
            raw = self.llm_client.chat_completion(
                messages, temperature=0.0, max_tokens=80
            )
            if raw and not str(raw).startswith("Error:"):
                cleaned = str(raw).strip().strip("“”\"'")
                # 安全护栏：过长、含提示词回显、空泛时退回原始代表内容，避免污染长期记忆。
                if 0 < len(cleaned) <= 120 and not self._looks_like_echo(cleaned):
                    return cleaned
            return None
        except Exception:
            return None

    @staticmethod
    def _looks_like_echo(text: str) -> bool:
        """抽象结果疑似把提示词原样回显（模型不稳定时常见），应丢弃。"""
        markers = (
            "你负责", "原始片段", "请只输出", "不要解释", "不要举例",
            "稳定陈述", "一句话", "不得引入", "不要发挥", "长期偏好",
        )
        return any(m in text for m in markers)
