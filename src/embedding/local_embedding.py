"""Local embedding for dev/test (BGE-M3). Owner: efca9634."""

from __future__ import annotations

import logging
import hashlib
import math
import re
import threading
from typing import Any

from src.common.interfaces import EmbeddingProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-m3"
_MODEL_CACHE: dict[tuple[str, str | None], Any] = {}
_MODEL_LOCK = threading.Lock()


class LocalEmbeddingAdapter(EmbeddingProvider):
    """Windows / 开发机 fallback：sentence-transformers BGE-M3."""

    def __init__(
        self,
        *,
        model_name: str = _DEFAULT_MODEL,
        device: str | None = None,
        normalize: bool = True,
        dimension: int | None = None,
        allow_stub_fallback: bool = True,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._normalize = normalize
        self._dimension = dimension or 1024
        self._allow_stub_fallback = allow_stub_fallback
        self._fallback_mode = False
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        cache_key = (self._model_name, self._device)
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(cache_key)
            if cached is not None:
                self._model = cached
                return self._model

            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                if self._allow_stub_fallback:
                    logger.warning(
                        "sentence-transformers 未安装，local embedding 回退为确定性哈希向量。"
                        "Windows 开发请执行: pip install sentence-transformers"
                    )
                    self._fallback_mode = True
                    return None
                raise ImportError(
                    "本地 embedding 需要 sentence-transformers。"
                    "请执行: pip install sentence-transformers"
                ) from exc

            logger.info("Loading local embedding model: %s", self._model_name)
            kwargs: dict[str, Any] = {}
            if self._device:
                kwargs["device"] = self._device
            try:
                model = SentenceTransformer(self._model_name, **kwargs)
            except Exception:
                if not self._allow_stub_fallback:
                    raise
                logger.exception(
                    "本地 embedding 模型加载失败，回退为确定性哈希向量；"
                    "该模式仅供离线联调，不可用于正式召回指标。"
                )
                self._fallback_mode = True
                return None
            _MODEL_CACHE[cache_key] = model
            self._model = model
            if self._dimension is None:
                self._dimension = int(model.get_sentence_embedding_dimension())
            return model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        model = self._ensure_model()
        if model is None:
            return [self._hash_embed(text) for text in texts]

        vectors = model.encode(
            texts,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )
        if self._dimension is None:
            self._dimension = len(vectors[0])
        return [vector.tolist() for vector in vectors]

    @property
    def dimension(self) -> int:
        if not self._fallback_mode and self._dimension is None:
            self._ensure_model()
        return int(self._dimension)

    def _hash_embed(self, text: str) -> list[float]:
        """无模型时保留词/字粒度重叠信号，避免零向量伪装成功。"""

        vector = [0.0] * self._dimension
        normalized = " ".join(text.lower().split())
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)
        tokens.extend(normalized[index : index + 2] for index in range(max(0, len(normalized) - 1)))
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self._dimension
            vector[index] += 1.0
        if self._normalize:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
        return vector


class HashEmbeddingAdapter(LocalEmbeddingAdapter):
    """Fast deterministic backend for unit tests and offline wiring checks."""

    def __init__(self, *, dimension: int = 1024) -> None:
        super().__init__(dimension=dimension, allow_stub_fallback=True)
        self._fallback_mode = True

    def _ensure_model(self) -> None:
        return None
