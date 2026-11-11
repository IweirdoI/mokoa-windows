"""Local / Kylin embedding tests. Owner: efca9634."""

from __future__ import annotations

import pytest

pytest.importorskip("sentence_transformers")

from src.embedding.embedding_interface import create_embedding_backend
from src.embedding.local_embedding import LocalEmbeddingAdapter
from src.knowledge.memory_service.create_memory import create_memory
from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.memory_schema import (
    CreateMemoryRequest,
    MemoryEntry,
    MemoryTypeTag,
    QueryMemoryRequest,
)
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
from src.knowledge.memory_service.query_memory import query_memory


@pytest.fixture(scope="module")
def local_provider():
    return create_embedding_backend("local")


def test_local_embed_returns_non_zero_vector(local_provider) -> None:
    vectors = local_provider.embed(["麒忆智脑 embedding 测试"])
    assert len(vectors) == 1
    assert len(vectors[0]) == local_provider.dimension
    assert any(value != 0.0 for value in vectors[0])


def test_local_embed_similar_texts_are_closer(local_provider) -> None:
    vectors = local_provider.embed(
        [
            "pip 安装失败应使用国内镜像",
            "pip install 超时时换国内源",
            "今天天气很好",
        ]
    )
    assert len(vectors) == 3

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb)

    related = cosine(vectors[0], vectors[1])
    unrelated = cosine(vectors[0], vectors[2])
    assert related > unrelated


def test_hash_fallback_is_non_zero_and_preserves_overlap(monkeypatch) -> None:
    provider = LocalEmbeddingAdapter(dimension=128, allow_stub_fallback=True)
    monkeypatch.setattr(provider, "_ensure_model", lambda: None)
    vectors = provider.embed(["pip 国内镜像", "pip 镜像超时", "天气晴朗"])
    assert all(any(value != 0.0 for value in vector) for vector in vectors)

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])


def test_query_memory_ranks_by_vector_similarity() -> None:
    milvus = InMemoryMilvusClient()
    create_memory(
        CreateMemoryRequest(
            entry=MemoryEntry(
                content="pip 安装失败应使用国内镜像",
                memory_type=MemoryTypeTag.TOOL_CALL,
                user_id="embed_user",
                source="agent_log",
            )
        ),
        milvus=milvus,
    )
    create_memory(
        CreateMemoryRequest(
            entry=MemoryEntry(
                content="用户喜欢把下载目录设置到 D 盘",
                memory_type=MemoryTypeTag.PREFERENCE,
                user_id="embed_user",
                source="manual",
            )
        ),
        milvus=milvus,
    )

    client = EmbeddingClient(backend="local")
    response = query_memory(
        QueryMemoryRequest(user_id="embed_user", query="pip 镜像超时", top_k=2),
        milvus=milvus,
        embedding=client,
    )
    assert len(response.items) >= 1
    assert "pip" in response.items[0]["content"]
