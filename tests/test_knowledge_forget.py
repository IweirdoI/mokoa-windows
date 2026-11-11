"""集成测试：知识库按自然语言条件批量定位遗忘（待办第 3 项）。

Owner: 2abac5a6
验证 Orchestrator.forget 在命中知识库遗忘意图时：
  1. 用 NL 抽取的主题做定向召回，把知识候选并入删除范围；
  2. 命中的知识条目经 _purge_memory → delete_memory 级联删除；
  3. 「删除全部某类知识」走 Milvus 批量过滤删除。

使用 FakeMilvus 与受控召回，不依赖真实 Milvus / 网络。
"""

from __future__ import annotations

import tempfile
import types
from typing import Any

import pytest

from src.common.models import MemoryItem, MemoryLayer, MemoryType, RetrievalResult, RetrievalQuery
from src.security.knowledge_forget import KnowledgeForgetResolver

try:
    from src.core.orchestrator import MemoryOrchestrator
    from src.core.layer_manager import LayerManager
    from src.security.service import SecurityAndForgetService

    _ORCH_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时跳过
    _ORCH_AVAILABLE = False


class _FakeMilvus:
    """最小化 Milvus 客户端桩，仅实现 forget 链路所需方法。"""

    def __init__(self) -> None:
        self.deleted_ids: list[str] = []
        self.filter_deletes: list[tuple[dict[str, Any], list[str]]] = []
        self._batch_ids: list[str] = []

    def delete(self, memory_id: str, soft: bool = True) -> bool:  # noqa: ARG002
        self.deleted_ids.append(memory_id)
        return True

    def delete_by_filters(self, filters: dict[str, Any], soft: bool = True) -> list[str]:  # noqa: ARG002
        ids = list(self._batch_ids)
        self.filter_deletes.append((filters, ids))
        return ids


def _make_orchestrator(knowledge_items: list[MemoryItem], milvus: _FakeMilvus) -> Any:
    from src.core.orchestrator import MemoryOrchestrator
    from src.core.layer_manager import LayerManager
    from src.security.service import SecurityAndForgetService

    class _TestOrchestrator(MemoryOrchestrator):
        def __init__(self) -> None:
            super().__init__(
                # 传入非 IngestionPipeline 实例 → _staging_store() 返回 None
                ingestion=object(),  # type: ignore[arg-type]
                security=SecurityAndForgetService(index_path=tempfile.mktemp(suffix=".json")),
                layer_manager=LayerManager(),
                preference=object(),  # type: ignore[arg-type]
                milvus=milvus,
            )
            self._knowledge_items = knowledge_items

        def list_short_term(self, user_id: str, limit: int = 100):  # noqa: ARG002
            return []

        def list_mid_term(self, user_id: str, limit: int = 100):  # noqa: ARG002
            return []

        def list_preferences(self, user_id: str, latest_only: bool = True, top_k: int = 20):  # noqa: ARG002
            return types.SimpleNamespace(items=[])

        def recall(self, query: RetrievalQuery, *, filters=None):  # noqa: ARG002
            items = self._knowledge_items if query.memory_type == MemoryType.KNOWLEDGE else []
            return RetrievalResult(items=items, latency_ms=0.0, strategy="fake")

    return _TestOrchestrator()


def _knowledge_items() -> list[MemoryItem]:
    return [
        MemoryItem(
            id="k1",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.LONG,
            content="SSH 配置方法：修改 /etc/ssh/sshd_config 端口",
            user_id="u1",
        ),
        MemoryItem(
            id="k2",
            memory_type=MemoryType.KNOWLEDGE,
            layer=MemoryLayer.LONG,
            content="Python 虚拟环境配置：conda create -n demo",
            user_id="u1",
        ),
    ]


def test_knowledge_forget_locates_and_deletes_by_nl() -> None:
    if not _ORCH_AVAILABLE:
        pytest.skip("orchestrator 不可用（缺少依赖）")
    milvus = _FakeMilvus()
    orch = _make_orchestrator(_knowledge_items(), milvus)

    deleted = orch.forget("u1", "忘记知识库中关于SSH配置的内容")

    assert "k1" in deleted
    assert "k2" not in deleted  # 与 SSH 无关
    assert "k1" in milvus.deleted_ids


def test_knowledge_forget_want_all_batch_delete() -> None:
    if not _ORCH_AVAILABLE:
        pytest.skip("orchestrator 不可用（缺少依赖）")
    milvus = _FakeMilvus()
    milvus._batch_ids = ["k1", "k2", "k3"]
    orch = _make_orchestrator(_knowledge_items(), milvus)

    deleted = orch.forget("u1", "删除所有知识文档")

    # 批量过滤删除覆盖了 recall top_k 之外的 k3
    assert set(["k1", "k2", "k3"]).issubset(set(deleted))
    assert milvus.filter_deletes  # delete_by_filters 被调用
    filters_used = milvus.filter_deletes[0][0]
    assert filters_used.get("memory_type") == list(KnowledgeForgetResolver.KNOWLEDGE_TYPES)
