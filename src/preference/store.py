"""Preference memory dynamic capture — Owner: 26a9835a.

Path B (cb0aeabe对接):
  extract() → list[MemoryEntry]
  本地版本索引供 get_version_history()
  不 import MilvusClient，不直接调 create_memory()

Industrial optimizations vs MemoryOS anti-patterns:
  - batched dirty flush (not per-key JSON rewrite)
  - structured feature profile (not free-text user blob)
  - canonical key=value embed text (value change ⇒ content change)
  - n_observations frequency signal for confidence / ranking
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.common.interfaces import PreferenceStore
from src.common.models import MemoryItem, MemoryLayer, MemoryType, RawRecord
from src.knowledge.memory_service.memory_schema import (
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    TaskType,
)
from src.preference.confidence import (
    calculate_confidence,
    map_data_source_to_confidence_source,
)
from src.preference.extractor import extract_preferences
from src.preference.features import build_user_feature_profile, canonical_embed_text
from src.preference.keys import LIST_MERGE_KEYS, merge_item_list, normalize_preference_key
from src.preference.llm_client import SiliconFlowClient
from src.preference.models import ExtractedPreference, PreferenceRecord
from src.preference.version_index import PreferenceVersionIndex

_SCENE_TO_TASK = {
    "coding": TaskType.CODING,
    "office": TaskType.OFFICE,
    "os_operation": TaskType.OS_OPERATION,
    "general": TaskType.GLOBAL,
    "web_browsing": TaskType.GLOBAL,
    "data_analysis": TaskType.GLOBAL,
}

_SOURCE_TO_MEMORY = {
    "manual_config": MemorySource.MANUAL,
    "manual": MemorySource.MANUAL,
    "conversation": MemorySource.CHAT,
    "chat": MemorySource.CHAT,
    "explicit_statement": MemorySource.CHAT,
    "user_behavior": MemorySource.AGENT_LOG,
    "tool_result": MemorySource.AGENT_LOG,
    "repeated_behavior": MemorySource.AGENT_LOG,
    "single_observation": MemorySource.AGENT_LOG,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PreferenceMemoryStore(PreferenceStore):
    """
    Owner: 26a9835a (Core module)
    Requirement: (2) habits, output style, security policy + versioning
    """

    def __init__(
        self,
        storage_dir: str | Path | None = None,
        llm_client: SiliconFlowClient | None = None,
        use_llm: bool = True,
        allow_rules: bool = True,
        open_domain: bool = False,
        restrict_llm_to_known_keys: bool = False,
    ) -> None:
        self.index = PreferenceVersionIndex(storage_dir=storage_dir, auto_persist=False)
        self.llm_client = llm_client or SiliconFlowClient()
        self.use_llm = use_llm
        self.allow_rules = allow_rules
        # 开放域抽取头：仅竞赛/开放域评测开启，产品默认关闭以免污染封闭键画像。
        self.open_domain = open_domain
        # 封闭键精确匹配口径：只令 OS 抽取头产出已知键、不发明动态键，避免 FP。
        # 默认 False——产品保留长尾兜底（a593907 动态键兜底）。
        self.restrict_llm_to_known_keys = restrict_llm_to_known_keys

    # ------------------------------------------------------------------ extract
    def extract(self, records: list[RawRecord], *, debug: bool = False) -> list[MemoryEntry]:
        """Extract preferences; one disk flush per batch (not per key)."""
        extracted = extract_preferences(
            records,
            client=self.llm_client,
            use_llm=self.use_llm,
            allow_rules=self.allow_rules,
            open_domain=self.open_domain,
            restrict_llm_to_known_keys=self.restrict_llm_to_known_keys,
            debug=debug,
        )
        if not records:
            return []
        user_id = records[0].user_id
        entries: list[MemoryEntry] = []
        try:
            for pref in extracted:
                entries.append(self._commit_extracted(user_id, pref))
        finally:
            # industrial: single atomic write after batch mutations
            self.index.flush(user_id)
        return entries

    def extract_from_text(
        self,
        text: str,
        *,
        user_id: str = "demo_user",
        scene_id: str | None = "general",
        source: str = "conversation",
    ) -> list[MemoryEntry]:
        """Convenience API for demos / benchmarks."""
        from src.common.models import DataSource

        try:
            ds = DataSource(source)
        except ValueError:
            ds = DataSource.CONVERSATION
        record = RawRecord(
            id=str(uuid4()),
            source=ds,
            user_id=user_id,
            scene_id=scene_id,
            payload={"content": text},
        )
        return self.extract([record])

    def _commit_extracted(self, user_id: str, pref: ExtractedPreference) -> MemoryEntry:
        full_key = normalize_preference_key(pref.category, pref.key)
        conf_source = pref.confidence_source or map_data_source_to_confidence_source(pref.source)
        existing = self.index.get_record(
            user_id,
            full_key,
            scope=pref.scope,
            scene_id=None if pref.scope == "global" else pref.scene_id,
        )
        if full_key in LIST_MERGE_KEYS and existing and existing.value:
            pref.value = merge_item_list(existing.value, pref.value)

        # frequency: same value reinforces; value change resets (handled in apply_value)
        if existing and existing.value == pref.value:
            n_obs = existing.n_observations + max(1, pref.n_observations)
        elif existing:
            n_obs = max(1, pref.n_observations)
        else:
            n_obs = max(1, pref.n_observations)

        # upgrade behavior source tier when enough observations
        if conf_source == "single_observation" and n_obs >= 3:
            conf_source = "repeated_behavior"

        all_sources: list[dict[str, Any]] = []
        if existing:
            all_sources.append(
                {
                    "source": existing.source,
                    "value": existing.value,
                    "n_observations": existing.n_observations,
                    "updated_at": existing.updated_at,
                    "statement_text": existing.description,
                }
            )
        all_sources.append(
            {
                "source": conf_source,
                "value": pref.value,
                "n_observations": n_obs,
                "updated_at": _utc_now(),
                "statement_text": pref.statement_text,
                "negation": False,
            }
        )
        # 同值重复观察才传入历史置信度做 EMA 平滑；改口则重新计（支持偏好漂移）。
        prev_conf = existing.confidence if (existing and existing.value == pref.value) else None
        confidence = calculate_confidence(
            source=conf_source,
            value=pref.value,
            n_observations=n_obs,
            updated_at=_utc_now(),
            statement_text=pref.statement_text,
            all_sources_for_key=all_sources if len(all_sources) > 1 else None,
            prev_confidence=prev_conf,
        )

        prev_latest_id = None
        if existing and existing.value != pref.value and existing.versions:
            prev_latest_id = existing.versions[-1].memory_id

        rec = self.index.apply_value(
            user_id,
            full_key,
            pref.value,
            description=pref.description or pref.statement_text or "",
            source=conf_source,
            confidence=confidence,
            scope=pref.scope,
            scene_id=pref.scene_id,
            reason=pref.reason,
            n_observations=n_obs,
            persist=False,
        )
        return self._record_to_entry(user_id, rec, supersedes_memory_id=prev_latest_id)

    def _record_to_entry(
        self,
        user_id: str,
        rec: PreferenceRecord,
        *,
        supersedes_memory_id: str | None = None,
    ) -> MemoryEntry:
        # canonical structured content → embedding tracks value changes
        content = canonical_embed_text(rec.preference_key, rec.value, rec.description)
        if len(content) > 2000:
            content = content[:2000]
        scene = rec.scene_id
        task = _SCENE_TO_TASK.get(scene or "general", TaskType.GLOBAL)
        mem_source = _SOURCE_TO_MEMORY.get(rec.source, MemorySource.CHAT)
        value_str = rec.value if len(rec.value) <= 512 else rec.value[:512]
        metadata = {
            "preference_key": rec.preference_key[:128],
            "preference_value": value_str,
            "category": rec.category[:64],
            "version": rec.current_version,
            "is_latest": True,
            "scope": rec.scope,
            "confidence_source": rec.source,
            "n_observations": rec.n_observations,
            "embed_canonical": True,
        }
        if scene:
            metadata["scene_id"] = scene[:128]
        if supersedes_memory_id:
            metadata["supersedes_memory_id"] = str(supersedes_memory_id)[:128]
        return MemoryEntry(
            content=content,
            memory_type=MemoryTypeTag.PREFERENCE,
            task_type=task,
            source=mem_source,
            user_id=user_id,
            scene_id=scene,
            confidence=float(rec.confidence),
            metadata=metadata,
        )

    # ------------------------------------------------------------------ structured profile
    def get_feature_profile(
        self,
        user_id: str,
        scene_id: str | None = None,
    ) -> dict[str, Any]:
        """Recommender-style structured profile for Agent injection (not prose blob)."""
        return build_user_feature_profile(self.index.all_records(user_id), scene_id=scene_id)

    # ------------------------------------------------------------------ version history
    def get_version_history(self, user_id: str, key: str) -> list[MemoryItem]:
        """Return version chain as MemoryItems for Orchestrator conflict checks."""
        preference_key = key
        records = self.index.list_history(user_id, preference_key, scene_id=None)
        if not records:
            self.index.load(user_id)
            records = [
                r
                for r in self.index._data.get(user_id, {}).values()
                if r.preference_key == preference_key
                or r.preference_key.endswith(f".{preference_key}")
                or preference_key in r.preference_key
            ]

        items: list[MemoryItem] = []
        for rec in records:
            for ver in rec.versions:
                items.append(
                    MemoryItem(
                        id=ver.memory_id or f"{rec.id}_v{ver.version}",
                        memory_type=MemoryType.PREFERENCE,
                        layer=MemoryLayer.LONG,
                        content=canonical_embed_text(rec.preference_key, ver.value, ver.description),
                        user_id=user_id,
                        scene_id=rec.scene_id,
                        version=ver.version,
                        metadata={
                            "preference_key": rec.preference_key,
                            "preference_value": ver.value,
                            "category": rec.category,
                            "version": ver.version,
                            "is_latest": ver.version == rec.current_version,
                            "scope": rec.scope,
                            "source_type": ver.source,
                            "confidence": ver.confidence,
                            "reason": ver.reason,
                            "changed_at": ver.changed_at.isoformat(),
                            "n_observations": rec.n_observations,
                        },
                        created_at=ver.changed_at,
                        updated_at=ver.changed_at,
                    )
                )
        items.sort(key=lambda x: x.version)
        return items

    def rollback(
        self,
        user_id: str,
        preference_key: str,
        to_version: int,
        *,
        scope: str = "scene",
        scene_id: str | None = "general",
    ) -> MemoryEntry:
        rec = self.index.rollback(
            user_id,
            preference_key,
            to_version,
            scope=scope,
            scene_id=scene_id,
        )
        return self._record_to_entry(user_id, rec)

    def resolve_preference(
        self,
        user_id: str,
        preference_key: str,
        scene_id: str | None,
    ) -> PreferenceRecord | None:
        """Read with scene > global priority."""
        return self.index.resolve_read(user_id, preference_key, scene_id)

    # ------------------------------------------------------------------ Orchestrator-compat
    def save(self, item: MemoryItem) -> MemoryItem:
        """Update local version index from MemoryItem. Does NOT call create_memory."""
        item.memory_type = MemoryType.PREFERENCE
        meta = item.metadata or {}
        preference_key = meta.get("preference_key")
        preference_value = meta.get("preference_value")
        if preference_key and preference_value is not None:
            scope = meta.get("scope") or ("scene" if item.scene_id else "global")
            rec = self.index.apply_value(
                item.user_id,
                preference_key,
                str(preference_value),
                description=item.content,
                source=map_data_source_to_confidence_source(str(meta.get("source_type", "conversation"))),
                confidence=float(meta.get("confidence", 0.92)),
                scope=scope,
                scene_id=item.scene_id,
                reason=str(meta.get("reason", "orchestrator_save")),
                persist=False,
            )
            self.index.flush(item.user_id)
            item.version = rec.current_version
            item.content = canonical_embed_text(rec.preference_key, rec.value, item.content)
            item.metadata = {
                **meta,
                "preference_key": rec.preference_key,
                "preference_value": rec.value,
                "category": rec.category,
                "version": rec.current_version,
                "is_latest": True,
                "scope": rec.scope,
                "n_observations": rec.n_observations,
                "embed_canonical": True,
            }
        return item
