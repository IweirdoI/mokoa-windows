"""召回重排与相关性过滤，不把向量近邻直接当成答案。

项目标识从记忆正文抽取（FACT project=、Project:、中英并列名），
查询命中某项目时只保留该项目；没有命中则空结果，不拿其它项目顶替。
不在代码里写死某个项目的端口或 SSID。
"""

from __future__ import annotations

import re
from typing import Iterable

from src.common.models import MemoryItem, MemoryType


_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{1,}", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_FACT_PROJECT = re.compile(r"FACT\s+project\s*=\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
_PROJECT_LINE = re.compile(r"Project\s*:\s*([^\n]+)", re.IGNORECASE)
_BILINGUAL = re.compile(
    r"([\u4e00-\u9fff]{2,12})[ \t]*[/\uFF0F]?[ \t]*([A-Z][A-Za-z0-9]+(?:[ \t]+[A-Z][A-Za-z0-9]+)*)"
)

_STOP_ASCII = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "of",
        "to",
        "for",
        "and",
        "or",
        "how",
        "what",
        "where",
        "who",
        "http",
        "https",
        "com",
        "www",
    }
)
_GENERIC_ALIASES = frozenset(
    {
        "wifi",
        "wi-fi",
        "ssid",
        "pdf",
        "http",
        "https",
        "port",
        "owner",
        "health",
        "export",
        "wps",
        "guest",
        "meeting",
        "room",
        "guide",
        "ops",
        "word",
        "docx",
        "update",
        "config",
        "notes",
        "faq",
        "topic",
        "file",
        "how",
        "office",
        "date",
        "fact",
        "effective",
        "weekly",
        "minutes",
        "image",
        "用户上传",
        "图片",
        "文档",
        "日期",
        "负责人",
        "生效日期",
        "可并存",
        "决议",
        "变更项",
    }
)
_BILINGUAL_ZH_SUFFIX = r"(?:配置变更通知|配置更新|部署手册|项目手册|使用手册|周会纪要|会议纪要)$"
_BILINGUAL_EN_SUFFIX = r"\s+(?:Config Update|Project Manual|Deployment Manual|Weekly Minutes|Meeting Notes)$"
_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "wifi": ("wifi", "wi-fi", "ssid", "guest"),
    "wi-fi": ("wifi", "ssid", "guest"),
    "ssid": ("ssid", "wifi", "guest"),
    "会议室": ("meeting", "wifi", "ssid", "guest"),
    "访客": ("guest", "wifi", "ssid"),
    "wps": ("wps", "export", "pdf"),
    "导出": ("export", "pdf", "wps"),
    "pdf": ("pdf", "export"),
    "课程助手": ("courseassistant", "course assistant"),
}

_STOP_CJK = frozenset(
    {
        "什么",
        "多少",
        "怎么",
        "如何",
        "哪个",
        "哪里",
        "记得",
        "请问",
        "一下",
        "相关",
        "信息",
        "问题",
        "明天",
        "今天",
        "现在",
        "当前",
        "最新",
        "以前",
        "是否",
        "可以",
        "怎么办",
        "是什么",
        "是多少",
        "在哪里",
    }
)
_PREFERENCE_HINTS = (
    "偏好",
    "习惯",
    "下载目录",
    "下载路径",
    "保存到",
    "语言",
    "喜欢",
    "讨厌",
    "爱吃",
    "不喜欢",
    "喜好",
    "preference",
    "download_path",
    "下载文件",
    "like",
    "hate",
    "dislike",
)
_KNOWLEDGE_HINTS = (
    "负责", "装包", "安装", "超时", "办法", "共享", "排查",
    "端口",
    "负责人",
    "健康",
    "打印机",
    "wifi",
    "ssid",
    "镜像",
    "配置",
    "手册",
    "会议",
    "日志",
    "怎么",
    "如何",
    "port",
    "owner",
    "health",
    "pip",
    "导出",
    "export",
    "wps",
    "pdf",
    # 开放域英文知识/运维意图词：避免 auto 模式下英文技术查询被漏判为知识意图
    "install", "configure", "config", "debug", "timeout", "error",
    "troubleshoot", "deploy", "server", "network", "dns", "vpn",
    "docker", "kubernetes", "k8s", "crash", "upgrade", "update",
    "patch", "dependency", "package", "environment", "manual",
    "mirror", "image", "registry", "ssh", "firewall", "proxy",
    "certificate", "ssl", "tls", "database", "redis", "mysql",
    "nginx", "linux", "macos", "windows", "terminal", "command",
    "script", "api", "sdk", "bug", "exception", "latency",
    "cache", "queue", "thread", "process", "service", "daemon",
    "cron", "backup", "restore", "migration", "schema",
)

