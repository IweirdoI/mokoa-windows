"""REST API for OS Agent integration. Owner: efca9634."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, File, Form, Query, Request, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.common.models import MemoryType, RetrievalQuery
from src.core.factory import create_orchestrator
from src.core.orchestrator import MemoryOrchestrator, ProcessInputResult
from src.knowledge.memory_service.memory_schema import MemoryEntry
from src.agent.service import ChatRequest, MemoryAgent, ModelUnavailable
from src.knowledge.memory_service.milvus_client import MemoryBackendUnavailable
from src.security.semantic_detector import SemanticDetectionUnavailable

orchestrator: MemoryOrchestrator = create_orchestrator()

app = FastAPI(title="Kylin OS Agent Memory API", version="0.3.0")


@app.exception_handler(SemanticDetectionUnavailable)
async def privacy_unavailable(request, exc):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "敏感信息检测服务暂不可用：已开启语义级检测但本地 LLM 服务"
            "（如 LM Studio）未就绪或不可达。可关闭 semantic_detection 回退到"
            "正则脱敏，或检查本地检测服务后重试。"
        },
    )


@app.exception_handler(MemoryBackendUnavailable)
async def backend_unavailable(request, exc):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "记忆后端（向量库）暂不可用：Milvus/Kylin 连接或集合初始化失败。"
            "请检查向量库服务是否就绪后重试。"
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 统一兜底：不向客户端泄露内部细节/堆栈，仅返回通用 500；详细异常由服务端日志记录。
    # 注：HTTPException(401/413/429/503 等) 与已注册的业务异常处理器优先级更高，不受影响。
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试或联系管理员。"},
    )


@app.post("/agent/chat")
def agent_chat(req: ChatRequest) -> dict[str, Any]:
    try:
        return MemoryAgent(orchestrator).chat(req)
    except ModelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/agent/chat/stream")
async def agent_chat_stream(req: ChatRequest):
    """SSE 流式对话：逐事件推送 token、工具调用开始/结果与最终答案。"""
    agent = MemoryAgent(orchestrator, stream=True)
    return StreamingResponse(agent.chat_stream(req), media_type="text/event-stream")

# CORS：绝不将 allow_origins 设为 "*" 同时 allow_credentials=True（无效且不安全的组合）。
# 来源由 API_CORS_ORIGINS 显式配置（逗号分隔）；未配置时仅放行本地前端，且不开凭证。
_api_cors_origins = os.getenv("API_CORS_ORIGINS", "").strip()
if _api_cors_origins:
    _allow_origins = [o.strip() for o in _api_cors_origins.split(",") if o.strip()]
    _allow_credentials = True
else:
    _allow_origins = ["http://127.0.0.1:8000", "http://localhost:8000"]
    _allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 可选 API 鉴权：仅在 API_AUTH_TOKEN 已配置时启用。启用后所有端点（/health 除外）
# 必须携带 Authorization: Bearer <token>，否则返回 401。未配置时保持本地开发兼容，
# 不阻断既有前端/脚本。鉴权中间件注册在防御中间件之外层，未通过鉴权即不计入限流。
# ---------------------------------------------------------------------------
_API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "").strip()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _API_AUTH_TOKEN:
        return await call_next(request)
    if request.url.path == "/health":
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if token and token == _API_AUTH_TOKEN:
            return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "未授权：缺少或无效的 API 令牌（Authorization: Bearer）"},
    )


# ---------------------------------------------------------------------------
# 基础防御中间件：请求体大小上限 + 按 IP 限流 + 处理超时守卫。
# 纯标准库实现，不引入新依赖；目标是挡住“超大 body 被完整读进内存”与“单 IP 高频”
# 两类最易触发的 DoS，以及单请求长时间挂起拖垮线程池。
# 注：限流为进程内滑动窗口，单实例足够；多实例需外接共享限流（如 Redis）。
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = int(os.getenv("API_MAX_BODY_BYTES", "8_000_000"))            # 普通 JSON 8 MB
MAX_FILE_BODY_BYTES = int(os.getenv("API_MAX_FILE_BODY_BYTES", "50_000_000"))  # 文件上传 50 MB
REQUEST_TIMEOUT_SECONDS = float(os.getenv("API_REQUEST_TIMEOUT", "180"))
RATE_LIMIT_MAX = int(os.getenv("API_RATE_LIMIT_MAX", "120"))
RATE_LIMIT_WINDOW = float(os.getenv("API_RATE_LIMIT_WINDOW", "60"))

_rate_state: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def defense_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"

    # 1) 限流：滑动窗口内超过阈值直接拒绝（先清理过期计数再判断）
    now = time.monotonic()
    bucket = _rate_state[client_ip]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX:
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    bucket.append(now)

    # 2) 请求体大小上限（基于 Content-Length 头；分块上传无该头时由端点读取兜底）
    if request.method in ("POST", "PUT", "PATCH"):
        limit = (
            MAX_FILE_BODY_BYTES
            if request.url.path.endswith("/ingest/file")
            else MAX_BODY_BYTES
        )
        raw_cl = request.headers.get("content-length")
        if raw_cl and raw_cl.isdigit() and int(raw_cl) > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": f"请求体过大（上限 {limit} 字节）"},
            )

    # 3) 处理超时守卫（流式端点除外，避免打断 SSE 长连接）
    if request.url.path.endswith("/agent/chat/stream"):
        return await call_next(request)
    try:
        return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"detail": "请求处理超时"})


async def _read_upload_bounded(file: UploadFile, limit: int) -> bytes:
    """有界分块读取上传文件，累计超过 limit 字节即拒绝，避免无限占用内存。"""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"上传文件超过上限（{limit} 字节）",
            )
    return b"".join(chunks)


class IngestRequest(BaseModel):
    user_id: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    scene_id: str | None = None


class RecallRequest(BaseModel):
    user_id: str
    query: str = ""
    top_k: int = Field(default=5, ge=1, le=100)
    memory_type: MemoryType | None = None
    scene_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    retrieval_mode: str = Field(default="auto", pattern="^(auto|semantic|hybrid)$")


class StoreEntryRequest(BaseModel):
    entry: MemoryEntry


class ForgetRequest(BaseModel):
    user_id: str
    instruction: str


class PreferenceExtractRequest(BaseModel):
    user_id: str
    text: str
    scene_id: str | None = None
    source: str = "conversation"


class StagingPurgeRequest(BaseModel):
    raw_retention_days: int | None = Field(default=None, ge=0)
    audit_retention_days: int | None = Field(default=None, ge=0)


def _serialize_process_result(result: ProcessInputResult) -> dict[str, Any]:
    record = result.record.model_dump(mode="json") if result.record else None
    return {
        "accepted": result.accepted,
        "memory_id": result.memory_id,
        "memory_type": result.memory_type,
        "reject_code": result.reject_code,
        "reasons": result.reasons,
        "quality_score": result.quality_score,
        "fingerprint": result.fingerprint,
        "evicted_memory_id": result.evicted_memory_id,
        "updated_existing": result.updated_existing,
        "processing_ms": result.processing_ms,
        "stage_ms": result.stage_ms,
        "preference_extracted": result.preference_extracted,
        "preference_stored_ids": result.preference_stored_ids,
        "preference_keys": result.preference_keys,
        "record": record,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    from src.common.kylin_runtime import acceptance_enabled, enforce_acceptance
    if acceptance_enabled():
        from src.embedding.embedding_interface import get_shared_embedding_backend
        from src.knowledge.memory_service.kylin_vector_client import KylinVectorClient
        enforce_acceptance()
        client = orchestrator._milvus
        if not isinstance(client, KylinVectorClient):
            raise RuntimeError("Acceptance API is not using KylinVectorClient")
        provider = get_shared_embedding_backend()
        vectors = provider.embed(["麒麟实机健康检查"])
        if provider.using_fallback or provider.backend_name != "kylin-coreai":
            raise RuntimeError("Real CoreAI embedding required")
        if len(vectors[0]) != client.vector_dim:
            raise RuntimeError("Embedding/vector database dimension mismatch")
        if client.bridge.info.get("backend") != "kylin-vector-sdk":
            raise RuntimeError("Real vector SDK required")
        client.search(vectors[0], {"user_id": "health_" + os.getenv("OS_AGENT_ACCEPTANCE_RUN_ID", "")}, top_k=1)
        return {"status": "ok", "memory_backend": "kylin", "embedding_backend": "kylin",
                "kylin_strict": "1", "using_fallback": False,
                "vector_dim": client.vector_dim, "vector_sdk": client.bridge.info,
                "collection": client.collection, "pid": os.getpid(),
                "run_id": os.getenv("OS_AGENT_ACCEPTANCE_RUN_ID", "")}
    return {
        "status": "ok",
        "memory_backend": os.getenv("OS_AGENT_RESOLVED_MEMORY_BACKEND", ""),
        "embedding_backend": os.getenv(
            "OS_AGENT_EMBEDDING_BACKEND",
            os.getenv("EMBEDDING_BACKEND", ""),
        ),
    }


def _serialize_layer_snapshot(snapshot, *, role: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "count": snapshot.count,
        "capacity": snapshot.capacity,
        "preference_count": snapshot.preference_count,
        "knowledge_count": snapshot.knowledge_count,
    }
    if snapshot.avg_heat is not None:
        payload["avg_heat"] = snapshot.avg_heat
    if role is not None:
        payload["role"] = role
    return payload


@app.get("/stats/{user_id}")
def memory_stats(user_id: str) -> dict[str, Any]:
    stats = orchestrator.memory_stats(user_id)
    return {
        "user_id": stats.user_id,
        "total_count": stats.total_count,
        "total_preference_count": stats.total_preference_count,
        "total_knowledge_count": stats.total_knowledge_count,
        "layers": {
            "short": _serialize_layer_snapshot(stats.short),
            "mid": _serialize_layer_snapshot(stats.mid),
            "long": _serialize_layer_snapshot(stats.long, role="pending_persist"),
            "milvus": _serialize_layer_snapshot(stats.milvus, role="authoritative"),
        },
    }


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    result = orchestrator.process_input(
        req.payload,
        req.source,
        user_id=req.user_id,
        scene_id=req.scene_id,
    )
    body = _serialize_process_result(result)
    body["user_id"] = req.user_id
    body["status"] = "accepted" if result.accepted else "rejected"
    return body


@app.post("/ingest/file")
async def ingest_file(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    scene_id: str | None = Form(default=None),
    caption: str | None = Form(default=None),
) -> dict[str, Any]:
    """上传 PDF / Word / 图片，解析后走与 ``/ingest`` 相同的记忆写入链。"""

    from src.ingestion.file_extract import FileExtractionError, extract_document

    filename = file.filename or "upload.bin"
    # 有界分块读取：无论客户端是否带 Content-Length，都不允许超出上限地读入内存
    # （防御中间件仅基于 Content-Length 头判断，分块上传会绕过该守卫，需在端点兜底）。
    data = await _read_upload_bounded(file, MAX_FILE_BODY_BYTES)
    try:
        payload = extract_document(filename, data, caption=caption)
    except FileExtractionError as exc:
        return {
            "accepted": False,
            "status": "rejected",
            "user_id": user_id,
            "filename": filename,
            "memory_id": None,
            "reject_code": (
                "invalid_source" if exc.code == "unsupported_file" else "invalid_payload"
            ),
            "reasons": [str(exc)],
            "record": None,
        }
    result = orchestrator.process_input(
        payload,
        "document",
        user_id=user_id,
        scene_id=scene_id,
    )
    body = _serialize_process_result(result)
    body["user_id"] = user_id
    body["status"] = "accepted" if result.accepted else "rejected"
    body["filename"] = filename
    return body


@app.get("/short-term/{user_id}")
def list_short_term(
    user_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    scene_id: str | None = None,
    memory_type: MemoryType | None = None,
) -> dict[str, Any]:
    parsed_type = MemoryType(memory_type) if memory_type is not None else None
    items = orchestrator.list_short_term(
        user_id,
        limit=limit,
        scene_id=scene_id,
        memory_type=parsed_type,
    )
    stats = orchestrator.short_term_stats(user_id)
    return {
        "user_id": user_id,
        "count": len(items),
        "capacity": stats.capacity,
        "preference_count": stats.preference_count,
        "knowledge_count": stats.knowledge_count,
        "items": [item.model_dump(mode="json") for item in items],
    }


@app.post("/short-term/{user_id}/{memory_id}/touch")
def touch_short_term(user_id: str, memory_id: str) -> dict[str, Any]:
    """记录记忆被 Agent 实际使用一次，为 short → mid 晋升提供热度信号。"""

    item = orchestrator.touch_short_term_item(user_id, memory_id)
    return {
        "user_id": user_id,
        "memory_id": memory_id,
        "touched": item is not None,
        "access_count": item.metadata.get("access_count") if item else None,
        "heat": item.metadata.get("heat") if item else None,
    }


@app.get("/mid-term/{user_id}")
def list_mid_term(
    user_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    scene_id: str | None = None,
    memory_type: MemoryType | None = None,
) -> dict[str, Any]:
    parsed_type = MemoryType(memory_type) if memory_type is not None else None
    items = orchestrator.list_mid_term(
        user_id,
        limit=limit,
        scene_id=scene_id,
        memory_type=parsed_type,
    )
    stats = orchestrator.mid_term_stats(user_id)
    return {
        "user_id": user_id,
        "count": len(items),
        "capacity": stats.capacity,
        "preference_count": stats.preference_count,
        "knowledge_count": stats.knowledge_count,
        "avg_heat": stats.avg_heat,
        "items": [item.model_dump(mode="json") for item in items],
    }


@app.post("/consolidate/{user_id}/mid")
def consolidate_mid(user_id: str) -> dict[str, Any]:
    result = orchestrator.consolidate_mid(user_id)
    return {
        "user_id": result.user_id,
        "promoted_to_mid": result.promoted_to_mid,
        "short_remaining": result.short_remaining,
        "mid_remaining": result.mid_remaining,
    }


@app.post("/consolidate/{user_id}")
def consolidate(user_id: str) -> dict[str, Any]:
    result = orchestrator.consolidate(user_id)
    return {
        "user_id": result.user_id,
        "promoted_to_mid": result.promoted_to_mid,
        "promoted_to_long": result.promoted_to_long,
        "retained_in_mid": result.retained_in_mid,
        "short_remaining": result.short_remaining,
        "mid_remaining": result.mid_remaining,
        "stored_memory_ids": result.stored_memory_ids,
    }


@app.post("/store-entry")
def store_entry(req: StoreEntryRequest) -> dict[str, Any]:
    result = orchestrator.store_memory_entry(req.entry)
    return {
        "memory_id": result.memory_id,
        "created": result.created,
        "reason": result.reason,
        "superseded_memory_id": result.superseded_memory_id,
    }


@app.get("/preferences/{user_id}")
def list_preferences(
    user_id: str,
    preference_key: str | None = None,
    latest_only: bool = True,
    top_k: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    result = orchestrator.list_preferences(
        user_id,
        preference_key=preference_key,
        latest_only=latest_only,
        top_k=top_k,
    )
    return {
        "user_id": user_id,
        "count": len(result.items),
        "latency_ms": result.latency_ms,
        "items": [item.model_dump(mode="json") for item in result.items],
    }


@app.post("/preferences/extract")
def extract_preferences(req: PreferenceExtractRequest) -> dict[str, Any]:
    """从文本自动提取偏好并写入权威存储（含版本历史）。"""

    result = orchestrator.extract_preferences_from_text(
        req.text,
        user_id=req.user_id,
        scene_id=req.scene_id,
        source=req.source,
    )
    return {
        "user_id": result.user_id,
        "extracted_count": result.extracted_count,
        "mode": result.mode,
        "degraded_to_rules": result.degraded_to_rules,
        "entries": [entry.model_dump(mode="json") for entry in result.entries],
        "stored": [
            {
                "memory_id": item.memory_id,
                "created": item.created,
                "reason": item.reason,
                "superseded_memory_id": item.superseded_memory_id,
            }
            for item in result.stored
        ],
    }


@app.get("/preferences/{user_id}/profile")
def preference_profile(
    user_id: str,
    scene_id: str | None = None,
) -> dict[str, Any]:
    """结构化偏好画像（以权威长期存储为准）。"""

    profile = orchestrator.get_preference_profile(user_id, scene_id=scene_id)
    return {"user_id": user_id, "scene_id": scene_id, "profile": profile}


@app.get("/preferences/{user_id}/history")
def preference_history(
    user_id: str,
    preference_key: str = Query(..., min_length=1),
) -> dict[str, Any]:
    """权威长期存储版本链。"""

    items = orchestrator.get_preference_history(user_id, preference_key)
    return {
        "user_id": user_id,
        "preference_key": preference_key,
        "count": len(items),
        "items": [item.model_dump(mode="json") for item in items],
    }


@app.post("/recall")
def recall(req: RecallRequest) -> dict[str, Any]:
    parsed_type = MemoryType(req.memory_type) if req.memory_type is not None else None
    result = orchestrator.recall(
        RetrievalQuery(
            query=req.query,
            user_id=req.user_id,
            memory_type=parsed_type,
            scene_id=req.scene_id,
            top_k=req.top_k,
            retrieval_mode=req.retrieval_mode,
        ),
        filters=req.filters,
    )
    return {
        "user_id": req.user_id,
        "count": len(result.items),
        "latency_ms": result.latency_ms,
        "strategy": result.strategy,
        "items": [item.model_dump(mode="json") for item in result.items],
    }


@app.post("/forget")
def forget(req: ForgetRequest) -> dict[str, Any]:
    deleted_ids = orchestrator.forget(req.user_id, req.instruction)
    return {
        "user_id": req.user_id,
        "instruction": req.instruction,
        "count": len(deleted_ids),
        "deleted_ids": deleted_ids,
    }


@app.get("/staging/stats")
def staging_stats(user_id: str | None = None) -> dict[str, Any]:
    """ingestion 拒绝码分布与处理耗时分位数。"""

    body = orchestrator.staging_stats(user_id)
    if user_id is not None:
        body["user_id"] = user_id
    return body


@app.post("/staging/purge")
def purge_staging(req: StagingPurgeRequest) -> dict[str, Any]:
    """手动执行 ingestion raw/audit 保留期清理；自动 purge 由配置间隔触发。"""

    return orchestrator.purge_staging(
        raw_retention_days=req.raw_retention_days,
        audit_retention_days=req.audit_retention_days,
    )
