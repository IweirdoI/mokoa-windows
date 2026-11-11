"""Short-term memory store — session-level working memory. Owner: efca9634."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.conflict_resolver import merge_identity_key


@dataclass
class WriteShortResult:
    item: MemoryItem
    evicted: MemoryItem | None = None
    updated_existing: bool = False


@dataclass
class ShortTermStats:
    user_id: str
    count: int
    capacity: int
    preference_count: int
    knowledge_count: int


class ShortTermMemory:
    """进程内短期记忆池。

    多线程（FastAPI 线程池）并发写入同一 user_id 时，_buckets/_items_by_id 的
    多步改写若不加锁会覆盖或丢失写入、甚至破坏列表结构。所有读写方法统一用
    一把 RLock 串行化，避免静默数据丢失。
    """

    def __init__(self, capacity: int = 10) -> None:
        if capacity < 1:
            raise ValueError("short-term capacity must be >= 1")
        self.capacity = capacity
        self._buckets: dict[str, list[MemoryItem]] = {}
        self._items_by_id: dict[str, MemoryItem] = {}
        self._lock = threading.RLock()

    def write(self, item: MemoryItem) -> WriteShortResult:
        with self._lock:
            item.layer = MemoryLayer.SHORT
            item.updated_at = datetime.utcnow()
            self._ensure_heat_fields(item)

            bucket = self._buckets.setdefault(item.user_id, [])
            identity = merge_identity_key(item.metadata)
            if identity:
                for index, existing in enumerate(bucket):
                    if merge_identity_key(existing.metadata) == identity:
                        merged = self._merge_update(existing, item)
                        bucket[index] = merged
                        self._items_by_id[merged.id] = merged
                        return WriteShortResult(item=merged, updated_existing=True)

            evicted: MemoryItem | None = None
            if len(bucket) >= self.capacity:
                evicted = bucket.pop(0)
                self._items_by_id.pop(evicted.id, None)

            item.metadata["access_count"] = 1
            item.metadata["heat"] = self._compute_heat(item)
            bucket.append(item)
            self._items_by_id[item.id] = item
            return WriteShortResult(item=item, evicted=evicted)

    def read(
        self,
        user_id: str,
        *,
        limit: int = 10,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
        newest_first: bool = True,
    ) -> list[MemoryItem]:
        with self._lock:
            indexed_items = list(enumerate(self._buckets.get(user_id, [])))
            if scene_id is not None:
                indexed_items = [
                    (index, item)
                    for index, item in indexed_items
                    if item.scene_id == scene_id
                ]
            if memory_type is not None:
                indexed_items = [
                    (index, item)
                    for index, item in indexed_items
                    if item.memory_type == memory_type
                ]

            # Insertion index is a deterministic tie-breaker because Pydantic
            # defaults may give multiple rapidly-created items the same timestamp.
            indexed_items.sort(
                key=lambda pair: (pair[1].created_at, pair[0]),
                reverse=newest_first,
            )
            return [item for _, item in indexed_items[:limit]]

    def get(self, memory_id: str) -> MemoryItem | None:
        with self._lock:
            return self._items_by_id.get(memory_id)

    def touch(self, memory_id: str) -> MemoryItem | None:
        with self._lock:
            item = self._items_by_id.get(memory_id)
            if item is None:
                return None
            item.metadata["access_count"] = int(item.metadata.get("access_count", 0)) + 1
            item.metadata["heat"] = self._compute_heat(item)
            item.updated_at = datetime.utcnow()
            return item

    def delete(self, user_id: str, memory_id: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(user_id)
            if not bucket:
                return False
            for index, item in enumerate(bucket):
                if item.id == memory_id:
                    bucket.pop(index)
                    self._items_by_id.pop(memory_id, None)
                    if not bucket:
                        self._buckets.pop(user_id, None)
                    return True
            return False

    def pop_all(self, user_id: str) -> list[MemoryItem]:
        with self._lock:
            items = self._buckets.pop(user_id, [])
            for item in items:
                self._items_by_id.pop(item.id, None)
            return items

    def stats(self, user_id: str) -> ShortTermStats:
        with self._lock:
            items = self._buckets.get(user_id, [])
            return ShortTermStats(
                user_id=user_id,
                count=len(items),
                capacity=self.capacity,
                preference_count=sum(1 for item in items if item.memory_type == MemoryType.PREFERENCE),
                knowledge_count=sum(1 for item in items if item.memory_type == MemoryType.KNOWLEDGE),
            )

    def candidates_for_mid(self, user_id: str, heat_threshold: float) -> list[MemoryItem]:
        with self._lock:
            return [
                item
                for item in self._buckets.get(user_id, [])
                if float(item.metadata.get("heat", 0)) >= heat_threshold
            ]

    @staticmethod
    def _ensure_heat_fields(item: MemoryItem) -> None:
        item.metadata.setdefault("access_count", 0)
        item.metadata.setdefault("heat", 0.0)

    @staticmethod
    def _compute_heat(item: MemoryItem) -> float:
        """热度 = 访问次数 + 质量分×5 + 置信度×1。

        加权后：一条明确且高质量的记忆（manual_config / 明确对话，
        quality≈0.75+ 且 confidence≈0.7）首次写入即可达到默认 3.0 的晋升阈值；
        普通行为或工具结果单次约 3.5~4.8 分，需重复或触达才达标，
        避免低价值内容过早晋升中期。
        """
        access = float(item.metadata.get("access_count", 0))
        quality = item.metadata.get("quality_score")
        quality_bonus = float(quality) * 5.0 if isinstance(quality, (int, float)) else 0.0
        confidence = item.metadata.get("confidence")
        confidence_bonus = float(confidence) * 1.0 if isinstance(confidence, (int, float)) else 0.0
        return round(access + quality_bonus + confidence_bonus, 4)

    @staticmethod
    def _merge_update(existing: MemoryItem, incoming: MemoryItem) -> MemoryItem:
        access_count = int(existing.metadata.get("access_count", 0)) + 1
        merged_metadata = dict(existing.metadata)
        merged_metadata.update(incoming.metadata)
        merged_metadata["access_count"] = access_count

        merged = existing.model_copy(
            update={
                "content": incoming.content,
                "memory_type": incoming.memory_type,
                "scene_id": incoming.scene_id or existing.scene_id,
                "version": existing.version + 1,
                "metadata": merged_metadata,
                "updated_at": datetime.utcnow(),
            }
        )
        merged.metadata["heat"] = ShortTermMemory._compute_heat(merged)
        return merged