# Topic concepts are counted once, regardless of overlapping Chinese slices.
# Project names are resolved separately from corpus metadata, never answer tables.
_TOPICS = (
    ("打印机", "打印", "printer", "printing"),
    ("端口", "port", "监听", "listening"),
    ("健康检查", "健康", "health", "healthcheck"),
    ("负责人", "负责", "owner", "maintainer"),
    ("装包", "安装包", "pip", "镜像", "package installation", "package install", "pypi"),
    ("wifi", "wi-fi", "ssid", "无线网络", "访客网络"),
    ("导出", "export"),
    ("共享", "share", "sharing"),
    ("手册", "manual"),
)


def _contains(text: str, token: str) -> bool:
    if token.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text.lower()) is not None
    return token in text


def _alias_key(token: str) -> str:
    return " ".join((token or "").split()).strip().lower()


def _is_generic_alias(token: str) -> bool:
    key = _alias_key(token)
    if len(key) < 2:
        return True
    compact = re.sub(r"[\s_\-]+", "", key)
    return key in _GENERIC_ALIASES or compact in _GENERIC_ALIASES


def expanded_query(query: str) -> str:
    additions = [word for group in _TOPICS if any(_contains(query, w) for w in group)
                 for word in group if word.isascii()]
    return " ".join([query, *dict.fromkeys(additions)])


def extract_project_tags(content: str) -> dict[str, list]:
    """从正文抽取项目 ID 与别名，供入库 metadata 使用。"""

    text = content or ""
    ids: list[str] = []
    aliases: list[str] = []

    def add_id(value: str) -> None:
        token = value.strip()
        if token and token not in ids:
            ids.append(token)

    def add_alias(value: str) -> None:
        token = " ".join(value.split()).strip(" -/|")
        if len(token) >= 2 and token not in aliases and not _is_generic_alias(token):
            aliases.append(token)

    pairs: list[tuple[str, str]] = []
    for match in _FACT_PROJECT.finditer(text):
        add_id(match.group(1))
        add_alias(match.group(1))
    for match in _PROJECT_LINE.finditer(text):
        for part in re.split(r"[/,;|]|\\", match.group(1)):
            add_alias(part)
            compact = re.sub(r"[^A-Za-z0-9]+", "", part)
            if compact and not _is_generic_alias(compact):
                add_id(compact)
    for match in _BILINGUAL.finditer(text):
        # Strip document descriptors from bilingual titles, retaining the
        # grounded name pair (e.g. 项目名配置变更通知 / Project Name Config Update).
        chinese = re.sub(_BILINGUAL_ZH_SUFFIX, "", match.group(1)).strip()
        english = re.sub(_BILINGUAL_EN_SUFFIX, "", match.group(2)).strip()
        if _is_generic_alias(chinese) or _is_generic_alias(english):
            continue
        if chinese.startswith(("仅保留", "决议", "文档事实", "变更项")):
            continue
        add_alias(chinese)
        add_alias(english)
        if chinese and english:
            pairs.append((chinese, english))

    return {"project_ids": ids, "project_aliases": aliases, "bilingual_pairs": pairs}


