"""Memory conflict detection and resolution. Owner: efca9634 + cb0aeabe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from src.common.models import MemoryItem, MemoryType


def merge_identity_key(metadata: dict[str, Any]) -> str | None:
    """短期合并与冲突识别共用的稳定身份键。

    优先 ``record_key``（来源层稳定键，配置改值不变），其次 ``fingerprint`` /
    ``dedup_fingerprint``（精确事件去重哈希）。
    """

    record_key = metadata.get("record_key")
    if record_key:
        return f"rk:{record_key}"

    fingerprint = metadata.get("fingerprint") or metadata.get("dedup_fingerprint")
    if fingerprint:
        return f"fp:{fingerprint}"

    return None


def same_feature_identity(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_scene: str | None = None,
    right_scene: str | None = None,
) -> bool:
    """偏好键 / 知识键相同且场景一致时视为同一主题。

    事件级 ``fingerprint`` 每次摄入都不同，不能挡住主题身份；跨对话固化
    和中期合并应优先走这条，而不是只认指纹。
    """

    left_key = left.get("preference_key") or left.get("knowledge_key")
    right_key = right.get("preference_key") or right.get("knowledge_key")
    if not left_key or not right_key or left_key != right_key:
        return False
    return (left_scene or "global") == (right_scene or "global")


class ConflictAction(str, Enum):
    KEEP_OLD = "keep_old"
    REPLACE = "replace"
    MERGE = "merge"


@dataclass
class ConflictDecision:
    action: ConflictAction
    reason: str
    result: MemoryItem


class ConflictResolver:
    """
    Orchestrator duty (3): conflict resolution (target correctness >= 88%).

    Example:
      Old: user likes Java (from historical behavior)
      New: user prefers Python (recent explicit statement)
      -> REPLACE with Python, record reason

    TODO:
      - 知识图谱实体级 LLM 合并（resolve_knowledge 当前按「最新事实」语义判定，
        足以覆盖端口/地址/取值类冲突；实体级 merge 待后续扩展）
    """

    _SOURCE_TRUST = {
        "manual_config": 3,
        "conversation": 2,
        "behavior": 1,
        "tool_result": 1,
    }
    _SOURCE_ALIASES = {
        "chat": "conversation",
        "explicit_statement": "conversation",
        "manual": "manual_config",
    }

    @staticmethod
    def memory_key(item: MemoryItem) -> str:
        """Stable identity for conflict/version lookup."""

        identity = merge_identity_key(item.metadata)
        if identity:
            return identity

        for field in ("preference_key", "knowledge_key", "config_key", "tool_name"):
            value = item.metadata.get(field)
            if value:
                scene = item.scene_id or "global"
                return f"{field}:{item.user_id}:{scene}:{value}"

        return f"content:{item.user_id}:{item.content[:64]}"

    def detect(self, old: MemoryItem, new: MemoryItem) -> bool:
        if old.memory_type != new.memory_type:
            return False
        if self.memory_key(old) != self.memory_key(new):
            return False
        return old.content.strip() != new.content.strip()

    def resolve(self, old: MemoryItem, new: MemoryItem) -> ConflictDecision:
        old_trust = self._trust_score(old)
        new_trust = self._trust_score(new)
        old_time = old.updated_at if isinstance(old.updated_at, datetime) else datetime.utcnow()
        new_time = new.updated_at if isinstance(new.updated_at, datetime) else datetime.utcnow()

        if new_trust > old_trust:
            new.version = old.version + 1
            new.metadata.setdefault("conflict_action", ConflictAction.REPLACE.value)
            return ConflictDecision(
                action=ConflictAction.REPLACE,
                reason=f"新信息来源更可信 ({new_trust}>{old_trust})",
                result=new,
            )
        if new_trust == old_trust and new_time >= old_time:
            new.version = old.version + 1
            new.metadata.setdefault("conflict_action", ConflictAction.REPLACE.value)
            return ConflictDecision(
                action=ConflictAction.REPLACE,
                reason="同来源信任度，新信息时间更近",
                result=new,
            )
        return ConflictDecision(
            action=ConflictAction.KEEP_OLD,
            reason="旧记忆更可信或更新",
            result=old,
        )

    def _trust_score(self, item: MemoryItem) -> float:
        source_type = item.metadata.get("source_type", "")
        base = float(self._SOURCE_TRUST.get(source_type, 0))
        quality = item.metadata.get("quality_score")
        if isinstance(quality, (int, float)):
            score = base + float(quality) * 0.5
        else:
            confidence = item.metadata.get("confidence")
            score = base + float(confidence) * 0.5 if isinstance(confidence, (int, float)) else base
        # 近因降权：陈旧记忆轻微降权，支持偏好漂移覆盖（参考 Graphiti 再固化）。
        age = self._age_days(item)
        if age > 30:
            score -= min((age - 30) * 0.01, 0.3)
        return max(score, 0.0)

    @staticmethod
    def _age_days(item: MemoryItem) -> int:
        ts = item.updated_at if isinstance(item.updated_at, datetime) else None
        if ts is None:
            return 0
        try:
            return max((datetime.utcnow() - ts).days, 0)
        except Exception:
            return 0

    def resolve_knowledge(self, old: MemoryItem, new: MemoryItem) -> ConflictDecision:
        """Knowledge-specific: latest fact wins, with a low-trust guard.

        知识是事实，应当取「最新」事实，而非「来源等级最高」的旧事实。
        旧实现直接透传 ``resolve``，导致 manual_config(3) 永远压过 conversation(2)，
        即使对话里刚说出更新鲜的事实（如「端口 8080 → 9090」）也会被旧手动配置
        压住 → 知识冲突正确率被压低。

        规则：
          - 新事实更新鲜时，默认 REPLACE（取最新事实）；
          - 但低信任观测（behavior / tool_result）不应覆盖更高信任的旧事实
            （conversation / manual_config），避免噪声埋点改写权威知识。
          - 新事实不更新鲜时 KEEP_OLD。
        """
        new_time = new.updated_at if isinstance(new.updated_at, datetime) else datetime.utcnow()
        old_time = old.updated_at if isinstance(old.updated_at, datetime) else datetime.utcnow()
        new_src = str(new.metadata.get("source_type") or new.metadata.get("source") or "").strip().lower()
        old_src = str(old.metadata.get("source_type") or old.metadata.get("source") or "").strip().lower()
        new_src = self._SOURCE_ALIASES.get(new_src, new_src)
        old_src = self._SOURCE_ALIASES.get(old_src, old_src)

        if new_time > old_time:
            if new_src in ("behavior", "tool_result") and old_src in ("conversation", "manual_config"):
                return ConflictDecision(
                    action=ConflictAction.KEEP_OLD,
                    reason="低信任观测（behavior/tool_result）不覆盖更高信任的旧事实",
                    result=old,
                )
            new.version = old.version + 1
            new.metadata.setdefault("conflict_action", ConflictAction.REPLACE.value)
            return ConflictDecision(
                action=ConflictAction.REPLACE,
                reason="知识取最新事实（新事实更新鲜）",
                result=new,
            )
        return ConflictDecision(
            action=ConflictAction.KEEP_OLD,
            reason="新事实不更新鲜，保留旧知识",
            result=old,
        )

    def resolve_preference(self, old: MemoryItem, new: MemoryItem) -> ConflictDecision:
        """同一 preference_key 的版本更新：按来源等级 + 时间，不把抽取置信度当否决条件。

        提取器给旧值 0.92、新值 0.77 时，用户明确改口仍应生成 v2，否则画像与召回会卡住旧值。
        """
        old_rank = self._source_rank(old)
        new_rank = self._source_rank(new)
        old_time = old.updated_at if isinstance(old.updated_at, datetime) else datetime.utcnow()
        new_time = new.updated_at if isinstance(new.updated_at, datetime) else datetime.utcnow()

        if new_rank > old_rank or (new_rank == old_rank and new_time >= old_time):
            new.version = old.version + 1
            new.metadata.setdefault("conflict_action", ConflictAction.REPLACE.value)
            return ConflictDecision(
                action=ConflictAction.REPLACE,
                reason="偏好键更新：来源可信或时间更新",
                result=new,
            )
        return ConflictDecision(
            action=ConflictAction.KEEP_OLD,
            reason="旧记忆更可信或更新",
            result=old,
        )

    def _source_rank(self, item: MemoryItem) -> int:
        raw = str(
            item.metadata.get("source_type")
            or item.metadata.get("confidence_source")
            or item.metadata.get("source")
            or ""
        ).strip().lower()
        bucket = self._SOURCE_ALIASES.get(raw, raw)
        rank = int(self._SOURCE_TRUST.get(bucket, 0))
        # 近因降权：陈旧但同来源的旧偏好降一级，使近期改口能正确覆盖（偏好漂移）。
        age = self._age_days(item)
        if age > 30:
            rank = max(rank - 1, 0)
        return rank
