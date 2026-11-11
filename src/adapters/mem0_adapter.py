"""
mem0 adapter - hybrid retrieval (semantic + BM25 + entity).

Repo: https://github.com/mem0ai/mem0
Owner: cb0aeabe

TODO:
  - pip install mem0ai[nlp]
  - wrap Memory.search() as retrieval backend
  - 作为 retrieval backend 接入 orchestrator.recall（当前真实检索走
    src/knowledge/memory_service + recall_ranker，见 src/core/orchestrator.py）
"""

from src.adapters.base import BaseAdapter


class Mem0RetrievalAdapter(BaseAdapter):
    name = "mem0"

    def search(self, query: str, user_id: str, top_k: int = 5) -> list[dict]:
        # TODO: memory.search(query=query, filters={"user_id": user_id}, top_k=top_k)
        return []
