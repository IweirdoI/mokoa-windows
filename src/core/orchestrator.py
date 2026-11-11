"""
Memory Orchestrator — 面向 OS Agent 的智能记忆管理控制器.

Owner: efca9634

职责：调用 ingestion / security / layer_manager / preference / knowledge，
完成记忆分类、短期写入、生命周期晋升、冲突消解与检索编排。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from time import perf_counter
from dataclasses import dataclass, field
from uuid import uuid4

from src.common.interfaces import (
    IngestionAdapter,
    MemoryLayerManager,
    PreferenceStore,
    SecurityService,
)
from src.common.models import (
    DataSource,
    MemoryItem,
    MemoryLayer,
    MemoryType,
    NormalizedRecord,
    RawRecord,
    RetrievalQuery,
    RetrievalResult,
)
from src.core.classifier import MemoryClassifier

# 兜底统计快照容量（仅用于无 LayerManager 时的轻量概览，与层真实容量解耦，避免误读）
_SHORT_SNAPSHOT_CAPACITY = 10
_MID_SNAPSHOT_CAPACITY = 50
from src.core.conflict_resolver import ConflictAction, ConflictResolver
from src.core.entry_adapter import memory_entry_to_item, memory_item_to_entry
from src.core.layer_manager import LayerManager
from src.core.mid_term import MidTermStats
from src.core.short_term import ShortTermStats
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.staging_store import StagingStore
from src.knowledge.memory_service.create_memory import create_memory
from src.knowledge.memory_service.delete_memory import delete_memory
from src.knowledge.memory_service.memory_schema import (
    CreateMemoryRequest,
    DeleteMemoryRequest,
    MemoryEntry,
    MemoryTypeTag,
    QueryMemoryRequest,
    UpdateMemoryRequest,
)
from src.knowledge.memory_service.milvus_client import MilvusClient
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
from src.knowledge.memory_service.query_memory import query_memory
from src.knowledge.memory_service.update_memory import update_memory


logger = logging.getLogger(__name__)


@dataclass
class ProcessInputResult:
    """process_input 的可审计返回结构。"""

    accepted: bool
    record: NormalizedRecord | None = None
    memory_id: str | None = None
    memory_type: str | None = None
    reject_code: str | None = None
    reasons: list[str] = field(default_factory=list)
    quality_score: float | None = None
    fingerprint: str | None = None
    evicted_memory_id: str | None = None
    updated_existing: bool = False
    processing_ms: float = 0.0
    stage_ms: dict[str, float] = field(default_factory=dict)
    # Path B：聊天显式偏好可并行入库长期画像（与 short→mid 热度门独立）
    preference_extracted: int = 0
    preference_stored_ids: list[str] = field(default_factory=list)
    preference_keys: list[str] = field(default_factory=list)


@dataclass
class ConsolidateMidResult:
    """仅执行 short → mid 晋升的返回结构。"""

    user_id: str
    promoted_to_mid: int = 0
    short_remaining: int = 0
    mid_remaining: int = 0


@dataclass
class ConsolidateResult:
    """consolidate 的可审计返回结构。"""

    user_id: str
    promoted_to_mid: int = 0
    promoted_to_long: int = 0
    retained_in_mid: int = 0
    short_remaining: int = 0
    mid_remaining: int = 0
    stored_memory_ids: list[str] = field(default_factory=list)


@dataclass
class StoreEntryResult:
    """store_memory_entry 的返回结构。"""

    memory_id: str
    created: bool
    reason: str = ""
    superseded_memory_id: str | None = None


@dataclass
class ExtractPreferencesResult:
    """自动提取偏好并入库的返回结构。"""

    user_id: str
    extracted_count: int = 0
    stored: list[StoreEntryResult] = field(default_factory=list)
    entries: list[MemoryEntry] = field(default_factory=list)
    mode: str = "fusion"  # rule | llm | fusion
    degraded_to_rules: bool = False


@dataclass
class MemoryLayerSnapshot:
    """单层记忆统计快照，供 Dashboard / GET /stats 使用。"""

    count: int
    capacity: int | None
    preference_count: int
    knowledge_count: int
    avg_heat: float | None = None


@dataclass
class MemoryStats:
    """用户跨层记忆总览。"""

    user_id: str
    short: MemoryLayerSnapshot
    mid: MemoryLayerSnapshot
    long: MemoryLayerSnapshot
    milvus: MemoryLayerSnapshot
    total_count: int
    total_preference_count: int
    total_knowledge_count: int


class MemoryOrchestrator:
    """记忆编排核心 — 麒忆智脑自研控制器。"""

    def __init__(
        self,
        ingestion: IngestionAdapter | None = None,
        security: SecurityService | None = None,
        layer_manager: MemoryLayerManager | None = None,
        preference: PreferenceStore | None = None,
        *,
        milvus: MilvusClient | InMemoryMilvusClient | None = None,
    ) -> None:
        from src.preference.store import PreferenceMemoryStore
        from src.security.service import SecurityAndForgetService

        self.ingestion = ingestion or IngestionPipeline()
        self.security = security or SecurityAndForgetService()
        self.layer_manager = layer_manager or LayerManager()
        self.preference = preference or PreferenceMemoryStore()
        self.classifier = MemoryClassifier()
        self.conflict_resolver = ConflictResolver()
        # Direct construction is used heavily by unit tests and lightweight
        # local demos. Production wiring must go through factory.py, where the
        # configured backend is selected explicitly.
        self._milvus = milvus or InMemoryMilvusClient()

    def process_input(
        self,
        raw: dict,
        source: str,
        *,
        user_id: str | None = None,
        scene_id: str | None = None,
    ) -> ProcessInputResult:
        """接入 → 清洗 → 分类 → 写入短期记忆。"""

        if not isinstance(self.ingestion, IngestionPipeline):
            return ProcessInputResult(
                accepted=False,
                reasons=["当前仅支持 IngestionPipeline 作为 ingestion 实现"],
            )

        ingestion_result = self.ingestion.process(
            raw,
            source,
            user_id=user_id or raw.get("user_id"),
            scene_id=scene_id or raw.get("scene_id"),
        )
        if not ingestion_result.accepted:
            reject_code = (
                ingestion_result.reject_code.value
                if ingestion_result.reject_code is not None
                else None
            )
            return ProcessInputResult(
                accepted=False,
                record=ingestion_result.record,
                reject_code=reject_code,
                reasons=ingestion_result.reasons,
                quality_score=ingestion_result.quality_score,
                fingerprint=ingestion_result.fingerprint,
                processing_ms=ingestion_result.processing_ms,
                stage_ms=dict(ingestion_result.stage_ms),
            )

        normalized = ingestion_result.record
        if normalized is None:
            return ProcessInputResult(
                accepted=False,
                reasons=["ingestion 未产出有效记录"],
                processing_ms=ingestion_result.processing_ms,
                stage_ms=dict(ingestion_result.stage_ms),
            )

        try:
            filtered, _, _ = self.security.filter_sensitive(
                normalized.content,
                user_id=normalized.user_id,
            )
            normalized.content = filtered

            item = MemoryItem(
                id=str(uuid4()),
                memory_type=self.classify(normalized),
                layer=MemoryLayer.SHORT,
                content=normalized.content,
                user_id=normalized.user_id,
                scene_id=normalized.scene_id,
                metadata=self._build_memory_metadata(normalized),
            )

            write_result = self.layer_manager.write_short(item)
        except Exception:
            if isinstance(self.ingestion, IngestionPipeline):
                self.ingestion.release_dedup(ingestion_result.dedup_fingerprint)
            raise

        written = write_result.item
        if ingestion_result.staging_event_id:
            self._bind_staging_event(
                ingestion_result.staging_event_id,
                written.id,
            )

        # 独立偏好路径（契约 Path B）：分类为偏好或规则已命中时，并行写入长期偏好画像。
        # 不跳过 short→mid 热度门；短期候选仍保留，长期生效走 extract→store_memory_entry。
        pref_keys: list[str] = []
        pref_ids: list[str] = []
        pref_count = 0
        try:
            from src.preference.extractor import extract_by_rules

            rule_hits = extract_by_rules(normalized.content, normalized.scene_id)
            if written.memory_type == MemoryType.PREFERENCE or (
                rule_hits and normalized.type in {"conversation", "behavior", "manual_config"}
            ):
                source_name = (
                    normalized.source.value
                    if hasattr(normalized.source, "value")
                    else str(normalized.source or source)
                )
                extracted = self.extract_preferences_from_text(
                    normalized.content,
                    user_id=normalized.user_id,
                    scene_id=normalized.scene_id,
                    source=source_name,
                )
                pref_count = extracted.extracted_count
                for entry, stored in zip(extracted.entries, extracted.stored):
                    key = str(entry.metadata.get("preference_key") or "")
                    if key:
                        pref_keys.append(key)
                        # 回填标准化偏好 key/value 到短期候选，使后续 consolidate
                        # 晋升时使用同一偏好身份（而非沿用摄入期的原始 key），
                        # 避免与 Path B 即时写入重复成两条长期偏好。
                        # 直接覆盖（摄入期可能已写入原始 key，如 "download_path"）。
                        written.metadata["preference_key"] = key
                        value = entry.metadata.get("preference_value")
                        if value is not None:
                            written.metadata["preference_value"] = value
                    if stored.memory_id:
                        pref_ids.append(stored.memory_id)
        except Exception:
            logger.exception("process_input 并行偏好提取失败，短期写入仍保留")

        return ProcessInputResult(
            accepted=True,
            record=normalized,
            memory_id=written.id,
            memory_type=written.memory_type.value,
            quality_score=written.metadata.get("quality_score"),
            fingerprint=written.metadata.get("fingerprint"),
            evicted_memory_id=write_result.evicted.id if write_result.evicted else None,
            updated_existing=write_result.updated_existing,
            processing_ms=ingestion_result.processing_ms,
            stage_ms=dict(ingestion_result.stage_ms),
            preference_extracted=pref_count,
            preference_stored_ids=pref_ids,
            preference_keys=pref_keys,
        )

    @staticmethod
    def _build_memory_metadata(normalized: NormalizedRecord) -> dict[str, object]:
        from src.core.recall_ranker import extract_project_tags

        metadata = dict(normalized.metadata)
        metadata["source_type"] = normalized.type
        metadata["confidence"] = normalized.confidence
        if config_key := metadata.get("config_key"):
            metadata.setdefault("preference_key", config_key)
        tags = extract_project_tags(normalized.content)
        if tags["project_ids"] and "project_ids" not in metadata:
            metadata["project_ids"] = tags["project_ids"]
        if tags["project_aliases"] and "project_aliases" not in metadata:
            metadata["project_aliases"] = tags["project_aliases"]
        return metadata

    def classify(self, record: NormalizedRecord) -> MemoryType:
        return self.classifier.classify(record)

    def list_short_term(
        self,
        user_id: str,
        *,
        limit: int = 10,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        manager = self._layer_manager()
        if manager is None:
            return self.layer_manager.read(MemoryLayer.SHORT, user_id, limit=limit)
        return manager.read_short(
            user_id,
            limit=limit,
            scene_id=scene_id,
            memory_type=memory_type,
        )

    def get_short_term_item(self, memory_id: str) -> MemoryItem | None:
        manager = self._layer_manager()
        if manager is None:
            return None
        return manager.get_short(memory_id)

    def touch_short_term_item(self, user_id: str, memory_id: str) -> MemoryItem | None:
        """记录一次真实访问并升温；拒绝跨用户触碰记忆。"""

        manager = self._layer_manager()
        if manager is None:
            return None
        item = manager.get_short(memory_id)
        if item is None or item.user_id != user_id:
            return None
        return manager.touch_short(memory_id)

    def short_term_stats(self, user_id: str) -> ShortTermStats:
        manager = self._layer_manager()
        if manager is not None:
            return manager.short_stats(user_id)

        items = self.layer_manager.read(MemoryLayer.SHORT, user_id, limit=1000)
        return ShortTermStats(
            user_id=user_id,
            count=len(items),
            capacity=getattr(self.layer_manager, "short_capacity", len(items)),
            preference_count=sum(1 for i in items if i.memory_type == MemoryType.PREFERENCE),
            knowledge_count=sum(1 for i in items if i.memory_type == MemoryType.KNOWLEDGE),
        )

    def list_mid_term(
        self,
        user_id: str,
        *,
        limit: int = 20,
        scene_id: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        manager = self._layer_manager()
        if manager is None:
            return self.layer_manager.read(MemoryLayer.MID, user_id, limit=limit)
        return manager.read_mid(
            user_id,
            limit=limit,
            scene_id=scene_id,
            memory_type=memory_type,
        )

    def mid_term_stats(self, user_id: str) -> MidTermStats:
        manager = self._layer_manager()
        if manager is not None:
            return manager.mid_stats(user_id)

        items = self.layer_manager.read(MemoryLayer.MID, user_id, limit=1000)
        heats = [float(i.metadata.get("heat", 0)) for i in items]
        avg_heat = round(sum(heats) / len(heats), 4) if heats else 0.0
        return MidTermStats(
            user_id=user_id,
            count=len(items),
            capacity=getattr(self.layer_manager, "mid_capacity", len(items)),
            preference_count=sum(1 for i in items if i.memory_type == MemoryType.PREFERENCE),
            knowledge_count=sum(1 for i in items if i.memory_type == MemoryType.KNOWLEDGE),
            avg_heat=avg_heat,
        )

    def memory_stats(self, user_id: str) -> MemoryStats:
        """跨 short / mid / 待入库队列 / Milvus 的记忆总览。

        ``long`` 仅统计待持久化队列（``_long``）；权威长期记忆在 ``milvus``。
        """

        manager = self._layer_manager()
        if manager is not None:
            short = manager.short_stats(user_id)
            mid = manager.mid_stats(user_id)
            long_stats = manager.long_stats(user_id)
            short_snapshot = MemoryLayerSnapshot(
                count=short.count,
                capacity=short.capacity,
                preference_count=short.preference_count,
                knowledge_count=short.knowledge_count,
            )
            mid_snapshot = MemoryLayerSnapshot(
                count=mid.count,
                capacity=mid.capacity,
                preference_count=mid.preference_count,
                knowledge_count=mid.knowledge_count,
                avg_heat=mid.avg_heat,
            )
            long_snapshot = MemoryLayerSnapshot(
                count=long_stats.count,
                capacity=None,
                preference_count=long_stats.preference_count,
                knowledge_count=long_stats.knowledge_count,
                avg_heat=long_stats.avg_heat,
            )
        else:
            short_items = self.layer_manager.read(MemoryLayer.SHORT, user_id, limit=1000)
            mid_items = self.layer_manager.read(MemoryLayer.MID, user_id, limit=1000)
            long_items = self.layer_manager.read(MemoryLayer.LONG, user_id, limit=1000)
            short_snapshot = self._snapshot_from_items(short_items, capacity=_SHORT_SNAPSHOT_CAPACITY)
            mid_snapshot = self._snapshot_from_items(mid_items, capacity=_MID_SNAPSHOT_CAPACITY)
            long_snapshot = self._snapshot_from_items(long_items, capacity=None)

        # 只依赖存储公开检索接口，避免 stats 与 mock 私有实现绑定。
        milvus_records = self._milvus.search(None, {"user_id": user_id}, top_k=100_000)
        milvus_snapshot = MemoryLayerSnapshot(
            count=len(milvus_records),
            capacity=None,
            preference_count=sum(
                1 for record in milvus_records if record.get("memory_type") == MemoryTypeTag.PREFERENCE.value
            ),
            knowledge_count=len(milvus_records)
            - sum(
                1 for record in milvus_records if record.get("memory_type") == MemoryTypeTag.PREFERENCE.value
            ),
        )

        total_count = (
            short_snapshot.count
            + mid_snapshot.count
            + long_snapshot.count
            + milvus_snapshot.count
        )
        return MemoryStats(
            user_id=user_id,
            short=short_snapshot,
            mid=mid_snapshot,
            long=long_snapshot,
            milvus=milvus_snapshot,
            total_count=total_count,
            total_preference_count=(
                short_snapshot.preference_count
                + mid_snapshot.preference_count
                + long_snapshot.preference_count
                + milvus_snapshot.preference_count
            ),
            total_knowledge_count=(
                short_snapshot.knowledge_count
                + mid_snapshot.knowledge_count
                + long_snapshot.knowledge_count
                + milvus_snapshot.knowledge_count
            ),
        )

    @staticmethod
    def _snapshot_from_items(
        items: list[MemoryItem],
        *,
        capacity: int | None,
    ) -> MemoryLayerSnapshot:
        heats = [float(item.metadata.get("heat", 0)) for item in items]
        avg_heat = round(sum(heats) / len(heats), 4) if heats else 0.0
        return MemoryLayerSnapshot(
            count=len(items),
            capacity=capacity,
            preference_count=sum(1 for item in items if item.memory_type == MemoryType.PREFERENCE),
            knowledge_count=sum(1 for item in items if item.memory_type == MemoryType.KNOWLEDGE),
            avg_heat=avg_heat,
        )

    def _layer_manager(self) -> LayerManager | None:
        return self.layer_manager if isinstance(self.layer_manager, LayerManager) else None

    def consolidate_mid(self, user_id: str) -> ConsolidateMidResult:
        """仅短期 → 中期晋升，不写入 Milvus。"""

        manager = self._layer_manager()
        if manager is None:
            return ConsolidateMidResult(user_id=user_id)

        promoted = manager.promote_to_mid(user_id)
        return ConsolidateMidResult(
            user_id=user_id,
            promoted_to_mid=len(promoted),
            short_remaining=manager.short_count(user_id),
            mid_remaining=manager.mid_count(user_id),
        )

    def consolidate(self, user_id: str) -> ConsolidateResult:
        """短期 → 中期 → 长期，再 resolve_and_store 持久化到 Milvus。

        对缺少 preference_key 的偏好文本，先走 preference.extract 自动结构化，
        再 store_memory_entry 写入权威存储（含版本历史）。
        """

        manager = self._layer_manager()
        if manager is None:
            return ConsolidateResult(user_id=user_id)

        promoted_mid = manager.promote_to_mid(user_id)
        promoted_long, retained = manager.promote_to_long(user_id)

        stored_ids: list[str] = []
        for item in promoted_long:
            if (
                item.memory_type == MemoryType.PREFERENCE
                and not str(item.metadata.get("preference_key") or "").strip()
            ):
                extracted = self.extract_and_store_preferences(
                    [
                        RawRecord(
                            id=item.id,
                            source=DataSource.CONVERSATION,
                            user_id=item.user_id,
                            scene_id=item.scene_id,
                            payload={"content": item.content},
                            metadata=dict(item.metadata or {}),
                        )
                    ]
                )
                if extracted.stored:
                    for result in extracted.stored:
                        if result.memory_id:
                            stored_ids.append(result.memory_id)
                            self._bind_staging_milvus_id(
                                item.user_id, item.id, result.memory_id
                            )
                    manager.release_long_pending(user_id, item.id)
                    continue

            stored = self.resolve_and_store(item)
            milvus_id = stored.metadata.get("milvus_memory_id")
            if isinstance(milvus_id, str) and milvus_id:
                stored_ids.append(milvus_id)
                manager.release_long_pending(user_id, item.id)

        return ConsolidateResult(
            user_id=user_id,
            promoted_to_mid=len(promoted_mid),
            promoted_to_long=len(promoted_long),
            retained_in_mid=len(retained),
            short_remaining=manager.short_count(user_id),
            mid_remaining=manager.mid_count(user_id),
            stored_memory_ids=stored_ids,
        )

    def extract_and_store_preferences(
        self,
        records: list[RawRecord],
    ) -> ExtractPreferencesResult:
        """调用 preference.extract → 逐条 store_memory_entry（主线权威入库路径）。"""

        user_id = records[0].user_id if records else "default"
        pref = self.preference
        use_llm = bool(getattr(pref, "use_llm", True))
        allow_rules = bool(getattr(pref, "allow_rules", True))
        if use_llm and allow_rules:
            mode = "fusion"
        elif use_llm:
            mode = "llm"
        else:
            mode = "rule"

        entries = list(pref.extract(records))
        # LLM 无密钥/失败且允许规则时，extractor 内部已降级；此处仅标记说明字段。
        degraded = False
        if mode == "fusion" and entries:
            llm_client = getattr(pref, "llm_client", None)
            if llm_client is not None and not getattr(llm_client, "available", True):
                degraded = True

        stored: list[StoreEntryResult] = []
        for entry in entries:
            result = self.store_memory_entry(entry)
            if result.memory_id:
                entry.metadata["milvus_memory_id"] = result.memory_id
            stored.append(result)

        return ExtractPreferencesResult(
            user_id=user_id,
            extracted_count=len(entries),
            stored=stored,
            entries=entries,
            mode=mode,
            degraded_to_rules=degraded,
        )

    def extract_preferences_from_text(
        self,
        text: str,
        *,
        user_id: str,
        scene_id: str | None = None,
        source: str = "conversation",
    ) -> ExtractPreferencesResult:
        """Demo / API：从自然语言文本自动提取偏好并入库。"""

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
        return self.extract_and_store_preferences([record])

    def get_preference_profile(
        self,
        user_id: str,
        scene_id: str | None = None,
    ) -> dict:
        """从权威长期存储构建画像，避免候选索引领先于实际入库状态。"""

        from src.preference.keys import PREFERENCE_KEYS

        items = self.list_preferences(user_id, latest_only=True, top_k=10_000).items
        by_key: dict[str, MemoryItem] = {}
        priorities: dict[str, int] = {}
        for item in items:
            key = str(item.metadata.get("preference_key") or "").strip()
            if not key:
                continue
            scope = str(item.metadata.get("scope") or ("scene" if item.scene_id else "global"))
            if scene_id is not None and scope == "scene" and item.scene_id != scene_id:
                continue
            priority = 2 if scene_id is not None and scope == "scene" else 1
            if priority >= priorities.get(key, 0):
                by_key[key] = item
                priorities[key] = priority

        features: list[dict] = []
        for key, item in by_key.items():
            metadata = item.metadata
            confidence = float(metadata.get("confidence", 0.0))
            observations = int(metadata.get("n_observations", 1) or 1)
            value = str(metadata.get("preference_value", ""))
            scope = str(metadata.get("scope") or ("scene" if item.scene_id else "global"))
            rank_score = round(confidence * (1.0 + 0.05 * min(observations, 20)), 4)
            features.append(
                {
                    "feature_id": f"{key}::{scope}::{item.scene_id or 'global'}",
                    "preference_key": key,
                    "category": str(metadata.get("category") or key.split(".", 1)[0]),
                    "value": value,
                    "value_type": PREFERENCE_KEYS.get(key, {}).get("type", "string"),
                    "confidence": confidence,
                    "n_observations": observations,
                    "recency_days": 0.0,
                    "version": item.version,
                    "scope": scope,
                    "scene_id": item.scene_id,
                    "source": str(
                        metadata.get("confidence_source")
                        or metadata.get("source_type")
                        or metadata.get("source")
                        or "unknown"
                    ),
                    "rank_score": rank_score,
                }
            )

        features.sort(key=lambda feature: feature["rank_score"], reverse=True)
        kv = {feature["preference_key"]: feature["value"] for feature in features}
        confidence = {
            feature["preference_key"]: feature["confidence"] for feature in features
        }
        return {
            "schema": "preference_feature_profile_v1",
            "scene_id": scene_id,
            "n_features": len(features),
            "kv": kv,
            "confidence": confidence,
            "features": features,
            "agent_context": {
                key: {"value": kv[key], "confidence": confidence[key]}
                for key in sorted(kv)
            },
        }

    def get_preference_history(
        self,
        user_id: str,
        preference_key: str,
    ) -> list[MemoryItem]:
        """返回权威长期存储中的版本链，不暴露尚未成功入库的候选版本。"""

        return self._preference_history_from_storage(user_id, preference_key)

    def resolve_and_store(self, item: MemoryItem) -> MemoryItem:
        entry = memory_item_to_entry(item)
        result = self.store_memory_entry(entry)
        stored = memory_entry_to_item(entry, memory_id=result.memory_id)
        if item.memory_type != MemoryType.PREFERENCE:
            stored.layer = MemoryLayer.LONG
        if result.memory_id:
            self._bind_staging_milvus_id(item.user_id, item.id, result.memory_id)
        return stored

    def store_memory_entry(self, entry: MemoryEntry) -> StoreEntryResult:
        """记忆入库统一入口：脱敏后按稳定键分发。

        - 直接入库与晋升都先脱敏，避免 /store-entry 绕过 /ingest。
        - ``preference_key`` 走偏好版本链。
        - ``knowledge_key`` 走知识覆盖（同键新事实替代旧事实，旧条默认不可召回）。
        - 其余按 ``record_key`` 做实体冲突；没有稳定键则直接创建。
        """

        filtered, _, _ = self.security.filter_sensitive(entry.content, user_id=entry.user_id)
        entry.content = filtered

        preference_key = str(entry.metadata.get("preference_key") or "").strip()
        knowledge_key = str(entry.metadata.get("knowledge_key") or "").strip()
        if preference_key:
            return self._store_preference_entry(entry, preference_key)
        if knowledge_key:
            return self._store_knowledge_entry(entry, knowledge_key)
        return self._store_knowledge_with_conflict(entry)

    def _store_preference_entry(
        self,
        entry: MemoryEntry,
        preference_key: str,
    ) -> StoreEntryResult:
        # 版本链按 scene 隔离：office 的 excel 与 data_analysis 的 python 可同时 is_latest。
        history = self._preference_history_from_storage(entry.user_id, preference_key)
        scene_scope = self._preference_scene_scope(entry.scene_id, entry.metadata)
        history = [
            item
            for item in history
            if self._preference_scene_scope(item.scene_id, item.metadata) == scene_scope
        ]
        latest = self._latest_versioned_item(history)
        candidate_item = memory_entry_to_item(entry, memory_id=str(uuid4()))

        if latest:
            latest_value = str(latest.metadata.get("preference_value") or "")
            candidate_value = str(candidate_item.metadata.get("preference_value") or "")
            same_content = latest.content.strip() == candidate_item.content.strip()
            same_value = bool(latest_value) and latest_value == candidate_value
            # 同偏好 key + 同场景 +（内容相同 或 偏好值相同）视为同一偏好，
            # 复用已有长期记录，避免摄入期(Path B)与晋升期(converge)重复入库。
            if same_content or same_value:
                return StoreEntryResult(
                    memory_id=latest.id,
                    created=False,
                    reason="unchanged_preference",
                )

        if latest and self.conflict_resolver.detect(latest, candidate_item):
            decision = self.conflict_resolver.resolve_preference(latest, candidate_item)
            if decision.action == ConflictAction.KEEP_OLD:
                return StoreEntryResult(
                    memory_id=latest.id,
                    created=False,
                    reason=decision.reason,
                )
            resolved = decision.result
            entry = memory_item_to_entry(resolved)
            entry.metadata["preference_key"] = preference_key

        result = self._commit_versioned_entry(entry, latest)
        if result.created:
            stored_item = memory_entry_to_item(entry, memory_id=result.memory_id)
            self.preference.save(stored_item)
        return result

    def _store_knowledge_entry(
        self,
        entry: MemoryEntry,
        knowledge_key: str,
    ) -> StoreEntryResult:
        history = self._knowledge_history_from_storage(entry.user_id, knowledge_key)
        scene_scope = self._preference_scene_scope(entry.scene_id, entry.metadata)
        history = [
            item
            for item in history
            if self._preference_scene_scope(item.scene_id, item.metadata) == scene_scope
        ]
        latest = self._latest_versioned_item(history)

        if latest and latest.content.strip() == entry.content.strip():
            return StoreEntryResult(
                memory_id=latest.id,
                created=False,
                reason="unchanged_knowledge",
            )

        if latest:
            entry.metadata.setdefault("conflict_action", ConflictAction.REPLACE.value)
        return self._commit_versioned_entry(entry, latest)

    def _commit_versioned_entry(
        self,
        entry: MemoryEntry,
        latest: MemoryItem | None,
    ) -> StoreEntryResult:
        next_version = int(latest.version + 1) if latest else int(entry.metadata.get("version", 1))
        entry.metadata["version"] = next_version
        entry.metadata["is_latest"] = True
        if latest:
            entry.metadata["supersedes_memory_id"] = latest.id

        response = self._create_entry(entry)
        if not response.created:
            return StoreEntryResult(
                memory_id=response.memory_id,
                created=False,
                reason=response.reason,
            )

        superseded_id: str | None = None
        if latest:
            superseded_id = latest.id
            update_memory(
                UpdateMemoryRequest(
                    memory_id=latest.id,
                    metadata={"is_latest": False},
                ),
                milvus=self._milvus,
            )
            latest.metadata["is_latest"] = False

        return StoreEntryResult(
            memory_id=response.memory_id,
            created=True,
            reason=response.reason,
            superseded_memory_id=superseded_id,
        )

    def _store_knowledge_with_conflict(self, entry: MemoryEntry) -> StoreEntryResult:
        """知识类入库：按 ``record_key`` 实体维度做冲突消解（赛题要求 3 的更新语义）。

        无 ``record_key`` 视为不可消歧的独立知识，直接写入（保持原行为）。
        有 ``record_key`` 时：
          - 内容相同则不重复写入；
          - 存在冲突（同实体不同事实）按信任度/时间 REPLACE 或 KEEP_OLD；
          - REPLACE 时软删除旧实体记录，保证召回不再返回过期事实。
        """

        record_key = str(entry.metadata.get("record_key") or "").strip()
        if not record_key:
            response = self._create_entry(entry)
            return StoreEntryResult(
                memory_id=response.memory_id,
                created=response.created,
                reason=response.reason,
            )

        existing_hits = self._milvus.search(
            None,
            {"user_id": entry.user_id, "record_key": record_key},
            top_k=10_000,
        )
        existing_items = [self._query_hit_to_memory_item(hit) for hit in existing_hits]
        new_item = memory_entry_to_item(entry, memory_id=str(uuid4()))

        if not existing_items:
            response = self._create_entry(entry)
            return StoreEntryResult(
                memory_id=response.memory_id,
                created=response.created,
                reason=response.reason,
            )

        latest = max(
            existing_items,
            key=lambda item: (item.metadata.get("is_latest") is True, item.version),
        )
        if latest.content.strip() == new_item.content.strip():
            return StoreEntryResult(
                memory_id=latest.id,
                created=False,
                reason="unchanged_knowledge",
            )

        if self.conflict_resolver.detect(latest, new_item):
            decision = self.conflict_resolver.resolve_knowledge(latest, new_item)
            if decision.action == ConflictAction.KEEP_OLD:
                return StoreEntryResult(
                    memory_id=latest.id,
                    created=False,
                    reason=decision.reason,
                )
            resolved = decision.result
            entry = memory_item_to_entry(resolved)
            entry.metadata["record_key"] = record_key

        response = self._create_entry(entry)
        if not response.created:
            return StoreEntryResult(
                memory_id=response.memory_id,
                created=False,
                reason=response.reason,
            )

        # 替换语义：软删除同实体旧记录，避免旧事实仍被召回。
        for old in existing_items:
            delete_memory(
                DeleteMemoryRequest(memory_id=old.id, user_id=entry.user_id, soft_delete=True),
                milvus=self._milvus,
            )

        return StoreEntryResult(
            memory_id=response.memory_id,
            created=True,
            reason="knowledge_replaced",
            superseded_memory_id=latest.id,
        )

    def recall(
        self,
        query: RetrievalQuery,
        *,
        filters: dict | None = None,
    ) -> RetrievalResult:
        """包装cb0aeabe query_memory，供 Agent / 前端检索。

        默认不返回已覆盖的旧条（``is_latest=false`` 的偏好或知识），避免未过滤
        召回夹带 D 盘、旧端口等过期版本。查版本历史时传
        ``filters={"is_latest": False}`` 或
        ``filters={"include_superseded_preferences": True}``（返回全部版本）。
        """

        started = perf_counter()
        # 外部 filters 不得覆盖可信 user_id（防止跨用户召回）。
        caller_filters = {k: v for k, v in (filters or {}).items() if k != "user_id"}
        include_superseded = bool(
            caller_filters.pop("include_superseded_preferences", False)
        )
        # is_latest=False 表示「不限制最新」，用于历史；不是「只查旧条」。
        if "is_latest" in caller_filters:
            explicit_latest = caller_filters.pop("is_latest")
            if explicit_latest is False:
                include_superseded = True
            elif explicit_latest is True:
                caller_filters["is_latest"] = True

        request_filters: dict = {"user_id": query.user_id}
        if query.memory_type is not None:
            if query.memory_type == MemoryType.PREFERENCE:
                request_filters["memory_type"] = MemoryTypeTag.PREFERENCE.value
            else:
                # 长期库用细分类标签；赛题/API 的 knowledge 对应非 preference。
                request_filters["memory_type"] = [
                    MemoryTypeTag.TOOL_CALL.value,
                    MemoryTypeTag.DOCUMENT_CHUNK.value,
                    MemoryTypeTag.PROJECT_CONTEXT.value,
                    MemoryTypeTag.FEEDBACK.value,
                ]
        if query.scene_id is not None:
            request_filters["scene_id"] = query.scene_id
        if caller_filters:
            request_filters.update(caller_filters)

        # 未要求历史时下推 is_latest=true，避免旧知识/旧偏好占满 top_k。
        if not include_superseded and "is_latest" not in request_filters:
            request_filters["is_latest"] = True

        from src.core.recall_ranker import (
            distinctive_terms,
            expanded_query,
            knowledge_intent,
            lexical_hits,
            merge_items,
            open_domain_semantic,
            preference_intent,
            rerank_and_filter,
        )

        # 开放域先多取候选，再按实体/项目过滤；宁可多看再丢掉，不把近邻当答案。
        fetch_k = query.top_k
        if query.memory_type is None:
            fetch_k = max(query.top_k * 4, 20)

        response = query_memory(
            QueryMemoryRequest(
                user_id=query.user_id,
                query=query.query,
                filters=request_filters,
                top_k=fetch_k,
            ),
            milvus=self._milvus,
        )

        items = [self._query_hit_to_memory_item(hit) for hit in response.items]
        if not include_superseded:
            items = [item for item in items if self._item_is_latest(item)]

        auto_semantic = (
            query.retrieval_mode == "auto"
            and query.memory_type != MemoryType.PREFERENCE
            and open_domain_semantic(query.query, items)
        )
        if query.retrieval_mode == "semantic" or auto_semantic:
            # Explicit open-domain route: preserve vector order and all trusted
            # scope/version filters, without office-topic lexical rejection.
            return RetrievalResult(items=items[:max(query.top_k, 0)],
                                   latency_ms=(perf_counter() - started) * 1000,
                                   strategy="query_memory:auto-semantic" if auto_semantic
                                   else "query_memory:semantic")

        query_text = query.query or ""
        strategy = "query_memory"
        know = knowledge_intent(query_text)
        pool = items
        missing_terms = [
            term
            for term in distinctive_terms(query_text)
            if not any(term.lower() in (item.content or "").lower() for item in items)
        ]
        # 英文通用问句已由上方 auto_semantic 提前走语义路径；其余知识问句补一轮词面
        # 扫描。open_domain_semantic 已对「命中已知项目别名 / 含办公短语的英文」放行
        # 词面补召，所以这里不再对英文做 blanket 跳过，避免误伤如 "XingHeNotes default
        # port" 这类需要词面找回的英文查询。
        should_lexical = query.memory_type != MemoryType.PREFERENCE and (
            know or bool(missing_terms)
        )
        if should_lexical:
            rewrite = expanded_query(query_text)
            extra_hits = []
            if rewrite and rewrite.strip() != query_text.strip():
                extra = query_memory(
                    QueryMemoryRequest(
                        user_id=query.user_id,
                        query=rewrite,
                        filters=request_filters,
                        top_k=fetch_k,
                    ),
                    milvus=self._milvus,
                )
                extra_hits = [self._query_hit_to_memory_item(hit) for hit in extra.items]
                if not include_superseded:
                    extra_hits = [item for item in extra_hits if self._item_is_latest(item)]
            pool = [
                self._query_hit_to_memory_item(hit)
                for hit in self._milvus.search(None, request_filters, top_k=10_000)
            ]
            if not include_superseded:
                pool = [item for item in pool if self._item_is_latest(item)]
            lexical = lexical_hits(query_text, pool)
            items = merge_items(items, extra_hits, lexical)
            strategy = "query_memory+lexical"

        if query.memory_type is None:
            items = rerank_and_filter(
                query_text,
                items,
                query.top_k,
                allow_empty=not preference_intent(query_text),
                project_context=pool,
            )
        elif knowledge_intent(query_text) and not preference_intent(query_text):
            items = rerank_and_filter(query_text, items, query.top_k, allow_empty=True, project_context=pool)
        else:
            items = items[: query.top_k]
        return RetrievalResult(
            items=items,
            latency_ms=(perf_counter() - started) * 1000,
            strategy=strategy,
        )

    def list_preferences(
        self,
        user_id: str,
        *,
        preference_key: str | None = None,
        latest_only: bool = True,
        top_k: int = 20,
    ) -> RetrievalResult:
        """按 preference_key 查询 Milvus 偏好记录（含版本历史）。"""

        filters: dict = {"memory_type": MemoryTypeTag.PREFERENCE.value}
        if preference_key is not None:
            filters["preference_key"] = preference_key
        if latest_only:
            filters["is_latest"] = True
        else:
            # 显式放开版本链，避免 recall 默认把旧偏好滤掉。
            filters["include_superseded_preferences"] = True

        return self.recall(
            RetrievalQuery(query="", user_id=user_id, top_k=top_k),
            filters=filters,
        )

    def forget(self, user_id: str, instruction: str) -> list[str]:
        """NL 遗忘：解析目标 ID → 删敏感索引 + 短期/中期/Milvus。

        P0 修复：除敏感索引外，新增对普通记忆/偏好的候选解析
        （忘掉下载路径偏好等非敏感内容的解析入口）。
        """

        # 1. 敏感信息目标解析（原有：仅匹配脱敏索引）
        target_ids = self.security.forget(user_id, instruction)
        # 2. 普通记忆/偏好目标解析（新增）
        candidates = self._forget_candidates(user_id)

        # 2.5 知识库按自然语言条件批量定位遗忘（2abac5a6）
        #     解析 NL 指令 → 命中知识意图时，用抽取的主题做定向召回，
        #     把命中的知识候选并入候选集，供后续目标解析与级联删除。
        knowledge_plan = self.security.plan_knowledge_forget(instruction)
        if knowledge_plan.is_knowledge_forget:
            try:
                knowledge_candidates = self.recall(
                    RetrievalQuery(
                        query=knowledge_plan.recall_query,
                        user_id=user_id,
                        memory_type=MemoryType.KNOWLEDGE,
                        top_k=100,
                    )
                ).items
                candidates.extend(knowledge_candidates)
            except Exception:
                # 知识库召回不可用不阻断普通记忆/偏好遗忘主流程
                pass

        resolution = self.security.parse_forget_targets(instruction, candidates)
        # 候选偏好 -> (preference_key, scope, scene_id)，用于遗忘后同步版本索引。
        pref_lookup = self._collect_pref_delete_keys(candidates)

        hash_pattern = re.compile(r"<HASH_[0-9a-f]{8}>")
        sensitive_ids: list[str] = []
        memory_ids: list[str] = []
        seen: set[str] = set()
        for tid in target_ids:
            if not tid or tid in seen:
                continue
            seen.add(tid)
            if hash_pattern.fullmatch(tid):
                sensitive_ids.append(tid)
            else:
                memory_ids.append(tid)
        # 普通记忆目标并入同一删除流程（去重）。
        for target in resolution.targets:
            if target.memory_id not in seen:
                seen.add(target.memory_id)
                memory_ids.append(target.memory_id)

        deleted: list[str] = []
        if sensitive_ids:
            deleted.extend(
                self.security.delete_sensitive_entries(user_id, sensitive_ids)
            )

        for memory_id in memory_ids:
            if self._purge_memory(user_id, memory_id):
                deleted.append(memory_id)
                # 偏好记忆：同步删除本地版本索引，保证画像一致。
                self._sync_pref_index_delete(user_id, memory_id, pref_lookup)

        # 知识库「删除全部某类」走 Milvus 批量过滤删除，覆盖 recall top_k
        # 之外的全量条目，效率高于逐条 purge。
        if knowledge_plan.is_knowledge_forget and knowledge_plan.want_all and knowledge_plan.memory_types:
            batch_filters = {
                "memory_type": knowledge_plan.memory_types,
                "nl_condition": instruction,
            }
            response = delete_memory(
                DeleteMemoryRequest(user_id=user_id, filters=batch_filters, soft_delete=True),
                milvus=self._milvus,
            )
            for did in response.deleted_ids:
                if did not in deleted:
                    deleted.append(did)

        return deleted

    @staticmethod
    def _collect_pref_delete_keys(
        candidates: list[MemoryItem],
    ) -> dict[str, tuple[str, str, str | None]]:
        """从候选偏好中提取 memory_id -> (preference_key, scope, scene_id)。"""
        lookup: dict[str, tuple[str, str, str | None]] = {}
        for cand in candidates:
            meta = cand.metadata or {}
            pref_key = meta.get("preference_key")
            if not pref_key:
                continue
            scope = meta.get("scope", "scene")
            scene_id = None if scope == "global" else (meta.get("scene_id") or cand.scene_id)
            lookup[cand.id] = (pref_key, scope, scene_id)
        return lookup

    def _sync_pref_index_delete(
        self,
        user_id: str,
        memory_id: str,
        pref_lookup: dict[str, tuple[str, str, str | None]],
    ) -> None:
        """按候选映射调用本地版本索删除偏好记录，与向量存储保持同步。"""
        spec = pref_lookup.get(memory_id)
        if not spec:
            return
        pref_key, scope, scene_id = spec
        index_getter = getattr(self.preference, "index", None)
        if index_getter is None or not hasattr(index_getter, "delete"):
            return
        index_getter.delete(user_id, pref_key, scope=scope, scene_id=scene_id, persist=True)

    def _forget_candidates(self, user_id: str) -> list[MemoryItem]:
        """汇总可被遗忘的候选：短期 + 中期 + 长期偏好 + 长期召回（含脱敏知识）。"""

        candidates: list[MemoryItem] = list(self.list_short_term(user_id, limit=100))
        candidates.extend(self.list_mid_term(user_id, limit=100))
        try:
            candidates.extend(
                self.list_preferences(user_id, latest_only=False, top_k=100).items
            )
        except Exception:
            # 长期偏好候选缺失（Milvus 未就绪）不阻断遗忘主流程。
            pass
        try:
            candidates.extend(
                self.recall(RetrievalQuery(query="", user_id=user_id, top_k=100)).items
            )
        except Exception:
            pass

        seen: set[str] = set()
        return [c for c in candidates if not (c.id in seen or seen.add(c.id))]

    def _purge_memory(self, user_id: str, memory_id: str) -> bool:
        """从短期、中期与 Milvus 中删除单条记忆。"""

        removed = False
        staging = self._staging_store()
        local_ids = {memory_id}
        milvus_ids: set[str] = set()
        if staging is not None:
            mapping = staging.resolve_memory_ids(user_id, memory_id)
            local_ids.update(mapping["memory_ids"])
            milvus_ids.update(mapping["milvus_memory_ids"])
            scrubbed = staging.forget_memory(user_id, memory_id)
            if scrubbed["events_scrubbed"] or scrubbed["raw_deleted"]:
                removed = True

        # 没有 staging 映射时，传入值可能本身就是最终 Milvus ID。
        if not milvus_ids:
            milvus_ids.add(memory_id)

        manager = self._layer_manager()
        if manager is not None:
            for local_id in local_ids:
                if manager.delete_short(user_id, local_id):
                    removed = True
                if manager.delete_mid(user_id, local_id):
                    removed = True
                if manager.delete_long(user_id, local_id):
                    removed = True

        for milvus_id in milvus_ids:
            response = delete_memory(
                DeleteMemoryRequest(memory_id=milvus_id, user_id=user_id),
                milvus=self._milvus,
            )
            if response.deleted_ids:
                removed = True
        return removed

    def purge_staging(
        self,
        *,
        raw_retention_days: int | None = None,
        audit_retention_days: int | None = None,
    ) -> dict[str, int | str | bool]:
        """手动执行 ingestion staging 保留期清理。"""

        staging = self._staging_store()
        if staging is None:
            return {
                "enabled": False,
                "raw_deleted": 0,
                "audit_deleted": 0,
                "dedup_deleted": 0,
            }
        return {
            "enabled": True,
            **staging.purge_expired(
                raw_retention_days=raw_retention_days,
                audit_retention_days=audit_retention_days,
            ),
        }

    def staging_stats(self, user_id: str | None = None) -> dict[str, object]:
        """返回 ingestion staging 拒绝分布与耗时监控。"""

        staging = self._staging_store()
        if staging is None:
            return {
                "enabled": False,
                "total": 0,
                "accepted": 0,
                "rejected": 0,
                "by_reject_code": {},
                "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
                "by_stage": {},
                "sample_size": 0,
            }
        return {"enabled": True, **staging.operational_stats(user_id)}

    def _staging_store(self) -> StagingStore | None:
        if isinstance(self.ingestion, IngestionPipeline):
            return self.ingestion.staging
        return None

    def _bind_staging_event(self, event_id: str, memory_id: str) -> None:
        staging = self._staging_store()
        if staging is None:
            return
        try:
            staging.bind_memory_id(event_id, memory_id)
        except (OSError, sqlite3.Error):
            logger.exception("SQLite staging 绑定 memory_id 失败")

    def _bind_staging_milvus_id(
        self,
        user_id: str,
        memory_id: str,
        milvus_memory_id: str,
    ) -> None:
        staging = self._staging_store()
        if staging is None:
            return
        try:
            staging.bind_milvus_id(user_id, memory_id, milvus_memory_id)
        except (OSError, sqlite3.Error):
            # Milvus 是长期权威库；这里只记录失败，不能阻塞 long pending 释放。
            logger.exception("SQLite staging 绑定 milvus_memory_id 失败")

    def _create_entry(self, entry: MemoryEntry):
        return create_memory(
            CreateMemoryRequest(entry=entry),
            milvus=self._milvus,
        )

    @staticmethod
    def _preference_scene_scope(scene_id: str | None, metadata: dict | None) -> str:
        meta = metadata or {}
        raw = scene_id or meta.get("scene_id") or "global"
        # 'global' 与 'general' 语义相同（全局场景），归一避免摄入期(Path B 写
        # 'general')与晋升期(consolidate 候选 'global')因命名差异被当成不同 scope，
        # 进而导致同一偏好的重复入库。
        if raw in ("global", "general"):
            raw = "global"
        return str(raw)

    @staticmethod
    def _metadata_field(metadata: dict | None, key: str):
        meta = metadata or {}
        value = meta.get(key)
        if value is None and isinstance(meta.get("metadata"), dict):
            value = meta["metadata"].get(key)
        return value

    @staticmethod
    def _rebalance_recall_items(query: str, items: list[MemoryItem], top_k: int) -> list[MemoryItem]:
        """开放域召回：实体重叠、项目约束、轻量重排；无关查询可返回空。"""

        from src.core.recall_ranker import rerank_and_filter

        return rerank_and_filter(query, items, top_k)

    @classmethod
    def _item_is_latest(cls, item: MemoryItem) -> bool:
        return cls._metadata_field(item.metadata, "is_latest") is not False

    @classmethod
    def _item_knowledge_key(cls, item: MemoryItem) -> str:
        return str(cls._metadata_field(item.metadata, "knowledge_key") or "").strip()

    @classmethod
    def _latest_versioned_item(cls, history: list[MemoryItem]) -> MemoryItem | None:
        if not history:
            return None
        for item in reversed(history):
            if cls._item_is_latest(item):
                return item
        return history[-1]

    def _preference_history_from_storage(
        self,
        user_id: str,
        preference_key: str,
    ) -> list[MemoryItem]:
        hits = self._milvus.search(
            None,
            {
                "user_id": user_id,
                "memory_type": MemoryTypeTag.PREFERENCE.value,
                "preference_key": preference_key,
            },
            top_k=10_000,
        )
        history = [self._query_hit_to_memory_item(hit) for hit in hits]
        history.sort(key=lambda item: item.version)
        return history

    def _knowledge_history_from_storage(
        self,
        user_id: str,
        knowledge_key: str,
    ) -> list[MemoryItem]:
        # knowledge_key 在 metadata JSON 中，向量库 schema 不能按该字段过滤。
        hits = self._milvus.search(None, {"user_id": user_id}, top_k=10_000)
        history = [
            item
            for item in (self._query_hit_to_memory_item(hit) for hit in hits)
            if self._item_knowledge_key(item) == knowledge_key
        ]
        history.sort(key=lambda item: item.version)
        return history

    @staticmethod
    def _query_hit_to_memory_item(hit: dict) -> MemoryItem:
        memory_type_raw = hit.get("memory_type", "knowledge")
        try:
            memory_type = (
                MemoryType.PREFERENCE
                if memory_type_raw == MemoryTypeTag.PREFERENCE.value
                else MemoryType.KNOWLEDGE
            )
        except (TypeError, ValueError):
            memory_type = MemoryType.KNOWLEDGE

        memory_id = str(hit.get("memory_id") or hit.get("id") or uuid4())
        metadata = {
            key: value
            for key, value in hit.items()
            if key not in {"content", "memory_id", "id", "embedding"}
        }
        if "score" in metadata:
            metadata["retrieval_score"] = metadata.pop("score")
        metadata.setdefault("milvus_memory_id", memory_id)
        nested = metadata.get("metadata")
        if isinstance(nested, dict) and not str(metadata.get("knowledge_key") or "").strip():
            nested_key = nested.get("knowledge_key")
            if nested_key:
                metadata["knowledge_key"] = nested_key

        return MemoryItem(
            id=memory_id,
            memory_type=memory_type,
            layer=MemoryLayer.LONG,
            content=str(hit.get("content", "")),
            user_id=str(hit.get("user_id", "default")),
            scene_id=hit.get("scene_id"),
            version=int(metadata.get("version", 1)),
            metadata=metadata,
        )
