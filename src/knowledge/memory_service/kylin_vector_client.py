"""System vector SDK transport via ABI 1. No fallback to pymilvus or memory.

Only the pymilvus-shaped operations used by our existing wrapper are exposed.
The system SDK remains responsible for every actual database operation.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import threading
from pathlib import Path

from src.knowledge.memory_service.milvus_client import MilvusClient


class KylinVectorError(RuntimeError):
    pass


class NativeVectorBridge:
    def __init__(self, library: str | None = None, *, endpoint: str = "", timeout_ms: int = 5000):
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        candidate = library or os.getenv("OS_AGENT_KYLIN_VECTOR_LIBRARY")
        if candidate:
            candidate = str(Path(candidate).expanduser().resolve(strict=True))
        else:
            candidate = ctypes.util.find_library("mokoa_kylin_vector_bridge")
        if not candidate:
            raise KylinVectorError("System SDK bridge not found; set OS_AGENT_KYLIN_VECTOR_LIBRARY")
        self._lock = threading.RLock()
        self._handle = None
        self.timeout_ms = timeout_ms
        self._lib = ctypes.CDLL(candidate)
        for name, args, result in (
            ("mokoa_vector_abi_version", [], ctypes.c_int),
            ("mokoa_vector_create", [], ctypes.c_void_p),
            ("mokoa_vector_call", [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
            ("mokoa_vector_free", [ctypes.c_void_p], None),
            ("mokoa_vector_destroy", [ctypes.c_void_p], None),
        ):
            fn = getattr(self._lib, name)
            fn.argtypes, fn.restype = args, result
        if self._lib.mokoa_vector_abi_version() != 1:
            raise KylinVectorError("Unsupported vector bridge ABI")
        self._handle = self._lib.mokoa_vector_create()
        if not self._handle:
            raise KylinVectorError("Native handle allocation failed")
        try:
            self.info = self.call("connect", endpoint=endpoint)
        except BaseException:
            self.close()
            raise

    def call(self, op: str, **payload):
        with self._lock:
            if not self._handle:
                raise KylinVectorError("Vector bridge is closed")
            request = json.dumps(dict(payload, op=op, timeout_ms=self.timeout_ms),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
            pointer = self._lib.mokoa_vector_call(self._handle, request)
            if not pointer:
                raise KylinVectorError("Native response allocation failed")
            try:
                response = json.loads(ctypes.string_at(pointer).decode("utf-8"))
            finally:
                self._lib.mokoa_vector_free(pointer)
            if not response.get("ok"):
                raise KylinVectorError(f"{op}: SDK code {response.get('code')}: {response.get('error')}")
            return response["result"]

    def close(self):
        with self._lock:
            if self._handle:
                self._lib.mokoa_vector_destroy(self._handle)
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _Schema:
    def __init__(self):
        self.fields = []

    def add_field(self, name, datatype, **kwargs):
        self.fields.append(dict(name=name, type=int(datatype), **kwargs))


class _Index:
    def add_index(self, **kwargs):
        if kwargs != dict(field_name="embedding", index_type="FLAT", metric_type="COSINE"):
            raise ValueError("System bridge currently supports embedding FLAT/COSINE only")


class _SdkTransport:
    def __init__(self, bridge):
        self.bridge = bridge
        self.schema = None

    def create_schema(self, *, auto_id, enable_dynamic_field):
        if auto_id or enable_dynamic_field:
            raise ValueError("Explicit memory schema required")
        self.schema = _Schema()
        return self.schema

    def prepare_index_params(self):
        return _Index()

    def has_collection(self, collection_name):
        return self.bridge.call("has_collection", collection=collection_name)

    def create_collection(self, *, collection_name, schema, index_params):
        return self.bridge.call("create_collection", collection=collection_name, fields=schema.fields)

    def load_collection(self, collection_name):
        return self.bridge.call("load_collection", collection=collection_name)

    def insert(self, *, collection_name, data):
        return self.bridge.call("insert", collection=collection_name, rows=data, fields=self.schema.fields)

    def upsert(self, *, collection_name, data):
        return self.bridge.call("upsert", collection=collection_name, rows=data, fields=self.schema.fields)

    def query(self, *, collection_name, filter, output_fields, limit):
        rows = self.bridge.call("query", collection=collection_name, filter=filter, output_fields=output_fields)
        # Client-side cap only. SDK does not expose server limit/offset.
        return rows[:limit]

    def search(self, *, collection_name, data, filter, limit, output_fields, search_params):
        if len(data) != 1 or search_params.get("metric_type") != "COSINE":
            raise ValueError("Expected one COSINE query vector")
        return self.bridge.call("search", collection=collection_name, vector=data[0],
                                filter=filter, top_k=limit, output_fields=output_fields)

    def delete(self, *, collection_name, filter):
        return self.bridge.call("delete", collection=collection_name, filter=filter)


class KylinVectorClient(MilvusClient):
    """Reuse row mapping, filters, full-row upsert and soft/hard delete logic."""
    def __init__(self, *, collection: str, vector_dim: int, library: str | None = None,
                 endpoint: str = "", timeout_ms: int = 5000):
        if vector_dim <= 0:
            raise ValueError("vector_dim must be positive")
        self.bridge = NativeVectorBridge(library, endpoint=endpoint, timeout_ms=timeout_ms)
        try:
            super().__init__(uri=endpoint or "kylin-system-sdk", collection=collection,
                             vector_dim=vector_dim, client=_SdkTransport(self.bridge))
        except BaseException:
            self.bridge.close()
            raise

    def close(self):
        self.bridge.close()
