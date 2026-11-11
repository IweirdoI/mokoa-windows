"""多源数据采集器。

本模块只负责把不同来源的输入包装为统一的 :class:`RawRecord`，不在这里进行
文本清洗、质量判断或记忆分类。保持采集阶段职责单一，可以让后续各阶段独立
测试，也能避免“采集时已经偷偷修改业务语义”的问题。

业务规则：
    * 仅接受 ``DataSource`` 中的来源，未知来源必须明确拒绝。
    * ``user_id`` 是生产环境必需的隔离键；缺失时的 ``default`` 仅用于兼容 Demo。
    * 采集器不修改调用方的 payload，不进行文本清洗、脱敏、质量评分或记忆分类。

算法思路：
    先校验来源与载荷类型，再按“显式传参优先”合并信封字段，
    最后移除信封键并生成带 UTC 时间的 ``RawRecord``。

依赖关系：
    输入由 API/Agent 提供；输出遵循 ``src.common.models.RawRecord``，
    下游仅依赖 ``DataCleaner``，不直接依赖 SQLite、security 或 Milvus。

Owner: 3fba60e1
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.common.models import DataSource, RawRecord


class CollectionError(ValueError):
    """采集输入不符合契约时抛出的可预期异常。

    Args:
        message: 面向日志或用户的错误说明，不应为空。
        code: 稳定的机器可读错误码；默认为 ``invalid_payload``。

    依赖关系：
        ``IngestionPipeline.process`` 会将 ``code`` 映射为 ``RejectCode``，
        因此这是正常业务拒绝，不应直接变成 HTTP 500。
    """

    def __init__(self, message: str, *, code: str = "invalid_payload") -> None:
        """初始化可预期采集异常，并保留机器可读 ``code``。"""
        super().__init__(message)
        self.code = code


class DataCollector:
    """将工具结果、用户行为、手动配置和对话统一封装为 ``RawRecord``。

    正式环境应由 API 或 Agent 显式传入 ``user_id``。为兼容仓库当前的
    ``IngestionAdapter.ingest(raw, source)`` 接口，若完全没有用户标识，则使用
    ``default``，并在 metadata 中写入 ``identity_defaulted=True``，方便联调时
    发现身份注入问题。
    """

    _ENVELOPE_KEYS = {"user_id", "scene_id", "metadata", "created_at"}

    def collect(
        self,
        payload: Mapping[str, Any],
        source: str | DataSource,
        *,
        user_id: str | None = None,
        scene_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RawRecord:
        """执行统一采集。

        Args:
            payload: 来源原始数据，必须是 ``Mapping``，不可为 ``None``；
                业务字段可以为空，由后续 Cleaner/Normalizer 决定是否拒绝。
            source: ``DataSource`` 实例或枚举字符串；取值限于
                ``tool_result``、``user_behavior``、``manual_config``、
                ``conversation``、``document``，不可为空。
            user_id: 可选用户标识，显式参数优先于 payload 同名字段；
                ``None`` 表示尝试从 payload 读取，两者都缺失时兼容为 ``default``；
                空字符串不允许。
            scene_id: 可选场景/会话标识，``None`` 允许；显式参数优先。
            metadata: 可选追踪信息，必须可转为字典；``None`` 表示不附加。

        Returns:
            新建的 ``RawRecord``。payload 中的信封字段已移除，
            ``created_at`` 已统一为带时区的 UTC 时间，原始输入不会被修改。

        Raises:
            CollectionError: payload 不是 ``Mapping``、source 不受支持，
                或解析后的 ``user_id`` 为空字符串。

        Business Rules:
            信封参数覆盖 payload 同名字段；metadata 则是 payload 先加载、
            显式 metadata 后覆盖。无用户标识时写入 ``identity_defaulted``。

        Algorithm:
            复制 payload → 解析来源枚举 → 合并信封 → 解析 UTC 时间
            → 剔除信封键 → 生成 UUID 和 ``RawRecord``。
        """

        if not isinstance(payload, Mapping):
            raise CollectionError("payload 必须是字典类型", code="invalid_payload")

        try:
            data_source = source if isinstance(source, DataSource) else DataSource(source)
        except ValueError as exc:
            supported = ", ".join(item.value for item in DataSource)
            raise CollectionError(
                f"不支持的数据来源 {source!r}；允许值：{supported}",
                code="invalid_source",
            ) from exc

        # 复制输入，禁止修改调用方持有的原始对象。
        raw_payload = dict(payload)
        resolved_user_id = self._resolve_text(user_id, raw_payload.get("user_id"))
        identity_defaulted = resolved_user_id is None
        if identity_defaulted:
            resolved_user_id = "default"

        if not resolved_user_id.strip():
            raise CollectionError("user_id 不能为空字符串", code="invalid_user_id")

        resolved_scene_id = self._resolve_text(scene_id, raw_payload.get("scene_id"))

        # payload.metadata 与参数 metadata 合并；显式参数优先，适合 API 注入 trace_id。
        payload_metadata = raw_payload.get("metadata")
        merged_metadata: dict[str, Any] = (
            dict(payload_metadata) if isinstance(payload_metadata, Mapping) else {}
        )
        if metadata:
            merged_metadata.update(dict(metadata))
        if identity_defaulted:
            merged_metadata["identity_defaulted"] = True

        created_at = self._parse_created_at(raw_payload.get("created_at"))

        # 信封字段不属于业务正文，移除后可以防止 Normalizer 把 user_id 等内容
        # 拼进最终记忆文本。
        business_payload = {
            key: value for key, value in raw_payload.items() if key not in self._ENVELOPE_KEYS
        }

        return RawRecord(
            id=str(uuid4()),
            source=data_source,
            user_id=resolved_user_id,
            scene_id=resolved_scene_id,
            payload=business_payload,
            created_at=created_at,
            metadata=merged_metadata,
        )

    def collect_tool_result(
        self,
        payload: Mapping[str, Any],
        user_id: str,
        scene_id: str | None = None,
    ) -> RawRecord:
        """兼容旧接口：采集工具调用结果。

        Args:
            payload: 工具结果映射，不可为 ``None``。
            user_id: 非空用户标识。
            scene_id: 可选场景标识。
        Returns:
            source 固定为 ``tool_result`` 的 ``RawRecord``。
        Raises:
            CollectionError: 参数不符合 ``collect`` 契约。
        """

        return self.collect(
            payload, DataSource.TOOL_RESULT, user_id=user_id, scene_id=scene_id
        )

    def collect_user_behavior(
        self,
        payload: Mapping[str, Any],
        user_id: str,
        scene_id: str | None = None,
    ) -> RawRecord:
        """兼容旧接口：采集用户行为事件。

        Args:
            payload: 行为事件映射，不可为 ``None``。
            user_id: 非空用户标识。
            scene_id: 可选场景标识。
        Returns:
            source 固定为 ``user_behavior`` 的 ``RawRecord``。
        Raises:
            CollectionError: 参数不符合 ``collect`` 契约。
        """

        return self.collect(
            payload, DataSource.USER_BEHAVIOR, user_id=user_id, scene_id=scene_id
        )

    def collect_manual_config(
        self,
        payload: Mapping[str, Any],
        user_id: str,
        scene_id: str | None = None,
    ) -> RawRecord:
        """兼容旧接口：采集用户明确配置。

        Args:
            payload: 手动配置映射，不可为 ``None``。
            user_id: 非空用户标识。
            scene_id: 可选场景标识。
        Returns:
            source 固定为 ``manual_config`` 的 ``RawRecord``。
        Raises:
            CollectionError: 参数不符合 ``collect`` 契约。
        """

        return self.collect(
            payload, DataSource.MANUAL_CONFIG, user_id=user_id, scene_id=scene_id
        )

    def collect_document(
        self,
        payload: Mapping[str, Any],
        user_id: str,
        scene_id: str | None = None,
    ) -> RawRecord:
        """采集用户上传的 PDF / Word / 图片。"""

        return self.collect(
            payload, DataSource.DOCUMENT, user_id=user_id, scene_id=scene_id
        )

    def collect_conversation(
        self,
        payload: Mapping[str, Any],
        user_id: str,
        scene_id: str | None = None,
    ) -> RawRecord:
        """采集对话内容。

        Args:
            payload: 对话映射，不可为 ``None``。
            user_id: 非空用户标识。
            scene_id: 可选会话/场景标识。
        Returns:
            source 固定为 ``conversation`` 的 ``RawRecord``。
        Raises:
            CollectionError: 参数不符合 ``collect`` 契约。
        """

        return self.collect(
            payload, DataSource.CONVERSATION, user_id=user_id, scene_id=scene_id
        )

    @staticmethod
    def _resolve_text(primary: Any, fallback: Any) -> str | None:
        """按优先级取得文本字段，并统一去除首尾空白。

        ``primary`` 非 ``None`` 时即使为空字符串也不回退；两者均为
        ``None`` 时返回 ``None``，其他值通过 ``str`` 转换。
        """

        value = primary if primary is not None else fallback
        if value is None:
            return None
        return str(value).strip()

    @staticmethod
    def _parse_created_at(value: Any) -> datetime:
        """解析来源时间并返回 UTC ``datetime``。

        入参可为 ``datetime``、ISO-8601 字符串或 ``None``。空值与
        无法解析的字符串按当前 UTC 时间处理，不向上抛出解析异常；
        无时区时间按 UTC 解释。
        """

        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.now(timezone.utc)
        else:
            parsed = datetime.now(timezone.utc)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
