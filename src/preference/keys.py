"""Preference key registry — aligned with preference-keys-enum.md v0.2."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class PreferenceCategory(str, Enum):
    HABIT = "habit"
    STYLE = "style"
    SECURITY = "security"
    TOOL_SELECTION = "tool_selection"
    WORKFLOW = "workflow"
    COMMUNICATION = "communication"


PREFERENCE_CATEGORIES = frozenset(c.value for c in PreferenceCategory)


class PreferenceScope(str, Enum):
    GLOBAL = "global"
    SCENE = "scene"


SCENE_IDS = frozenset(
    {
        "general",
        "os_operation",
        "office",
        "coding",
        "web_browsing",
        "data_analysis",
    }
)

# category.key -> allowed values (None = free string / int)
PREFERENCE_KEYS: dict[str, dict[str, Any]] = {
    # habit
    "habit.download_path": {"type": "string", "default": "C:/Users/{user}/Downloads", "scene_optional": True},
    "habit.default_editor": {
        "type": "enum",
        "values": ["notepad", "vscode", "vim", "wps", "sublime", "none"],
        "default": "notepad",
        "scene_optional": True,
    },
    "habit.file_naming_rule": {
        "type": "enum",
        "values": ["original", "date_prefix", "project_prefix", "lowercase_with_underscore", "auto_increment"],
        "default": "original",
        "scene_optional": True,
    },
    "habit.terminal_shell": {
        "type": "enum",
        "values": ["bash", "zsh", "powershell", "cmd", "auto"],
        "default": "bash",
        "scene_optional": True,
    },
    "habit.file_organize_rule": {
        "type": "enum",
        "values": ["by_type", "by_date", "by_size", "by_project", "none"],
        "default": "none",
        "scene_optional": True,
    },
    "habit.temp_file_cleanup": {
        "type": "enum",
        "values": ["never", "after_task", "daily", "weekly", "on_demand"],
        "default": "never",
        "scene_optional": False,
    },
    "habit.screenshot_format": {
        "type": "enum",
        "values": ["png", "jpg", "bmp"],
        "default": "png",
        "scene_optional": False,
    },
    "habit.archive_format": {
        "type": "enum",
        "values": ["zip", "tar.gz", "7z", "rar"],
        "default": "zip",
        "scene_optional": True,
    },
    # 开放域喜好：喜欢/爱/讨厌对应的事物，值为顿号分隔列表。
    "habit.likes": {"type": "string", "default": "", "scene_optional": True, "merge_list": True},
    "habit.dislikes": {"type": "string", "default": "", "scene_optional": True, "merge_list": True},
    "habit.occupation": {"type": "string", "default": "", "scene_optional": True},
    "habit.possessions": {"type": "string", "default": "", "scene_optional": True, "merge_list": True},
    # style
    "style.reply_language": {
        "type": "enum",
        "values": ["zh-CN", "en-US", "ja-JP", "auto"],
        "default": "zh-CN",
        "scene_optional": True,
    },
    "style.code_runnable": {"type": "bool", "default": True, "scene_optional": True},
    "style.output_detail_level": {
        "type": "enum",
        "values": ["brief", "normal", "detailed"],
        "default": "normal",
        "scene_optional": True,
    },
    "style.report_format": {
        "type": "enum",
        "values": ["markdown", "html", "docx", "pdf", "plain"],
        "default": "markdown",
        "scene_optional": True,
    },
    "style.code_style": {
        "type": "enum",
        "values": ["python", "shell", "both"],
        "default": "both",
        "scene_optional": True,
    },
    "style.table_style": {
        "type": "enum",
        "values": ["plain", "grid", "minimal", "html"],
        "default": "minimal",
        "scene_optional": False,
    },
    "style.response_tone": {
        "type": "enum",
        "values": ["professional", "friendly", "concise", "detailed", "academic"],
        "default": "professional",
        "scene_optional": True,
    },
    "style.emoji_usage": {
        "type": "enum",
        "values": ["never", "minimal", "normal"],
        "default": "never",
        "scene_optional": False,
    },
    "style.chart_theme": {
        "type": "enum",
        "values": ["default", "dark", "light", "corporate", "colorblind_friendly"],
        "default": "default",
        "scene_optional": True,
    },
    # security
    "security.confirm_before_rm": {"type": "bool", "default": True, "scene_optional": True},
    "security.confirm_before_overwrite": {"type": "bool", "default": True, "scene_optional": True},
    "security.confirm_before_highrisk": {"type": "bool", "default": True, "scene_optional": True},
    "security.confirm_before_network": {"type": "bool", "default": False, "scene_optional": True},
    "security.sensitive_info_filter": {
        "type": "enum",
        "values": ["none", "mask", "block", "warn"],
        "default": "mask",
        "scene_optional": True,
    },
    "security.max_risk_level": {
        "type": "enum",
        "values": ["safe", "low", "medium", "high", "any"],
        "default": "high",
        "scene_optional": True,
    },
    "security.sandbox_required": {"type": "bool", "default": False, "scene_optional": True},
    "security.auto_backup_before_operation": {"type": "bool", "default": False, "scene_optional": True},
    "security.clipboard_monitor": {"type": "bool", "default": False, "scene_optional": False},
    # tool_selection
    "tool_selection.data_processing_tool": {
        "type": "enum",
        "values": ["python", "excel", "sql", "auto"],
        "default": "python",
        "scene_optional": True,
    },
    "tool_selection.file_search_tool": {
        "type": "enum",
        "values": ["find", "everything", "grep", "auto"],
        "default": "find",
        "scene_optional": True,
    },
    "tool_selection.batch_operation_tool": {
        "type": "enum",
        "values": ["bash", "python_script", "powershell", "auto"],
        "default": "bash",
        "scene_optional": True,
    },
    "tool_selection.text_processing_tool": {
        "type": "enum",
        "values": ["sed", "awk", "python", "powershell"],
        "default": "python",
        "scene_optional": True,
    },
    "tool_selection.image_processing_tool": {
        "type": "enum",
        "values": ["pillow", "opencv", "imagemagick", "auto"],
        "default": "pillow",
        "scene_optional": True,
    },
    "tool_selection.web_scraping_tool": {
        "type": "enum",
        "values": ["requests+bs4", "selenium", "playwright", "curl"],
        "default": "requests+bs4",
        "scene_optional": True,
    },
    "tool_selection.git_operation_tool": {
        "type": "enum",
        "values": ["bash_git", "gui", "vscode_git"],
        "default": "bash_git",
        "scene_optional": True,
    },
    "tool_selection.package_manager": {
        "type": "enum",
        "values": ["pip", "conda", "npm", "yarn", "auto"],
        "default": "pip",
        "scene_optional": True,
    },
    # workflow
    "workflow.preview_before_execute": {"type": "bool", "default": False, "scene_optional": True},
    "workflow.backup_before_modify": {"type": "bool", "default": False, "scene_optional": True},
    "workflow.batch_dry_run": {"type": "bool", "default": False, "scene_optional": True},
    "workflow.auto_complete_steps": {"type": "bool", "default": False, "scene_optional": True},
    "workflow.step_by_step_confirm": {
        "type": "enum",
        "values": ["always", "first_only", "never"],
        "default": "first_only",
        "scene_optional": True,
    },
    "workflow.error_handling": {
        "type": "enum",
        "values": ["stop", "skip", "retry_once", "retry_auto", "ask"],
        "default": "stop",
        "scene_optional": True,
    },
    "workflow.parallel_execution": {"type": "bool", "default": False, "scene_optional": True},
    "workflow.progress_reporting": {
        "type": "enum",
        "values": ["silent", "summary", "step_by_step", "verbose"],
        "default": "summary",
        "scene_optional": True,
    },
    "workflow.undo_history_length": {"type": "int", "default": 10, "scene_optional": False},
    # communication
    "communication.confirmation_style": {
        "type": "enum",
        "values": ["always", "minimal", "never"],
        "default": "minimal",
        "scene_optional": True,
    },
    "communication.input_mode": {
        "type": "enum",
        "values": ["natural_language", "command", "hybrid"],
        "default": "hybrid",
        "scene_optional": True,
    },
    "communication.explanation_style": {
        "type": "enum",
        "values": ["none", "brief", "detailed"],
        "default": "brief",
        "scene_optional": True,
    },
    "communication.error_reporting": {
        "type": "enum",
        "values": ["simple", "technical", "solution_oriented"],
        "default": "solution_oriented",
        "scene_optional": True,
    },
    "communication.suggestion_activeness": {
        "type": "enum",
        "values": ["passive", "on_request", "proactive"],
        "default": "on_request",
        "scene_optional": True,
    },
    "communication.abbreviation_enabled": {"type": "bool", "default": False, "scene_optional": False},
    "communication.reference_style": {
        "type": "enum",
        "values": ["inline", "footnote", "endnote", "none"],
        "default": "inline",
        "scene_optional": False,
    },
    "communication.greeting_style": {
        "type": "enum",
        "values": ["formal", "casual", "none"],
        "default": "formal",
        "scene_optional": True,
    },
}

MAX_VERSION_CHAIN = 50
LIST_MERGE_KEYS = frozenset({"habit.likes", "habit.dislikes", "habit.possessions"})
_ITEM_SPLIT = re.compile(r"[,，、;/]+")


def split_item_list(value: str | None) -> list[str]:
    """把顿号/逗号分隔的喜好列表拆成去重后的条目。"""
    items: list[str] = []
    seen: set[str] = set()
    for part in _ITEM_SPLIT.split(str(value or "").strip()):
        token = part.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        items.append(token)
    return items


def merge_item_list(old: str | None, new: str | None) -> str:
    """追加新喜好，保留原有顺序。"""
    return "、".join(split_item_list(old) + split_item_list(new))


def split_preference_key(full_key: str) -> tuple[str, str]:
    if "." not in full_key:
        raise ValueError(f"Invalid preference_key: {full_key}")
    category, key = full_key.split(".", 1)
    return category, key


_KEY_PART = re.compile(r"[^0-9a-zA-Z_]")


def sanitize_key_part(part: str) -> str:
    """把任意字符串收敛成合法键片段（小写、仅字母数字下划线、折叠重复下划线）。"""
    cleaned = _KEY_PART.sub("_", str(part or "").strip().lower())
    return re.sub(r"_+", "_", cleaned).strip("_")


def normalize_preference_key(category: str, key: str, *, allow_dynamic: bool = True) -> str:
    """返回规范化 full_key（category.key）。

    已知键直接返回；未知键**不再抛异常**，而是降级为动态键，避免长尾偏好被静默丢弃：
      - category 合法时自动归入 custom_ 命名空间（如 habit.custom_coding_language）；
      - category 也不合法时兜底到 habit.custom_{topic}。
    这样「写代码默认用 Go」这类未登记偏好也能结构化落库，而不是丢失。
    """
    category = (category or "").strip().lower()
    key = (key or "").strip()
    full = f"{category}.{key}" if category else key
    if full in PREFERENCE_KEYS:
        return full
    if not allow_dynamic:
        raise ValueError(f"Unknown preference_key: {full}")
    if category in PREFERENCE_CATEGORIES:
        sk = sanitize_key_part(key)
        if sk:
            return full if sk.startswith("custom_") else f"{category}.custom_{sk}"
        return f"{category}.custom_unknown"
    sk = sanitize_key_part(key) or sanitize_key_part(category) or "unknown"
    return f"habit.custom_{sk}"


def validate_value(full_key: str, value: Any) -> str:
    """Normalize and validate value; always return string for metadata storage.

    动态键（自定义键）或未登记键按自由文本 string 处理（截断 512），不抛 KeyError。
    """
    meta = PREFERENCE_KEYS.get(full_key)
    if meta is None:
        text = "" if value is None else str(value).strip()
        if len(text) > 512:
            text = text[:512]
        return text
    vtype = meta["type"]
    if vtype == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return "true"
        if text in {"false", "0", "no", "n"}:
            return "false"
        raise ValueError(f"{full_key} expects bool, got {value!r}")
    if vtype == "enum":
        text = str(value).strip()
        allowed = meta["values"]
        if text not in allowed:
            # case-insensitive match
            lowered = {a.lower(): a for a in allowed}
            if text.lower() in lowered:
                return lowered[text.lower()]
            raise ValueError(f"{full_key} expects one of {allowed}, got {value!r}")
        return text
    if vtype == "int":
        num = int(value)
        if not 0 <= num <= 50 and full_key == "workflow.undo_history_length":
            raise ValueError(f"{full_key} out of range: {num}")
        return str(num)
    # string
    text = str(value).strip()
    if len(text) > 512:
        text = text[:512]
    return text


def index_key(preference_key: str, scope: str, scene_id: str | None) -> str:
    if scope == PreferenceScope.GLOBAL.value or not scene_id:
        return f"{preference_key}::global"
    return f"{preference_key}::scene:{scene_id}"