def distinctive_terms(query: str) -> list[str]:
    """查询里可用于相关性判断的词，去掉套话。"""

    text = (query or "").strip()
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        token = term.strip().lower()
        if len(token) < 2 or token in seen:
            return
        if token in _STOP_ASCII or token in _STOP_CJK:
            return
        seen.add(token)
        terms.append(term.strip())

    for word in _ASCII_WORD.findall(text):
        add(word)
    for run in _CJK_RUN.findall(text):
        if run in _STOP_CJK:
            continue
        if len(run) <= 6:
            add(run)
        for size in (4, 3, 2):
            if len(run) < size:
                continue
            for index in range(0, len(run) - size + 1):
                add(run[index : index + size])
    return terms


def _overlap_needles(term: str) -> list[str]:
    token = term.strip().lower()
    needles = [token]
    needles.extend(_QUERY_SYNONYMS.get(token, ()))
    unique: list[str] = []
    seen: set[str] = set()
    for needle in needles:
        if needle and needle not in seen:
            seen.add(needle)
            unique.append(needle)
    return unique


# 词面命中打分权重（相对量纲，非概率，仅用于同查询内候选排序）：
# 命中一个"主题组"（如 打印机/端口 这类项目级概念）权重显著高于单 token 命中，
# 因为主题组能可靠区分"问的是哪类事"；重叠针（token/子串）权重按长度区分。
_TOPIC_HIT_WEIGHT = 8       # 命中主题组
_TOKEN_HIT_WEIGHT_LONG = 3  # 重叠针：长度>=4 或为英文（更可能是实义词）
_TOKEN_HIT_WEIGHT_SHORT = 1 # 重叠针：短中文片段（噪声风险高，低权重）


def term_overlap(query: str, content: str) -> int:
    """查询词在正文中的命中分；只统计词面，不引入具体答案。"""

    body = (content or "").lower()
    if not body:
        return 0
    score = 0
    remainder = query.lower()
    active = [group for group in _TOPICS if any(_contains(remainder, w) for w in group)]
    # Generic document/share words cannot alone justify a specific topic query.
    specific = [g for g in active if g not in _TOPICS[-2:]]
    if specific and not any(any(_contains(body, w) for w in g) for g in specific):
        return 0
    for group in active:
        if any(_contains(body, w) for w in group):
            score += _TOPIC_HIT_WEIGHT
        for word in sorted(group, key=len, reverse=True):
            remainder = remainder.replace(word, " ")
    covered: list[str] = []
    for term in sorted(distinctive_terms(remainder), key=len, reverse=True):
        if any(term.lower() in old for old in covered):
            continue
        for needle in _overlap_needles(term):
            if _contains(body, needle):
                # 长英文/长 token 命中权重更高，短中文片段权重低（噪声多）
                score += _TOKEN_HIT_WEIGHT_LONG if len(needle) >= 4 or needle.isascii() else _TOKEN_HIT_WEIGHT_SHORT
                covered.append(term.lower())
                break
    return score


# 语种占比切分阈值（cjk_ratio 值域 0~1，表示中日韩字符占比）：
# 某语种占比 >= 0.4 视为"主导语种"，另一语种 <= 0.1 视为"可忽略"。
# 仅当"一侧主导、一侧可忽略"时才判定为跨语种，避免中英混排文档被误判。
_CJK_DOMINANT_RATIO = 0.4
_OTHER_MINOR_RATIO = 0.1


def is_cross_language(query: str, content: str) -> bool:
    """查询与候选正文主要语种不同（如中文问句 vs 英文办公文档）。

    仅供语种诊断；不能独立作为相关证据或保留候选的依据。
    """

    def cjk_ratio(text: str) -> float:
        if not text:
            return 0.0
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        return cjk / max(len(text), 1)

    q = cjk_ratio(query)
    c = cjk_ratio(content or "")
    return (q >= _CJK_DOMINANT_RATIO and c <= _OTHER_MINOR_RATIO) or (
        c >= _CJK_DOMINANT_RATIO and q <= _OTHER_MINOR_RATIO
    )


