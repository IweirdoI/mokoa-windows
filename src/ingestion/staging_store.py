"""Ingestion SQLite 中转库。

这个模块只保存摄取阶段的事件审计、短期去重状态和可选的原始载荷，不承担
最终记忆存储职责。最终可检索记忆仍由 ``src.knowledge.memory_service`` 写入
Milvus；这里也不会保存 embedding。

每个 ``StagingStore`` 维护一个允许跨线程使用的 SQLite 长连接，并用 ``RLock``
串行保护该连接。这样可以避开 Windows 上频繁开关数据库文件的明显耗时，同时
仍允许多个进程或多个 ``StagingStore`` 实例依靠 SQLite 文件锁共享同一个库。

业务规则：
    * staging 仅保存 ingestion 审计、TTL 指纹和可选 raw，不保存 embedding。
    * event 通过 ingestion event ID 绑定本地 memory ID，再可选补充 Milvus ID，三者不互相覆盖。
    * raw 默认脱敏且上限 16 KiB；metadata 脱敏且上限 4 KiB。
    * 默认 raw 保留 7 天、审计保留 90 天；删除记忆时保留无内容审计骨架。

算法思路：
    使用 WAL + busy timeout 适配“频繁追加、偶尔查询”；多步写操作统一使用
    ``BEGIN IMMEDIATE`` 事务。进程内 RLock 保护共享连接，跨实例/进程依赖 SQLite 文件锁。

依赖关系：
    上游由 ``IngestionPipeline`` 调用；脱敏依赖
    ``src.security.service.redact_sensitive_text``；映射绑定与擦除由
    Orchestrator 调用；与 ``memory_service``/Milvus 没有直接数据库依赖。

Owner: 3fba60e1
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.ingestion.models import IngestionResult
from src.security.service import redact_sensitive_text


logger = logging.getLogger(__name__)


class StagingStore:
    """管理 ingestion staging SQLite 数据库。

    Args:
        db_path: SQLite 文件路径，不可为空/``None``；父目录不存在时自动创建。
        dedup_ttl_seconds: 已接收指纹的有效秒数，范围 ``[0, +∞)``；0 关闭去重。
        raw_archive_mode: ``redacted`` 默认脱敏归档；``off`` 不归档；
            ``plaintext`` 仅供显式开启的开发环境。
        allow_plaintext_raw_archive: 布尔值；plaintext 模式必须为 ``True``。
        raw_retention_days: raw 保留天数，必须是非负整数且不可为布尔值。
        audit_retention_days: 审计保留天数，约束同上。
        auto_purge_interval_seconds: 自动 purge 间隔；``0`` 关闭后台线程。

    Raises:
        ValueError: TTL/保留期越界、raw 模式未知，或 plaintext 缺少二次确认。
        OSError: 数据库父目录无法创建或路径无法访问。
        sqlite3.Error: 建立连接、PRAGMA、建表或最小迁移失败。

    ``init_schema`` 会在构造时自动执行，调用方不需要额外判断数据库是否首次
    创建。该方法使用 ``IF NOT EXISTS``，因此重复启动服务也是安全的。
    """

    _METADATA_LIMIT_BYTES = 4 * 1024
    _RAW_LIMIT_BYTES = 16 * 1024
    _BUSY_TIMEOUT_MS = 5_000
    _RAW_ARCHIVE_MODES = frozenset({"redacted", "off", "plaintext"})
    _SENSITIVE_KEY_PARTS = frozenset(
        {
            "authorization",
            "bank_card",
            "cookie",
            "credential",
            "email",
            "id_card",
            "identity",
            "passwd",
            "password",
            "phone",
            "secret",
            "token",
        }
    )
    # ingestion 自有信封/评分字段不得按敏感子串误伤，尤其是 identity_defaulted。
    _SAFE_METADATA_KEYS = frozenset(
        {
            "explicit",
            "identity_defaulted",
            "occurrence_accumulated",
            "occurrence_count",
            "occurrence_prior_count",
            "quality_reasons",
            "schema_version",
            "stage_ms",
            "truncated_fields",
        }
    )

    def __init__(
        self,
        db_path: str = "./data/ingestion_staging.db",
        dedup_ttl_seconds: float = 300.0,
        *,
        raw_archive_mode: str = "redacted",
        allow_plaintext_raw_archive: bool = False,
        raw_retention_days: int = 7,
        audit_retention_days: int = 90,
        auto_purge_interval_seconds: float = 0.0,
        semantic_redact: Callable[[str], str] | None = None,
    ) -> None:
        """验证配置、创建长连接并幂等建库。

        参数范围和异常契约见类文档。构造成功后三张业务表和索引
        已可用；构造失败时不返回部分初始化的实例。
        """
        if dedup_ttl_seconds < 0:
            raise ValueError("dedup_ttl_seconds 不能小于 0")

        normalized_archive_mode = raw_archive_mode.strip().lower()
        if normalized_archive_mode not in self._RAW_ARCHIVE_MODES:
            raise ValueError(
                "raw_archive_mode 仅支持 redacted、off 或 plaintext，"
                f"当前值为 {raw_archive_mode!r}"
            )
        if normalized_archive_mode == "plaintext" and not allow_plaintext_raw_archive:
            raise ValueError(
                "plaintext 原始归档仅允许开发环境显式启用："
                "请同时设置 allow_plaintext_raw_archive=true"
            )
        self._validate_retention_days(raw_retention_days, "raw_retention_days")
        self._validate_retention_days(audit_retention_days, "audit_retention_days")
        if (
            isinstance(auto_purge_interval_seconds, bool)
            or float(auto_purge_interval_seconds) < 0
        ):
            raise ValueError("auto_purge_interval_seconds 必须是大于等于 0 的数字")

        self.db_path = Path(db_path).expanduser()
        self.dedup_ttl_seconds = float(dedup_ttl_seconds)
        self.raw_archive_mode = normalized_archive_mode
        self.allow_plaintext_raw_archive = bool(allow_plaintext_raw_archive)
        self.raw_retention_days = int(raw_retention_days)
        self.audit_retention_days = int(audit_retention_days)
        self.auto_purge_interval_seconds = float(auto_purge_interval_seconds)
        # 语义级脱敏回调（规则+语义，且不建可逆索引），用于审计/归档落库前脱敏；
        # 未注入时回落到规则级 redact_sensitive_text。
        self._semantic_redact = semantic_redact
        self._lock = threading.RLock()
        self._closed = False
        self._stop_purge = threading.Event()
        self._purge_thread: threading.Thread | None = None

        # 建库前只创建目标文件的直接父目录，不触碰工作区中的其他路径。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.db_path),
            timeout=self._BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            # FastAPI 的同步端点可能由不同工作线程调用。连接本身由下方 RLock
            # 串行保护，因此可以安全关闭 SQLite 默认的同线程限制。
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        self.init_schema()
        self._start_auto_purge()

    def init_schema(self) -> None:
        """幂等创建三张业务表及查询索引。

        WAL 允许读取和单个写入并行进行，适合本项目“频繁追加审计、偶尔查询”的
        工作负载。``busy_timeout`` 让短暂写锁竞争等待一小段时间，而不是立刻向
        API 抛出 ``database is locked``。

        Returns:
            ``None``。成功后表、索引和向前兼容列均存在。
        Raises:
            sqlite3.Error: PRAGMA、DDL 或旧库列迁移失败。

        Business Rules:
            使用 ``IF NOT EXISTS`` 和列检测保证反复调用幂等；不删除旧列/数据。

        Algorithm:
            持实例锁 → 配置 WAL/NORMAL → 执行 DDL → 检测并补充
            memory_id/milvus_memory_id 列 → 创建关联索引。
        """

        ddl = """
        CREATE TABLE IF NOT EXISTS ingestion_events (
            id                      TEXT PRIMARY KEY,
            user_id                 TEXT NOT NULL,
            scene_id                TEXT,
            source                  TEXT NOT NULL,
            stage                   TEXT NOT NULL,
            accepted                INTEGER NOT NULL DEFAULT 0,
            reject_code             TEXT,
            content                 TEXT,
            confidence              REAL,
            quality_score           REAL,
            quality_threshold       REAL,
            dedup_fingerprint       TEXT,
            record_key              TEXT,
            source_event_id         TEXT,
            memory_id               TEXT,
            milvus_memory_id        TEXT,
            metadata_json           TEXT NOT NULL DEFAULT '{}',
            quality_components_json TEXT NOT NULL DEFAULT '{}',
            reasons_json            TEXT NOT NULL DEFAULT '[]',
            processing_ms           REAL NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_user_created
            ON ingestion_events(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_fingerprint
            ON ingestion_events(dedup_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_events_record_key
            ON ingestion_events(user_id, record_key);
        CREATE INDEX IF NOT EXISTS idx_events_accepted
            ON ingestion_events(accepted, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_events_source
            ON ingestion_events(source, created_at DESC);

        CREATE TABLE IF NOT EXISTS dedup_cache (
            dedup_fingerprint TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL,
            first_seen_at     TEXT NOT NULL,
            expires_at        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_dedup_expires
            ON dedup_cache(expires_at);

        CREATE TABLE IF NOT EXISTS raw_payload_archive (
            event_id      TEXT PRIMARY KEY,
            source        TEXT NOT NULL,
            raw_json      TEXT NOT NULL,
            envelope_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_raw_event
            ON raw_payload_archive(event_id);
        """

        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(ddl)
            self._ensure_column(connection, "ingestion_events", "memory_id", "TEXT")
            self._ensure_column(
                connection,
                "ingestion_events",
                "milvus_memory_id",
                "TEXT",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_memory
                ON ingestion_events(user_id, memory_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_milvus_memory
                ON ingestion_events(user_id, milvus_memory_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_user_fingerprint
                ON ingestion_events(user_id, dedup_fingerprint)
                """
            )

    def append_event(
        self,
        result: IngestionResult,
        *,
        user_id: str,
        scene_id: str | None,
        source: str,
        raw: dict[str, Any] | None = None,
    ) -> str:
        """追加一条完整摄取结果，并可在同一事务中归档原始载荷。

        事件表和原始载荷表使用同一个事务：只要其中任意一条 SQL 失败，两张表
        都不会留下半条记录。返回的 UUID 同时是 ``ingestion_events.id`` 和
        ``raw_payload_archive.event_id``。

        Args:
            result: 必填 ``IngestionResult``，可为成功或拒绝结果。
            user_id: 非空审计用户 ID；result.record 存在时以 record 中的值为准。
            scene_id: 可选场景 ID，可为 ``None``。
            source: 来源枚举值或字符串，不可为 ``None``。
            raw: 可选原始 payload；``None`` 或 raw 归档关闭时不写 raw 表。
        Returns:
            新生成的 UUID 字符串，即 staging event ID。
        Raises:
            sqlite3.Error: 事务开启、事件/raw 写入或提交失败。
            OSError: 数据库文件在运行期不可访问。
            Exception: 序列化或未预期错误会回滚后原样上抛。

        Business Rules:
            content 和 metadata 在写入前脱敏；已有独立列的字段不重复写
            metadata JSON；normalize 之前失败仍尽量从 raw 提取 source event ID。

        Algorithm:
            生成 event ID → 拆分独立列/metadata → 脱敏与限长序列化
            → ``BEGIN IMMEDIATE`` → 写 event → 按配置写 raw → commit；任一失败 rollback。
        """

        event_id = str(uuid4())
        record = result.record
        metadata = dict(record.metadata) if record is not None else {}

        # 这些字段已经拥有独立列，metadata_json 中不再重复保存，避免 benchmark
        # 读取时出现两个值不一致的问题。兼容字段 fingerprint 也一并剔除。
        standalone_metadata_keys = {
            "quality_score",
            "quality_threshold",
            "quality_components",
            "quality_reasons",
            "dedup_fingerprint",
            "fingerprint",
            "record_key",
            "source_event_id",
        }
        remaining_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in standalone_metadata_keys
        }
        sanitized_metadata = self._sanitize_raw_value(remaining_metadata)
        if result.stage_ms:
            sanitized_metadata["stage_ms"] = {
                name: elapsed for name, elapsed in result.stage_ms.items()
            }

        resolved_user_id = record.user_id if record is not None else user_id
        resolved_scene_id = record.scene_id if record is not None else scene_id
        resolved_source = (
            record.source.value if record is not None else self._enum_value(source)
        )
        reject_code = (
            result.reject_code.value if result.reject_code is not None else None
        )
        quality_threshold = metadata.get("quality_threshold")
        quality_components = metadata.get("quality_components", {})
        # normalize 之前失败时 result.record 为空，但原始来源事件 ID 仍然有审计
        # 价值。按 Normalizer 相同的优先级从 raw 中回退提取。
        source_event_id = metadata.get("source_event_id")
        if source_event_id is None and raw is not None:
            for field in ("action_id", "event_id", "record_id", "turn_id"):
                candidate = raw.get(field)
                if candidate is not None and str(candidate).strip():
                    source_event_id = str(candidate).strip()
                    break

        event_values = (
            event_id,
            resolved_user_id,
            resolved_scene_id,
            resolved_source,
            result.stage.value,
            int(result.accepted),
            reject_code,
            (
                self._semantic_redact(record.content)
                if self._semantic_redact is not None
                else redact_sensitive_text(record.content)
                if record is not None
                else None
            ),
            record.confidence if record is not None else None,
            result.quality_score,
            quality_threshold,
            result.dedup_fingerprint,
            metadata.get("record_key"),
            source_event_id,
            self._json_text(
                sanitized_metadata,
                max_bytes=self._METADATA_LIMIT_BYTES,
            ),
            self._json_text(quality_components),
            self._json_text(result.reasons),
            result.processing_ms,
            self._utc_now_text(),
        )

        insert_event_sql = """
        INSERT INTO ingestion_events (
            id, user_id, scene_id, source, stage, accepted, reject_code,
            content, confidence, quality_score, quality_threshold,
            dedup_fingerprint, record_key, source_event_id, metadata_json,
            quality_components_json, reasons_json, processing_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(insert_event_sql, event_values)
                raw_to_archive = self._prepare_raw_archive(raw)
                if raw_to_archive is not None:
                    envelope = {
                        "user_id": resolved_user_id,
                        "scene_id": resolved_scene_id,
                        "archive_mode": self.raw_archive_mode,
                    }
                    connection.execute(
                        """
                        INSERT INTO raw_payload_archive (
                            event_id, source, raw_json, envelope_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            resolved_source,
                            self._json_text(
                                raw_to_archive,
                                max_bytes=self._RAW_LIMIT_BYTES,
                            ),
                            self._json_text(envelope),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return event_id

    def is_duplicate(self, dedup_fingerprint: str) -> bool:
        """清理过期行后，判断指纹是否仍处于 TTL 窗口内。

        Args:
            dedup_fingerprint: 非空指纹字符串。
        Returns:
            存在且未过期时为 ``True``；TTL 为 0 时始终为 ``False``。
        Raises:
            ValueError: 指纹为空或不是字符串。
            sqlite3.Error: 查询、过期删除或事务失败。

        业务规则：
            该方法只查询，不登记新指纹；正式并发路径应用 ``register_if_new``。
        """

        self._validate_fingerprint(dedup_fingerprint)
        if self.dedup_ttl_seconds == 0:
            return False

        now = self._utc_now_text()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._delete_expired(connection, now)
                row = connection.execute(
                    """
                    SELECT 1 FROM dedup_cache
                    WHERE dedup_fingerprint = ? AND expires_at > ?
                    """,
                    (dedup_fingerprint, now),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return row is not None

    def register_fingerprint(self, dedup_fingerprint: str, user_id: str) -> None:
        """登记一个已经通过质量门控的指纹。

        这个方法按建库说明使用 ``INSERT OR REPLACE``：显式再次登记同一指纹时，
        TTL 会从新的登记时间重新计算。正常 pipeline 使用 ``register_if_new``，
        不会无条件刷新已有指纹。

        Args:
            dedup_fingerprint: 非空指纹字符串。
            user_id: 归属用户 ID，应为非空字符串；本方法不额外 strip。
        Returns:
            ``None``；TTL 为 0 时不执行数据库写入。
        Raises:
            ValueError: 指纹为空或类型错误。
            sqlite3.Error: INSERT/COMMIT 失败。

        Algorithm:
            计算当前 UTC 与过期时间，用 ``INSERT OR REPLACE`` 原子替换该指纹。
        """

        self._validate_fingerprint(dedup_fingerprint)
        if self.dedup_ttl_seconds == 0:
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.dedup_ttl_seconds)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO dedup_cache (
                    dedup_fingerprint, user_id, first_seen_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    dedup_fingerprint,
                    user_id,
                    self._datetime_text(now),
                    self._datetime_text(expires_at),
                ),
            )
            connection.commit()

    def register_if_new(self, dedup_fingerprint: str, user_id: str) -> bool:
        """原子地完成“检查未重复并登记”，供 pipeline 正式写入路径使用。

        建库说明分别定义了 ``is_duplicate`` 和 ``register_fingerprint``，便于调试
        和测试。但正式请求若先查再写，两个并发请求可能同时查到“不重复”。这里
        使用 ``BEGIN IMMEDIATE`` 把清理、查询和插入放入同一个写事务，确保同一
        指纹在一个 TTL 窗口内最多只有一个请求返回 ``True``。

        Args:
            dedup_fingerprint: 非空指纹字符串。
            user_id: 归属用户 ID，应为非空字符串。
        Returns:
            未存在/已过期且成功登记时为 ``True``；未过期重复时为
            ``False``；TTL 为 0 时始终返回 ``True``且不写库。
        Raises:
            ValueError: 指纹为空或类型错误。
            sqlite3.Error: 事务、查询、删除过期项或插入失败。

        Algorithm:
            ``BEGIN IMMEDIATE`` → 删除过期行 → 查未过期指纹 →
            重复则 commit/False，否则 INSERT/commit/True；异常统一 rollback。
        """

        self._validate_fingerprint(dedup_fingerprint)
        if self.dedup_ttl_seconds == 0:
            return True

        now = datetime.now(timezone.utc)
        now_text = self._datetime_text(now)
        expires_text = self._datetime_text(
            now + timedelta(seconds=self.dedup_ttl_seconds)
        )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._delete_expired(connection, now_text)
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM dedup_cache
                    WHERE dedup_fingerprint = ? AND expires_at > ?
                    """,
                    (dedup_fingerprint, now_text),
                ).fetchone()
                if duplicate is not None:
                    connection.commit()
                    return False

                connection.execute(
                    """
                    INSERT INTO dedup_cache (
                        dedup_fingerprint, user_id, first_seen_at, expires_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (dedup_fingerprint, user_id, now_text, expires_text),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release_fingerprint(self, dedup_fingerprint: str) -> bool:
        """释放已登记的 TTL 指纹，供下游写入失败后重试同一事件。

        Args:
            dedup_fingerprint: 非空指纹字符串。
        Returns:
            删除到未过期/未清理行时为 ``True``；TTL 为 0 或未命中为 ``False``。
        Raises:
            ValueError: 指纹为空或类型错误。
            sqlite3.Error: DELETE/COMMIT 失败。
        """

        self._validate_fingerprint(dedup_fingerprint)
        if self.dedup_ttl_seconds == 0:
            return False

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM dedup_cache WHERE dedup_fingerprint = ?",
                (dedup_fingerprint,),
            )
            connection.commit()
        return cursor.rowcount > 0

    def query_events(
        self,
        user_id: str,
        *,
        accepted: bool | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按创建时间倒序查询用户的摄取审计记录。

        Args:
            user_id: 必填用户 ID；查询使用精确匹配，不自动 strip。
            accepted: ``True`` 仅查通过、``False`` 仅查拒绝、``None`` 不过滤。
            source: 可选来源枚举/字符串；``None`` 不过滤。
            limit: 最大返回条数，必须大于 0。
        Returns:
            新到旧的事件字典列表；JSON 列已解析为
            ``metadata``/``quality_components``/``reasons``，无命中时为空列表。
        Raises:
            ValueError: ``limit < 1``。
            sqlite3.Error: 查询失败或 store 已关闭。

        Dependencies:
            返回结构由 ``_event_from_row`` 统一解析，坏 JSON 会使用稳定默认值。
        """

        if limit < 1:
            raise ValueError("limit 必须大于 0")

        clauses = ["user_id = ?"]
        parameters: list[Any] = [user_id]
        if accepted is not None:
            clauses.append("accepted = ?")
            parameters.append(int(accepted))
        if source is not None:
            clauses.append("source = ?")
            parameters.append(self._enum_value(source))
        parameters.append(limit)

        sql = f"""
        SELECT * FROM ingestion_events
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._event_from_row(row) for row in rows]

    def count_events(
        self,
        user_id: str,
        *,
        dedup_fingerprint: str,
    ) -> int:
        """统计某用户下同一去重指纹已审计的事件数。

        用于行为频次的服务端累计：低质量拒绝也会写入 ``ingestion_events``，
        但不登记 ``dedup_cache``。遗忘擦除后指纹为空，不再计入。

        Args:
            user_id: 必填用户 ID，精确匹配，防止跨用户累计。
            dedup_fingerprint: 非空去重指纹。
        Returns:
            已落库条数，范围 ``[0, +∞)``；用户或指纹为空时为 0。
        Raises:
            sqlite3.Error: 查询失败或 store 已关闭。
        """

        if not str(user_id).strip() or not dedup_fingerprint:
            return 0

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM ingestion_events
                WHERE user_id = ? AND dedup_fingerprint = ?
                """,
                (user_id, dedup_fingerprint),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def bind_memory_id(self, event_id: str, memory_id: str) -> bool:
        """把 ingestion 审计事件绑定到后续生成的本地记忆 ID。

        Args:
            event_id: 非空 staging event UUID。
            memory_id: 非空 short/mid-term 本地记忆 ID。
        Returns:
            更新到事件时为 ``True``；任一 ID 为空或事件不存在时为 ``False``。
        Raises:
            AttributeError: ID 不是字符串/``None``。
            sqlite3.Error: UPDATE/COMMIT 失败。

        业务规则：
            只更新 ``memory_id``，不覆盖 event ID、record key 或 Milvus ID。
        """

        if not event_id.strip() or not memory_id.strip():
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE ingestion_events SET memory_id = ? WHERE id = ?",
                (memory_id, event_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def bind_milvus_id(
        self,
        user_id: str,
        memory_id: str,
        milvus_memory_id: str,
    ) -> int:
        """为本地记忆补充最终 Milvus ID。

        Args:
            user_id: 必填用户 ID，用于隔离更新范围。
            memory_id: 非空本地记忆 ID。
            milvus_memory_id: 非空最终 Milvus ID。
        Returns:
            受影响的 staging event 条数，范围 ``[0, +∞)``；任一记忆 ID
            为空时返回 0。
        Raises:
            AttributeError: memory ID 不是字符串/``None``。
            sqlite3.Error: UPDATE/COMMIT 失败。

        业务规则：
            以 ``user_id + memory_id`` 精确匹配，只更新 ``milvus_memory_id``。
        """

        if not memory_id.strip() or not milvus_memory_id.strip():
            return 0
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_events
                SET milvus_memory_id = ?
                WHERE user_id = ? AND memory_id = ?
                """,
                (milvus_memory_id, user_id, memory_id),
            )
            connection.commit()
        return max(0, cursor.rowcount)

    def rebind_memory_id(
        self,
        user_id: str,
        old_memory_id: str,
        new_memory_id: str,
    ) -> int:
        """兼容旧调用名；将 ``new_memory_id`` 作为 Milvus ID 补充。

        Args:
            user_id: 用户 ID。
            old_memory_id: 本地 memory ID。
            new_memory_id: 最终 Milvus ID（仅为兼容旧参数名）。
        Returns:
            与 ``bind_milvus_id`` 相同的更新条数。
        Raises:
            AttributeError: ID 类型错误。
            sqlite3.Error: 更新失败。
        """

        return self.bind_milvus_id(user_id, old_memory_id, new_memory_id)

    def resolve_memory_ids(
        self,
        user_id: str,
        memory_id: str,
    ) -> dict[str, list[str]]:
        """用本地或 Milvus ID 查询一组 staging 映射。

        Args:
            user_id: 非空用户 ID。
            memory_id: 非空本地或 Milvus ID。
        Returns:
            含 ``memory_ids`` 和 ``milvus_memory_ids`` 的字典；值为去重排序列表。
            入参任一为空或无命中时，两个列表均为空。
        Raises:
            AttributeError: ID 不是字符串/``None``。
            sqlite3.Error: SELECT 失败。

        业务规则：
            用户 ID 参与查询，不允许跨用户解析映射。
        """

        if not user_id.strip() or not memory_id.strip():
            return {"memory_ids": [], "milvus_memory_ids": []}
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, milvus_memory_id
                FROM ingestion_events
                WHERE user_id = ?
                  AND (memory_id = ? OR milvus_memory_id = ?)
                """,
                (user_id, memory_id, memory_id),
            ).fetchall()
        return {
            "memory_ids": sorted(
                {str(row["memory_id"]) for row in rows if row["memory_id"]}
            ),
            "milvus_memory_ids": sorted(
                {
                    str(row["milvus_memory_id"])
                    for row in rows
                    if row["milvus_memory_id"]
                }
            ),
        }

    def forget_memory(self, user_id: str, memory_id: str) -> dict[str, int]:
        """擦除目标记忆的内容与原始归档，同时保留非内容审计骨架。

        为使用户遗忘后可以重新写入同一内容，本方法也删除对应 TTL 去重指纹。
        ``stage``、``accepted``、质量指标、处理耗时和创建时间等审计字段保留。

        Args:
            user_id: 非空用户 ID，作为强制隔离条件。
            memory_id: 非空本地或 Milvus memory ID。
        Returns:
            包含 ``events_scrubbed``、``raw_deleted``、``dedup_deleted`` 的计数字典；
            无命中/空入参时各项为 0。
        Raises:
            AttributeError: ID 不是字符串/``None``。
            sqlite3.Error: 事务中的 SELECT/DELETE/UPDATE/COMMIT 失败。

        Business Rules:
            擦除 content、指纹、record/source event 身份、metadata 和 reasons；
            删除 raw 与 TTL 指纹；保留质量分、阶段、状态、耗时和时间。

        Algorithm:
            事务内按 user + 任一 memory ID 查事件/指纹 → 删 raw
            → 删 dedup → NULL/清空内容列 → commit；异常时整体 rollback。
        """

        if not user_id.strip() or not memory_id.strip():
            return {"events_scrubbed": 0, "raw_deleted": 0, "dedup_deleted": 0}

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT id, dedup_fingerprint
                    FROM ingestion_events
                    WHERE user_id = ?
                      AND (memory_id = ? OR milvus_memory_id = ?)
                    """,
                    (user_id, memory_id, memory_id),
                ).fetchall()
                event_ids = [str(row["id"]) for row in rows]
                fingerprints = [
                    str(row["dedup_fingerprint"])
                    for row in rows
                    if row["dedup_fingerprint"]
                ]

                raw_deleted = 0
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    cursor = connection.execute(
                        f"DELETE FROM raw_payload_archive WHERE event_id IN ({placeholders})",
                        event_ids,
                    )
                    raw_deleted = max(0, cursor.rowcount)

                dedup_deleted = 0
                if fingerprints:
                    placeholders = ",".join("?" for _ in fingerprints)
                    cursor = connection.execute(
                        f"DELETE FROM dedup_cache WHERE dedup_fingerprint IN ({placeholders})",
                        fingerprints,
                    )
                    dedup_deleted = max(0, cursor.rowcount)

                cursor = connection.execute(
                    """
                    UPDATE ingestion_events
                    SET content = NULL,
                        dedup_fingerprint = NULL,
                        record_key = NULL,
                        source_event_id = NULL,
                        metadata_json = '{}',
                        reasons_json = '[]'
                    WHERE user_id = ?
                      AND (memory_id = ? OR milvus_memory_id = ?)
                      AND (
                          content IS NOT NULL
                          OR dedup_fingerprint IS NOT NULL
                          OR record_key IS NOT NULL
                          OR source_event_id IS NOT NULL
                          OR metadata_json != '{}'
                          OR reasons_json != '[]'
                      )
                    """,
                    (user_id, memory_id, memory_id),
                )
                events_scrubbed = max(0, cursor.rowcount)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return {
            "events_scrubbed": events_scrubbed,
            "raw_deleted": raw_deleted,
            "dedup_deleted": dedup_deleted,
        }

    def purge_expired(
        self,
        *,
        raw_retention_days: int | None = None,
        audit_retention_days: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, int | str]:
        """手动执行 raw、审计和去重缓存清理。

        当前只提供显式调用，不在请求路径中自动清理。默认采用 7 天 raw、90 天
        ingestion 审计；自动定时任务待部署方式确定后再接入。

        Args:
            raw_retention_days: 可选非负整数；``None`` 使用实例默认值。
            audit_retention_days: 可选非负整数；``None`` 使用实例默认值。
            now: 可选基准时间，``None`` 使用当前 UTC；无时区值按 UTC 解释。
        Returns:
            包含 raw/audit/dedup 删除条数和 raw/audit UTC 截止时间的字典。
        Raises:
            ValueError: 保留天数为负数、非整数或布尔值。
            sqlite3.Error: 清理事务失败。

        Business Rules:
            ``created_at <= cutoff`` 视为到期；raw 还会清理无对应 event 的孤儿行；
            dedup 按自身 ``expires_at`` 清理，不使用审计保留期。

        Algorithm:
            验证保留期/归一 UTC → 计算截止时间 → 事务内删过期/孤儿 raw
            → 删审计前再清 raw → 删审计 → 删过期 dedup → commit。
        """

        raw_days = (
            self.raw_retention_days
            if raw_retention_days is None
            else raw_retention_days
        )
        audit_days = (
            self.audit_retention_days
            if audit_retention_days is None
            else audit_retention_days
        )
        self._validate_retention_days(raw_days, "raw_retention_days")
        self._validate_retention_days(audit_days, "audit_retention_days")

        purge_time = now or datetime.now(timezone.utc)
        if purge_time.tzinfo is None:
            purge_time = purge_time.replace(tzinfo=timezone.utc)
        else:
            purge_time = purge_time.astimezone(timezone.utc)
        raw_cutoff = self._datetime_text(purge_time - timedelta(days=raw_days))
        audit_cutoff = self._datetime_text(purge_time - timedelta(days=audit_days))
        now_text = self._datetime_text(purge_time)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                raw_cursor = connection.execute(
                    """
                    DELETE FROM raw_payload_archive
                    WHERE event_id IN (
                        SELECT id FROM ingestion_events WHERE created_at <= ?
                    )
                    OR event_id NOT IN (SELECT id FROM ingestion_events)
                    """,
                    (raw_cutoff,),
                )
                raw_deleted = max(0, raw_cursor.rowcount)

                # SQLite 未启用级联外键，删除审计前再次清理其可能残留的 raw。
                old_audit_raw_cursor = connection.execute(
                    """
                    DELETE FROM raw_payload_archive
                    WHERE event_id IN (
                        SELECT id FROM ingestion_events WHERE created_at <= ?
                    )
                    """,
                    (audit_cutoff,),
                )
                raw_deleted += max(0, old_audit_raw_cursor.rowcount)

                audit_cursor = connection.execute(
                    "DELETE FROM ingestion_events WHERE created_at <= ?",
                    (audit_cutoff,),
                )
                audit_deleted = max(0, audit_cursor.rowcount)

                dedup_cursor = connection.execute(
                    "DELETE FROM dedup_cache WHERE expires_at <= ?",
                    (now_text,),
                )
                dedup_deleted = max(0, dedup_cursor.rowcount)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return {
            "raw_deleted": raw_deleted,
            "audit_deleted": audit_deleted,
            "dedup_deleted": dedup_deleted,
            "raw_cutoff": raw_cutoff,
            "audit_cutoff": audit_cutoff,
        }

    def export_jsonl(
        self,
        path: str,
        *,
        user_id: str | None = None,
        accepted: bool | None = None,
    ) -> int:
        """把符合条件的审计事件导出为 UTF-8 JSONL，并返回行数。

        导出顺序固定为最早事件在前，方便 benchmark 重放。数据库中的 JSON 字符
        串会先还原成对象，因此每一行都能直接作为结构化样本读取。

        Args:
            path: 必填输出文件路径，不可为空/``None``；父目录自动创建，
                已存在文件会被覆盖。
            user_id: 可选精确用户过滤；``None`` 导出所有用户。
            accepted: ``True``/通过、``False``/拒绝、``None``/全部。
        Returns:
            成功写入的 JSONL 行数，范围 ``[0, +∞)``。
        Raises:
            OSError: 目录创建、文件打开或写入失败。
            sqlite3.Error: 导出查询失败。
            TypeError: path 不能构造为文件路径。

        Business Rules:
            输出固定 UTF-8/LF，按 ``created_at, id`` 升序，便于确定性重放。
        """

        clauses: list[str] = []
        parameters: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            parameters.append(user_id)
        if accepted is not None:
            clauses.append("accepted = ?")
            parameters.append(int(accepted))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT * FROM ingestion_events
        {where_sql}
        ORDER BY created_at ASC, id ASC
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()

        output_path = Path(path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                event = self._event_from_row(row)
                handle.write(self._json_text(event))
                handle.write("\n")
        return len(rows)

    def stats(self, user_id: str | None = None) -> dict[str, Any]:
        """返回总量、通过/拒绝数量及拒绝码分布。

        Args:
            user_id: 可选用户 ID；``None`` 统计全库，其他值精确匹配。
        Returns:
            ``{"total", "accepted", "rejected", "by_reject_code"}`` 字典；
            无数据时计数为 0、分布为空字典。
        Raises:
            sqlite3.Error: 聚合查询失败或 store 已关闭。

        Algorithm:
            一次聚合总量/通过数，一次按 reject_code 分组；拒绝数由差值得到。
        """

        where_sql = "WHERE user_id = ?" if user_id is not None else ""
        parameters: tuple[Any, ...] = (user_id,) if user_id is not None else ()
        with self._lock, self._connect() as connection:
            totals = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(accepted), 0) AS accepted
                FROM ingestion_events
                {where_sql}
                """,
                parameters,
            ).fetchone()
            reject_rows = connection.execute(
                f"""
                SELECT reject_code, COUNT(*) AS count
                FROM ingestion_events
                {where_sql}
                {'AND' if where_sql else 'WHERE'} reject_code IS NOT NULL
                GROUP BY reject_code
                ORDER BY reject_code
                """,
                parameters,
            ).fetchall()

        total = int(totals["total"])
        accepted_count = int(totals["accepted"])
        return {
            "total": total,
            "accepted": accepted_count,
            "rejected": total - accepted_count,
            "by_reject_code": {
                row["reject_code"]: int(row["count"]) for row in reject_rows
            },
        }

    def operational_stats(self, user_id: str | None = None) -> dict[str, Any]:
        """返回拒绝分布与处理耗时分位数，供准上线监控。

        Args:
            user_id: 可选用户 ID；``None`` 统计全库。
        Returns:
            ``stats()`` 字段，外加 ``latency_ms``（avg/p50/p95/max）
            和 ``by_stage`` 平均耗时。
        Raises:
            sqlite3.Error: 查询失败或 store 已关闭。
        """

        summary = self.stats(user_id)
        where_sql = "WHERE user_id = ?" if user_id is not None else ""
        parameters: tuple[Any, ...] = (user_id,) if user_id is not None else ()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT processing_ms, metadata_json
                FROM ingestion_events
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT 1000
                """,
                parameters,
            ).fetchall()

        latencies = [float(row["processing_ms"] or 0.0) for row in rows]
        stage_values: dict[str, list[float]] = {}
        for row in rows:
            stage_ms = self._json_load(row["metadata_json"], {}).get("stage_ms")
            if not isinstance(stage_ms, Mapping):
                continue
            for name, elapsed in stage_ms.items():
                try:
                    stage_values.setdefault(str(name), []).append(float(elapsed))
                except (TypeError, ValueError):
                    continue

        summary["latency_ms"] = self._latency_summary(latencies)
        summary["by_stage"] = {
            name: self._latency_summary(values)
            for name, values in sorted(stage_values.items())
        }
        summary["sample_size"] = len(latencies)
        return summary

    def close(self) -> None:
        """关闭 SQLite 长连接，无入参和返回值。

        Raises:
            sqlite3.Error: 首次关闭连接失败。

        业务规则：
            重复调用幂等；成功关闭后的其他数据库操作会抛
            ``sqlite3.ProgrammingError``。
        """

        self._stop_purge.set()
        thread = self._purge_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> StagingStore:
        """返回当前实例，支持 ``with StagingStore(...)`` 自动释放句柄。"""

        return self

    def __exit__(self, *_: Any) -> None:
        """退出上下文时调用 ``close``；不吞掉 with 块内异常。"""
        self.close()

    def __del__(self) -> None:
        """对象回收时兜底释放句柄。

        无入参与返回值；显式 ``close`` 仍是首选。为避免解释器退出阶段
        产生未处理异常，``sqlite3.Error`` 会在此兜底路径被忽略。
        """

        connection = getattr(self, "_connection", None)
        if connection is None or getattr(self, "_closed", True):
            return
        try:
            connection.close()
        except sqlite3.Error:
            # 解释器退出阶段不应因清理文件句柄产生新的未处理异常。
            pass

    def _start_auto_purge(self) -> None:
        """在间隔大于 0 时启动守护线程，周期调用 ``purge_expired``。"""

        if self.auto_purge_interval_seconds <= 0:
            return
        self._purge_thread = threading.Thread(
            target=self._auto_purge_loop,
            name="ingestion-staging-purge",
            daemon=True,
        )
        self._purge_thread.start()

    def _auto_purge_loop(self) -> None:
        """等待间隔后执行清理，直到 ``close`` 发出停止信号。"""

        interval = self.auto_purge_interval_seconds
        while not self._stop_purge.wait(interval):
            if self._closed:
                return
            try:
                self.purge_expired()
            except Exception:
                logger.exception("自动 purge ingestion staging 失败")

    @staticmethod
    def _latency_summary(values: list[float]) -> dict[str, float]:
        """计算平均、p50、p95 和最大值；空列表返回全 0。"""

        if not values:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        ordered = sorted(values)
        last = len(ordered) - 1

        def percentile(ratio: float) -> float:
            index = min(last, max(0, int(round((len(ordered) - 1) * ratio))))
            return round(ordered[index], 4)

        return {
            "avg": round(sum(ordered) / len(ordered), 4),
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "max": round(ordered[-1], 4),
        }

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """产生实例长连接，并拒绝关闭后的误用。

        Returns:
            context manager 内的 ``sqlite3.Connection``；本方法不新建或关闭连接。
        Raises:
            sqlite3.ProgrammingError: store 已被 ``close``。

        依赖规则：
            调用方应与 ``self._lock`` 组合使用，确保共享连接不被并发操作。
        """

        if self._closed:
            raise sqlite3.ProgrammingError("StagingStore 已关闭")
        yield self._connection

    def _prepare_raw_archive(
        self,
        raw: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """按配置关闭、脱敏或保留原始 payload。

        Args:
            raw: 可选原始字典；``None`` 不归档。
        Returns:
            ``off``/``None`` 返回 ``None``；plaintext 返回浅拷贝；redacted
            返回递归脱敏后的 JSON 兼容结构。
        """

        if raw is None or self.raw_archive_mode == "off":
            return None
        if self.raw_archive_mode == "plaintext":
            return dict(raw)
        return self._sanitize_raw_value(
            raw, redact=self._semantic_redact or redact_sensitive_text
        )

    @classmethod
    def _sanitize_raw_value(
        cls,
        value: Any,
        *,
        key: str | None = None,
        redact: Callable[[str], str] | None = None,
    ) -> Any:
        """递归脱敏 JSON 兼容值。

        Args:
            value: 任意标量或 Mapping/list/tuple，可为 ``None``。
            key: 可选当前字段名；命中敏感关键字时整值替换。
            redact: 叶子字符串脱敏函数；默认规则级 ``redact_sensitive_text``。
                注入语义级回调（规则+语义）时可一并覆盖语义 PII。
        Returns:
            保留容器层级的脱敏副本；tuple 转为 list，字符串交给 ``redact``，
            其他标量原样返回。

        Algorithm:
            先按字段名整值拦截，再深度遍历容器，最后对叶子字符串做脱敏。
        """
        leaf_redact = redact or redact_sensitive_text

        if key is not None and cls._is_sensitive_key(key):
            return None if value is None else "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                map_key: cls._sanitize_raw_value(
                    map_value,
                    key=str(map_key),
                    redact=leaf_redact,
                )
                for map_key, map_value in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize_raw_value(item, redact=leaf_redact) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_raw_value(item, redact=leaf_redact) for item in value]
        if isinstance(value, str):
            return leaf_redact(value)
        return value

    @classmethod
    def _is_sensitive_key(cls, key: str) -> bool:
        """按完整 token 序列判断字段名是否敏感。

        ``identity_defaulted`` 等 ingestion 自有字段走白名单；``tokenizer``
        这类只是子串碰巧包含 ``token`` 的键不会被整值清空。

        Args:
            key: 必填字符串，不可为 ``None``。
        Returns:
            命中 password/token/email/id_card 等完整 token 时为 ``True``。
        Raises:
            AttributeError: key 不是字符串。
        """
        normalized = key.strip().lower().replace("-", "_")
        if normalized in cls._SAFE_METADATA_KEYS:
            return False
        tokens = [token for token in normalized.split("_") if token]
        for part in cls._SENSITIVE_KEY_PARTS:
            part_tokens = [token for token in part.split("_") if token]
            if part_tokens and cls._contains_token_sequence(tokens, part_tokens):
                return True
        return False

    @staticmethod
    def _contains_token_sequence(
        tokens: list[str],
        part_tokens: list[str],
    ) -> bool:
        """判断 ``part_tokens`` 是否作为连续 token 出现在 ``tokens`` 中。"""

        span = len(part_tokens)
        if span == 0 or span > len(tokens):
            return False
        return any(
            tokens[index : index + span] == part_tokens
            for index in range(len(tokens) - span + 1)
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """为已有 SQLite 文件执行最小向前迁移。

        ``connection``/``table``/``column``/``definition`` 均必填，且后三者仅允许
        内部常量，不可传用户输入。列已存在时无操作，否则执行
        ``ALTER TABLE``；无返回值，SQL 失败抛 ``sqlite3.Error``。
        """

        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _validate_retention_days(value: int, field: str) -> None:
        """验证保留期为非负整数且不是 bool，失败抛 ``ValueError``。"""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} 必须是大于等于 0 的整数")

    @staticmethod
    def _delete_expired(connection: sqlite3.Connection, now_text: str) -> None:
        """删除 ``expires_at <= now_text`` 的指纹。

        Args:
            connection: 已处于调用方事务中的 SQLite 连接。
            now_text: ``_datetime_text`` 生成的 UTC ISO 时间。
        Raises:
            sqlite3.Error: DELETE 失败。

        算法依赖：
            所有时间使用相同 UTC/微秒格式，因此 ISO 字符串可直接按时间比较。
        """

        connection.execute(
            "DELETE FROM dedup_cache WHERE expires_at <= ?",
            (now_text,),
        )

    @classmethod
    def _event_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        """把 SQLite 事件行转换为可直接消费的字典。

        Args:
            row: 来自 ``ingestion_events`` 且包含完整列的 ``sqlite3.Row``。
        Returns:
            accepted 已转布尔值、三个 JSON 列已解析并改名的新字典。
        Raises:
            KeyError: row 缺少预期列，表示查询/表结构违反内部契约。
        """

        event = dict(row)
        event["accepted"] = bool(event["accepted"])
        event["metadata"] = cls._json_load(event.pop("metadata_json"), {})
        event["quality_components"] = cls._json_load(
            event.pop("quality_components_json"), {}
        )
        event["reasons"] = cls._json_load(event.pop("reasons_json"), [])
        return event

    @staticmethod
    def _json_load(value: str, fallback: Any) -> Any:
        """解析 JSON 字符串；类型错误/非法 JSON 返回 ``fallback`` 而不抛出。"""

        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback

    @classmethod
    def _json_text(cls, value: Any, *, max_bytes: int | None = None) -> str:
        """稳定序列化 JSON，并在指定字节上限内生成合法截断对象。

        不能直接对 JSON 字符串做字节切片，否则可能切断 UTF-8 中文字符、引号或
        括号，最终得到无法解析的 JSON。超限时改存一个带原始大小和文本预览的
        包装对象，并用二分搜索找出能放入上限的最长预览。

        Args:
            value: 任意待审计值，允许 datetime、Enum 和自定义对象。
            max_bytes: 可选 UTF-8 字节上限；``None`` 不限长，应为非负整数。
        Returns:
            始终可被 ``json.loads`` 解析的紧凑 JSON 字符串。超限时返回
            ``_truncated/original_bytes/preview`` 包装对象；上限极小时可回退为 ``{}``。

        Business Rules:
            序列化失败不影响 ingestion，而是存储错误类型和 repr 预览。

        Algorithm:
            稳定序列化 → 比较 UTF-8 字节长度 → 超限时对预览字符数
            二分搜索，每次重新序列化以保证 JSON/UTF-8 边界完整。
        """

        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=cls._json_default,
            )
        except (TypeError, ValueError) as exc:
            # JSON 只允许有限类型作为字典键，调用方也可能意外传入循环容器。
            # 审计层把这类输入包装为可追踪文本，而不是让序列化错误影响 ingestion。
            text = json.dumps(
                {
                    "_serialization_error": type(exc).__name__,
                    "preview": repr(value),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if max_bytes is None or len(text.encode("utf-8")) <= max_bytes:
            return text

        original_bytes = len(text.encode("utf-8"))
        low, high = 0, len(text)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate = json.dumps(
                {
                    "_truncated": True,
                    "original_bytes": original_bytes,
                    "preview": text[:middle],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(candidate.encode("utf-8")) <= max_bytes:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best or "{}"

    @staticmethod
    def _json_default(value: Any) -> str:
        """将 JSON 不原生支持的值转为字符串。

        ``datetime`` 返回 ISO-8601，``Enum`` 返回其 value，其他对象返回
        ``str(value)``；无业务异常，但对象自定义 ``__str__`` 可能向上抛错。
        """

        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return str(value.value)
        return str(value)

    @staticmethod
    def _enum_value(value: Any) -> str:
        """枚举返回 ``value`` 的字符串，其他类型直接 ``str`` 转换。"""
        return str(value.value) if isinstance(value, Enum) else str(value)

    @staticmethod
    def _validate_fingerprint(dedup_fingerprint: str) -> None:
        """验证指纹为非空字符串，否则抛 ``ValueError``。

        本方法不强制 64 位 SHA-256 格式，以保持对旧数据/测试替身的兼容。
        """
        if not isinstance(dedup_fingerprint, str) or not dedup_fingerprint:
            raise ValueError("dedup_fingerprint 不能为空")

    @staticmethod
    def _datetime_text(value: datetime) -> str:
        """将带时区 ``datetime`` 转为 UTC、微秒精度 ISO 字符串。

        Args:
            value: 必填 datetime；应带时区，无时区值会按 Python 运行环境
                的本地时区解释，因此业务调用方不应传入。
        Returns:
            可按字典序比较时间先后的 UTC ISO 字符串。
        Raises:
            AttributeError: value 不是 datetime。
        """
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @classmethod
    def _utc_now_text(cls) -> str:
        """返回当前 UTC 时间的微秒精度 ISO 字符串，无入参。"""
        return cls._datetime_text(datetime.now(timezone.utc))
