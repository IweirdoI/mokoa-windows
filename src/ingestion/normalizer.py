"""来源专属标准化器。

Cleaner 负责字符层清洗；Normalizer 负责把结构化字段转换成稳定、可读的记忆候选
文本，并且只把白名单 metadata 传给下游，避免整段原始日志重复写入元数据。

业务规则：
    * 只处理经 Cleaner 清洗的四类 ``RawRecord``；正文不可为空。
    * 显式 ``content`` 优先保持原文，仅对结构化输入拼接稳定正文。
    * ``record_key`` 是与正文改值无关的来源键，不是去重指纹或 memory ID。

算法思路：
    按 source 分派到专用处理器，构造正文和白名单 metadata，对置信度
    做有限数校验与截断，时间统一为 UTC，最后产生 ``NormalizedRecord``。

依赖关系：
    依赖 ``src.common.models`` 的 ``DataSource``/``RawRecord``/
    ``NormalizedRecord``；上游为 Cleaner，下游为 ``QualityEvaluator``。

Owner: 3fba60e1
"""

from __future__ import annotations

import math
import re
from datetime import timezone
from typing import Any

from src.common.models import DataSource, NormalizedRecord, RawRecord


class NormalizationError(ValueError):
    """来源数据无法形成统一正文时抛出的可预期异常。

    ``IngestionPipeline`` 会将其转换为 ``normalization_failed`` 业务拒绝；
    它不表示系统故障，通常表示来源缺少构造正文的必要字段。
    """


