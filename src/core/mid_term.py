"""Mid-term memory store — recent high-value fragment pool. Owner: efca9634.

改进（参考 MemoryOS memoryos-pypi/mid_term.py）：
- 合并由字符相似 (SequenceMatcher) 改为 **语义(embedding)余弦 + 关键词 Jaccard**，
  使跨对话的「同义不同表述」也能去重/合并。
- 热度引入近因衰减 (R_recency = exp(-Δt/τ))，老记忆自动降权；淘汰由 FIFO 改为
  按 effective_heat（含近因）最低淘汰，避免冷记忆长期占坑。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.conflict_resolver import merge_identity_key, same_feature_identity
from src.core.short_term import ShortTermMemory

try:
    from src.embedding.embedding_interface import get_shared_embedding_backend
except Exception:  # pragma: no cover - 极端环境
    get_shared_embedding_backend = None  # type: ignore


_STOPWORDS = {
    "a", "an", "and", "in", "is", "me", "my", "of", "or", "the", "to",
    "的", "了", "是", "在", "我", "有", "和", "吧", "啊", "呢", "请", "用",
}


def _keywords(text: str) -> set[str]:
    """英文/数字词 + 单汉字，作为轻量关键词集合。"""

    lowered = (text or "").lower()
    toks = re.findall(r"[a-z0-9_]+", lowered)
    toks.extend(re.findall(r"[一-鿿]", (text or "")))
    return {tok for tok in toks if tok and tok not in _STOPWORDS}


def _is_short_text(text: str) -> bool:
    """一两词英文或极短字符串上，麒麟 GTE 余弦会把反义近邻抬到 0.7+。"""

    stripped = (text or "").strip()
    if len(stripped) <= 8:
        return True
    return len(_keywords(stripped)) <= 2


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, dot / (na * nb))


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _ts(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


@dataclass
class MidTermStats:
    user_id: str
    count: int
    capacity: int
    preference_count: int
    knowledge_count: int
    avg_heat: float


@dataclass
class WriteMidResult:
    item: MemoryItem
    evicted: MemoryItem | None = None
    merged_existing: bool = False


class MidTermMemory:
    """进程内中期记忆池。"""

    def __init__(
        self,
        capacity: int = 50,
        similarity_threshold: float = 0.7,
        keyword_alpha: float = 0.3,
        recency_tau_hours: float = 168.0,
        recency_gamma: float = 1.0,
    ) -> None:
        """进程内中期记忆池。

        Args:
            capacity: 每个用户桶的最大条目数；超出时按 effective_heat 最低者淘汰。
            similarity_threshold: 合并相似度门限；语义分 + 关键词混合分 >= 该值才合并
                （默认 0.7：语义向量余弦通常需较高才判为同一事）。
            keyword_alpha: 关键词 Jaccard 在相似度中的混合系数（0~1），
                与 embedding 余弦线性组合；值越大关键词权重越高（默认 0.3）。
            recency_tau_hours: 近因衰减时间常数 τ（小时）；=168.0 即 7 天，
                老记忆按 exp(-Δt/τ) 降权，避免冷记忆长期占坑。
            recency_gamma: 近因衰减总增益系数；=1.0 表示不额外放大/缩小。
        """
        if capacity < 1:
            raise ValueError("mid-term capacity must be >= 1")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0 and 1")
        self.capacity = capacity
        self.similarity_threshold = similarity_threshold
        self.keyword_alpha = keyword_alpha
        self.recency_tau_hours = recency_tau_hours
        self.recency_gamma = recency_gamma
        self._buckets: dict[str, list[MemoryItem]] = {}
        self._items_by_id: dict[str, MemoryItem] = {}
        self._embed_cache: dict[str, list[float] | None] = {}

    # ------------------------------------------------------------------ embedding
    def _embed(self, text: str) -> list[float] | None:
        if get_shared_embedding_backend is None:
            return None
        try:
            return get_shared_embedding_backend().embed([text])[0]
        except Exception:
            return None

    def _embed_for(self, item: MemoryItem) -> list[float] | None:
        if item.id in self._embed_cache:
            return self._embed_cache[item.id]
        vec = self._embed(item.content)
        self._embed_cache[item.id] = vec
        return vec

    def effective_heat(self, item: MemoryItem) -> float:
        """基础热度 × 近因衰减，用于合并/淘汰排序（不改变 metadata 原始 heat）。"""

        base = float(item.metadata.get("heat", 0))
        now = datetime.now(timezone.utc)
        lv = item.metadata.get("last_visit_time")
        lv_dt: datetime | None = None
        if isinstance(lv, datetime):
            lv_dt = lv
        elif isinstance(lv, str):
            try:
                lv_dt = datetime.fromisoformat(lv)
            except Exception:
                lv_dt = None
        if lv_dt is None and isinstance(item.created_at, datetime):
            lv_dt = item.created_at
        if lv_dt is None:
            return base
        try:
            if lv_dt.tzinfo is None:
                lv_dt = lv_dt.replace(tzinfo=timezone.utc)
            hours = (now - lv_dt.astimezone(timezone.utc)).total_seconds() / 3600.0
        except Exception:
            hours = 0.0
        recency = math.exp(-hours / max(self.recency_tau_hours, 1e-6))
        return base * self.recency_gamma * recency

    def _evict_lowest_heat(self, bucket: list[MemoryItem]) -> MemoryItem | None:
        if not bucket:
            return None
        victim = min(
            bucket,
            key=lambda it: (self.effective_heat(it), _ts(it.created_at)),
        )
        bucket.remove(victim)
        self._items_by_id.pop(victim.id, None)
        return victim

    @staticmethod
    def content_similarity(left: str, right: str) -> float:
        """基于 SequenceMatcher 的文本相似度，开发期不依赖 embedding。"""

        normalized_left = " ".join(left.strip().lower().split())
        normalized_right = " ".join(right.strip().lower().split())
        if not normalized_left or not normalized_right:
            return 0.0
        return SequenceMatcher(None, normalized_left, normalized_right).ratio()

    def similarity_score(self, existing: MemoryItem, incoming: MemoryItem) -> float:
        """同一 memory_type 下计算合并相似度；身份键一致视为 1.0。

        默认走语义(embedding 余弦) + 关键词 Jaccard（参考 MemoryOS mid_term.py:196）；
        embedding 不可用时回退到字符相似，保证链路不中断。
        """

        if existing.memory_type != incoming.memory_type:
            return 0.0

        left_identity = merge_identity_key(existing.metadata)
        right_identity = merge_identity_key(incoming.metadata)
        if left_identity and right_identity and left_identity == right_identity:
            return 1.0
        if same_feature_identity(
            existing.metadata,
            incoming.metadata,
            left_scene=existing.scene_id,
            right_scene=incoming.scene_id,
        ):
            return 1.0

        ev = self._embed_for(existing)
        iv = self._embed_for(incoming)
        if ev is not None and iv is not None:
            sem = _cosine(ev, iv)
            kw = _jaccard(_keywords(existing.content), _keywords(incoming.content))
            # 短文本且无关键词重叠时，不能单靠向量合并（GTE: hot/cold ≈ 0.77）。
            if kw <= 0 and (
                _is_short_text(existing.content) or _is_short_text(incoming.content)
            ):
                return 0.0
            return float(sem) + self.keyword_alpha * kw

        return self.content_similarity(existing.content, incoming.content)

    def append(self, item: MemoryItem) -> WriteMidResult:
        """写入中期池；相似条目合并，超容量时按 effective_heat 最低淘汰。"""

        item.layer = MemoryLayer.MID
        item.updated_at = datetime.utcnow()
        bucket = self._buckets.setdefault(item.user_id, [])

        merge_target = self._find_merge_target(bucket, item)
        if merge_target is not None:
            merged = self._merge_update(merge_target, item)
            for index, existing in enumerate(bucket):
                if existing.id == merge_target.id:
                    bucket[index] = merged
                    break
            self._items_by_id[merged.id] = merged
            return WriteMidResult(item=merged, merged_existing=True)

        evicted: MemoryItem | None = None
        if len(bucket) >= self.capacity:
            evicted = self._evict_lowest_heat(bucket)

        item.metadata.setdefault("access_count", 1)
        item.metadata.setdefault("heat", ShortTermMemory._compute_heat(item))
        item.metadata["last_visit_time"] = _now_iso()
        bucket.append(item)
        self._items_by_id[item.id] = item
        return WriteMidResult(item=item, evicted=evicted)

    def extend(self, user_id: str, items: list[MemoryItem]) -> list[MemoryItem]:
        """批量晋升写入，返回被淘汰的条目。"""

        evicted: list[MemoryItem] = []
        for item in items:
            result = self.append(item)
            if result.evicted is not None:
                evicted.append(result.evicted)
        return evicted

    def _find_merge_target(self, bucket: list[MemoryItem], incoming: MemoryItem) -> MemoryItem | None:
        best_score = 0.0
        best_item: MemoryItem | None = None
        for existing in bucket:
            score = self.similarity_score(existing, incoming)
            if score >= self.similarity_threshold and score > best_score:
                best_score = score
                best_item = existing
        return best_item

    @staticmethod
    def _merge_update(existing: MemoryItem, incoming: MemoryItem) -> MemoryItem:
        access_count = int(existing.metadata.get("access_count", 0)) + 1
        merged_metadata = dict(existing.metadata)
        merged_metadata.update(incoming.metadata)
        merged_metadata["access_count"] = access_count
        merged_metadata["mid_merged"] = True

        incoming_quality = incoming.metadata.get("quality_score")
        existing_quality = existing.metadata.get("quality_score")
        qualities = [
            float(q)
            for q in (existing_quality, incoming_quality)
            if isinstance(q, (int, float))
        ]
        if qualities:
            merged_metadata["quality_score"] = max(qualities)
        use_incoming_content = isinstance(incoming_quality, (int, float)) and (
            not isinstance(existing_quality, (int, float))
            or float(incoming_quality) >= float(existing_quality)
        )

        merged = existing.model_copy(
            update={
                "content": incoming.content if use_incoming_content else existing.content,
                "memory_type": incoming.memory_type,
                "scene_id": incoming.scene_id or existing.scene_id,
                "version": existing.version + 1,
                "metadata": merged_metadata,
                "updated_at": datetime.utcnow(),
            }
        )
        merged.metadata["heat"] = ShortTermMemory._compute_heat(merged)
        merged.metadata["last_visit_time"] = _now_iso()
        return merged

    def read(
        self,
        user_id: str,
        *,
        limit: int = 20,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
        newest_first: bool = True,
    ) -> list[MemoryItem]:
        items = list(self._buckets.get(user_id, []))
        if scene_id is not None:
            items = [item for item in items if item.scene_id == scene_id]
        if memory_type is not None:
            items = [item for item in items if item.memory_type == memory_type]

        items.sort(key=lambda item: item.created_at, reverse=newest_first)
        return items[:limit]

    def get(self, memory_id: str) -> MemoryItem | None:
        return self._items_by_id.get(memory_id)

    def delete(self, user_id: str, memory_id: str) -> bool:
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
        items = self._buckets.pop(user_id, [])
        for item in items:
            self._items_by_id.pop(item.id, None)
        return items

    def count(self, user_id: str) -> int:
        return len(self._buckets.get(user_id, []))

    def stats(self, user_id: str) -> MidTermStats:
        items = self._buckets.get(user_id, [])
        heats = [float(item.metadata.get("heat", 0)) for item in items]
        avg_heat = round(sum(heats) / len(heats), 4) if heats else 0.0
        return MidTermStats(
            user_id=user_id,
            count=len(items),
            capacity=self.capacity,
            preference_count=sum(1 for item in items if item.memory_type == MemoryType.PREFERENCE),
            knowledge_count=sum(1 for item in items if item.memory_type == MemoryType.KNOWLEDGE),
            avg_heat=avg_heat,
        )

    @staticmethod
    def meets_quality_gate(item: MemoryItem, quality_threshold: float) -> bool:
        quality = item.metadata.get("quality_score")
        if not isinstance(quality, (int, float)):
            return False
        return float(quality) >= quality_threshold
