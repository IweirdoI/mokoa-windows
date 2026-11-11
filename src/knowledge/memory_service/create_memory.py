"""Create memory. Owner: cb0aeabe."""

from __future__ import annotations

from uuid import uuid4

from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.memory_policy import MemoryPolicy
from src.knowledge.memory_service.memory_schema import (
    CreateMemoryRequest,
    CreateMemoryResponse,
)
from src.knowledge.memory_service.milvus_client import MilvusClient


def create_memory(
    request: CreateMemoryRequest,
    milvus: MilvusClient | None = None,
    embedding: EmbeddingClient | None = None,
    policy: MemoryPolicy | None = None,
) -> CreateMemoryResponse:
    milvus = milvus or MilvusClient()
    embedding = embedding or EmbeddingClient()
    policy = policy or MemoryPolicy()

    entry = request.entry
    ok, score, reason = policy.should_save(entry)
    if not ok:
        return CreateMemoryResponse(memory_id="", created=False, reason=reason)

    entry.save_score = score
    memory_id = str(uuid4())
    vector = embedding.embed(entry.content)

    metadata = {
        "user_id": entry.user_id,
        "memory_type": entry.memory_type.value,
        "task_type": entry.task_type.value,
        "source": entry.source.value,
        "confidence": entry.confidence,
        "save_score": score,
        "scene_id": entry.scene_id,
        "preference_key": entry.metadata.get("preference_key"),
        "preference_value": entry.metadata.get("preference_value"),
        "category": entry.metadata.get("category"),
        "version": entry.metadata.get("version", 1),
        "is_latest": entry.metadata.get("is_latest", True),
        "supersedes_memory_id": entry.metadata.get("supersedes_memory_id"),
        **entry.metadata,
    }

    created = milvus.insert(memory_id, entry.content, vector, metadata)
    return CreateMemoryResponse(memory_id=memory_id, created=created, reason=reason)

