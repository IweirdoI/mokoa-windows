"""多源摄取管线统一门面。

该类串联采集、清洗、标准化、质量评分与去重，并同时提供两类接口：

* ``process`` / ``process_batch``：正式接口，返回完整、可审计的处理结果；
* ``ingest`` / ``validate`` / ``run_pipeline``：保留给现有团队代码的兼容接口。

业务规则：
    固定顺序为 collect → clean → normalize → quality → deduplicate。
    只有通过质量门控的指纹才会登记去重；所有成功/拒绝分支都尝试审计。

算法思路：
    每个阶段只消费上一阶段的统一模型，可预期坏数据转为稳定
    ``RejectCode``，未预期编程错误继续抛出。SQLite 审计/去重失效时
    采用 fail-open 或内存回退，保持主业务契约。

依赖关系：
    编排 ``DataCollector``、``DataCleaner``、``DataNormalizer``、
    ``QualityEvaluator`` 和可选 ``StagingStore``；实现
    ``src.common.interfaces.IngestionAdapter`` 供 Orchestrator/API 调用。

Owner: 3fba60e1
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from src.common.interfaces import IngestionAdapter
from src.common.models import NormalizedRecord, RawRecord
from src.ingestion.cleaner import DataCleaner
from src.ingestion.collector import CollectionError, DataCollector
from src.ingestion.models import (
    BatchIngestionResult,
    IngestionInput,
    IngestionResult,
    PipelineStage,
    RejectCode,
)
from src.ingestion.normalizer import DataNormalizer, NormalizationError
from src.ingestion.quality import QualityEvaluator
from src.ingestion.staging_store import StagingStore
from src.security.service import detect_privacy_risk


logger = logging.getLogger(__name__)


class IngestionPipeline(IngestionAdapter):
    """面向 Orchestrator 的四阶段摄取管线。

    Args:
        quality_threshold: 质量准入阈值，默认与 ``configs/default.yaml`` 一致。
        max_text_length: 单个正文相关字段的最大字符数，避免日志无限膨胀。
        dedup_ttl_seconds: 已接收指纹的去重窗口；传 0 可关闭跨请求去重。
        staging_store: 可选 SQLite staging；``None`` 使用进程内去重且不写审计。

    Raises:
        ValueError: 质量阈值不在 ``[0, 1]``、正文上限小于 1，
            或去重 TTL 小于 0。
    """

    def __init__(
        self,
        quality_threshold: float = 0.5,
        *,
        max_text_length: int = 8000,
        dedup_ttl_seconds: float = 300.0,
        staging_store: StagingStore | None = None,
        quality_weights: dict[str, float] | None = None,
        quality_type_thresholds: dict[str, float] | None = None,
    ) -> None:
        """初始化摄取管线及可选的 SQLite 中转库。

        直接构造 ``IngestionPipeline()`` 时仍使用原有内存去重，保证单元测试、
        临时脚本和旧调用方互不共享状态。正式 API 由 ``src.core.factory`` 根据
        ``configs/default.yaml`` 创建并注入 ``StagingStore``。

        Args:
            quality_threshold: ``[0, 1]`` 内的准入阈值，不可为 ``None``。
            max_text_length: 每个文本字段最大字符数，必须大于 0。
            dedup_ttl_seconds: 非负 TTL 秒数；0 关闭去重登记。
            staging_store: 已初始化的 ``StagingStore`` 或 ``None``。
            quality_weights: 可选覆盖默认质量维度权重。
            quality_type_thresholds: 可选按 ``record.type`` 覆盖的准入阈值；``None`` 时
                使用 ``QualityEvaluator`` 内置默认（见 ``configs/default.yaml`` 的
                ``memory.quality_type_thresholds``）。

        Raises:
            ValueError: 任一数值参数越界。

        Dependencies:
            本方法只组装对象；SQLite 连接的创建由调用方/
            ``src.core.factory`` 负责。
        """

        self.collector = DataCollector()
        self.cleaner = DataCleaner(
            max_text_length=max_text_length,
            dedup_ttl_seconds=dedup_ttl_seconds,
        )
        self.normalizer = DataNormalizer()
        self.quality = QualityEvaluator(
            threshold=quality_threshold,
            weights=quality_weights,
            type_thresholds=quality_type_thresholds,
        )
        self.staging = staging_store
        # 无 staging 时，按指纹累计已见过的行为次数；有 staging 则读审计表。
        self._behavior_seen: dict[str, int] = {}
        self._behavior_seen_lock = threading.Lock()

    def ingest(self, raw: dict[str, Any], source: str) -> RawRecord:
        """兼容公共接口：只执行采集。

        Args:
            raw: 必填原始字典，不可为 ``None``。
            source: 必填支持的来源枚举字符串。
        Returns:
            仅完成信封封装的 ``RawRecord``。
        Raises:
            CollectionError: payload、source 或用户标识不符合采集契约。

        业务规则：
            不清洗、不评分、不去重、不写 staging；新代码应优先用 ``process``。
        """

        return self.collector.collect(raw, source)

    def validate(self, record: RawRecord) -> bool:
        """兼容公共接口：校验一条 ``RawRecord`` 是否具有入库价值。

        此方法不会登记去重指纹，因此反复调用校验不会影响真正的 ``process``。

        Args:
            record: 必填 ``RawRecord``，不可为 ``None``。
        Returns:
            清洗后存在正文、能成功标准化且质量达标时为 ``True``。
        Raises:
            AttributeError: 输入不符合 ``RawRecord`` 契约。可预期的标准化失败
                会转为 ``False``，不抛出。

        Algorithm:
            clean → normalize → evaluate；不执行采集、去重或审计。
        """

        cleaned = self.cleaner.clean(record)
        if cleaned is None:
            return False
        try:
            normalized = self.normalizer.normalize(cleaned)
        except NormalizationError:
            return False
        return self.quality.evaluate(normalized).accepted

    def run_pipeline(self, raw: dict[str, Any], source: str) -> NormalizedRecord | None:
        """兼容旧 Orchestrator 的简化全流程接口。

        Args:
            raw: 必填原始字典，不可为 ``None``。
            source: 必填来源字符串。
        Returns:
            通过时返回 ``NormalizedRecord``，任一业务拒绝返回 ``None``。
        Raises:
            Exception: ``process`` 中未被定义为可预期业务拒绝的程序/基础设施异常。

        业务规则：
            该接口会丢失拒绝码和评分明细；新代码应使用 ``process``。
        """

        result = self.process(raw, source)
        return result.record if result.accepted else None

    def process_file(
        self,
        filename: str,
        data: bytes,
        *,
        user_id: str | None = None,
        scene_id: str | None = None,
        caption: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestionResult:
        """解析 PDF / Word / 图片后走正式 ``process`` 管线。

        解析失败转为 ``invalid_payload`` / ``invalid_source`` 业务拒绝，
        不抛给 API 变成 500。
        """

        from src.ingestion.file_extract import FileExtractionError, extract_document

        started_at = time.perf_counter()
        try:
            payload = extract_document(filename, data, caption=caption)
        except FileExtractionError as exc:
            code = (
                RejectCode.INVALID_SOURCE
                if exc.code == "unsupported_file"
                else RejectCode.INVALID_PAYLOAD
            )
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.COLLECT,
                    code=code,
                    reasons=[str(exc)],
                    started_at=started_at,
                    stage_ms={"collect": self._elapsed_ms(started_at)},
                ),
                raw={"filename": filename, "caption": caption},
                source="document",
                user_id=user_id,
                scene_id=scene_id,
                started_at=started_at,
                stage_ms={"collect": self._elapsed_ms(started_at)},
            )
        except RecursionError:
            # 深层嵌套的畸形 XML（如 BPMN）会触发递归解析上限；必须在此兜底，
            # 否则异常会穿透本层变成 API 500，违反“解析失败转业务拒绝”的契约。
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.COLLECT,
                    code=RejectCode.INVALID_PAYLOAD,
                    reasons=["XML/BPMN 嵌套过深，解析中止"],
                    started_at=started_at,
                    stage_ms={"collect": self._elapsed_ms(started_at)},
                ),
                raw={"filename": filename, "caption": caption},
                source="document",
                user_id=user_id,
                scene_id=scene_id,
                started_at=started_at,
                stage_ms={"collect": self._elapsed_ms(started_at)},
            )
        return self.process(
            payload,
            "document",
            user_id=user_id,
            scene_id=scene_id,
            metadata=metadata,
        )

    def process(
        self,
        raw: Mapping[str, Any],
        source: str,
        *,
        user_id: str | None = None,
        scene_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestionResult:
        """处理单条来源数据并返回完整审计结果。

        Args:
            raw: 必填原始业务映射，不可为 ``None``；可为空映射，
                但通常会在 clean/normalize 阶段被拒绝。
            source: 必填来源字符串，范围见 ``DataSource``。
            user_id: 可选用户标识；``None`` 允许回退到 raw，空字符串拒绝。
            scene_id: 可选场景标识，可为 ``None``。
            metadata: 可选追踪 metadata，``None`` 表示不附加。

        Returns:
            始终返回 ``IngestionResult``。可预期坏数据使 ``accepted=False``
            并携带 stage/reject_code/reasons；成功时携带 record、评分、指纹和
            可选 staging event ID。

        Raises:
            ValueError: 内部契约或严格参数校验异常，且未被定义为业务拒绝。
            OSError: 除已降级处理的 SQLite 操作外，其他未预期 I/O 失败。
            Exception: 未预期程序错误保持向上传播，不静默伪装成业务拒绝。

        Business Rules:
            只有质量通过的数据才登记去重；拒绝不是 HTTP 500；
            staging 审计写失败不改变已形成的业务结果。
            ``user_behavior`` 在评分前按同指纹历史次数抬升
            ``occurrence_count``，调用方已提供的更大频次仍优先。

        Algorithm:
            collect → clean → fingerprint → normalize →
            accumulate-behavior-count → quality →
            register-if-new → build result → append staging audit。任一业务失败
            立即构造对应阶段的拒绝结果，但仍统一进入审计收尾。
            返回结果含 ``stage_ms`` 分阶段耗时。

        已知的坏数据会转换为稳定拒绝码；意外的程序错误不会被静默吞掉，便于开发
        和 CI 尽早暴露真正缺陷。
        """

        started_at = time.perf_counter()
        stage_started = started_at
        stage_ms: dict[str, float] = {}
        try:
            collected = self.collector.collect(
                raw,
                source,
                user_id=user_id,
                scene_id=scene_id,
                metadata=metadata,
            )
        except CollectionError as exc:
            code_map = {
                "invalid_source": RejectCode.INVALID_SOURCE,
                "invalid_user_id": RejectCode.INVALID_USER_ID,
                "invalid_payload": RejectCode.INVALID_PAYLOAD,
            }
            self._mark_stage(stage_ms, PipelineStage.COLLECT.value, stage_started)
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.COLLECT,
                    code=code_map.get(exc.code, RejectCode.INVALID_PAYLOAD),
                    reasons=[str(exc)],
                    started_at=started_at,
                    stage_ms=stage_ms,
                ),
                raw=raw,
                source=source,
                user_id=user_id,
                scene_id=scene_id,
                started_at=started_at,
                stage_ms=stage_ms,
            )

        stage_started = self._mark_stage(
            stage_ms, PipelineStage.COLLECT.value, stage_started
        )
        cleaned = self.cleaner.clean(collected)
        if cleaned is None:
            self._mark_stage(stage_ms, PipelineStage.CLEAN.value, stage_started)
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.CLEAN,
                    code=RejectCode.EMPTY_CONTENT,
                    reasons=["清洗后没有可形成记忆的有效正文"],
                    started_at=started_at,
                    stage_ms=stage_ms,
                ),
                raw=raw,
                source=source,
                user_id=collected.user_id,
                scene_id=collected.scene_id,
                started_at=started_at,
                stage_ms=stage_ms,
            )

        stage_started = self._mark_stage(
            stage_ms, PipelineStage.CLEAN.value, stage_started
        )
        dedup_fingerprint = str(
            cleaned.metadata.get("dedup_fingerprint")
            or cleaned.metadata.get("fingerprint", "")
        )
        try:
            normalized = self.normalizer.normalize(cleaned)
        except NormalizationError as exc:
            self._mark_stage(stage_ms, PipelineStage.NORMALIZE.value, stage_started)
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.NORMALIZE,
                    code=RejectCode.NORMALIZATION_FAILED,
                    reasons=[str(exc)],
                    dedup_fingerprint=dedup_fingerprint,
                    started_at=started_at,
                    stage_ms=stage_ms,
                ),
                raw=raw,
                source=source,
                user_id=collected.user_id,
                scene_id=collected.scene_id,
                started_at=started_at,
                stage_ms=stage_ms,
            )

        stage_started = self._mark_stage(
            stage_ms, PipelineStage.NORMALIZE.value, stage_started
        )
        if normalized.type == "behavior":
            from src.core.persona_query import is_persona_as_user_behavior

            action = str(normalized.metadata.get("event_type") or "")
            if is_persona_as_user_behavior(normalized.content, action=action):
                return self._finalize_result(
                    self._rejected(
                        stage=PipelineStage.NORMALIZE,
                        code=RejectCode.INVALID_PAYLOAD,
                        reasons=[
                            "人设自我描述请走 conversation，user_behavior 只接受操作+目标+频次"
                        ],
                        record=normalized,
                        dedup_fingerprint=dedup_fingerprint,
                        started_at=started_at,
                        stage_ms=stage_ms,
                    ),
                    raw=raw,
                    source=source,
                    user_id=normalized.user_id,
                    scene_id=normalized.scene_id,
                    started_at=started_at,
                    stage_ms=stage_ms,
                )
        self._apply_behavior_occurrence(normalized, dedup_fingerprint)
        self._mark_supported_preferences(normalized)
        if detect_privacy_risk(normalized.content):
            normalized.metadata["privacy_risk"] = True
        decision = self.quality.evaluate(normalized)
        # 质量明细放入 metadata，后续 Orchestrator、前端与 benchmark 使用同一份依据。
        normalized.metadata["quality_score"] = decision.score
        normalized.metadata["quality_threshold"] = decision.threshold
        normalized.metadata["quality_components"] = decision.components
        normalized.metadata["quality_reasons"] = decision.reasons

        if not decision.accepted:
            self._mark_stage(stage_ms, PipelineStage.QUALITY.value, stage_started)
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.QUALITY,
                    code=RejectCode.LOW_QUALITY,
                    reasons=decision.reasons,
                    record=normalized,
                    quality_score=decision.score,
                    dedup_fingerprint=dedup_fingerprint,
                    started_at=started_at,
                    stage_ms=stage_ms,
                ),
                raw=raw,
                source=source,
                user_id=normalized.user_id,
                scene_id=normalized.scene_id,
                started_at=started_at,
                stage_ms=stage_ms,
            )

        stage_started = self._mark_stage(
            stage_ms, PipelineStage.QUALITY.value, stage_started
        )
        # 只登记已经通过质量门控的数据。这样低质量的偶然行为不会阻止后续同内容、
        # 但更明确或更高频的数据进入系统。
        if not self._register_if_new(dedup_fingerprint, normalized.user_id):
            self._mark_stage(stage_ms, PipelineStage.DEDUPLICATE.value, stage_started)
            return self._finalize_result(
                self._rejected(
                    stage=PipelineStage.DEDUPLICATE,
                    code=RejectCode.DUPLICATE,
                    reasons=["同一来源事件在去重窗口内已被接收"],
                    record=normalized,
                    quality_score=decision.score,
                    dedup_fingerprint=dedup_fingerprint,
                    started_at=started_at,
                    stage_ms=stage_ms,
                ),
                raw=raw,
                source=source,
                user_id=normalized.user_id,
                scene_id=normalized.scene_id,
                started_at=started_at,
                stage_ms=stage_ms,
            )

        self._mark_stage(stage_ms, PipelineStage.DEDUPLICATE.value, stage_started)
        return self._finalize_result(
            IngestionResult(
                accepted=True,
                stage=PipelineStage.COMPLETED,
                record=normalized,
                reasons=decision.reasons,
                quality_score=decision.score,
                dedup_fingerprint=dedup_fingerprint,
                # 兼容当前 Orchestrator；两字段在迁移期保持相同。
                fingerprint=dedup_fingerprint,
                processing_ms=self._elapsed_ms(started_at),
                stage_ms=stage_ms,
            ),
            raw=raw,
            source=source,
            user_id=normalized.user_id,
            scene_id=normalized.scene_id,
            started_at=started_at,
            stage_ms=stage_ms,
        )

    def process_batch(
        self,
        inputs: Iterable[IngestionInput | Mapping[str, Any]],
    ) -> BatchIngestionResult:
        """顺序处理一批输入，并汇总通过/拒绝数量。

        顺序处理是有意设计：同一批中的后续重复项可以复用单条接口的 TTL 去重
        语义。未来如需并行，必须先明确同批重复项的确定性胜出规则。

        Args:
            inputs: ``IngestionInput`` 或可由 Pydantic 验证的映射可迭代对象；
                允许为空，不应为 ``None``。
        Returns:
            ``BatchIngestionResult``，计数与逐条结果顺序均与输入对齐。
        Raises:
            TypeError: ``inputs`` 本身不可迭代。单条信封的验证错误会转换为
                ``invalid_payload`` 结果，不中断其他条目。
            Exception: 单条 ``process`` 的未预期异常保持向上传播。

        Algorithm:
            按输入顺序验证信封并调用 ``process``，最后线性统计通过数。
        """

        results: list[IngestionResult] = []
        for item in inputs:
            try:
                envelope = (
                    item
                    if isinstance(item, IngestionInput)
                    else IngestionInput.model_validate(item)
                )
            except (ValidationError, TypeError) as exc:
                results.append(
                    IngestionResult(
                        accepted=False,
                        stage=PipelineStage.COLLECT,
                        reject_code=RejectCode.INVALID_PAYLOAD,
                        reasons=[f"批量输入信封不符合规范：{exc}"],
                    )
                )
                continue

            results.append(
                self.process(
                    envelope.raw,
                    envelope.source,
                    user_id=envelope.user_id,
                    scene_id=envelope.scene_id,
                    metadata=envelope.metadata,
                )
            )

        accepted_count = sum(1 for result in results if result.accepted)
        return BatchIngestionResult(
            total=len(results),
            accepted_count=accepted_count,
            rejected_count=len(results) - accepted_count,
            results=results,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        """返回从 ``perf_counter`` 起点至今的非负毫秒数，保留四位小数。"""

        return round(max(0.0, (time.perf_counter() - started_at) * 1000), 4)

    @classmethod
    def _mark_stage(
        cls,
        stage_ms: dict[str, float],
        name: str,
        started_at: float,
    ) -> float:
        """记录某一阶段耗时，并返回下一阶段的起点。"""

        now = time.perf_counter()
        stage_ms[name] = round(max(0.0, (now - started_at) * 1000), 4)
        return now

    @staticmethod
    def _looks_like_preference_query(content: str) -> bool:
        """问句/回忆句不应当成新偏好写入。"""

        text = (content or "").strip()
        if not text:
            return False
        if any(token in text for token in ("以后", "请记住", "默认", "请更", "不要", "必须")):
            return False
        lowered = text.lower()
        if re.search(
            r"(?:do|does|did)\s+(?:you|we|they)\s+(?:like|love|hate|prefer)\b",
            lowered,
        ):
            return True
        return any(
            token in text
            for token in (
                "你记得",
                "记得我",
                "是什么",
                "是多少",
                "在哪",
                "怎么办",
                "吗？",
                "吗?",
            )
        )

    @classmethod
    def _mark_supported_preferences(cls, normalized: NormalizedRecord) -> None:
        """质量评分前识别已支持的明确偏好，不降低全局 0.6 阈值。"""

        if normalized.type != "conversation":
            return
        if cls._looks_like_preference_query(normalized.content):
            return
        try:
            from src.preference.extractor import extract_by_rules
        except Exception:
            return
        hits = extract_by_rules(normalized.content, normalized.scene_id)
        if not hits:
            return
        normalized.metadata["explicit"] = True
        normalized.metadata["supported_preference_keys"] = [
            f"{item.category}.{item.key}" for item in hits
        ]

    def _apply_behavior_occurrence(
        self,
        normalized: NormalizedRecord,
        dedup_fingerprint: str,
    ) -> None:
        """在质量评分前，把同指纹行为的历史次数折算进 ``occurrence_count``。

        调用方若已提供更大的频次，仍以调用方为准；否则使用
        ``历史审计条数 + 1``。无 staging 时退回进程内计数。
        只处理 ``behavior``；空指纹不累计。本方法会就地更新 metadata。
        """

        if normalized.type != "behavior" or not dedup_fingerprint:
            return

        prior = self._prior_behavior_count(dedup_fingerprint, normalized.user_id)
        try:
            payload_count = max(1, int(normalized.metadata.get("occurrence_count", 1)))
        except (TypeError, ValueError):
            payload_count = 1
        effective = max(payload_count, prior + 1)
        normalized.metadata["occurrence_count"] = effective
        normalized.metadata["occurrence_prior_count"] = prior
        if effective > payload_count:
            normalized.metadata["occurrence_accumulated"] = True
        self._remember_behavior_occurrence(dedup_fingerprint)

    def _prior_behavior_count(self, dedup_fingerprint: str, user_id: str) -> int:
        """读取同指纹已出现次数；staging 不可用时使用进程内计数。"""

        if self.staging is not None:
            try:
                return self.staging.count_events(
                    user_id,
                    dedup_fingerprint=dedup_fingerprint,
                )
            except (OSError, sqlite3.Error):
                logger.exception("SQLite 行为频次查询失败，回退到进程内计数")
        with self._behavior_seen_lock:
            return self._behavior_seen.get(dedup_fingerprint, 0)

    def _remember_behavior_occurrence(self, dedup_fingerprint: str) -> None:
        """无 staging 时登记本次行为，供后续请求累计。"""

        if self.staging is not None or not dedup_fingerprint:
            return
        with self._behavior_seen_lock:
            self._behavior_seen[dedup_fingerprint] = (
                self._behavior_seen.get(dedup_fingerprint, 0) + 1
            )

    @classmethod
    def _rejected(
        cls,
        *,
        stage: PipelineStage,
        code: RejectCode,
        reasons: list[str],
        started_at: float,
        record: NormalizedRecord | None = None,
        quality_score: float | None = None,
        dedup_fingerprint: str | None = None,
        stage_ms: dict[str, float] | None = None,
    ) -> IngestionResult:
        """集中构造拒绝结果，保证所有失败分支字段一致。

        ``stage``/``code``/``reasons``/``started_at`` 必填；其余字段可为 ``None``。
        返回 ``accepted=False`` 的 ``IngestionResult``，不写 staging 且不抛出业务异常。
        """

        return IngestionResult(
            accepted=False,
            stage=stage,
            record=record,
            reject_code=code,
            reasons=reasons,
            quality_score=quality_score,
            dedup_fingerprint=dedup_fingerprint,
            # 迁移期保持兼容，当前 Orchestrator 仍从该字段读取值。
            fingerprint=dedup_fingerprint,
            processing_ms=cls._elapsed_ms(started_at),
            stage_ms=dict(stage_ms or {}),
        )

    def _register_if_new(self, dedup_fingerprint: str, user_id: str) -> bool:
        """使用 SQLite 原子去重；存储不可用时退回原有内存实现。

        staging 是增强审计能力，不应让临时磁盘故障把原本合法的 ingestion 请求
        变成服务器错误。因此这里只捕获操作系统和 SQLite 基础设施异常，并打印
        完整堆栈；参数错误、代码错误仍会正常暴露，避免开发期静默掩盖缺陷。

        Args:
            dedup_fingerprint: 非空去重指纹，SQLite 路径会严格校验。
            user_id: 非空用户标识，作为去重审计归属。
        Returns:
            本 TTL 窗口内首次登记返回 ``True``，重复返回 ``False``。
        Raises:
            ValueError: staging 启用时指纹为空/类型错误。
            Exception: 非 ``OSError``/``sqlite3.Error`` 的编程错误不会降级。
        """

        if self.staging is not None:
            try:
                return self.staging.register_if_new(dedup_fingerprint, user_id)
            except (OSError, sqlite3.Error):
                logger.exception("SQLite 去重不可用，当前请求退回进程内 TTL 去重")
        return self.cleaner.register_if_new(dedup_fingerprint)

    def release_dedup(self, dedup_fingerprint: str | None) -> bool:
        """下游写入失败时释放指纹，允许同一事件在 TTL 内重试。

        不删除 staging 审计行。staging 与进程内缓存都会尝试释放，
        以覆盖 fail-open 回退到内存去重的情况。

        Args:
            dedup_fingerprint: 可选指纹；空值视为无需释放。
        Returns:
            任一后端删除到指纹时为 ``True``。
        """

        if not dedup_fingerprint:
            return False
        released = False
        if self.staging is not None:
            try:
                released = self.staging.release_fingerprint(dedup_fingerprint) or released
            except (OSError, sqlite3.Error):
                logger.exception("SQLite 去重释放失败，继续尝试进程内释放")
        return self.cleaner.release_fingerprint(dedup_fingerprint) or released

    def _finalize_result(
        self,
        result: IngestionResult,
        *,
        raw: Mapping[str, Any] | Any,
        source: Any,
        user_id: str | None,
        scene_id: str | None,
        started_at: float,
        stage_ms: dict[str, float],
    ) -> IngestionResult:
        """在返回结果前统一写审计，确保成功与所有拒绝分支行为一致。

        collect 阶段可能在 ``RawRecord`` 生成前失败，因此这里不能假设 result 中
        一定存在 record。缺少合法身份时使用 ``default`` 作为审计兜底；原始空
        字符串仍保留在 raw archive 中，方便定位调用方为何被拒绝。

        Args:
            result: 已构造的成功或拒绝结果。
            raw: 原始载荷；非 ``Mapping`` 时不归档 raw。
            source: 原始来源值，可在 collect 失败时仍被审计。
            user_id: 可选信封用户 ID。
            scene_id: 可选信封场景 ID。
            started_at: ``process`` 入口的 ``perf_counter`` 起点。
            stage_ms: 已记录的各阶段耗时；本方法会补 ``staging``。
        Returns:
            原 ``result`` 对象；审计成功时已填入 ``staging_event_id``，
            ``processing_ms`` / ``stage_ms`` 已按墙钟更新。
        Raises:
            Exception: 非 ``OSError``/``sqlite3.Error`` 的编程错误保持上抛。

        业务规则：
            未注入 staging 仍回写耗时；SQLite 基础设施异常记录日志但不篡改业务结果。
        """

        if self.staging is None:
            result.stage_ms = dict(stage_ms)
            result.processing_ms = self._elapsed_ms(started_at)
            return result

        audit_user_id = self._audit_text(
            result.record.user_id if result.record is not None else user_id,
            fallback=(raw.get("user_id") if isinstance(raw, Mapping) else None),
        ) or "default"
        audit_scene_id = self._audit_text(
            result.record.scene_id if result.record is not None else scene_id,
            fallback=(raw.get("scene_id") if isinstance(raw, Mapping) else None),
        )
        raw_archive = dict(raw) if isinstance(raw, Mapping) else None

        staging_started = time.perf_counter()
        try:
            result.staging_event_id = self.staging.append_event(
                result,
                user_id=audit_user_id,
                scene_id=audit_scene_id,
                source=source,
                raw=raw_archive,
            )
        except (OSError, sqlite3.Error):
            # 审计写入遵循 fail-open：记录错误但不篡改已经形成的业务结果。这样
            # SQLite 短暂锁定或磁盘故障不会破坏 process() 的历史返回契约。
            logger.exception("SQLite ingestion 事件审计写入失败")
        self._mark_stage(stage_ms, "staging", staging_started)
        result.stage_ms = dict(stage_ms)
        result.processing_ms = self._elapsed_ms(started_at)
        return result

    @staticmethod
    def _audit_text(value: Any, *, fallback: Any = None) -> str | None:
        """安全提取审计信封文本。

        ``value`` 非 ``None`` 时优先，否则使用 ``fallback``；值经 ``str``
        转换和 strip，空字符串与两者均缺失时返回 ``None``。
        """

        candidate = value if value is not None else fallback
        if candidate is None:
            return None
        text = str(candidate).strip()
        return text or None
