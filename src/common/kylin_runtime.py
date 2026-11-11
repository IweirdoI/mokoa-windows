"""Detect whether this process can use the host Kylin V11 SDKs.

Cursor/KARE shares /etc/os-release with V11 but still runs glibc 2.31, so
OS name alone must not select the system SDK. Unit tests must keep the
in-memory backend unless they opt into kylin explicitly.
"""
from __future__ import annotations

import ctypes.util
import os
import sys
from pathlib import Path


def acceptance_enabled() -> bool:
    return os.getenv("OS_AGENT_KYLIN_ACCEPTANCE") == "1"


def enforce_acceptance() -> None:
    """Reject conflicting settings before factories can select a substitute."""
    if not acceptance_enabled():
        return
    required = {"OS_AGENT_MEMORY_BACKEND": "kylin",
                "OS_AGENT_EMBEDDING_BACKEND": "kylin",
                "OS_AGENT_KYLIN_STRICT": "1"}
    for key, value in required.items():
        if os.getenv(key, value).strip().lower() != value:
            raise RuntimeError(f"V11 acceptance requires {key}={value}")
    if os.getenv("EMBEDDING_BACKEND", "kylin").strip().lower() != "kylin":
        raise RuntimeError("V11 acceptance rejects EMBEDDING_BACKEND override")
    os.environ.update(required)


def glibc_version() -> tuple[int, int]:
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, OSError, ValueError):
        return (0, 0)
    parts = (text or "").replace("glibc", "").strip().split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return (0, 0)


def host_can_load_kylin_sdk() -> bool:
    # V11 client libraries require GLIBC_2.32+; this machine's SDK needs 2.38.
    return glibc_version() >= (2, 38)


def under_pytest() -> bool:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    if "pytest" in sys.modules:
        return True
    argv0 = Path(sys.argv[0]).name.lower() if sys.argv else ""
    return argv0 in {"pytest", "py.test"}


def discover_bridge_library(project_root: Path) -> Path | None:
    explicit = os.getenv("OS_AGENT_KYLIN_VECTOR_LIBRARY")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    candidate = project_root / "build" / "kylin-vector" / "libmokoa_kylin_vector_bridge.so"
    if candidate.is_file():
        return candidate
    found = ctypes.util.find_library("mokoa_kylin_vector_bridge")
    return Path(found) if found else None
