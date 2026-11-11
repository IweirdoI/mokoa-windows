"""Factory to wire Memory Orchestrator."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable

from src.common.config import load_default_config
from src.common.kylin_runtime import (
    discover_bridge_library,
    host_can_load_kylin_sdk,
    under_pytest,
)
from src.core.consolidator import MemoryConsolidator
from src.core.layer_manager import LayerManager
from src.core.orchestrator import MemoryOrchestrator
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.staging_store import StagingStore
from src.knowledge.memory_service.milvus_client import MilvusClient
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
from src.preference.llm_client import SiliconFlowClient
from src.preference.store import PreferenceMemoryStore
from src.security.service import SecurityAndForgetService, redact_sensitive_text


logger = logging.getLogger(__name__)


def _load_default_config() -> dict:
    """读取默认配置（复用 src/common/config.py 的缓存式加载器）。"""
    return load_default_config()


def _create_staging_store(
    storage_config: dict,
    *,
    project_root: Path,
    semantic_redact: Callable[[str], str] | None = None,
) -> tuple[StagingStore | None, float]:
    """根据配置创建 ingestion staging，并返回统一的去重 TTL。

    配置文件中的相对路径以项目根目录为基准，而不是以启动命令的当前目录为
    基准。这样从仓库根目录、IDE 或系统服务启动 API 时都会落到同一个数据库。

    SQLite 是辅助审计设施。如果目录权限、文件锁等基础设施问题导致初始化失败，
    工厂会记录异常并回退到进程内去重，避免整个 API 在 import 阶段无法启动。
    未知 backend 则属于配置错误，直接抛出异常以便尽早修正。
    """

    backend = str(storage_config.get("staging_backend", "memory")).strip().lower()
    dedup_ttl_seconds = float(storage_config.get("dedup_ttl_seconds", 300.0))
    if backend == "memory":
        return None, dedup_ttl_seconds
    if backend != "sqlite":
        raise ValueError(
            "storage.staging_backend 仅支持 sqlite 或 memory，"
            f"当前值为 {backend!r}"
        )

    configured_path = Path(
        str(storage_config.get("staging_sqlite", "./data/ingestion_staging.db"))
    ).expanduser()
    db_path = (
        configured_path
        if configured_path.is_absolute()
        else project_root / configured_path
    )
    try:
        return (
            StagingStore(
                str(db_path),
                dedup_ttl_seconds=dedup_ttl_seconds,
                raw_archive_mode=str(
                    storage_config.get("staging_raw_archive_mode", "redacted")
                ),
                # 只接受 YAML 布尔 true；字符串 "true" 不应意外打开明文归档。
                allow_plaintext_raw_archive=(
                    storage_config.get("staging_allow_plaintext_raw_archive", False)
                    is True
                ),
                raw_retention_days=int(
                    storage_config.get("staging_raw_retention_days", 7)
                ),
                audit_retention_days=int(
                    storage_config.get("staging_audit_retention_days", 90)
                ),
                auto_purge_interval_seconds=float(
                    storage_config.get("staging_auto_purge_interval_seconds", 0)
                ),
                semantic_redact=semantic_redact,
            ),
            dedup_ttl_seconds,
        )
    except (OSError, sqlite3.Error):
        from src.common.kylin_runtime import acceptance_enabled
        if acceptance_enabled():
            raise
        logger.exception("SQLite staging 初始化失败，回退到进程内去重")
        return None, dedup_ttl_seconds


def _probe_kylin_vector_dim() -> int:
    configured = os.getenv("MEMORY_VECTOR_DIM")
    if configured:
        return int(configured)
    from src.embedding.embedding_interface import get_shared_embedding_backend

    provider = get_shared_embedding_backend("kylin")
    dimension = provider.dimension
    if dimension <= 0:
        raise RuntimeError("麒麟 Embedding 探测到非法维度")
    os.environ.setdefault("MEMORY_VECTOR_DIM", str(dimension))
    return dimension


def _lock_kylin_runtime(project_root: Path, library: Path) -> None:
    os.environ.setdefault("OS_AGENT_KYLIN_VECTOR_LIBRARY", str(library))
    os.environ.setdefault("OS_AGENT_EMBEDDING_BACKEND", "kylin")
    os.environ.setdefault("OS_AGENT_KYLIN_STRICT", "1")
    os.environ.setdefault("OS_AGENT_MILVUS_COLLECTION", "mokoa_kylin_sdk")


def resolve_memory_backend(storage_config: dict, *, project_root: Path) -> str:
    from src.common.kylin_runtime import enforce_acceptance
    enforce_acceptance()
    requested = os.getenv(
        "OS_AGENT_MEMORY_BACKEND",
        str(storage_config.get("memory_backend", "auto")),
    ).strip().lower()
    if requested in {"memory", "milvus", "kylin"}:
        if requested == "kylin":
            library = discover_bridge_library(project_root)
            if library is None:
                raise RuntimeError(
                    "OS_AGENT_MEMORY_BACKEND=kylin 但未找到桥接库；"
                    "请设置 OS_AGENT_KYLIN_VECTOR_LIBRARY 或先编译 "
                    "build/kylin-vector/libmokoa_kylin_vector_bridge.so"
                )
            _lock_kylin_runtime(project_root, library)
        return requested
    if requested != "auto":
        raise ValueError(
            "storage.memory_backend 仅支持 auto、memory、milvus 或 kylin，"
            f"当前值为 {requested!r}"
        )
    if under_pytest():
        return "memory"
    library = discover_bridge_library(project_root)
    if host_can_load_kylin_sdk() and library is not None:
        _lock_kylin_runtime(project_root, library)
        logger.info("麒麟 V11 运行时已锁定系统向量 SDK：%s", library)
        return "kylin"
    logger.warning(
        "记忆后端 auto 回落到进程内 memory：当前不是可加载系统 SDK 的麒麟 V11 用户态"
    )
    return "memory"


def create_orchestrator() -> MemoryOrchestrator:
    from src.common.kylin_runtime import enforce_acceptance
    enforce_acceptance()
    config = _load_default_config()
    memory_cfg = config.get("memory", {})
    storage_cfg = config.get("storage", {})
    project_root = Path(__file__).resolve().parents[2]

    # 安全模块从配置构造：semantic_detection.enabled=true 时启用语义层，
    # 端点/密钥/模型名继承顶层 llm 段（见 src/common/config.py）。提前到 staging
    # 之前创建，以便审计/原始归档也能使用语义级脱敏（规则+语义，且不建可逆索引）。
    security = SecurityAndForgetService.from_config(config)

    def _audit_redact(text: str) -> str:
        # 语义检测抖动时回落规则级，绝不让脱敏失败拖垮 ingest。
        try:
            return security.redact_for_export(text)
        except Exception:  # noqa: BLE001
            return redact_sensitive_text(text)

    staging_store, dedup_ttl_seconds = _create_staging_store(
        storage_cfg,
        project_root=project_root,
        semantic_redact=_audit_redact,
    )

    quality_weights = memory_cfg.get("quality_weights")
    # 按类型分阈值：未配置/配置为空时传 None，回落到 QualityEvaluator 内置默认。
    # 各类型的推荐取值与安全区间见 configs/default.yaml 的 memory.quality_type_thresholds。
    type_thresholds_cfg = memory_cfg.get("quality_type_thresholds")
    quality_type_thresholds: dict[str, float] | None = None
    if isinstance(type_thresholds_cfg, dict):
        quality_type_thresholds = {
            str(name): float(value)
            for name, value in type_thresholds_cfg.items()
            if value is not None
        } or None
    ingestion = IngestionPipeline(
        quality_threshold=float(memory_cfg.get("quality_threshold", 0.46)),
        dedup_ttl_seconds=dedup_ttl_seconds,
        staging_store=staging_store,
        quality_weights=quality_weights if isinstance(quality_weights, dict) else None,
        quality_type_thresholds=quality_type_thresholds,
    )
    # 偏好/固化共用的硅基流动客户端（26a9835a方向：提取与跨对话固化抽象共用）。
    pref_llm_cfg = (config.get("preference", {}) or {}).get("llm", {}) or {}
    pref_llm = SiliconFlowClient(
        api_key=pref_llm_cfg.get("api_key") or None,
        base_url=pref_llm_cfg.get("base_url") or None,
        model=pref_llm_cfg.get("model") or None,
    )
    # 中期→长期 LLM 抽象（固化）：默认关闭，避免每次 consolidate 高频调模型；
    # 配置 memory.mid_term_llm_abstraction=true 且 API key 就绪时启用。
    mid_abstraction_enabled = bool(memory_cfg.get("mid_term_llm_abstraction", False))
    consolidator = MemoryConsolidator(
        similarity_threshold=float(memory_cfg.get("mid_term_similarity_threshold", 0.7)),
        keyword_alpha=float(memory_cfg.get("mid_term_keyword_alpha", 0.3)),
        use_llm=mid_abstraction_enabled,
        llm_client=pref_llm if mid_abstraction_enabled else None,
    )
    layer_manager = LayerManager(
        short_capacity=int(memory_cfg.get("short_term_capacity", 10)),
        mid_capacity=int(memory_cfg.get("mid_term_capacity", 50)),
        heat_threshold=float(memory_cfg.get("mid_term_heat_threshold", 3.0)),
        quality_threshold=float(memory_cfg.get("quality_threshold", 0.46)),
        similarity_threshold=float(memory_cfg.get("mid_term_similarity_threshold", 0.7)),
        keyword_alpha=float(memory_cfg.get("mid_term_keyword_alpha", 0.3)),
        recency_tau_hours=float(memory_cfg.get("mid_term_recency_tau_hours", 168.0)),
        recency_gamma=float(memory_cfg.get("mid_term_recency_gamma", 1.0)),
        consolidator=consolidator,
    )
    memory_backend = resolve_memory_backend(storage_cfg, project_root=project_root)
    os.environ["OS_AGENT_RESOLVED_MEMORY_BACKEND"] = memory_backend
    if memory_backend == "memory":
        milvus = InMemoryMilvusClient()
        logger.warning(
            "使用进程内记忆后端：仅供开发/测试，不代表 Milvus 持久化或性能验收"
        )
    elif memory_backend == "kylin":
        from src.knowledge.memory_service.kylin_vector_client import KylinVectorClient

        milvus = KylinVectorClient(
            collection=os.getenv("OS_AGENT_MILVUS_COLLECTION", str(storage_cfg.get("collection_name", "os_agent_memory"))),
            vector_dim=_probe_kylin_vector_dim(),
            endpoint=os.getenv("OS_AGENT_KYLIN_VECTOR_ENDPOINT", ""),
            timeout_ms=int(os.getenv("OS_AGENT_KYLIN_VECTOR_TIMEOUT_MS", "5000")),
        )
    elif memory_backend == "milvus":
        configured_uri = os.getenv(
            "OS_AGENT_MILVUS_URI",
            str(storage_cfg.get("milvus_uri", "http://localhost:19530")),
        )
        configured_collection = os.getenv(
            "OS_AGENT_MILVUS_COLLECTION",
            str(storage_cfg.get("collection_name", "os_agent_memory")),
        )
        milvus = MilvusClient(
            uri=configured_uri,
            collection=configured_collection,
        )
    else:
        raise ValueError(
            "storage.memory_backend 仅支持 auto、memory、milvus 或 kylin，"
            f"当前值为 {memory_backend!r}"
        )

    # 偏好模块：独立 preference.llm（硅基流动），与顶层 llm（安全语义）分离。
    # 复用上方为固化创建的同一客户端，避免重复初始化。
    preference_cfg = config.get("preference", {}) or {}
    pref_llm_cfg = preference_cfg.get("llm", {}) or {}
    storage_dir = preference_cfg.get("storage_dir", "./data/preferences")
    pref_storage = Path(str(storage_dir)).expanduser()
    if not pref_storage.is_absolute():
        pref_storage = project_root / pref_storage
    preference = PreferenceMemoryStore(
        storage_dir=pref_storage,
        llm_client=pref_llm,
        use_llm=bool(preference_cfg.get("use_llm", True)),
        allow_rules=bool(preference_cfg.get("allow_rules", True)),
    )

    return MemoryOrchestrator(
        ingestion=ingestion,
        security=security,
        layer_manager=layer_manager,
        preference=preference,
        milvus=milvus,
    )
