
"""Query memory. Owner: cb0aeabe."""

from __future__ import annotations

import time

from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.memory_schema import QueryMemoryRequest, QueryMemoryResponse
from src.knowledge.memory_service.milvus_client import MilvusClient


def query_memory(
    request: QueryMemoryRequest,
    milvus: MilvusClient | None = None,
    embedding: EmbeddingClient | None = None,
) -> QueryMemoryResponse:
    start = time.perf_counter()
    milvus = milvus or MilvusClient()
    embedding = embedding or EmbeddingClient()

    if request.memory_id:
        item = milvus.get_by_id(request.memory_id)
        items = [item] if item else []
    else:
        filters = {"user_id": request.user_id, **request.filters}
        query = (request.query or "").strip()
        vector = embedding.embed(query) if query else None
        items = milvus.search(vector, filters, top_k=request.top_k)

    latency_ms = (time.perf_counter() - start) * 1000
    return QueryMemoryResponse(items=items, latency_ms=latency_ms)