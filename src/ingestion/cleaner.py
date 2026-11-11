"""确定性清洗、内容截断与时间窗口去重。

清洗只做不会改变核心语义的机械处理，例如移除 ANSI 控制码、统一空白和过滤
纯 DEBUG 标记。敏感信息识别仍由 ``src/security`` 负责，本模块不会用不完整的
正则替代安全模块。

        业务规则：
    * 只做不改变核心语义的机械清洗；没有有效业务正文时拒绝。
    * 遇到常见 HTML 标签时用解析器抽取正文，不把 ``<T>`` 这类非标签尖括号删掉。
    * 单字段最长默认 8000 字符，截断长度会写入 metadata 供审计。
    * 去重指纹同时隔离用户、场景、来源和事件身份。

算法思路：
    对白名单文本字段做 NFKC、ANSI/控制字符清理、空白归一和截断，
    再对稳定 canonical JSON 计算 SHA-256。内存去重用单调时钟 + RLock
    实现 TTL 原子登记。

依赖关系：
    输入/输出依赖 ``RawRecord``；正式 API 的跨进程去重由
    ``StagingStore`` 替代内存缓存；敏感信息仍依赖下游 ``src.security``。

Owner: 3fba60e1
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import threading
import time
import unicodedata
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

from src.common.models import RawRecord


class _HtmlTextExtractor(HTMLParser):
    """只拆白名单 HTML 标签，保留标签内文本和未知尖括号内容。"""

    def __init__(self, html_tags: frozenset[str], block_tags: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._html_tags = html_tags
        self._block_tags = block_tags
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._block_tags:
            self._chunks.append("\n")
        elif lowered not in self._html_tags:
            self._chunks.append(self.get_starttag_text() or f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._block_tags:
            self._chunks.append("\n")
        elif lowered not in self._html_tags:
            self._chunks.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._block_tags:
            self._chunks.append("\n")
        elif lowered not in self._html_tags:
            self._chunks.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        self._chunks.append(html_lib.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._chunks.append(html_lib.unescape(f"&#{name};"))

    def text(self) -> str:
        return "".join(self._chunks)


class DataCleaner:
    """清洗 ``RawRecord``，并维护进程内的短期去重窗口。

    去重缓存只保存哈希和单调时钟，不保存用户正文。它适合单进程 Demo 和测试；
    多进程生产部署时可将同样的 ``fingerprint`` 接到 Redis/SQLite，但无需改变
    上游输出 Schema。

    Args:
        max_text_length: 每个文本字段的最大字符数，范围 ``[1, +∞)``。
        dedup_ttl_seconds: 进程内去重窗口秒数，范围 ``[0, +∞)``；0 表示关闭。

    Raises:
        ValueError: 最大长度小于 1，或 TTL 小于 0。
    """

    _ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    _CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
    _INLINE_SPACES = re.compile(r"[ \t]+")
    _EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
    _ONLY_NOISE = re.compile(
        r"^\s*(?:[-=_*]{3,}|"
        r"\[?(?:\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]+)\]?\s*"
        r"(?:DEBUG|TRACE)\s*:?)\s*$",
        re.IGNORECASE,
    )
    _HTML_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>")
    _HTML_TAGS = frozenset(
        {
            "a",
            "b",
            "i",
            "u",
            "em",
            "strong",
            "span",
            "p",
            "div",
            "br",
            "hr",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "thead",
            "tbody",
            "tr",
            "td",
            "th",
            "caption",
            "font",
            "center",
            "pre",
            "code",
            "blockquote",
            "nav",
            "section",
            "article",
            "header",
            "footer",
            "main",
            "small",
            "sub",
            "sup",
            "mark",
            "del",
            "ins",
        }
    )
    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    )

    # 可能包含自然语言的字段都需要执行相同字符清洗。
    _TEXT_KEYS = (
        "content",
        "text",
        "message",
        "error",
        "stderr",
        "result",
        "output",
        "stdout",
        "action",
        "event",
        "event_type",
        "target",
        "object",
        "key",
        "config_key",
        "name",
        "value",
        "tool_name",
        "tool",
        "status",
        "role",
        "scope",
        # 来源系统的事件身份也执行基础字符清洗，但不作为业务正文。
        "action_id",
        "event_id",
        "record_id",
        "turn_id",
        "filename",
        "caption",
        "file_kind",
        "title",
    )
    # 这些字段能够证明记录至少含有业务正文。tool_name/status 等上下文字段本身
    # 不足以形成记忆，因此不列入此集合。
    _MEANINGFUL_KEYS = {
        "content",
        "text",
        "message",
        "error",
        "stderr",
        "result",
        "output",
        "stdout",
        "action",
        "event",
        "event_type",
        "target",
        "object",
        "key",
        "config_key",
        "value",
        "filename",
        "caption",
        "title",
    }
    # 指纹除正文外还纳入工具名、配置键、角色等语义上下文，避免不同工具返回相同
    # 错误文本或不同配置项使用相同值时被误判为重复。
    _FINGERPRINT_KEYS = (
        "content",
        "text",
        "message",
        "error",
        "stderr",
        "result",
        "output",
        "stdout",
        "action",
        "event",
        "event_type",
        "target",
        "object",
        "key",
        "config_key",
        "name",
        "value",
        "tool_name",
        "tool",
        "status",
        "role",
        "scope",
        # action/event/record ID 用来区分“不同事件内容相同”和“同一事件重复投递”。
        "action_id",
        "event_id",
        "record_id",
        "turn_id",
        "filename",
        "caption",
        "file_kind",
        "title",
    )

    def __init__(self, *, max_text_length: int = 8000, dedup_ttl_seconds: float = 300.0) -> None:
        """初始化清洗规则和线程安全的内存 TTL 缓存。

        Args:
            max_text_length: 正整数，截断后长度包含末尾省略号。
            dedup_ttl_seconds: 非负数，可为整数或浮点数。
        Raises:
            ValueError: 参数越界。
        """
        if max_text_length < 1:
            raise ValueError("max_text_length 必须大于 0")
        if dedup_ttl_seconds < 0:
            raise ValueError("dedup_ttl_seconds 不能小于 0")

        self.max_text_length = max_text_length
        self.dedup_ttl_seconds = dedup_ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.RLock()

    def clean(self, record: RawRecord) -> RawRecord | None:
        """返回清洗后的副本；没有任何有效正文时返回 ``None``。

        Pydantic 的 ``model_copy(deep=True)`` 保证不会就地修改采集结果，便于审计
        “清洗前/清洗后”差异。

        Args:
            record: ``DataCollector`` 生成的 ``RawRecord``，不可为 ``None``。

        Returns:
            有有效正文时返回深拷贝并清洗的 ``RawRecord``；其 metadata
            含相同的 ``dedup_fingerprint``/``fingerprint``，必要时含
            ``truncated_fields``。没有业务正文时返回 ``None``。

        Raises:
            AttributeError: 调用方绕过类型契约，传入非 ``RawRecord``。
            TypeError: payload 中含有与该模型契约不兼容的极端自定义值。

        Business Rules:
            数字和布尔配置值保留原类型并视为有效内容；仅 tool/status
            等上下文不足以构成记忆。

        Algorithm:
            深拷贝 → 遍历文本白名单 → 标准化与截断 → 判断有效正文
            → 记录截断信息 → 计算 SHA-256 指纹。本方法不登记 TTL。
        """

        cleaned = record.model_copy(deep=True)
        truncations: dict[str, int] = {}
        has_meaningful_content = False

        for key in self._TEXT_KEYS:
            value = cleaned.payload.get(key)
            if value is None:
                continue

            # 数字和布尔型配置值也具有业务含义，保留原类型并视为有效正文。
            if not isinstance(value, str):
                if key in self._MEANINGFUL_KEYS and isinstance(value, (int, float, bool)):
                    has_meaningful_content = True
                continue

            normalized = self.normalize_text(value)
            if len(normalized) > self.max_text_length:
                truncations[key] = len(normalized)
                # 最大长度包含末尾的省略号。旧实现先截到 max_text_length 再追加
                # “…”会实际多出一个字符，使 SQLite content 的 8000 字符边界失效。
                if self.max_text_length == 1:
                    normalized = "…"
                else:
                    normalized = normalized[: self.max_text_length - 1].rstrip() + "…"
            cleaned.payload[key] = normalized
            if normalized and key in self._MEANINGFUL_KEYS:
                has_meaningful_content = True

        if not has_meaningful_content:
            return None

        if truncations:
            cleaned.metadata["truncated_fields"] = truncations
        dedup_fingerprint = self.fingerprint(cleaned)
        # dedup_fingerprint 是正式语义；fingerprint 仅为兼容目前已经接入的 core/API。
        # 两者在迁移期保持完全相同，避免团队更新造成行为变化。
        cleaned.metadata["dedup_fingerprint"] = dedup_fingerprint
        cleaned.metadata["fingerprint"] = dedup_fingerprint
        return cleaned

    @classmethod
    def normalize_text(cls, text: str) -> str:
        """执行可重复、与语言无关的基础文本清洗。

        Args:
            text: 必填字符串；允许空字符串，不接受 ``None``。
        Returns:
            NFKC 归一、去 ANSI/控制字符、有边界的 HTML 去标签、换行统一、
            空白压缩、纯 DEBUG/TRACE 噪声行过滤后的文本。
        Raises:
            TypeError: ``text`` 不是字符串。

        Algorithm:
            NFKC → 删除 ANSI 和控制字符 → 统一换行 → 白名单 HTML 去标签
            → 逐行压缩空白/去噪 → 将连续三个以上空行压缩为两个换行。
        """

        # NFKC 统一全角/兼容字符，便于去重和后续检索。
        value = unicodedata.normalize("NFKC", text)
        value = cls._ANSI_ESCAPE.sub("", value)
        value = cls._CONTROL_CHARS.sub("", value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = cls._strip_html_markup(value)

        lines: list[str] = []
        for line in value.split("\n"):
            compact = cls._INLINE_SPACES.sub(" ", line).strip()
            if cls._ONLY_NOISE.fullmatch(compact):
                continue
            lines.append(compact)

        value = "\n".join(lines).strip()
        return cls._EXCESS_BLANK_LINES.sub("\n\n", value)

    @classmethod
    def _strip_html_markup(cls, text: str) -> str:
        """用 HTML 解析器去标签，保留正文；非 HTML 的 ``<T>`` / 比较式不动。"""

        tags = {match.group(1).lower() for match in cls._HTML_TAG.finditer(text)}
        if not tags.intersection(cls._HTML_TAGS):
            return html_lib.unescape(text)

        parser = _HtmlTextExtractor(cls._HTML_TAGS, cls._BLOCK_TAGS)
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            return html_lib.unescape(cls._HTML_TAG.sub("", text))
        return parser.text()

    def fingerprint(self, record: RawRecord) -> str:
        """生成用户/场景隔离且与字典键顺序无关的 SHA-256 指纹。

        Args:
            record: 通常为已清洗的 ``RawRecord``，不可为 ``None``。
        Returns:
            64 位小写十六进制 SHA-256 字符串，始终非空。
        Raises:
            AttributeError: 输入不具备 ``RawRecord`` 字段。

        Business Rules:
            只对白名单语义字段取指纹，metadata 不参与；source event ID
            参与计算，因此“同文本不同事件”不会被误合并。

        Algorithm:
            选取语义 payload → 与 user/scene/source 组合 → 生成键排序的
            紧凑 canonical JSON → UTF-8 编码 → SHA-256。
        """

        semantic_payload = {
            key: record.payload.get(key)
            for key in self._FINGERPRINT_KEYS
            if record.payload.get(key) not in (None, "")
        }
        canonical = json.dumps(
            {
                "user_id": record.user_id,
                "scene_id": record.scene_id,
                "source": record.source.value,
                "payload": semantic_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def register_if_new(self, fingerprint: str) -> bool:
        """原子地检查并登记指纹。

        Args:
            fingerprint: 非空指纹字符串；通常为 ``fingerprint`` 返回的 SHA-256。

        Returns:
            ``True`` 表示首次出现；``False`` 表示 TTL 窗口内已经接收过。

        业务规则：
            TTL 为 0 时始终返回 ``True`` 且不保存状态。本方法不校验
            指纹格式；正式 SQLite 路径由 ``StagingStore`` 负责更严格的校验。

        Algorithm:
            获取单调时钟 → 持 RLock 删除过期项 → 查找指纹 →
            不存在时写入当前时间。
        """

        if self.dedup_ttl_seconds == 0:
            return True

        now = time.monotonic()
        with self._lock:
            self._prune_expired(now)
            if fingerprint in self._seen:
                return False
            self._seen[fingerprint] = now
            return True

    def release_fingerprint(self, fingerprint: str) -> bool:
        """释放进程内 TTL 指纹，供下游写入失败后重试。

        Args:
            fingerprint: 去重指纹；空字符串视为未登记。
        Returns:
            删除到已登记指纹时为 ``True``。
        """

        if not fingerprint:
            return False
        with self._lock:
            return self._seen.pop(fingerprint, None) is not None

    def reset_dedup_cache(self) -> None:
        """清空进程内去重状态，无入参和返回值。

        业务规则：
            主要用于测试和显式开始新 Demo；不会清理 SQLite
            ``dedup_cache``，正式环境不应将其当作全局清理接口。
        """

        with self._lock:
            self._seen.clear()

    def deduplicate(self, records: Iterable[RawRecord]) -> list[RawRecord]:
        """兼容旧批量接口：在本次传入集合内去重。

        Args:
            records: ``RawRecord`` 可迭代对象，允许为空，但不应为 ``None``。
        Returns:
            按首次出现顺序保留的清洗后记录列表；空正文和后续重复项被忽略。
        Raises:
            TypeError: ``records`` 不可迭代，或元素不符合 ``RawRecord`` 契约。

        业务规则：
            只在本次调用内去重，不读写实例 TTL 缓存，也不读写 SQLite。
        """

        seen: set[str] = set()
        out: list[RawRecord] = []
        for record in records:
            cleaned = self.clean(record)
            if cleaned is None:
                continue
            fingerprint = str(cleaned.metadata["fingerprint"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(cleaned)
        return out

    def _prune_expired(self, now: float) -> None:
        """删除早于 ``now - TTL`` 的指纹，无返回值。

        ``now`` 必须使用与缓存一致的 ``time.monotonic`` 秒值；
        调用方必须已持有 ``_lock``。
        """

        expired_before = now - self.dedup_ttl_seconds
        expired = [key for key, seen_at in self._seen.items() if seen_at < expired_before]
        for key in expired:
            del self._seen[key]
