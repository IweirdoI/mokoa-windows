"""Pre-Kylin readiness tests owned by the orchestration/embedding module."""

from __future__ import annotations

import pytest

from src.embedding.embedding_interface import create_embedding_backend
from src.embedding.kylin_adapter import KylinEmbeddingAdapter
from src.embedding.local_embedding import HashEmbeddingAdapter
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient


def test_hash_backend_is_explicit_and_deterministic(monkeypatch) -> None:
    monkeypatch.setenv("OS_AGENT_EMBEDDING_BACKEND", "hash")
    provider = create_embedding_backend()
    assert isinstance(provider, HashEmbeddingAdapter)
    assert provider.embed(["相同文本"])[0] == provider.embed(["相同文本"])[0]
    assert any(provider.embed(["非零向量"])[0])


def test_unknown_embedding_backend_fails_fast() -> None:
    with pytest.raises(ValueError, match="不支持的 embedding backend"):
        create_embedding_backend("unknown")


def test_kylin_strict_mode_never_falls_back(monkeypatch) -> None:
    def fail_session(self) -> None:
        raise OSError("SDK unavailable")

    monkeypatch.setattr(KylinEmbeddingAdapter, "_ensure_session", fail_session)
    provider = KylinEmbeddingAdapter(allow_fallback=False)
    with pytest.raises(OSError, match="SDK unavailable"):
        provider.embed(["probe"])
    assert provider.using_fallback is False


def test_in_memory_backend_hides_soft_deleted_rows() -> None:
    client = InMemoryMilvusClient()
    client.insert("m1", "内容", [1.0, 0.0], {"user_id": "u1"})
    assert client.get_by_id("m1") is not None
    assert client.delete("m1", soft=True)
    assert client.get_by_id("m1") is None
    assert client.search(None, {"user_id": "u1"}) == []


def test_embedding_client_reuses_process_provider(monkeypatch) -> None:
    from src.embedding.embedding_interface import (
        create_embedding_backend,
        get_shared_embedding_backend,
        reset_shared_embedding_backends,
    )
    from src.knowledge.memory_service.embedding_client import EmbeddingClient

    monkeypatch.setenv("OS_AGENT_EMBEDDING_BACKEND", "hash")
    reset_shared_embedding_backends()
    first = EmbeddingClient(backend="hash")
    second = EmbeddingClient(backend="hash")
    shared = get_shared_embedding_backend("hash")
    assert first._provider is second._provider is shared
    assert create_embedding_backend("hash") is not shared
