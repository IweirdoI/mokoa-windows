"""MemoryItem ↔ MemoryEntry 转换 — Orchestrator 与 memory_service 衔接."""

from __future__ import annotations

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.knowledge.memory_service.memory_schema import (
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    TaskType,
)


_SOURCE_TYPE_TO_MEMORY_SOURCE: dict[str, MemorySource] = {
    "manual_config": MemorySource.MANUAL,
    "conversation": MemorySource.CHAT,
    "tool_result": MemorySource.AGENT_LOG,
    "behavior": MemorySource.CHAT,
}

_SCENE_TASK_HINTS: dict[str, TaskType] = {
    "coding": TaskType.CODING,
    "paper": TaskType.PAPER_WRITING,
    "office": TaskType.OFFICE,
    "os_operation": TaskType.OS_OPERATION,
}


def memory_item_to_entry(item: MemoryItem) -> MemoryEntry:
    """将 Orchestrator 层 MemoryItem 转为cb0aeabe create_memory 所需的 MemoryEntry."""

    metadata = dict(item.metadata)
    source_type = str(metadata.get("source_type", ""))
    memory_type = _memory_type_tag(item.memory_type, source_type)
    task_type = _task_type(item, metadata)
    source = _memory_source(source_type, metadata)

    if item.memory_type == MemoryType.PREFERENCE and "preference_key" not in metadata:
        if config_key := metadata.get("config_key"):
            metadata["preference_key"] = config_key

    return MemoryEntry(
        content=item.content,
        memory_type=memory_type,
        task_type=task_type,
        source=source,
        user_id=item.user_id,
        scene_id=item.scene_id,
        confidence=float(metadata.get("confidence", 0.7)),
        metadata=metadata,
    )


def memory_entry_to_item(entry: MemoryEntry, *, memory_id: str) -> MemoryItem:
    """将 MemoryEntry 与 Milvus memory_id 转回 MemoryItem，供本地版本索引与冲突消解使用."""

    metadata = dict(entry.metadata)
    metadata.setdefault("milvus_memory_id", memory_id)
    metadata.setdefault("is_latest", True)
    # 与 create_memory 落库字段保持一致，使冲突消解的信任度计算
    # （_trust_score / _source_rank 读取 metadata 的 source/confidence）对
    # 由 MemoryEntry 新构造的 MemoryItem 也能正确生效。
    metadata.setdefault("source", entry.source.value)
    metadata.setdefault("confidence", entry.confidence)

    memory_type = (
        MemoryType.PREFERENCE
        if entry.memory_type == MemoryTypeTag.PREFERENCE
        else MemoryType.KNOWLEDGE
    )

    return MemoryItem(
        id=memory_id,
        memory_type=memory_type,
        layer=MemoryLayer.LONG,
        content=entry.content,
        user_id=entry.user_id,
        scene_id=entry.scene_id,
        version=int(metadata.get("version", 1)),
        metadata=metadata,
    )


def _memory_type_tag(memory_type: MemoryType, source_type: str) -> MemoryTypeTag:
    if memory_type == MemoryType.PREFERENCE:
        return MemoryTypeTag.PREFERENCE
    if source_type == "tool_result":
        return MemoryTypeTag.TOOL_CALL
    return MemoryTypeTag.DOCUMENT_CHUNK


def _memory_source(source_type: str, metadata: dict) -> MemorySource:
    if raw := metadata.get("source"):
        try:
            return MemorySource(str(raw))
        except ValueError:
            pass
    return _SOURCE_TYPE_TO_MEMORY_SOURCE.get(source_type, MemorySource.CHAT)


def _task_type(item: MemoryItem, metadata: dict) -> TaskType:
    if raw := metadata.get("task_type"):
        try:
            return TaskType(str(raw))
        except ValueError:
            pass

    scene = (item.scene_id or "").lower()
    for hint, task in _SCENE_TASK_HINTS.items():
        if hint in scene:
            return task

    if metadata.get("source_type") == "tool_result":
        return TaskType.OS_OPERATION
    if metadata.get("config_key"):
        return TaskType.GLOBAL
    return TaskType.GLOBAL
