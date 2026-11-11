"""多源数据摄取模块公开入口。

公开依赖边界：
    外部模块应仅从此处导入 ``IngestionPipeline``、``StagingStore`` 和
    结果模型，不应依赖 cleaner/normalizer 的私有实现细节。

业务边界：
    ``IngestionPipeline`` 负责把原始事件转为经质量门控的标准记录；
    ``StagingStore`` 只负责审计/去重中转。偏好分类、安全脱敏索引、
    Embedding 和 Milvus 最终入库属于下游模块。
"""

from src.ingestion.models import (
    BatchIngestionResult,
    IngestionInput,
    IngestionResult,
    PipelineStage,
    QualityDecision,
    RejectCode,
)
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.staging_store import StagingStore

__all__ = [
    "BatchIngestionResult",
    "IngestionInput",
    "IngestionPipeline",
    "IngestionResult",
    "PipelineStage",
    "QualityDecision",
    "RejectCode",
    "StagingStore",
]
