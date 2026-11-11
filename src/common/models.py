"""Shared data models across all modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 枚举：记忆分层、记忆类型、数据来源
# ---------------------------------------------------------------------------


class MemoryLayer(str, Enum):
    """记忆存储层级：短期 / 中期 / 长期。"""

    SHORT = "short"  # 短期记忆（会话级、易过期）
    MID = "mid"  # 中期记忆（跨会话、定期整理）
    LONG = "long"  # 长期记忆（持久化、高价值信息）


class MemoryType(str, Enum):
    """记忆内容分类。"""

    PREFERENCE = "preference"  # 用户偏好（习惯、风格、设置等）
    KNOWLEDGE = "knowledge"  # 事实性知识（领域信息、上下文事实）


class DataSource(str, Enum):
    """原始数据的采集来源，用于追溯与清洗策略。"""

    TOOL_RESULT = "tool_result"  # 工具/API 调用返回
    USER_BEHAVIOR = "user_behavior"  # 用户行为埋点
    MANUAL_CONFIG = "manual_config"  # 人工或系统配置写入
    CONVERSATION = "conversation"  # 对话内容
    DOCUMENT = "document"  # 用户上传的 PDF / Word / 图片


# ---------------------------------------------------------------------------
# 数据管道模型：采集 → 清洗归一化 → 记忆存储
# ---------------------------------------------------------------------------


class RawRecord(BaseModel):
    """采集器输出的原始记录，尚未清洗。"""

    id: str  # 原始记录唯一 ID
    source: DataSource  # 数据来源
    user_id: str  # 所属用户
    scene_id: str | None = None  # 可选场景/会话 ID
    payload: dict[str, Any]  # 未结构化的原始载荷
    created_at: datetime = Field(default_factory=datetime.utcnow)  # 采集时间
    metadata: dict[str, Any] = Field(default_factory=dict)  # 扩展元数据


class NormalizedRecord(BaseModel):
    """清洗 + 归一化后的统一格式，作为 ingestion 模块的输出。"""

    type: str  # behavior | tool_result | manual_config | conversation | document
    content: str  # 归一化后的文本内容
    time: datetime = Field(default_factory=datetime.utcnow)  # 事件发生时间
    confidence: float = 0.0  # 置信度（0~1），用于后续筛选或加权
    source: DataSource  # 原始来源
    user_id: str
    scene_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryItem(BaseModel):
    """写入任意记忆层的通用记忆单元。"""

    id: str  # 记忆 ID
    memory_type: MemoryType  # 偏好 or 知识
    layer: MemoryLayer  # 所在层级（短/中/长）
    content: str  # 记忆正文
    user_id: str
    scene_id: str | None = None
    version: int = 1  # 版本号，支持更新与冲突合并
    embedding: list[float] | None = None  # 向量嵌入，供语义检索
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# 检索：查询入参与返回结果
# ---------------------------------------------------------------------------


class RetrievalQuery(BaseModel):
    """检索请求的输入参数。"""

    query: str  # 自然语言或关键词查询
    user_id: str  # 限定用户范围
    memory_type: MemoryType | None = None  # 可选：只查偏好或知识
    scene_id: str | None = None  # 可选：限定场景
    top_k: int = 5  # 返回条数上限
    retrieval_mode: str = Field(default="auto", pattern="^(auto|semantic|hybrid)$")


class RetrievalResult(BaseModel):
    """检索模块的返回结果。"""

    items: list[MemoryItem]  # 命中的记忆列表
    latency_ms: float  # 检索耗时（毫秒）
    strategy: str  # 使用的检索策略名称（如 vector / hybrid）


# ---------------------------------------------------------------------------
# 安全脱敏模型
# ---------------------------------------------------------------------------


class SensitiveEntry(BaseModel):
    """敏感信息索引条目。
    
    用于在脱敏后保留原始敏感信息到占位符的映射关系，
    实现隐私与记忆的分离。
    """

    placeholder_id: str  # 唯一占位符ID，如 <HASH_7f3a9b>
    original_content: str  # 原始敏感内容
    sensitive_type: str  # 敏感类型（如 phone, id_card, email等）
    user_id: str  # 所属用户
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_deleted: bool = False  # 标记是否已删除（软删除）
