
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LITE_DB = _REPO_ROOT / "data" / "os_agent_memory.db"

# Do not use MILVUS_URI as the primary project setting. pymilvus reads that
# name itself during import and interprets a Windows Milvus Lite path as a
# remote connection URI, which makes importing the client fail before our
# wrapper is constructed. Keep remote legacy values compatible, while using a
# project-scoped variable for local paths and all new deployments.
_legacy_milvus_uri = os.getenv("MILVUS_URI")
if _legacy_milvus_uri and "://" in _legacy_milvus_uri:
    _legacy_remote_uri = _legacy_milvus_uri
else:
    _legacy_remote_uri = None
    # Prevent pymilvus from consuming a legacy local filesystem path as its
    # own remote-only global configuration during import.
    if _legacy_milvus_uri:
        os.environ.pop("MILVUS_URI", None)

MILVUS_URI = os.getenv(
    "OS_AGENT_MILVUS_URI",
    _legacy_remote_uri or str(_DEFAULT_LITE_DB),
)
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION_NAME", "os_agent_memory")
EMBEDDING_BACKEND = os.getenv(
    "OS_AGENT_EMBEDDING_BACKEND",
    os.getenv("EMBEDDING_BACKEND", "local"),
)
VECTOR_DIM = int(os.getenv("MEMORY_VECTOR_DIM", "1024"))

TEXT_MAX_LENGTH = 4096
SHORT_TEXT_MAX_LENGTH = 512
ID_MAX_LENGTH = 128


def get_milvus_uri() -> str:
    return MILVUS_URI


def get_collection_name() -> str:
    return COLLECTION_NAME


def get_embedding_backend() -> str:
    return EMBEDDING_BACKEND


def get_vector_dim() -> int:
    return VECTOR_DIM


