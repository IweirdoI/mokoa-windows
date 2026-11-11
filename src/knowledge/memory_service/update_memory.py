"""Update memory. Owner: cb0aeabe."""

from __future__ import annotations

from datetime import datetime

from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.memory_policy import MemoryPolicy
from src.knowledge.memory_service.memory_schema import UpdateMemoryRequest, UpdateMemoryResponse
from src.knowledge.memory_service.milvus_client import MilvusClient


def update_memory(
    request: UpdateMemoryRequest,
    milvus: MilvusClient | None = None,
    embedding: EmbeddingClient | None = None,
    policy: MemoryPolicy | None = None,
) -> UpdateMemoryResponse:
    milvus = milvus or MilvusClient()
    embedding = embedding or EmbeddingClient()
    policy = policy or MemoryPolicy()

    existing = milvus.get_by_id(request.memory_id)
    if not existing:
        return UpdateMemoryResponse(memory_id=request.memory_id, updated=False)

    old_content = str(existing.get("content") or "")
    fields: dict = {"updated_at": datetime.utcnow().isoformat() + "Z"}
    new_embedding = None

    if request.content and policy.should_update(old_content, request.content):
        fields["content"] = request.content
        new_embedding = embedding.embed(request.content)
        fields["version"] = int(existing.get("version") or 1) + 1

    if request.metadata:
        fields.update(request.metadata)

    updated = milvus.update(request.memory_id, fields, new_embedding)
    return UpdateMemoryResponse(
        memory_id=request.memory_id,
        updated=updated,
        old_content=old_content,
        new_content=fields.get("content"),
    )