class DataNormalizer:
    """将四类 ``RawRecord`` 转换为团队统一的 ``NormalizedRecord``。

    该类无可变状态和外部 I/O，可在请求间复用；输入对象不会被修改。
    """

    _SOURCE_TYPE_MAP = {
        DataSource.TOOL_RESULT: "tool_result",
        DataSource.USER_BEHAVIOR: "behavior",
        DataSource.MANUAL_CONFIG: "manual_config",
        DataSource.CONVERSATION: "conversation",
        DataSource.DOCUMENT: "document",
    }

    def normalize(self, record: RawRecord) -> NormalizedRecord:
        """按来源构造正文、置信度和标准 metadata。

        Args:
            record: 已经 ``DataCleaner.clean`` 处理的 ``RawRecord``，
                不可为 ``None``；source 必须是支持的来源之一。

        Returns:
            新的 ``NormalizedRecord``，其正文非空、时间为 UTC、
            置信度为 ``[0, 1]``，metadata 含 ``schema_version=1.0``
            及对应来源的白名单字段。

        Raises:
            NormalizationError: source 没有对应处理器，或来源字段无法
                形成非空正文。
            AttributeError: 调用方绕过类型契约传入了非 ``RawRecord``。

        Business Rules:
            原 metadata 先复制，来源 metadata 后覆盖；稳定 ``record_key``
            仅在有可靠来源身份时生成。

        Algorithm:
            source 分派 → 构造正文/来源 metadata → 清除正文边缘空白
            → 归一置信度和时间 → 合并 metadata → 构造统一模型。
        """

        handlers = {
            DataSource.TOOL_RESULT: self._normalize_tool_result,
            DataSource.USER_BEHAVIOR: self._normalize_behavior,
            DataSource.MANUAL_CONFIG: self._normalize_manual_config,
            DataSource.CONVERSATION: self._normalize_conversation,
            DataSource.DOCUMENT: self._normalize_document,
        }
        handler = handlers.get(record.source)
        if handler is None:
            raise NormalizationError(f"没有来源 {record.source!r} 的标准化器")

        content, source_metadata = handler(record.payload)
        content = content.strip()
        if not content:
            raise NormalizationError("标准化后正文为空")

        confidence = self._to_confidence(record.payload.get("confidence"), default=0.7)
        event_time = record.created_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        else:
            event_time = event_time.astimezone(timezone.utc)

        metadata = dict(record.metadata)
        metadata.update(source_metadata)
        metadata["schema_version"] = "1.0"
        record_key = self._build_record_key(record, source_metadata)
        if record_key:
            # record_key 是来源层稳定键，只表达“同一配置项/行为项/来源事件”。
            # 它不是最终 memory_id，是否用于偏好版本合并由 Orchestrator 决定。
            metadata["record_key"] = record_key

        return NormalizedRecord(
            type=self._SOURCE_TYPE_MAP[record.source],
            content=content,
            time=event_time,
            confidence=confidence,
            source=record.source,
            user_id=record.user_id,
            scene_id=record.scene_id,
            metadata=metadata,
        )

    def _normalize_tool_result(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """将工具输出压缩为“工具 + 状态 + 核心结果”。

        入参为清洗后 payload；返回 ``(content, metadata)``。显式 ``content``
        原样保留，否则按 error/stderr/result/output/stdout/message 顺序取值。
        缺少所有正文候选时抛出 ``NormalizationError``。
        """

        explicit_content = self._first_text(payload, "content")
        tool_name = self._first_text(payload, "tool_name", "tool", "name") or "未知工具"
        status = self._first_text(payload, "status")
        if not status:
            status = "failed" if self._first_text(payload, "error", "stderr") else "completed"

        message = explicit_content or self._first_text(
            payload, "error", "stderr", "result", "output", "stdout", "message"
        )
        if not message:
            raise NormalizationError("工具结果缺少 content/error/result/output 等正文")

        # 已经由上游明确生成可读 content 时保持原文；结构化工具日志才自动补上下文。
        if explicit_content:
            content = explicit_content
        else:
            status_text = "失败" if status.lower() in {"failed", "error", "timeout"} else "完成"
            content = f"{tool_name} 执行{status_text}：{message}"

        metadata = {
            "tool_name": tool_name,
            "status": status,
        }
        self._copy_source_event_id(payload, metadata)
        duration = self._first_number(payload, "duration_ms", "latency_ms")
        if duration is not None:
            metadata["duration_ms"] = duration
        return content, metadata

    def _normalize_behavior(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """将行为事件转换成可读描述，并保留频次。

        入参为清洗后 payload；返回 ``(content, metadata)``。
        ``occurrence_count`` 最小为 1。缺少 content/message/action/event/event_type
        时抛出 ``NormalizationError``。
        """

        explicit_content = self._first_text(payload, "content", "message")
        action = self._first_text(payload, "action", "event", "event_type")
        target = self._first_text(payload, "target", "object")

        if explicit_content:
            content = explicit_content
        elif action and target:
            content = f"用户{action}：{target}"
        elif action:
            content = f"用户行为：{action}"
        else:
            raise NormalizationError("用户行为缺少 content 或 action/event")

        occurrence_count = self._to_positive_int(
            payload.get("occurrence_count", payload.get("frequency", 1))
        )
        metadata: dict[str, Any] = {
            "occurrence_count": occurrence_count,
            "explicit": bool(payload.get("explicit", False)),
        }
        self._copy_source_event_id(payload, metadata)
        if action:
            metadata["event_type"] = action
        if target:
            metadata["target"] = target
        return content, metadata

    def _normalize_manual_config(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """将用户明确配置转成记忆候选。

        入参为清洗后 payload；返回 ``(content, metadata)``。
        显式 content/message 优先，否则要求 key/config_key/name 与非 ``None``
        value 同时存在；不满足时抛出 ``NormalizationError``。
        配置值仅写入正文，不在 metadata 内再保存一份。
        """

        explicit_content = self._first_text(payload, "content", "message")
        key = self._first_text(payload, "key", "config_key", "name")
        value = payload.get("value")

        if explicit_content:
            content = explicit_content
        elif key and value is not None:
            content = f"用户设置“{key}”为“{value}”"
        else:
            raise NormalizationError("手动配置缺少 content，或缺少 key/value")

        metadata: dict[str, Any] = {"explicit": True}
        self._copy_source_event_id(payload, metadata)
        if key:
            metadata["config_key"] = key
        scope = self._first_text(payload, "scope")
        if scope:
            metadata["scope"] = scope
        return content, metadata

    def _normalize_conversation(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """保留对话原文，同时携带角色和轮次信息。

        入参为清洗后 payload；返回 ``(content, metadata)``。
        content/text/message 均缺失时抛出 ``NormalizationError``；role 可空且
        默认为 ``user``。``explicit`` 只是质量特征，不在此判断偏好类型。
        """

        content = self._first_text(payload, "content", "text", "message")
        if not content:
            raise NormalizationError("对话数据缺少 content/text/message")

        from src.core.persona_query import persona_facts_from_content, persona_ingest_text

        facts = persona_facts_from_content(content)
        content = persona_ingest_text(content)
        role = self._first_text(payload, "role") or "user"
        metadata: dict[str, Any] = {
            "role": role,
            "explicit": self._contains_explicit_intent(content),
        }
        if facts:
            metadata["persona_facts"] = facts
        self._copy_source_event_id(payload, metadata)
        turn_id = payload.get("turn_id")
        if turn_id is not None:
            metadata["turn_id"] = str(turn_id)
        return content, metadata

    def _normalize_document(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """将上传文档整理为“类型 + 文件名 + 正文/说明”。"""

        from src.ingestion.file_extract import KIND_LABELS

        filename = self._first_text(payload, "filename", "name") or "未命名文件"
        kind = self._first_text(payload, "file_kind", "kind") or "document"
        label = KIND_LABELS.get(kind, "文档")
        body = self._first_text(payload, "content", "text", "caption")
        if not body:
            raise NormalizationError("文档未能提取到正文，且未提供说明")

        content = f"用户上传{label}「{filename}」：{body}"
        metadata: dict[str, Any] = {
            "explicit": True,
            "filename": filename,
            "file_kind": kind,
        }
        self._copy_source_event_id(payload, metadata)
        for key in (
            "page_count",
            "char_count",
            "image_width",
            "image_height",
            "ocr_used",
            "vision_used",
            "vision_model",
            "embedded_text_used",
        ):
            if payload.get(key) is not None:
                metadata[key] = payload[key]
        caption = self._first_text(payload, "caption")
        if caption:
            metadata["caption"] = caption
        return content, metadata

    @classmethod
    def _copy_source_event_id(
        cls,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """把来源事件身份规范化为 ``source_event_id``。

        字段优先级体现业务明确程度：工具 action_id 优先，其次通用 event_id，
        最后是采集系统 record_id。保留原字段名便于排查来源日志。

        ``payload`` 和 ``metadata`` 不可为 ``None``；方法就地更新 metadata，
        无可用 ID 时不写入任何字段，无返回值且不抛出业务异常。
        """

        for field in ("action_id", "event_id", "record_id", "turn_id"):
            value = cls._first_text(payload, field)
            if value:
                metadata["source_event_id"] = value
                metadata[field] = value
                return

    @staticmethod
    def _build_record_key(
        record: RawRecord,
        source_metadata: dict[str, Any],
    ) -> str | None:
        """构造与正文内容无关的来源层稳定键。

        ``dedup_fingerprint`` 随正文变化，用于精确事件去重；``record_key`` 在同一
        配置项或行为目标改值时保持不变，可供 core 后续生成 memory_key。

        返回非空稳定键，或在没有可靠身份字段时返回 ``None``。
        优先级为配置业务键、行为业务键、来源事件 ID。
        """

        scene = record.scene_id or "global"
        # 配置项和行为项具有天然业务键，应优先于每次投递都可能变化的 event_id。
        if record.source == DataSource.MANUAL_CONFIG:
            config_key = source_metadata.get("config_key")
            if config_key:
                return f"manual_config:{record.user_id}:{scene}:{config_key}"

        if record.source == DataSource.DOCUMENT:
            filename = source_metadata.get("filename")
            if filename:
                return f"document:{record.user_id}:{scene}:{filename}"

        if record.source == DataSource.USER_BEHAVIOR:
            event_type = source_metadata.get("event_type")
            target = source_metadata.get("target")
            if event_type and target:
                return f"behavior:{record.user_id}:{scene}:{event_type}:{target}"

        event_id = source_metadata.get("source_event_id")
        if event_id:
            return f"{record.source.value}:{record.user_id}:{event_id}"

        return None

    @staticmethod
    def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
        """按 ``keys`` 顺序返回第一个去空白后的非空文本。

        非字符串值通过 ``str`` 转换；所有键缺失或为空时返回 ``None``。
        """

        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
        """按 ``keys`` 顺序读取第一个有限非负数。

        可转浮点数的字符串允许；负数截断为 0，NaN/无穷大/非法值
        被跳过；没有可用值时返回 ``None``，不抛出转换异常。
        """

        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return max(0.0, number)
        return None

    @staticmethod
    def _to_confidence(value: Any, *, default: float) -> float:
        """把外部置信度限制在 ``[0, 1]``。

        ``value`` 可为数字或数字字符串；空值、非法值、NaN/无穷大使用
        ``default``，最终结果会被截断到闭区间。
        """

        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = default
        if not math.isfinite(confidence):
            confidence = default
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _to_positive_int(value: Any) -> int:
        """将值转为最小为 1 的整数；空值/非法值按 1 处理。"""

        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    _INTENT_PROBE = re.compile(
        r"(?:do|does|did)\s+(?:you|we|they|he|she)\s+(?:like|love|hate|prefer)\b"
        r"|(?:what|which|who)(?:\s+\w+){0,6}\s+do\s+(?:you|we|they)\s+(?:like|love|prefer|hate)"
        r"|喜欢(?:什么|哪|吗)|讨厌(?:什么|哪|吗)|偏好(?:什么|哪|吗)",
        re.I,
    )
    _INTENT_HINT = re.compile(
        r"以后|不要|必须|默认|喜欢|偏好|请用|请不要|请记住|"
        r"\b(?:prefer|always|never|likes?|loves?|hates?|dislikes?)\b",
        re.I,
    )

    @classmethod
    def _contains_explicit_intent(cls, content: str) -> bool:
        """判断正文是否包含常见明确意图提示词。

        Args:
            content: 非 ``None`` 对话正文；允许空字符串。
        Returns:
            命中任一中英文提示词时为 ``True``。

        业务规则：
            结果仅用于质量特征，不执行偏好/知识分类，也不保证语义准确率。
            ``like/love/hate`` 按词边界匹配；问句探测（do you like）不计 explicit。
        """

        stripped = cls._INTENT_PROBE.sub(" ", content or "")
        return bool(cls._INTENT_HINT.search(stripped))
