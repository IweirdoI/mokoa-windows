"""Base class for all OSS adapters."""

from __future__ import annotations

from abc import ABC
from typing import Any


class BaseAdapter(ABC):
    """
    All third-party integrations inherit from this.

    Pattern:
      1. Implement unified interface (see src/common/interfaces.py)
      2. Delegate to OSS SDK internally
      3. Map OSS types <-> our models (RawRecord, MemoryItem)
    """

    name: str = "base"
    enabled: bool = False

    def configure(self, config: dict[str, Any]) -> None:
        """Load adapter-specific config from configs/adapters.yaml."""
        self.enabled = config.get("enabled", False)

    def health_check(self) -> bool:
        """Return True if OSS dependency is available."""
        return self.enabled
