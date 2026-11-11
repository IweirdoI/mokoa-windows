"""
Cognee adapter - knowledge graph + forget/improve.

Repo: https://github.com/topoteretes/cognee
Owner: cb0aeabe, 2abac5a6

TODO:
  - pip install cognee
  - wrap remember/recall/forget/improve
  - use for knowledge store and security forget fallback
"""

from src.adapters.base import BaseAdapter


class CogneeAdapter(BaseAdapter):
    name = "cognee"

    async def remember(self, content: str, session_id: str | None = None) -> None:
        # TODO: await cognee.remember(content, session_id=session_id)
        pass

    async def recall(self, query: str) -> list:
        # TODO: await cognee.recall(query)
        return []

    async def forget(self, dataset: str = "main_dataset") -> None:
        # TODO: await cognee.forget(dataset=dataset)
        pass
