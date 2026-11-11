"""Short / mid / long-term memory flow."""

from __future__ import annotations

from src.common.interfaces import MemoryLayerManager
from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.consolidator import MemoryConsolidator
from src.core.mid_term import MidTermMemory, MidTermStats
from src.core.short_term import ShortTermMemory, ShortTermStats, WriteShortResult


class LayerManager(MemoryLayerManager):
    """
    Owner: efca9634
    Reference: MemoryOS (short_term.py, mid_term.py, long_term.py, updater.py)

    Acceptance:
      - Short-term capacity configurable
      - Mid-term promotion by heat/similarity threshold
      - Long-term split into preference vs knowledge routing

    TODO: Optional SQLite memory_schema backend (coordinate with 3fba60e1).
    """

    def __init__(
        self,
        short_capacity: int = 10,
        mid_capacity: int = 50,
        heat_threshold: float = 3.0,
        quality_threshold: float = 0.5,
        similarity_threshold: float = 0.7,
        keyword_alpha: float = 0.3,
        recency_tau_hours: float = 168.0,
        recency_gamma: float = 1.0,
        consolidator: "MemoryConsolidator | None" = None,
    ) -> None:
        self.short_capacity = short_capacity
        self.mid_capacity = mid_capacity
        self.heat_threshold = heat_threshold
        self.quality_threshold = quality_threshold
        self.similarity_threshold = similarity_threshold
        self.keyword_alpha = keyword_alpha
        self.recency_tau_hours = recency_tau_hours
        self.recency_gamma = recency_gamma
        self._short = ShortTermMemory(capacity=short_capacity)
        self._mid = MidTermMemory(
            capacity=mid_capacity,
            similarity_threshold=similarity_threshold,
            keyword_alpha=keyword_alpha,
            recency_tau_hours=recency_tau_hours,
            recency_gamma=recency_gamma,
        )
        self._consolidator = consolidator or MemoryConsolidator(
            similarity_threshold=similarity_threshold,
            keyword_alpha=keyword_alpha,
        )
        self._long: dict[str, list[MemoryItem]] = {}

    def write_short(self, item: MemoryItem) -> WriteShortResult:
        return self._short.write(item)

    def read_short(
        self,
        user_id: str,
        *,
        limit: int = 10,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        return self._short.read(
            user_id,
            limit=limit,
            scene_id=scene_id,
            memory_type=memory_type,
        )

    def read_mid(
        self,
        user_id: str,
        *,
        limit: int = 20,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        return self._mid.read(
            user_id,
            limit=limit,
            scene_id=scene_id,
            memory_type=memory_type,
            newest_first=False,
        )

    def get_short(self, memory_id: str) -> MemoryItem | None:
        return self._short.get(memory_id)

    def get_mid(self, memory_id: str) -> MemoryItem | None:
        return self._mid.get(memory_id)

    def touch_short(self, memory_id: str) -> MemoryItem | None:
        return self._short.touch(memory_id)

    def delete_short(self, user_id: str, memory_id: str) -> bool:
        return self._short.delete(user_id, memory_id)

    def delete_mid(self, user_id: str, memory_id: str) -> bool:
        return self._mid.delete(user_id, memory_id)

    def short_stats(self, user_id: str) -> ShortTermStats:
        return self._short.stats(user_id)

    def mid_stats(self, user_id: str) -> MidTermStats:
        return self._mid.stats(user_id)

    def promote_to_mid(self, user_id: str) -> list[MemoryItem]:
        """将 heat 达标的短期记忆晋升到中期；晋升前先做跨对话语义聚类合并。"""

        candidates = self._short.candidates_for_mid(user_id, self.heat_threshold)
        if not candidates:
            return []

        # 跨对话固化：把同义/同身份碎片先合并成一条，减少中期冗余。
        if self._consolidator is not None:
            representatives, removed_ids = self._consolidator.consolidate_short_to_mid(
                candidates
            )
        else:
            representatives, removed_ids = candidates, [c.id for c in candidates]

        # 只删除实际晋升项。旧实现 pop_all 后用 write() 塞回未晋升项，
        # 会把它们当成一次新访问，进而重算 access_count / heat，导致每次
        # consolidate 都给冷记忆“升温”。
        for memory_id in removed_ids:
            self._short.delete(user_id, memory_id)

        promoted: list[MemoryItem] = []
        for item in representatives:
            item.layer = MemoryLayer.MID
            promoted.append(item)

        if promoted:
            self._mid.extend(user_id, promoted)
        return promoted

    def promote_to_long(self, user_id: str) -> tuple[list[MemoryItem], list[MemoryItem]]:
        """将中期记忆晋升到长期待入库队列；仅 quality_score 达标的条目进入 _long。

        晋升前先对中期碎片做固化（语义聚类 / 可选 LLM 抽象），把跨对话累积的
        同主题碎片提炼成稳定长期记忆，而非逐条原样搬运。
        """

        mid_bucket = self._mid.pop_all(user_id)
        if not mid_bucket:
            return [], []

        if self._consolidator is not None:
            mid_bucket = self._consolidator.abstract_mid_to_long(mid_bucket)

        promoted: list[MemoryItem] = []
        retained: list[MemoryItem] = []
        for item in mid_bucket:
            if MidTermMemory.meets_quality_gate(item, self.quality_threshold):
                item.layer = MemoryLayer.LONG
                promoted.append(item)
            else:
                item.layer = MemoryLayer.MID
                retained.append(item)

        if promoted:
            self._long.setdefault(user_id, []).extend(promoted)
        if retained:
            self._mid.extend(user_id, retained)
        return promoted, retained

    def read(self, layer: MemoryLayer, user_id: str, limit: int = 10) -> list[MemoryItem]:
        if layer == MemoryLayer.SHORT:
            return self._short.read(user_id, limit=limit, newest_first=False)
        if layer == MemoryLayer.MID:
            return self._mid.read(user_id, limit=limit, newest_first=False)
        return self._long.get(user_id, [])[:limit]

    def mid_count(self, user_id: str) -> int:
        return self._mid.count(user_id)

    def short_count(self, user_id: str) -> int:
        return self._short.stats(user_id).count

    def long_count(self, user_id: str) -> int:
        return len(self._long.get(user_id, []))

    def release_long_pending(self, user_id: str, memory_id: str) -> bool:
        """Milvus 入库成功后，从待持久化队列移除对应条目。"""

        bucket = self._long.get(user_id)
        if not bucket:
            return False
        for index, item in enumerate(bucket):
            if item.id == memory_id:
                bucket.pop(index)
                if not bucket:
                    self._long.pop(user_id, None)
                return True
        return False

    def delete_long(self, user_id: str, memory_id: str) -> bool:
        """从待持久化队列删除（遗忘链路使用）。"""

        return self.release_long_pending(user_id, memory_id)

    def long_stats(self, user_id: str) -> MidTermStats:
        """待持久化队列统计（Milvus 入库前；成功 consolidate 后应为 0）。"""

        items = self._long.get(user_id, [])
        heats = [float(item.metadata.get("heat", 0)) for item in items]
        avg_heat = round(sum(heats) / len(heats), 4) if heats else 0.0
        return MidTermStats(
            user_id=user_id,
            count=len(items),
            capacity=0,
            preference_count=sum(1 for item in items if item.memory_type == MemoryType.PREFERENCE),
            knowledge_count=sum(1 for item in items if item.memory_type == MemoryType.KNOWLEDGE),
            avg_heat=avg_heat,
        )
