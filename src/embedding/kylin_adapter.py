"""Kylin CoreAI embedding adapter. Owner: efca9634. See docs/KYLIN_SDK.md."""

from __future__ import annotations

import logging
import os
import threading

from src.common.interfaces import EmbeddingProvider
from src.embedding.local_embedding import LocalEmbeddingAdapter

logger = logging.getLogger(__name__)

# After kylin-ai-abstract-models 1.2.0.1-0k1.0, get_model_list returns the
# Triton ensemble id. Older JSON used name=gte-base_uint8-text.
DEFAULT_KYLIN_TEXT_MODEL = "ensemble-embd_gte-base_uint8-text"


class KylinEmbeddingAdapter(EmbeddingProvider):
    """Wraps libkylin-coreai-embedding-dev via ctypes; falls back to local on failure."""

    def __init__(self, *, allow_fallback: bool = True) -> None:
        self._session = None
        self._allow_fallback = allow_fallback
        self._fallback = LocalEmbeddingAdapter() if allow_fallback else None
        self._dimension: int | None = None
        self._using_fallback = False
        self._last_error: str | None = None
        self._lock = threading.RLock()

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        from src.embedding.kylin_ctypes import KylinEmbeddingLib

        lib = KylinEmbeddingLib()
        session = lib.create()
        if not session:
            raise RuntimeError("text_embedding_create_session returned null")
        try:
            if lib.init(session) != 0:
                raise RuntimeError("text_embedding_init_session failed")
            list_error, names = lib.list_model_names(session)
            requested = os.getenv("OS_AGENT_KYLIN_EMBEDDING_MODEL", "").strip()
            if requested and (not names or requested in names):
                model_name = requested
            elif names:
                model_name = next((name for name in names if "gte-base" in name), names[0])
            else:
                model_name = requested or DEFAULT_KYLIN_TEXT_MODEL
            loaded = lib.init_model(session, model_name.encode("utf-8"))
            if loaded != 0:
                raise RuntimeError(
                    f"text_embedding_init_model({model_name!r}) failed: {loaded}; "
                    f"get_model_list error={list_error} names={names}"
                )
        except Exception:
            lib.destroy_session(session)
            raise
        self._session = (lib, session)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            with self._lock:
                self._ensure_session()
                assert self._session is not None
                lib, session = self._session
                vectors = [lib.embed_text(session, text) for text in texts]
            if vectors:
                dimension = len(vectors[0])
                if dimension <= 0 or any(len(vector) != dimension for vector in vectors):
                    raise RuntimeError("Kylin embedding 返回了空向量或不一致维度")
                self._dimension = dimension
            return vectors
        except Exception:
            self._last_error = "麒麟 embedding 初始化或运行失败"
            self.close()
            if not self._allow_fallback:
                logger.exception("麒麟 embedding 初始化或运行失败（严格模式，不降级）")
                raise
            logger.exception("麒麟 embedding 初始化或运行失败，降级到本地 backend")
            self._using_fallback = True
            assert self._fallback is not None
            return self._fallback.embed(texts)

    def close(self) -> None:
        with self._lock:
            if self._session is None:
                return
            lib, session = self._session
            self._session = None
            lib.destroy_session(session)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._dimension = len(self.embed(["probe"])[0])
        return self._dimension

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    @property
    def backend_name(self) -> str:
        return "local-fallback" if self._using_fallback else "kylin-coreai"

    @property
    def last_error(self) -> str | None:
        return self._last_error
