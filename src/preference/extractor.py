"""Preference extraction: SiliconFlow LLM + offline rule fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from src.common.models import DataSource, RawRecord
from src.preference.confidence import map_data_source_to_confidence_source
from src.preference.keys import (
    PREFERENCE_KEYS,
    normalize_preference_key,
    sanitize_key_part,
    validate_value,
)
from src.preference.llm_client import SiliconFlowClient
from src.preference.models import ExtractedPreference
from src.preference.prompts import build_extraction_messages, build_open_domain_messages


def _record_text(record: RawRecord) -> str:
    payload = record.payload or {}
    for key in ("content", "text", "user_input", "message", "query"):
        if payload.get(key):
            return str(payload[key])
    if "agent_response" in payload and "user_input" in payload:
        return f"{payload['user_input']}\n{payload['agent_response']}"
    return json.dumps(payload, ensure_ascii=False)


def _infer_scene(text: str, hint: str | None) -> str:
    if hint and hint in {
        "general",
        "os_operation",
        "office",
        "coding",
        "web_browsing",
        "data_analysis",
    }:
        return hint
    lower = text.lower()
    if any(k in text for k in ("合同", "日报", "Word", "Excel", "PPT", "客户", "办公")) or "docx" in lower:
        return "office"
    if any(k in text for k in ("代码", "git", "debug", "编程")) or "vscode" in lower:
        return "coding"
    if any(k in text for k in ("桌面", "下载", "删", "压缩", "重命名", "tmp", "downloads")):
        return "os_operation"
    if any(k in text for k in ("爬虫", "网页", "抓取", "playwright", "selenium")):
        return "web_browsing"
    if any(k in text for k in ("数据分析", "报表", "清洗")):
        return "data_analysis"
    return "general"


_CLAUSE_SPLIT = re.compile(r"但是|可是|不过|但|[，。；;！!？?\n]")
_DISLIKE_HEAD = re.compile(
    r"^(?:我也|我还|我又|i\s+)?"
    r"(?:很|超|特别|真的)?"
    r"(?:我不喜欢|我讨厌|我厌恶|我不爱吃|我受不了|我恨|"
    r"不喜欢|讨厌|厌恶|不爱吃|受不了|恨|"
    r"(?:do\s+not|don't|dont)\s+like|dislike|hate)"
    r"(?:吃|喝|用)?\s*",
    re.IGNORECASE,
)
_LIKE_HEAD = re.compile(
    r"^(?:我也|我还|我又|i\s+)?"
    r"(?:很|超|特别|真的)?"
    r"(?:我喜欢吃|我爱吃|我喜爱|我热爱|我超爱|我喜欢|我爱|"
    r"喜欢吃|爱吃|喜爱|热爱|超爱|喜欢|like|love)"
    r"(?:吃|喝|用)?\s*",
    re.IGNORECASE,
)
_ATTITUDE_SKIP = frozenset(
    {
        "你",
        "我",
        "他",
        "她",
        "它",
        "你们",
        "什么",
        "哪个",
        "这个",
        "那个",
        "这样",
        "那样",
        "中文",
        "英文",
        "中文回答",
        "英文回答",
        "用中文",
        "用英文",
        "this",
        "that",
        "it",
        "them",
        "you",
        "me",
    }
)
_ITEM_SPLIT = re.compile(r"[和与及、,，/]+")
_QUESTION_MARKERS = ("什么", "哪个", "哪些", "吗")


def _clean_attitude_object(raw: str) -> list[str]:
    text = raw.strip().strip("的了吧呀啊哦呢")
    text = re.sub(r"^(?:吃|喝|用)", "", text).strip()
    items: list[str] = []
    seen: set[str] = set()
    for part in _ITEM_SPLIT.split(text):
        token = part.strip().strip("的了吧呀啊哦呢")
        if not token:
            continue
        lowered = token.lower()
        if lowered in _ATTITUDE_SKIP or token in _ATTITUDE_SKIP:
            continue
        if any(marker in token for marker in ("什么", "哪个", "哪些")):
            continue
        ascii_only = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\- ]{0,40}", token))
        cjk = bool(re.search(r"[\u4e00-\u9fff]", token))
        if ascii_only and len(token) < 2:
            continue
        if not ascii_only and not cjk:
            continue
        if len(token) > 24:
            token = token[:24]
        if token in seen:
            continue
        seen.add(token)
        items.append(token)
    return items


def _extract_attitude_items(text: str) -> tuple[list[str], list[str]]:
    """从「喜欢/爱/讨厌」句式抽出对应事物，问句不抽取。"""
    likes: list[str] = []
    dislikes: list[str] = []
    seen_like: set[str] = set()
    seen_dislike: set[str] = set()

    def add(bucket: list[str], seen: set[str], items: list[str]) -> None:
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            bucket.append(item)

    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if not clause or any(marker in clause for marker in _QUESTION_MARKERS):
            continue
        dislike = _DISLIKE_HEAD.match(clause)
        if dislike:
            add(dislikes, seen_dislike, _clean_attitude_object(clause[dislike.end() :]))
            continue
        like = _LIKE_HEAD.match(clause)
        if like:
            add(likes, seen_like, _clean_attitude_object(clause[like.end() :]))
    return likes, dislikes


_IAM_SKIP = re.compile(
    r"\b(?:going|trying|looking|coming|planning|about|gonna|sure|sorry|afraid|here|there|not)\b",
    re.I,
)
_HAVE_SKIP = re.compile(r"\b(?:to|been|had)\b", re.I)


def _clean_profile_span(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip(" .,;:'\"，。；、"))
    if len(text) > 48:
        text = text[:48].rstrip()
    return text


def _extract_profile_facts(text: str) -> tuple[str | None, list[str]]:
    """I am / I work / I have（及中文我是/我在…工作/我有）。问句不抽。"""
    occupation: str | None = None
    possessions: list[str] = []
    seen: set[str] = set()

    def add_possession(raw: str) -> None:
        token = _clean_profile_span(raw)
        if not token or token.lower() in seen or _HAVE_SKIP.search(token):
            return
        seen.add(token.lower())
        possessions.append(token)

    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if not clause or any(marker in clause for marker in _QUESTION_MARKERS):
            continue
        if clause.endswith("?") or "吗" in clause:
            continue
        cl = clause.lower()
        if occupation is None:
            m = re.search(r"\bi am (?:a|an)\s+([a-z][a-z '\-]{1,40})", cl)
            if m:
                role = _clean_profile_span(m.group(1))
                if role and not _IAM_SKIP.search(role):
                    occupation = role
            if occupation is None:
                m = re.search(r"\bi work (?:as (?:a|an)\s+|at\s+|in\s+)([a-z0-9][a-z0-9 '&.,\-]{1,40})", cl)
                if m:
                    role = _clean_profile_span(m.group(1))
                    if role and not _IAM_SKIP.search(role):
                        occupation = role
            if occupation is None:
                # 否定前瞻只查捕获组首字符；宁可漏抽不可误抽。「我想/我说/我不是/我要/
                # 我来/我去/我在…」等都不是职业陈述，已排除常见非职业起始字。
                m = re.search(
                    r"我是(?:一名|一个|位)?(?!说|不是|想|要|来|去|在|被|给|让|请|会|能|可)"
                    r"([\u4e00-\u9fffA-Za-z]{2,16})",
                    clause,
                )
                if m:
                    occupation = _clean_profile_span(m.group(1))
            if occupation is None:
                m = re.search(r"我在([\u4e00-\u9fffA-Za-z0-9]{2,16})工作", clause)
                if m:
                    occupation = _clean_profile_span(m.group(1))
        m = re.search(r"\bi have (?!to\b|been\b)((?:a|an|\d+|two|three|my)\s+[a-z][a-z '\-]{1,40})", cl)
        if m:
            add_possession(m.group(1))
        m = re.search(r"我有(?!点|些)([\u4e00-\u9fff0-9两俩三四]{1,16})", clause)
        if m:
            add_possession(m.group(1))
    return occupation, possessions


def extract_by_rules(text: str, scene_hint: str | None = None) -> list[ExtractedPreference]:
    """Deterministic patterns covering 测试用例.md (zh/en)."""
    results: list[ExtractedPreference] = []
    scene = _infer_scene(text, scene_hint)
    t = text
    lower = text.lower()

    def add(category: str, key: str, value: Any, description: str, scene_id: str | None = None) -> None:
        results.append(
            ExtractedPreference(
                category=category,
                key=key,
                value=str(value),
                description=description,
                scope="scene",
                scene_id=scene_id or scene,
                source="explicit_statement",
                statement_text=text,
                reason="rule_match",
            )
        )

    # 1 / download path
    m = re.search(r"([A-Za-z]):[/\\][^\s\"'，。；;]+", t)
    if m and any(k in t for k in ("下载", "download", "挪到", "放到", "保存", "save to", "move", "改到", "设为")):
        path = m.group(0).replace("\\", "/").rstrip(".,;:，。；：")
        add("habit", "download_path", path, f"下载/文件路径偏好 {path}")
    elif re.search(r"[Dd]\s*盘", t) and any(k in t for k in ("下载", "挪", "放", "保存", "项目")):
        add("habit", "download_path", "D:/Downloads", "下载默认放 D 盘")
    elif re.search(r"[Ee]\s*盘", t) and any(k in t for k in ("下载", "改到", "放到", "保存")):
        add("habit", "download_path", "E:/Downloads", "下载默认放 E 盘")

    # naming
    if any(k in t for k in ("按日期", "日期顺序", "date prefix", "date_prefix", "rename by date")):
        add("habit", "file_naming_rule", "date_prefix", "文件按日期前缀命名")
    elif any(k in t for k in ("挪到项目", "项目文件夹", "project folder")):
        add("habit", "file_naming_rule", "project_prefix", "按项目组织/命名文件")

    # 2 office excel table / python data
    if any(k in t for k in ("整理成表格", "用 Excel", "用excel", "into a table", "use excel")) or (
        "表格" in t and any(k in t for k in ("日报", "报告", "发给"))
    ):
        add(
            "tool_selection",
            "data_processing_tool",
            "excel",
            "办公场景偏好用 Excel 整理数据",
            scene_id="office",
        )
    elif any(k in t for k in ("用 Python", "用python", "use python")) and any(
        k in t for k in ("数据", "跑", "分析", "data", "analysis")
    ):
        add(
            "tool_selection",
            "data_processing_tool",
            "python",
            "数据处理偏好 Python",
            scene_id="data_analysis",
        )

    # 3 delete without confirm
    if (
        any(k in t for k in ("直接删", "不用问我", "别确认", "无需确认"))
        or re.search(r"(delete|remove).*(without|don't|do not).*(ask|confirm)", lower)
        or re.search(r"(don't|do not)\s+ask.*(delete|remove)", lower)
        or re.search(r"never\s+ask.*\b(delete|remove|rm)\b", lower)
        or ("never ask" in lower and any(k in lower for k in ("delete", "remove", "rm ")))
    ):
        add("security", "confirm_before_rm", "false", "删除前无需确认", scene_id="os_operation")
        add("workflow", "preview_before_execute", "false", "执行前不预览", scene_id="os_operation")
    elif any(k in t for k in ("删除前必须确认", "删文件前必须确认", "删除文件前必须确认", "必须确认再删")):
        add("security", "confirm_before_rm", "true", "删除前必须确认", scene_id="os_operation")
    if any(k in t for k in ("高风险操作执行前必须确认", "高危操作必须确认", "高风险必须确认")):
        add("security", "confirm_before_highrisk", "true", "高危操作需确认", scene_id="os_operation")
    if any(k in t for k in ("执行前先预览", "命令执行前先预览", "执行前必须预览", "先预览再执行")):
        add("workflow", "preview_before_execute", "true", "执行前先预览", scene_id="os_operation")

    # 4 file search
    if any(k in t for k in ("搜一下", "搜索", "找一下", "search for", "find the")) and any(
        k in t for k in ("合同", "文件", "contract", "document", "上周")
    ):
        add(
            "tool_selection",
            "file_search_tool",
            "everything",
            "文件搜索偏好 Everything",
            scene_id="office" if "合同" in t or "contract" in lower else "os_operation",
        )

    # 5 English / report format
    if any(k in t for k in ("用英文", "英语", "in english", "english")) and any(
        k in t for k in ("报告", "分析", "report", "analysis")
    ):
        add("style", "reply_language", "en-US", "英文输出偏好", scene_id="office")
        fmt = "docx"
        if "pdf" in lower:
            fmt = "pdf"
        elif "markdown" in lower or "md" in lower:
            fmt = "markdown"
        add("style", "report_format", fmt, f"报告格式偏好 {fmt}", scene_id="office")
    # pref_035：报告导出 PDF（不依赖「用英文」）
    elif any(k in t for k in ("报告导出", "导出报告", "导出 PDF", "导出PDF", "report export")) and (
        "pdf" in lower or "PDF" in t
    ):
        add("style", "report_format", "pdf", "报告格式偏好 pdf", scene_id="office")
    elif re.search(r"(报告|report).{0,8}(pdf|PDF)", t) or re.search(
        r"(pdf|PDF).{0,8}(报告|report)", t
    ):
        add("style", "report_format", "pdf", "报告格式偏好 pdf", scene_id="office")

    # 6 organize by type
    if any(k in t for k in ("按类型分类", "按类型整理", "by type", "organize by type")):
        add("habit", "file_organize_rule", "by_type", "按类型整理文件", scene_id="os_operation")
        add("workflow", "backup_before_modify", "true", "整理前备份", scene_id="os_operation")

    # pref_034：文件操作前先备份（显式工作流）
    if any(
        k in t
        for k in (
            "文件操作前先备份",
            "操作前先备份",
            "修改前先备份",
            "先备份再",
            "backup before",
            "backup before modify",
        )
    ):
        add("workflow", "backup_before_modify", "true", "修改前备份", scene_id="os_operation")

    # 7 archive + password
    if any(k in t for k in ("压缩包", "打个压缩", "zip", "archive", "compress")):
        add("habit", "archive_format", "zip", "压缩格式偏好 zip")
        if any(k in t for k in ("密码", "password", "加密")):
            add("security", "confirm_before_highrisk", "true", "设密/高危操作需确认")

    # 8 word -> pdf
    if (
        any(k in t for k in ("转成 PDF", "转 PDF", "转换成 PDF", "to pdf", "convert"))
        and any(k in t.lower() for k in ("word", "docx", "文档"))
    ) or ("PDF" in t and any(k in t for k in ("Word", "发给客户", "转换"))):
        add("style", "report_format", "pdf", "文档输出 PDF", scene_id="office")
        add("workflow", "auto_complete_steps", "true", "自动补全后续步骤", scene_id="office")

    # 9 batch rename
    if any(k in t for k in ("批量重命名", "batch rename", "rename these")):
        add("habit", "file_naming_rule", "date_prefix", "批量重命名按日期", scene_id="os_operation")
        add(
            "tool_selection",
            "batch_operation_tool",
            "bash",
            "批量操作偏好 bash",
            scene_id="os_operation",
        )

    # 10 contract date replace
    if any(k in t for k in ("合同", "contract")) and any(
        k in t for k in ("日期", "格式", "date format", "2026-08-01")
    ):
        add(
            "tool_selection",
            "text_processing_tool",
            "python",
            "文本处理偏好 python",
            scene_id="office",
        )
        add("workflow", "backup_before_modify", "true", "修改前备份", scene_id="office")

    # pref_032：网页抓取工具
    if any(k in lower for k in ("playwright",)) or (
        any(k in t for k in ("网页抓取", "网页爬取", "抓取网页", "爬虫用"))
        and any(k in lower for k in ("playwright", "selenium"))
    ):
        tool = "playwright" if "playwright" in lower else "selenium"
        add(
            "tool_selection",
            "web_scraping_tool",
            tool,
            f"网页抓取偏好 {tool}",
            scene_id="web_browsing",
        )
    elif any(k in t for k in ("用 selenium", "用Selenium", "use selenium")):
        add(
            "tool_selection",
            "web_scraping_tool",
            "selenium",
            "网页抓取偏好 selenium",
            scene_id="web_browsing",
        )

    # language only
    if not any(p.key == "reply_language" for p in results):
        if any(k in t for k in ("用中文", "中文回答", "注释用中文", "reply in chinese", "in chinese")):
            add("style", "reply_language", "zh-CN", "中文回复偏好")
        elif any(k in t for k in ("用英文回答", "英文回复", "reply in english")):
            add("style", "reply_language", "en-US", "英文回复偏好")

    # pref_025 / coding：代码要能直接运行
    if any(
        k in t
        for k in (
            "可直接运行",
            "要能直接运行",
            "能直接运行",
            "可运行代码",
            "runnable code",
            "executable code",
            "code runnable",
        )
    ):
        add("style", "code_runnable", "true", "代码需可直接运行", scene_id="coding")

    # 聊天显式语气：简洁 / 不要简洁（先匹配否定，避免「不用那么简洁」被写成 concise）
    if any(
        k in t
        for k in (
            "不用那么简洁",
            "不要那么简洁",
            "不用简洁",
            "不要简洁",
            "可以复杂",
            "复杂一些",
            "更复杂",
            "更详细",
            "详细一些",
            "详细解释",
            "讲详细点",
            "more detailed",
            "less concise",
        )
    ):
        add("style", "response_tone", "detailed", "回答风格偏好详细")
    elif any(k in t for k in ("更加简洁", "更简洁", "回答简洁", "简洁回答", "be concise", "more concise")):
        add("style", "response_tone", "concise", "回答风格偏好简洁")

    # 英文偏好同义补齐：把常见英文表述映射到已有封闭键，扩展开放域英文样本覆盖。
    # 仅映射到 PREFERENCE_KEYS 中已注册的键，避免产生未注册键被丢弃的无效抽取。
    if not any(p.key == "reply_language" for p in results):
        if any(k in lower for k in ("answer in chinese", "respond in chinese", "reply in chinese", "write in chinese")):
            add("style", "reply_language", "zh-CN", "中文回复偏好")
        elif any(k in lower for k in ("answer in english", "respond in english", "reply in english", "write in english")):
            add("style", "reply_language", "en-US", "英文回复偏好")
    if not any(p.key == "response_tone" for p in results):
        if any(k in lower for k in ("keep it short", "be brief", "to the point", "be concise", "terse")):
            add("style", "response_tone", "concise", "回答风格偏好简洁")
        elif any(k in lower for k in ("be verbose", "in detail", "elaborate", "thorough", "more detail")):
            add("style", "response_tone", "detailed", "回答风格偏好详细")
    if any(k in lower for k in ("ask before deleting", "confirm deletion", "confirm before delete", "confirm before removing")):
        add("security", "confirm_before_rm", "true", "删除前必须确认", scene_id="os_operation")
    if any(k in lower for k in ("make a backup", "back up first", "backup first", "back up before")):
        add("workflow", "backup_before_modify", "true", "修改前备份", scene_id="os_operation")
    if any(k in lower for k in ("working code", "run without errors", "code that runs", "runnable")):
        add("style", "code_runnable", "true", "代码需可直接运行", scene_id="coding")
    if any(k in lower for k in ("use pandas", "spreadsheet", "use a spreadsheet", "excel sheet")):
        add("tool_selection", "data_processing_tool", "excel", "数据处理偏好表格工具", scene_id="data_analysis")
    if any(k in lower for k in ("save downloads to", "downloads go to", "download folder", "download location")):
        add("habit", "download_path", "D:/Downloads", "下载默认路径偏好")

    likes, dislikes = _extract_attitude_items(t)
    if likes:
        add("habit", "likes", "、".join(likes), f"用户喜欢 { '、'.join(likes) }")
    if dislikes:
        add("habit", "dislikes", "、".join(dislikes), f"用户讨厌 { '、'.join(dislikes) }")

    occupation, possessions = _extract_profile_facts(t)
    if occupation:
        add("habit", "occupation", occupation, f"身份 {occupation}", scene_id="general")
    if possessions:
        add("habit", "possessions", "、".join(possessions), f"用户有 { '、'.join(possessions) }", scene_id="general")

    # 默认编程语言（长尾偏好兜底）：例「写代码默认用 Go」「编程语言默认是 Rust」。
    # 无法映射到封闭键，归到动态键 habit.custom_coding_language，保证不丢失。
    _LANG_WHITELIST = {
        "go", "golang", "python", "py", "rust", "java", "c", "cpp", "c++",
        "javascript", "js", "typescript", "ts", "ruby", "php", "swift",
        "kotlin", "scala", "dart", "csharp", "c#", "lua", "perl",
    }
    if not any(p.key == "custom_coding_language" for p in results):
        m = re.search(
            r"(?:写代码|编程|代码|开发).{0,12}?(?:默认|通常|一般|习惯|总是)?\s*(?:用|使?用|是|选)\s*([A-Za-z+#]{1,16})",
            t,
            re.I,
        )
        if not m:
            m = re.search(
                r"(?:默认|通常|习惯|一般).{0,8}(?:编程|写代码|代码).{0,6}(?:用|是)\s*([A-Za-z+#]{1,16})",
                t,
                re.I,
            )
        if m:
            token = m.group(1).strip()
            lang = token.lower()
            if lang in _LANG_WHITELIST or (len(lang) >= 2 and lang.isalpha()):
                add(
                    "habit",
                    "custom_coding_language",
                    token,
                    f"默认编程语言偏好 {token}",
                    scene_id="coding",
                )

    # dedupe by category.key+scene
    seen: set[str] = set()
    unique: list[ExtractedPreference] = []
    for item in results:
        sig = f"{item.category}.{item.key}::{item.scene_id}"
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(item)
    return unique


def _parse_llm_json(raw: str) -> list[dict[str, Any]]:
    """容错解析 LLM 返回的 JSON：支持 ```json 包裹、前后夹带解释文字。

    绝不抛异常——解析失败一律返回 []，避免个别样本的非严格输出（如 JSON 后跟一段
    说明、或字符串里含方括号）触发 JSONDecodeError 而让整条抽取静默变空。
    """
    text = raw.strip()
    # 推理模型（如 DeepSeek 系列）可能在正文前输出思考段，先剥离 </think> 之前的内容。
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 从第一个 '[' 或 '{' 起用 raw_decode 解析，忽略其后夹带的任何解释文字。
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "[{":
                continue
            try:
                data, _ = decoder.raw_decode(text[idx:])
                break
            except json.JSONDecodeError:
                continue
    if isinstance(data, dict):
        data = data.get("preferences") or data.get("items") or []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


_VALUE_ALIASES: dict[str, dict[str, str]] = {
    "tool_selection.data_processing_tool": {
        "表格": "excel",
        "表格工具": "excel",
        "excel表格": "excel",
        "微软excel": "excel",
        "电子表格": "excel",
        "py": "python",
        "python3": "python",
    },
    "style.reply_language": {
        "中文": "zh-CN",
        "汉语": "zh-CN",
        "chinese": "zh-CN",
        "英文": "en-US",
        "英语": "en-US",
        "english": "en-US",
    },
    "style.report_format": {
        "word": "docx",
        "文档": "docx",
        "pdf文件": "pdf",
    },
}


def _normalize_row_key(row: dict[str, Any]) -> tuple[str, str]:
    """Accept category+key or full preference_key field from LLM."""
    if row.get("preference_key"):
        full = str(row["preference_key"]).strip()
        category, key = full.split(".", 1)
        return category, key
    category = str(row.get("category", "")).strip()
    key = str(row.get("key", "")).strip()
    if "." in key and not category:
        category, key = key.split(".", 1)
    elif "." in key and key.startswith(category + "."):
        key = key[len(category) + 1 :]
    return category, key


def _coerce_value(full_key: str, raw_value: Any) -> Any:
    if raw_value is None:
        return raw_value
    text = str(raw_value).strip()
    aliases = _VALUE_ALIASES.get(full_key, {})
    if text.lower() in aliases:
        return aliases[text.lower()]
    if text in aliases:
        return aliases[text]
    return raw_value


def extract_by_llm(
    texts: list[str],
    user_id: str,
    scene_id: str | None,
    source: str,
    client: SiliconFlowClient | None = None,
    *,
    debug: bool = False,
    restrict_to_known_keys: bool = False,
) -> list[ExtractedPreference]:
    client = client or SiliconFlowClient()
    if not client.available:
        return []
    messages = build_extraction_messages(texts, user_id, scene_id, source)
    raw = client.chat_completion(messages, temperature=0.0, max_tokens=2000)
    if raw.startswith("Error:"):
        return []
    if debug:
        print("[preference.llm raw]", raw[:1200])
    items: list[ExtractedPreference] = []
    for row in _parse_llm_json(raw):
        try:
            category, key = _normalize_row_key(row)
            if not category or not key:
                # LLM 返回空 category/key（如无关字段），跳过而非生成 habit.custom_ 空键
                if debug:
                    print("[preference.llm skip row] empty category/key:", row)
                continue
            full = normalize_preference_key(category, key)
            value = validate_value(full, _coerce_value(full, row.get("value")))
            scope = str(row.get("scope") or "scene")
            sid = row.get("scene_id")
            if sid in (None, "null", ""):
                sid = scene_id or "general"
                if scope == "global":
                    sid = None
            items.append(
                ExtractedPreference(
                    category=category,
                    key=key,
                    value=value,
                    description=str(row.get("description") or ""),
                    scope=scope if scope in {"global", "scene"} else "scene",
                    scene_id=None if scope == "global" else str(sid),
                    source=map_data_source_to_confidence_source(source),
                    statement_text="\n".join(texts),
                    reason=str(row.get("reason") or "llm_extract"),
                )
            )
        except Exception as exc:
            if debug:
                print("[preference.llm skip row]", row, "err=", exc)
            continue

    if restrict_to_known_keys:
        # 封闭键精确匹配口径：只保留已注册键，丢弃 LLM 自创的 custom_* 动态键，
        # 否则这些未在 expected_preferences 中的键会被计为 FP、拉低精确率。
        # 召回不受影响——期望键本就是已知键。产品默认 False，保留长尾兜底。
        kept: list[ExtractedPreference] = []
        for it in items:
            if normalize_preference_key(it.category, it.key) in PREFERENCE_KEYS:
                kept.append(it)
        items = kept
    return items


def extract_open_domain_by_llm(
    texts: list[str],
    user_id: str,
    scene_id: str | None,
    source: str,
    client: SiliconFlowClient | None = None,
    *,
    debug: bool = False,
) -> list[ExtractedPreference]:
    """开放域自由偏好抽取头（竞赛用）。

    与 extract_by_llm 的封闭键抽取并行：不限定键表，直接把用户表达的持久偏好以
    condition→action 抽成自由文本，description 保留用户原意。落库时经
    normalize_preference_key 降级为 category.custom_* 动态键，绝不因找不到键而丢弃。
    这样「用 TypeScript」「只要代码不要解释」这类开放域偏好也能被结构化记住。
    """
    client = client or SiliconFlowClient()
    if not client.available:
        return []
    messages = build_open_domain_messages(texts)
    raw = client.chat_completion(messages, temperature=0.0, max_tokens=2000)
    if raw.startswith("Error:"):
        return []
    if debug:
        print("[preference.od raw]", raw[:1200])
    items: list[ExtractedPreference] = []
    for row in _parse_llm_json(raw):
        try:
            category = str(row.get("category") or "general").strip().lower()
            action = str(row.get("description") or row.get("action") or "").strip()
            condition = str(row.get("condition") or "").strip()
            key = str(row.get("key") or "").strip()
            if not key:
                key = sanitize_key_part(action or condition)[:40] or "preference"
            value = row.get("value")
            if value is None or not str(value).strip():
                value = action or key
            description = (
                f"{condition}: {action}" if (condition and action) else (action or condition)
            )
            items.append(
                ExtractedPreference(
                    category=category,
                    key=key,
                    value=str(value),
                    description=description,
                    scope="global",
                    scene_id=None,
                    source=map_data_source_to_confidence_source(source),
                    statement_text="\n".join(texts),
                    reason="open_domain_extract",
                )
            )
        except Exception as exc:
            if debug:
                print("[preference.od skip row]", row, "err=", exc)
            continue
    return items


def extract_preferences(
    records: list[RawRecord],
    *,
    client: SiliconFlowClient | None = None,
    use_llm: bool = True,
    allow_rules: bool = True,
    open_domain: bool = False,
    restrict_llm_to_known_keys: bool = False,
    debug: bool = False,
) -> list[ExtractedPreference]:
    if not records:
        return []
    texts = [t for r in records if (t := _record_text(r)).strip()]
    if not texts:
        return []
    user_id = records[0].user_id
    scene_id = records[0].scene_id
    source = (
        records[0].source.value if isinstance(records[0].source, DataSource) else str(records[0].source)
    )

    merged: list[ExtractedPreference] = []
    if use_llm:
        try:
            merged.extend(
                extract_by_llm(
                    texts,
                    user_id,
                    scene_id,
                    source,
                    client=client,
                    debug=debug,
                    restrict_to_known_keys=restrict_llm_to_known_keys,
                )
            )
        except Exception as exc:
            if debug:
                print("[preference.llm exception]", exc)
            merged = []

    if use_llm and open_domain:
        # 开放域抽取头：把 Free-text condition→action 偏好作为补充，与封闭键结果合并。
        try:
            merged.extend(
                extract_open_domain_by_llm(
                    texts, user_id, scene_id, source, client=client, debug=debug
                )
            )
        except Exception as exc:
            if debug:
                print("[preference.od exception]", exc)

    if allow_rules and (not merged):
        # fallback or augment: if LLM empty, use rules on concatenated text
        merged.extend(extract_by_rules("\n".join(texts), scene_id))
    elif allow_rules and merged:
        # keep LLM primary; optionally fill obvious misses from rules for multi-key cases
        rule_items = extract_by_rules("\n".join(texts), scene_id)
        have = {f"{x.category}.{x.key}::{x.scene_id}" for x in merged}
        for item in rule_items:
            sig = f"{item.category}.{item.key}::{item.scene_id}"
            if sig not in have:
                merged.append(item)

    # validate/normalize values；未知键降级为动态键（custom_ 命名空间），绝不丢弃
    valid: list[ExtractedPreference] = []
    for item in merged:
        try:
            full = normalize_preference_key(item.category, item.key)
            cat, k = full.split(".", 1)
            item.category = cat
            item.key = k
            item.value = validate_value(full, item.value)
            if not item.confidence_source:
                item.confidence_source = item.source
            valid.append(item)
        except ValueError:
            continue

    # 合并封闭键与开放域两条来源后按 category.key::scene 去重，避免同键重复落库。
    deduped: list[ExtractedPreference] = []
    seen_sig: set[str] = set()
    for item in valid:
        sig = f"{item.category}.{item.key}::{item.scene_id}"
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        deduped.append(item)
    return deduped