def preference_intent(query: str) -> bool:
    text = (query or "").strip().lower()
    return any(_contains(text, token) for token in _PREFERENCE_HINTS)


def knowledge_intent(query: str) -> bool:
    text = (query or "").strip().lower()
    return any(_contains(text, token) for token in _KNOWLEDGE_HINTS)


def open_domain_semantic(query: str, items: Iterable[MemoryItem]) -> bool:
    """Keep English general questions out of the office lexical pipeline.

    Broad words such as health, image and service are not office evidence.
    Known project aliases and explicit workplace phrases retain hybrid recall;
    callers can request hybrid for projects missing from the vector candidates.
    """
    text = (query or "").strip()
    if not re.search(r"[a-zA-Z]", text) or re.search(r"[\u4e00-\u9fff]", text):
        return False
    if preference_intent(text) or mentioned_project_aliases(text, items):
        return False
    office_phrases = (
        "printer share", "meeting room", "office wifi", "guest wifi",
        "health check endpoint", "default port", "project manual", "project owner",
    )
    return not any(_contains(text, phrase) for phrase in office_phrases)


def item_aliases(item: MemoryItem) -> list[str]:
    meta = item.metadata or {}
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    collected: list[str] = []
    for source in (meta, nested or {}):
        for key in ("project_aliases", "project_ids"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                collected.append(value.strip())
            elif isinstance(value, (list, tuple)):
                collected.extend(str(part).strip() for part in value if str(part).strip())
    tags = extract_project_tags(item.content or "")
    collected.extend(tags["project_ids"])
    collected.extend(tags["project_aliases"])
    unique: list[str] = []
    seen: set[str] = set()
    for alias in collected:
        key = alias.lower()
        if key in seen or _is_generic_alias(alias):
            continue
        seen.add(key)
        unique.append(alias)
    return unique


def _alias_mentioned_in_query(alias: str, query: str) -> bool:
    """项目别名必须足够具体；wifi/pdf 等通用词不当成项目名。"""

    key = " ".join(alias.split()).strip().lower()
    if len(key) < 4 or key in _GENERIC_ALIASES or key in _STOP_ASCII or key in _STOP_CJK:
        return False
    text = (query or "").lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", key):
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None
    return key in text


def mentioned_project_aliases(query: str, items: Iterable[MemoryItem]) -> list[str]:
    """查询里点名、且能在候选记忆上对上的项目别名。"""

    items = list(items)
    mentioned: list[str] = []
    seen: set[str] = set()

    def add(alias: str) -> bool:
        key = alias.lower()
        if not alias or key in seen or _is_generic_alias(alias):
            return False
        seen.add(key)
        mentioned.append(alias)
        return True

    for item in items:
        for alias in item_aliases(item):
            if _alias_mentioned_in_query(alias, query):
                add(alias)

    # Compact spelling (Acme Notes ↔ AcmeNotes) and bilingual title pairs.
    # Do not copy every alias on a multi-project document: a change notice
    # that mentions two products must not merge them into one scope.
    def norm(alias: str) -> str:
        return re.sub(r"[\s_\-]+", "", alias.lower())

    changed = True
    while changed:
        changed = False
        keys = {norm(a) for a in mentioned}
        for item in items:
            for alias in item_aliases(item):
                if norm(alias) in keys and add(alias):
                    changed = True
            for left, right in extract_project_tags(item.content or "").get("bilingual_pairs") or ():
                if norm(left) in keys and add(right):
                    changed = True
                if norm(right) in keys and add(left):
                    changed = True
    return mentioned


def item_matches_projects(item: MemoryItem, aliases: list[str]) -> bool:
    if not aliases:
        return True
    known = {re.sub(r"[\s_\-]+", "", a.lower()) for a in item_aliases(item)}
    content = (item.content or "").lower()
    for alias in aliases:
        token = alias.lower()
        if token and (re.sub(r"[\s_\-]+", "", token) in known
                      or (not known and _contains(content, token))):
            return True
    return False


def is_documentish(item: MemoryItem) -> bool:
    meta = item.metadata or {}
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    tags = {
        str(meta.get("memory_type") or ""),
        str(meta.get("source") or ""),
        str(meta.get("source_type") or ""),
        str(meta.get("file_kind") or ""),
        str((nested or {}).get("memory_type") or ""),
        str((nested or {}).get("file_kind") or ""),
    }
    if item.memory_type == MemoryType.KNOWLEDGE:
        if any(
            token in tags
            for token in (
                "document_chunk",
                "document",
                "pdf",
                "docx",
                "image",
                "tool_call",
                "project_context",
            )
        ):
            return True
        content = item.content or ""
        return content.startswith("用户上传") or "FACT" in content
    return False


def retrieval_score(item: MemoryItem) -> float:
    meta = item.metadata or {}
    raw = meta.get("retrieval_score")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def merge_items(*groups: Iterable[MemoryItem]) -> list[MemoryItem]:
    seen: set[str] = set()
    merged: list[MemoryItem] = []
    for group in groups:
        for item in group:
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)
    return merged


