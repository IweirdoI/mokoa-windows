"""Unified embedding interface. Owner: efca9634."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

import yaml

from src.common.interfaces import EmbeddingProvider
from src.common.kylin_runtime import host_can_load_kylin_sdk

_SHARED_PROVIDERS: dict[str, EmbeddingProvider] = {}
_SHARED_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def load_embedding_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
    if not config_path.exists():
        return {}
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    embedding_cfg = data.get("embedding", {})
    return embedding_cfg if isinstance(embedding_cfg, dict) else {}


def resolve_embedding_backend_name(backend: str | None = None) -> str:
    """Resolve auto/env/config to a concrete backend name."""

    cfg = load_embedding_config()
    from src.common.kylin_runtime import acceptance_enabled, enforce_acceptance
    enforce_acceptance()
    if acceptance_enabled() and backend is not None and backend.strip().lower() != "kylin":
        raise RuntimeError("V11 acceptance requires the system embedding SDK")
    resolved = (
        backend
        or os.getenv("OS_AGENT_EMBEDDING_BACKEND")
        or os.getenv("EMBEDDING_BACKEND")
        or str(cfg.get("backend", "auto"))
    ).strip().lower()
    if resolved == "auto":
        # Only the V11 host userspace can load CoreAI; KARE/Linux-dev must not
        # pretend the system embedding SDK is selected and then silently fall back.
        resolved = "kylin" if host_can_load_kylin_sdk() else "local"
    return resolved


def _build_embedding_backend(resolved: str) -> EmbeddingProvider:
    cfg = load_embedding_config()
    if resolved == "kylin":
        from src.embedding.kylin_adapter import KylinEmbeddingAdapter

        strict = os.getenv("OS_AGENT_KYLIN_STRICT", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not strict and host_can_load_kylin_sdk():
            strict = True
        return KylinEmbeddingAdapter(allow_fallback=not strict)

    if resolved == "hash":
        from src.embedding.local_embedding import HashEmbeddingAdapter

        return HashEmbeddingAdapter(
            dimension=int(os.getenv("MEMORY_VECTOR_DIM", "1024"))
        )

    if resolved != "local":
        raise ValueError(f"不支持的 embedding backend: {resolved!r}")

    from src.embedding.local_embedding import LocalEmbeddingAdapter

    device = cfg.get("device")
    return LocalEmbeddingAdapter(
        model_name=str(cfg.get("local_model", "BAAI/bge-m3")),
        device=str(device) if device else None,
        normalize=bool(cfg.get("normalize", True)),
        allow_stub_fallback=bool(cfg.get("allow_stub_fallback", True)),
    )


def create_embedding_backend(backend: str | None = None) -> EmbeddingProvider:
    """
    Factory for pluggable embedding backends. Returns a new instance.

    auto   -> kylin on Linux, local on Windows/dev
    kylin  -> 银河麒麟 CoreAI embedding SDK
    local  -> BGE-M3 / sentence-transformers for dev
    hash   -> deterministic offline vectors for tests/wiring only

    Request paths should use ``get_shared_embedding_backend`` so CoreAI
    sessions are not created and destroyed per /store-entry.
    """
    return _build_embedding_backend(resolve_embedding_backend_name(backend))


def get_shared_embedding_backend(backend: str | None = None) -> EmbeddingProvider:
    """Process-wide provider. CoreAI init_session/init_model runs once per backend."""

    resolved = resolve_embedding_backend_name(backend)
    with _SHARED_LOCK:
        provider = _SHARED_PROVIDERS.get(resolved)
        if provider is None:
            provider = _build_embedding_backend(resolved)
            _SHARED_PROVIDERS[resolved] = provider
        return provider


def reset_shared_embedding_backends() -> None:
    """Drop cached providers. Tests only; closes adapters that expose close()."""

    with _SHARED_LOCK:
        providers = list(_SHARED_PROVIDERS.values())
        _SHARED_PROVIDERS.clear()
    for provider in providers:
        closer = getattr(provider, "close", None)
        if not callable(closer):
            continue
        try:
            closer()
        except Exception:
            pass
