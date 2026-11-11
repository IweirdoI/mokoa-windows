"""
Memory entry schema — shared contract between ingestion, preference, and RAG.

Owner: cb0aeabe (schema) | Review: 3fba60e1, 26a9835a, efca9634

cb0aeabe定义的五类 memory_type:
  - preference      用户偏好（26a9835a产出候选，cb0aeabe入库）
  - tool_call       工具调用经验
  - document_chunk  文档/论文片段
  - project_context 编程项目上下文
  - feedback        用户反馈（含「不要保存」信号）
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MemoryTypeTag(str, Enum):
    """Fine-grained type stored in Milvus metadata (cb0aeabe RAG 模块)."""

    PREFERENCE = "preference"
    TOOL_CALL = "tool_call"
    DOCUMENT_CHUNK = "document_chunk"
    PROJECT_CONTEXT = "project_context"
    FEEDBACK = "feedback"


class TaskType(str, Enum):
    CODING = "coding"
    PAPER_WRITING = "paper_writing"
    OFFICE = "office"
    OS_OPERATION = "os_operation"
    GLOBAL = "global"


class MemorySource(str, Enum):
    CHAT = "chat"
    AGENT_LOG = "agent_log"
    PDF = "pdf"
    REPO_SCAN = "repo_scan"
    MANUAL = "manual"


class MemoryEntry(BaseModel):
    """清洗/偏好模块输出的标准记忆条目 — create_memory 的输入."""

    content: str
    memory_type: MemoryTypeTag
    task_type: TaskType = TaskType.GLOBAL
    source: MemorySource
    user_id: str = "default"
    scene_id: str | None = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)      #置信度，由26a9835a完成
    metadata: dict[str, Any] = Field(default_factory=dict)      #张嘉询
    # 多维评价后由 memory_policy 填充
    save_score: float | None = None


class CreateMemoryRequest(BaseModel):
    entry: MemoryEntry


class CreateMemoryResponse(BaseModel):
    memory_id: str
    created: bool
    reason: str = ""


class QueryMemoryRequest(BaseModel):
    user_id: str
    query: str | None = None
    memory_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = 5


class QueryMemoryResponse(BaseModel):
    items: list[dict[str, Any]]
    latency_ms: float = 0.0


class UpdateMemoryRequest(BaseModel):
    memory_id: str
    content: str | None = None
    metadata: dict[str, Any] | None = None


class UpdateMemoryResponse(BaseModel):
    memory_id: str
    updated: bool
    old_content: str | None = None
    new_content: str | None = None


class DeleteMemoryRequest(BaseModel):
    memory_id: str | None = None
    user_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    nl_condition: str | None = None
    soft_delete: bool = True

class DeleteMemoryResponse(BaseModel):
    deleted_ids: list[str]
    count: int
