"""Ingestion 模块内部使用的结果模型。

公共模块 ``src.common.models`` 只描述团队间已经约定的数据契约。本文件则描述
摄取管线自身的执行状态、拒绝原因和质量评分明细。将两类模型分开，可以避免
其他模块被迫依赖 ingestion 的内部实现，同时让 API、测试和演示页面获得足够
详细的诊断信息。

业务规则：
    结果模型只描述 ingestion 执行事实，不表示数据已进入 Milvus。
    ``accepted=True`` 仅表示通过清洗、标准化、质量门控与去重。

算法思路：
    通过 Pydantic 在边界上验证分数范围和字段类型，使 API、测试、
    benchmark 与 SQLite 审计共用同一份结构。

依赖关系：
    依赖 ``src.common.models.NormalizedRecord`` 作为跨模块数据契约；
    由 ``pipeline`` 产生，由 API、``staging_store`` 和 Orchestrator 消费。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.common.models import NormalizedRecord


class PipelineStage(str, Enum):
    """摄取失败或结束时所在的处理阶段。

    值按管线顺序为 collect、clean、normalize、quality、deduplicate；
    ``completed`` 表示全部通过。该枚举是稳定的 API/审计字段。
    """

    COLLECT = "collect"
    CLEAN = "clean"
    NORMALIZE = "normalize"
    QUALITY = "quality"
    DEDUPLICATE = "deduplicate"
    COMPLETED = "completed"


class RejectCode(str, Enum):
    """稳定的机器可读拒绝码，供测试、监控和前端国际化使用。

    拒绝是正常业务结果，不等于程序异常；枚举值变更需同步
    API、前端、benchmark 与旧审计数据。
    """

    INVALID_SOURCE = "invalid_source"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_USER_ID = "invalid_user_id"
    EMPTY_CONTENT = "empty_content"
    DUPLICATE = "duplicate"
    LOW_QUALITY = "low_quality"
    NORMALIZATION_FAILED = "normalization_failed"


class QualityDecision(BaseModel):
    """质量评估的完整结果。

    Fields:
        accepted: ``score >= threshold`` 时为 ``True``。
        score: 最终分数，范围 ``[0, 1]``。
        threshold: 准入阈值，范围 ``[0, 1]``。
        components: 各维度未加权分数，值原则上为 ``[0, 1]``。
        reasons: 面向人工审计的中文解释，可为空列表。

    Raises:
        pydantic.ValidationError: ``score`` 或 ``threshold`` 超出范围，
            或字段类型不可转换。
    """

    accepted: bool
    score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class IngestionInput(BaseModel):
    """批量处理时使用的标准输入信封。

    ``raw`` 保留来源的原始业务字段；用户和场景信息放在信封层，避免 API 将
    ``user_id`` 混入工具日志或配置正文。为兼容现有调用，单条 ``process`` 仍
    允许从 ``raw`` 中读取这些字段。

    Fields:
        raw: 必填原始业务载荷，可以是空字典，但可能在后续被拒绝。
        source: 必填来源字符串；枚举范围由 ``DataCollector`` 验证。
        user_id: 可选用户标识；``None`` 允许，空字符串会在采集阶段拒绝。
        scene_id: 可选场景标识，可为 ``None``。
        metadata: 可选附加信息，缺省为新空字典。

    Raises:
        pydantic.ValidationError: 批量信封缺少必填字段或类型不符。
    """

    raw: dict[str, Any]
    source: str
    user_id: str | None = None
    scene_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionResult(BaseModel):
    """单条数据经过完整摄取管线后的可审计结果。

    Fields:
        accepted: 是否通过 ingestion；不代表已持久化至 Milvus。
        stage: 结束或被拒绝的阶段。
        record: 可选标准化记录；collect/clean/normalize 失败时通常为 ``None``。
        reject_code: 拒绝时的稳定代码；成功时为 ``None``。
        reasons: 质量解释或拒绝原因，可为空列表。
        quality_score: 可选分数，若已执行质量阶段则范围为 ``[0, 1]``。
        dedup_fingerprint: 可选 SHA-256 事件去重指纹，不是 memory ID。
        fingerprint: 与上字段相同的迁移期兼容别名。
        staging_event_id: 可选 SQLite 审计事件 ID；未注入 staging
            或审计写入失败时为 ``None``。
        processing_ms: 本条 ingestion 墙钟耗时毫秒数，范围 ``[0, +∞)``，
            含可选 staging 审计写入。
        stage_ms: 各阶段耗时毫秒数。键为 ``collect`` / ``clean`` /
            ``normalize`` / ``quality`` / ``deduplicate`` / ``staging``；
            未执行到的阶段不会出现。

    Raises:
        pydantic.ValidationError: 分数/耗时越界或字段类型不符。
    """

    accepted: bool
    stage: PipelineStage
    record: NormalizedRecord | None = None
    reject_code: RejectCode | None = None
    reasons: list[str] = Field(default_factory=list)
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # 正式名称：仅用于识别同一来源事件的重复投递，不代表逻辑记忆主键。
    dedup_fingerprint: str | None = None
    # 兼容字段：core/API 当前仍读取 fingerprint，待团队完成迁移后再考虑移除。
    fingerprint: str | None = None
    # SQLite staging 审计事件 ID，供 Orchestrator 在生成 memory_id 后建立精确关联。
    staging_event_id: str | None = None
    processing_ms: float = Field(default=0.0, ge=0.0)
    stage_ms: dict[str, float] = Field(default_factory=dict)


class BatchIngestionResult(BaseModel):
    """批量处理汇总；每条结果仍完整保留。

    Fields:
        total: 输入条数，应等于 ``len(results)``。
        accepted_count: ``accepted=True`` 的条数。
        rejected_count: 拒绝条数，应等于 ``total - accepted_count``。
        results: 与输入顺序一致的单条结果，可为空列表。

    业务规则：
        模型本身不重算计数；一致性由 ``process_batch`` 构造时保证。
    """

    total: int
    accepted_count: int
    rejected_count: int
    results: list[IngestionResult]
