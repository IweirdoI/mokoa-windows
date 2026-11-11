#!/usr/bin/env python3
"""Smoke test for local BGE-M3 embedding on Windows/dev. Owner: efca9634."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.embedding.embedding_interface import create_embedding_backend, load_embedding_config


def main() -> None:
    cfg = load_embedding_config()
    print("embedding config:", cfg)
    provider = create_embedding_backend("local")
    vectors = provider.embed(["麒忆智脑 BGE-M3 本地 embedding 测试"])
    vector = vectors[0]
    print("dimension:", len(vector))
    print("head:", vector[:5])
    print("non_zero:", any(value != 0.0 for value in vector))


if __name__ == "__main__":
    main()
