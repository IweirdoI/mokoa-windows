"""Milvus connection wrapper. Owner: cb0aeabe."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.knowledge.memory_service.config import (
    COLLECTION_NAME,
    ID_MAX_LENGTH,
    MILVUS_URI,
    SHORT_TEXT_MAX_LENGTH,
    TEXT_MAX_LENGTH,
    VECTOR_DIM,
)

class MemoryBackendUnavailable(Exception):
    """向量后端（Milvus / Kylin）当前不可达；API 层据此返回 503 而非 500。"""


class MilvusClient:
    """Thin wrapper around pymilvus MilvusClient."""

    _FILTERABLE_FIELDS = {
        "memory_id",
        "user_id",
        "memory_type",
        "task_type",
        "source",
        "preference_key",
        "category",
        "version",
        "is_latest",
        "scene_id",
        "is_deleted",
    }

    '''背后的逻辑：在 _build_expr 方法中，只有在这个集合里的字段，才能被用作 filters 参数传入数据库查询。

为什么这样设计：

安全性：防止调用者传入类似 metadata（JSON）或 content（长文本）这种不适合作为 WHERE 条件的字段，避免数据库报错或性能崩溃。
业务语义：它限定了“记忆”只允许按用户ID、类型、版本、是否删除等结构化标签进行筛选。
注意，preference_value（偏好值）虽然在输出里，但不在过滤白名单中，说明业务上不允许按具体的值去过滤，只能按 preference_key（偏好键）过滤。'''

    _OUTPUT_FIELDS = [
        "memory_id",
        "user_id",
        "content",
        "memory_type",
        "task_type",
        "source",
        "preference_key",
        "preference_value",
        "category",
        "version",
        "is_latest",
        "scene_id",
        "confidence",
        "save_score",
        "supersedes_memory_id",
        "created_at",
        "updated_at",
        "is_deleted",
        "metadata",
    ]

    '''背后的逻辑：Milvus 查询默认可能返回所有字段（包括巨大的向量），这个列表定义了业务层关心的标准字段。

为什么少了 embedding：因为向量是浮点数列表，数据量巨大。
在 99% 的业务展示场景（如展示记忆列表）中，我们只需要看文本和元数据，不需要向量。
只有在 get_by_id(include_embedding=True) 时，代码才会临时把 embedding 追加进去。

metadata 的特殊性：注意列表中包含 metadata（JSON类型）。
在 _public_record 方法中，代码会把这 18 个平铺的字段重新塞进 metadata 字典里返回给上层，这叫做“字段折叠”。这样上层业务只需解析 metadata 一个字段，就能拿到全部信息，简化了前端对接。'''

    def __init__(
        self,
        uri: str = MILVUS_URI,
        collection: str = COLLECTION_NAME,
        vector_dim: int = VECTOR_DIM,
        client: Any | None = None,
    ) -> None:
        self.uri = uri
        self.collection = collection
        self.vector_dim = vector_dim
        self.client = client
        # 连接延迟到首次实际操作时进行，避免 Milvus 不可达时整个服务起不来（#4）。
        # 子类（KylinVectorClient）已注入 transport 时按原行为立即建集合。
        if client is not None:
            self._ensure_collection()

    def _ensure_connected(self) -> None:
        """惰性建立真实 Milvus 连接并确认集合存在（仅 client 未注入时执行）。

        任何连接/建集合故障统一包装为 MemoryBackendUnavailable，供 API 层映射为 503
        而非 500。pymilvus 在此处函数内导入，使 memory 后端在缺失该可选依赖时仍能启动。
        """
        if self.client is not None:
            return
        try:
            from pymilvus import MilvusClient as PyMilvusClient

            self._ensure_lite_parent(self.uri)
            self.client = PyMilvusClient(uri=self.uri)
            self._ensure_collection()
        except Exception as exc:  # noqa: BLE001
            self.client = None
            raise MemoryBackendUnavailable(f"向量后端连接失败：{exc}") from exc

    def insert(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> bool:
        self._ensure_connected()
        row = self._build_row(memory_id, content, embedding, metadata)
        self.client.insert(collection_name=self.collection, data=[row])
        return True

    def search(
        self,
        embedding: list[float] | None,
        filters: dict[str, Any],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self._ensure_connected()
        merged_filters = {"is_deleted": False, **filters}
        expr = self._build_expr(merged_filters)

        if embedding is None:
            rows = self.client.query(
                collection_name=self.collection,
                filter=expr,
                output_fields=self._OUTPUT_FIELDS,
                limit=top_k,
            )
            return [self._public_record(row) for row in rows]

        results = self.client.search(
            collection_name=self.collection,
            data=[embedding],
            filter=expr,
            limit=top_k,
            output_fields=self._OUTPUT_FIELDS,
            search_params={"metric_type": "COSINE", "params": {}},
        )

        items: list[dict[str, Any]] = []
        for hit in results[0] if results else []:
            entity = hit.get("entity", {})
            item = self._public_record(entity)
            item["score"] = hit.get("distance")     #score是相似度分数
            items.append(item)
        return items

    def get_by_id(self, memory_id: str, *, include_embedding: bool = False) -> dict[str, Any] | None:
        self._ensure_connected()
        output_fields = list(self._OUTPUT_FIELDS)
        if include_embedding:
            output_fields.append("embedding")

        rows = self.client.query(
            collection_name=self.collection,
            filter=f'memory_id == "{self._escape(memory_id)}" and is_deleted == false',
            output_fields=output_fields,
            limit=1,
        )
        if not rows:
            return None
        return self._public_record(rows[0], include_embedding=include_embedding)
        #query执行标量检索，search执行向量检索

    def update(
        self,
        memory_id: str,
        fields: dict[str, Any],
        new_embedding: list[float] | None = None,
    ) -> bool:
        self._ensure_connected()
        existing = self.get_by_id(memory_id, include_embedding=True)
        if existing is None:
            return False

        updated = dict(existing)
        updated.pop("score", None)
        updated.update(fields)
        if new_embedding is not None:
            updated["embedding"] = new_embedding

        self.client.upsert(collection_name=self.collection, data=[updated])
        return True

    '''两个delete里，以及其他里面的collection是指明某张表'''

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        self._ensure_connected()
        if self.get_by_id(memory_id) is None:
            return False

        if soft:
            return self.update(
                memory_id,
                {
                    "is_deleted": True,
                    "updated_at": self._now(),
                },
            )

        self.client.delete(
            collection_name=self.collection,
            filter=f'memory_id == "{self._escape(memory_id)}"',
        )
        return True

    def delete_by_filters(self, filters: dict[str, Any], soft: bool = True) -> list[str]:
        self._ensure_connected()
        merged_filters = {"is_deleted": False, **filters}
        expr = self._build_expr(merged_filters)
        rows = self.client.query(
            collection_name=self.collection,
            filter=expr,
            output_fields=["memory_id"],
            limit=1000,
        )
        ids = [row["memory_id"] for row in rows]

        for memory_id in ids:
            self.delete(memory_id, soft=soft)
        return ids

    def _ensure_collection(self) -> None:
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("memory_id", DataType.VARCHAR, is_primary=True, max_length=ID_MAX_LENGTH)
        schema.add_field("user_id", DataType.VARCHAR, max_length=ID_MAX_LENGTH)
        schema.add_field("content", DataType.VARCHAR, max_length=TEXT_MAX_LENGTH)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.vector_dim)
        schema.add_field("memory_type", DataType.VARCHAR, max_length=64)
        schema.add_field("task_type", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=64)
        schema.add_field("preference_key", DataType.VARCHAR, max_length=128)
        schema.add_field("preference_value", DataType.VARCHAR, max_length=512)
        schema.add_field("category", DataType.VARCHAR, max_length=64)
        schema.add_field("version", DataType.INT64)
        schema.add_field("is_latest", DataType.BOOL)
        schema.add_field("scene_id", DataType.VARCHAR, max_length=128)
        schema.add_field("confidence", DataType.FLOAT)
        schema.add_field("save_score", DataType.FLOAT)
        schema.add_field("supersedes_memory_id", DataType.VARCHAR, max_length=128)
        schema.add_field("created_at", DataType.VARCHAR, max_length=64)
        schema.add_field("updated_at", DataType.VARCHAR, max_length=64)
        schema.add_field("is_deleted", DataType.BOOL)
        schema.add_field("metadata", DataType.JSON)
        if self.client.has_collection(self.collection):
            return
        #定义表结构

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="FLAT",
            metric_type="COSINE",
        )       #添加索引

        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )       #在磁盘上创建表结构
        self.client.load_collection(self.collection)        #将表结构加载到内存

    '''这里开始是原子（基础）操作'''
    def _build_row(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()       #调用后面的静态方法
        return {
            "memory_id": self._text(memory_id, ID_MAX_LENGTH),      #调用后面的静态方法
            "user_id": self._text(metadata.get("user_id"), ID_MAX_LENGTH, "default"),
            "content": self._text(content, TEXT_MAX_LENGTH),
            "embedding": self._normalize_embedding(embedding),
            "memory_type": self._text(metadata.get("memory_type"), 64),
            "task_type": self._text(metadata.get("task_type"), 64),
            "source": self._text(metadata.get("source"), 64),
            "preference_key": self._text(metadata.get("preference_key"), 128),
            "preference_value": self._text(metadata.get("preference_value"), 512),
            "category": self._text(metadata.get("category"), 64),
            "version": int(metadata.get("version") or 1),
            "is_latest": bool(metadata.get("is_latest", True)),
            "scene_id": self._text(metadata.get("scene_id"), 128),
            "confidence": float(metadata.get("confidence") or 0.0),
            "save_score": float(metadata.get("save_score") or 0.0),
            "supersedes_memory_id": self._text(metadata.get("supersedes_memory_id"), 128),
            "created_at": self._text(metadata.get("created_at") or now, 64),
            "updated_at": self._text(metadata.get("updated_at") or now, 64),
            "is_deleted": bool(metadata.get("is_deleted", False)),
            "metadata": dict(metadata),
        }

    def _build_expr(self, filters: dict[str, Any]) -> str:
        '''过滤解析器，变成SQL相似语句'''
        parts: list[str] = []
        for key, value in filters.items():
            if value is None or key not in self._FILTERABLE_FIELDS:
                continue
            if isinstance(value, list | tuple | set):
                values = ", ".join(self._literal(v) for v in value)
                parts.append(f"{key} in [{values}]")
            else:
                parts.append(f"{key} == {self._literal(value)}")
        return " and ".join(parts) if parts else ""

    def _public_record(self, row: dict[str, Any], *, include_embedding: bool = False) -> dict[str, Any]:
        '''把所有东西塞进metadata'''
        item = dict(row)
        metadata = dict(item.get("metadata") or {})
        for key in self._OUTPUT_FIELDS:
            if key != "metadata" and key in item:
                metadata.setdefault(key, item[key])
        item["metadata"] = metadata
        if not include_embedding:
            item.pop("embedding", None)
        return item

    def _normalize_embedding(self, embedding: list[float]) -> list[float]:
        if len(embedding) != self.vector_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured dim {self.vector_dim}")
        return [float(v) for v in embedding]

    @staticmethod
    def _ensure_lite_parent(uri: str) -> None:
        if "://" in uri:
            return
        path = Path(uri)
        if path.parent:
            path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _text(value: Any, max_length: int, default: str = "") -> str:
        if value is None:
            value = default
        text = str(value)
        return text[:max_length]

    @staticmethod
    def _literal(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        return f'"{MilvusClient._escape(str(value))}"'

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat() + "Z"