def lexical_hits(query: str, pool: Iterable[MemoryItem]) -> list[MemoryItem]:
    scored: list[tuple[int, MemoryItem]] = []
    for item in pool:
        overlap = term_overlap(query, item.content or "")
        if overlap > 0:
            scored.append((overlap, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def rerank_and_filter(
    query: str,
    items: list[MemoryItem],
    top_k: int,
    *,
    allow_empty: bool = True,
    project_context: list[MemoryItem] | None = None,
) -> list[MemoryItem]:
    """实体重叠 + 项目约束 + 轻量重排。无相关记忆时返回空列表。"""

    if top_k <= 0:
        return []
    text = (query or "").strip()
    if not text:
        return items[:top_k]

    pref = preference_intent(text)
    know = knowledge_intent(text)
    projects = mentioned_project_aliases(text, project_context if project_context is not None else items)
    working = list(items)
    if projects:
        scoped = [item for item in working if item_matches_projects(item, projects)]
        working = scoped if scoped else ([] if allow_empty and know else working)

    topic_text = text
    for alias in sorted(projects, key=len, reverse=True):
        topic_text = re.sub(re.escape(alias), " ", topic_text, flags=re.I)
    terms = distinctive_terms(topic_text)

    def rank_key(item: MemoryItem) -> tuple[int, int, int, int, float]:
        overlap = term_overlap(topic_text, item.content or "")
        project_hit = 1 if projects and item_matches_projects(item, projects) else 0
        document_hit = 1 if is_documentish(item) else 0
        pref_hit = 1 if item.memory_type == MemoryType.PREFERENCE else 0
        if know and not pref:
            type_bonus = -1 if pref_hit else document_hit
        elif pref and not know:
            type_bonus = pref_hit
        else:
            type_bonus = document_hit
        return (project_hit, -pref_hit if know and not pref else 0, overlap, type_bonus, retrieval_score(item))

    # 纯偏好查询（pref 且非知识）按 key/类型召回，不依赖词面重叠；
    # 知识或混合意图查询仍需按词面重叠过滤，否则无关候选会被保留、相关性静默劣化。
    if terms and not (pref and not know):
        relevant: list[MemoryItem] = []
        for item in working:
            if term_overlap(topic_text, item.content or "") > 0:
                relevant.append(item)
        if relevant or allow_empty:
            working = relevant

    working.sort(key=rank_key, reverse=True)
    unique: list[MemoryItem] = []
    seen: set[str] = set()
    for item in working:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
        if len(unique) >= top_k:
            break
    return unique
