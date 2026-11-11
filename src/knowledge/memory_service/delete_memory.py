"""Delete memory. Owner: cb0aeabe."""

from __future__ import annotations

from src.knowledge.memory_service.memory_schema import DeleteMemoryRequest, DeleteMemoryResponse
from src.knowledge.memory_service.milvus_client import MilvusClient


def delete_memory(
    request: DeleteMemoryRequest,
    milvus: MilvusClient | None = None,
) -> DeleteMemoryResponse:
    milvus = milvus or MilvusClient()

    if request.memory_id:
        deleted = milvus.delete(request.memory_id, soft=request.soft_delete)
        ids = [request.memory_id] if deleted else []
        return DeleteMemoryResponse(deleted_ids=ids, count=len(ids))

    filters = dict(request.filters or {})
    if request.user_id:
        filters["user_id"] = request.user_id

    if not filters:
        return DeleteMemoryResponse(deleted_ids=[], count=0)

    deleted_ids = milvus.delete_by_filters(filters, soft=request.soft_delete)
    return DeleteMemoryResponse(deleted_ids=deleted_ids, count=len(deleted_ids))