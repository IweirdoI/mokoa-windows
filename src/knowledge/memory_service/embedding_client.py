"""Embedding client — wraps efca9634's unified embedding interface. Owner: cb0aeabe."""

from __future__ import annotations

from src.embedding.embedding_interface import get_shared_embedding_backend

# 这里只负责包装统一 embedding 接口。
# `embed(content: str)` 返回单条向量。
# `embed_batch(contents: list[str])` 返回批量向量。
#
# 入库时只对 `MemoryEntry.content` 做 embedding。
# WSL/Linux 环境优先用这里的统一接口，不要在业务层直接依赖具体 SDK。
#
# 默认复用进程内 provider：麒麟 CoreAI 会话不可按请求创建/销毁。


class EmbeddingClient:
    """Only embed `content` field per cb0aeabe spec."""

    def __init__(self, backend: str | None = None) -> None:
        self._provider = get_shared_embedding_backend(backend)

    def embed(self, content: str) -> list[float]:
        return self._provider.embed([content])[0]

    def embed_batch(self, contents: list[str]) -> list[list[float]]:
        return self._provider.embed(contents)
