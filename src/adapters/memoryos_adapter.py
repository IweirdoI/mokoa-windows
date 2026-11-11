"""
MemoryOS adapter - short/mid/long-term flow.

Repo: https://github.com/BAI-LAB/MemoryOS
Owner: efca9634

TODO:
  - pip install memoryos-pro (or vendor memoryos-pypi/)
  - wrap Memoryos class as MemoryLayerManager
  - map short_term_capacity, mid_term_heat_threshold from config
"""

from __future__ import annotations

from src.adapters.base import BaseAdapter


class MemoryOSAdapter(BaseAdapter):
    name = "memoryos"

    def __init__(self) -> None:
        self._client = None  # TODO: Memoryos(...)

    def connect(self, **kwargs) -> None:
        # TODO: initialize MemoryOS client
        pass
