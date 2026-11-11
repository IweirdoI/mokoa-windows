"""通用自然语言遗忘目标解析器。

Owner: 2abac5a6
Ref: 比赛题目 (5) 敏感信息识别与精准遗忘

背景（P0 根因）：
    原 ``SensitiveService.forget`` 只扫描脱敏索引（SensitiveIndex），
    匹配手机号/身份证/邮箱等敏感类型。普通记忆 / 偏好（如下载路径偏好）
    不在索引中，导致 ``POST /forget`` 对非敏感内容恒返回 ``count=0``。

本模块面向「候选记忆快照」做多级文本匹配，输出可解释的删除目标列表：

    1. 类型意图识别 —— 指令是否点名某类记忆（偏好/知识），用于收缩删除范围
    2. 话题关键词抽取 —— 抽取指令中的主题词（中文词组 + 英文单词）
    3. 分级打分 —— 全文包含 > 关键词包含候选 > 共享词元，逐条候选取最优
    4. 安全级联 —— 低于置信度门槛不纳入；仅当指令显式出现「所有/全部/all」
       且限定类型时才放行全量删除，避免误删

解析阶段不做任何删除，正式删除统一由 Orchestrator 执行（见 orchestrator.forget）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 指令中的遗忘动作 / 提权词 / 无意义虚词，抽取关键词时剔除。
_STOP_WORDS: set[str] = {
    # 中文动作
    "忘掉", "忘记", "遗忘", "删除", "移除", "清除", "清除掉", "清空",
    "删掉", "去掉", "取消", "撤销", "撤回", "消掉", "把这个", "那条",
    # 中文虚词
    "了", "的", "是", "在", "有", "和", "与", "或", "一个", "这个", "那个",
    "上", "把", "请", "帮我", "刚刚", "刚才", "之前的", "之前", "测试的",
    "关于", "那些", "这些", "一下", "我的",
    # 英文动作 / 虚词
    "forget", "forgot", "remove", "delete", "drop", "erase", "clear", "x",
    "a", "an", "the", "this", "that", "of", "to", "for", "on", "at", "it",
    "my", "me", "about", "and", "or", "just", "only",
}

# 全量删除的提权词：仅当同时限定记忆类型时生效。
_ALL_WORDS: set[str] = {"所有", "全部", "一切", "所有偏好", "全部偏好", "all", "every"}

_PREF_KEY_HINTS: dict[str, tuple[str, ...]] = {
    "download_path": ("下载路径", "下载目录", "download_path", "download"),
    "download": ("下载路径", "下载目录", "download"),
    "reply_language": (
        "中文回答",
        "用中文",
        "中文回复",
        "英文回答",
        "用英文",
        "reply_language",
        "回复语言",
        "回答语言",
    ),
    "response_tone": ("更简洁", "response_tone", "concise"),
    "likes": ("喜欢", "爱吃", "喜爱", "likes", "喜好"),
    "dislikes": ("讨厌", "不喜欢", "厌恶", "dislikes"),
}

# 指令中的记忆类型意图词 → 对应的 MemoryType 值。
_TYPE_INTENT: dict[str, list[str]] = {
    "preference": ["偏好", "习惯", "设置", "偏爱", "喜好", "preference", "习惯性"],
    "knowledge": ["知识", "事实", "记录", "信息", "资料", "knowledge", "fact", "文档", "片段"],
}


@dataclass
class ForgetTarget:
    """一条命中待删除的记忆目标（含可解释的命中依据）。"""

    memory_id: str
    content: str
    memory_type: str
    layer: str
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    reason: str = ""
    # 偏好候选对应的标准键（如 habit.download_path），用于同步删除版本索引。
    preference_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转字典，供前端 / 日志展示删除范围预览。"""
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "memory_type": self.memory_type,
            "layer": self.layer,
            "score": round(self.score, 3),
            "matched_keywords": self.matched_keywords,
            "reason": self.reason,
            "preference_key": self.preference_key,
        }


@dataclass
class ForgetResolution:
    """NL 遗忘指令的解析结果（目标列表 + 删除范围 + 说明）。"""

    instruction: str
    targets: list[ForgetTarget] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转字典，供 /forget/parse 一类接口及日志使用。"""
        return {
            "instruction": self.instruction,
            "targets": [t.to_dict() for t in self.targets],
            "scope": self.scope,
            "note": self.note,
        }


def _split_tokens(text: str) -> list[str]:
    """把文本切成候选词元：中文连续块 + 英文单词（各至少 2 字符）。"""
    chinese = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
    english = re.findall(r"[a-z]{2,}", text.lower())
    return chinese + english


class ForgetTargetParser:
    """把自然语言遗忘指令映射到候选记忆快照中的目标 ID。"""

    def __init__(
        self,
        *,
        min_score: float = 0.6,
        contain_weight: float = 1.0,
        reverse_contain_weight: float = 0.6,
        token_weight: float = 0.35,
        type_weight: float = 0.25,
    ) -> None:
        """参数均可注入以减少硬编码，便于单测构造边界场景。

        Args:
            min_score: 命中置信度门槛，低于则丢弃（安全级联）。
            contain_weight: 候选内容完整包含某主题词时的加分。
            reverse_contain_weight: 主题词包含候选内容（候选更短）时的加分。
            token_weight: 仅共享部分词元时的最低加分。
            type_weight: 类型意图命中时的加分。
        """
        self.min_score = min_score
        self.contain_weight = contain_weight
        self.reverse_contain_weight = reverse_contain_weight
        self.token_weight = token_weight
        self.type_weight = type_weight

    def parse(
        self,
        instruction: str,
        candidates: list[Any],
    ) -> ForgetResolution:
        """解析遗忘指令，返回目标列表与删除范围说明。不执行删除。

        Args:
            instruction: 自然语言遗忘指令，如「忘掉下载路径偏好」。
            candidates: 候选记忆快照，元素需具备
                ``id / content / memory_type / layer`` 属性（MemoryItem 即可）。

        Returns:
            ForgetResolution：命中的 ForgetTarget 列表 + 范围 + 说明。
        """
        instruction = (instruction or "").strip()
        if not instruction:
            return ForgetResolution(instruction=instruction, note="空指令，未解析任何目标")

        type_intent = self._detect_type_intent(instruction)
        topics = self._extract_topics(instruction, type_intent)
        want_all = self._detect_all(instruction)
        allow_note = "全量删除需要显式限定类型，请补充偏好/知识等范围"

        # 未识别出任何范围：类型意图与主题词都为空，保险起见不删除。
        if not type_intent and not topics and not want_all:
            return ForgetResolution(
                instruction=instruction,
                note="未能从指令中识别出明确删除范围，未生成任何目标",
            )

        targets: list[ForgetTarget] = []
        for cand in candidates:
            memory_id = getattr(cand, "id", None)
            content = getattr(cand, "content", "") or ""
            memory_type = getattr(cand, "memory_type", None)
            if memory_id is None:
                continue

            cand_meta = getattr(cand, "metadata", None) or {}
            cand_pref_key = cand_meta.get("preference_key") if isinstance(cand_meta, dict) else None

            # 类型限定：意图明确点名某类型时，过滤非目标类型候选。
            type_matched = True
            if type_intent is not None:
                cand_type = str(getattr(memory_type, "value", memory_type)).lower()
                type_matched = cand_type == type_intent
            if not type_matched:
                continue

            score, matched_kws, reason = self._score(
                instruction,
                content,
                topics,
                type_intent,
                preference_key=cand_pref_key if isinstance(cand_pref_key, str) else None,
            )
            if want_all and type_intent is not None:
                score = 1.0
                reason = "指令显式要求删除该类型全部记忆"

            if score < self.min_score:
                continue

            targets.append(
                ForgetTarget(
                    memory_id=memory_id,
                    content=content[:120],
                    memory_type=str(getattr(memory_type, "value", memory_type)),
                    layer=str(getattr(cand, "layer", "") or ""),
                    score=round(score, 3),
                    matched_keywords=matched_kws[:8],
                    reason=reason,
                    preference_key=cand_pref_key,
                )
            )

        scope: dict[str, Any] = {
            "memory_type": type_intent,
            "matched_keywords": topics,
            "all_type": want_all and type_intent is not None,
        }
        note = self._build_note(type_intent, topics, want_all, len(targets), allow_note)
        return ForgetResolution(
            instruction=instruction,
            targets=targets,
            scope=scope,
            note=note,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _detect_type_intent(self, instruction: str) -> str | None:
        """从指令中识别记忆类型意图（preference / knowledge / None）。"""
        lowered = instruction.lower()
        for mem_type, words in _TYPE_INTENT.items():
            if any(w.lower() in lowered for w in words):
                return mem_type
        return None

    def _detect_all(self, instruction: str) -> bool:
        """判断指令是否为「删除某类全部记忆」的提权意图。"""
        lowered = instruction.lower()
        return any(w.lower() in lowered for w in _ALL_WORDS)

    def _extract_topics(self, instruction: str, type_intent: str | None) -> list[str]:
        """抽取指令中的主题词语，剔除动作词 / 类型意图词 / 虚词。

        中文无标点连续句（如「忘掉下载路径偏好」）整串分词为单个长词元，
        直接作主题词会整串误匹配。这里对过长（>6 字）的中文块丢弃，主题
        识别主靠候选内容词元的反向匹配（见 _score 分支 A）。
        """
        chinese_blocks = re.findall(r"[\u4e00-\u9fa5]{2,}", instruction)
        english_words = re.findall(r"[a-z]{2,}", instruction.lower())
        cleaned = instruction
        for word in sorted(_STOP_WORDS, key=len, reverse=True):
            cleaned = re.sub(re.escape(word), "", cleaned, flags=re.IGNORECASE)
        chinese_blocks.extend(re.findall(r"[\u4e00-\u9fa5]{2,}", cleaned))
        tokens = [w for w in chinese_blocks if len(w) <= 6] + english_words
        topics: list[str] = []
        seen: set[str] = set()
        for tok in tokens:
            t = tok.lower()
            if t in _STOP_WORDS:
                continue
            if type_intent is not None and t in _TYPE_INTENT.get(type_intent, []):
                continue
            if t not in seen:
                seen.add(t)
                topics.append(t)
        instruction_l = instruction.lower()
        for hints in _PREF_KEY_HINTS.values():
            for hint in hints:
                token = hint.lower()
                if token in seen or token in _STOP_WORDS or len(token) < 2:
                    continue
                if token in instruction_l:
                    seen.add(token)
                    topics.append(hint)
        # 保留原始大小写更贴近候选正文，但匹配仍统一小写。
        return topics

    def _score(
        self,
        instruction: str,
        content: str,
        topics: list[str],
        type_intent: str | None,
        preference_key: str | None = None,
    ) -> tuple[float, list[str], str]:
        """对单条候选打分，返回 (分数, 命中关键词, 命中理由)。

        两级匹配：
          A. 候选内容词元反向命中指令（中文指令无标点整串时的主力路径）；
          B. 指令主题词正向命中候选内容（英文/词分隔指令的补充路径）。
          C. 标准偏好键别名（如 download_path ↔ 下载路径）。
        """
        instr_l = instruction.lower()
        content_l = content.lower()

        matched: list[str] = []
        best = 0.0

        # 分支 A：候选内容的词元出现在指令中 → 指令确实引用了该记忆内容。
        for token in _split_tokens(content):
            tl = token.lower()
            if len(tl) >= 2 and tl in instr_l:
                matched.append(token)
                if self.reverse_contain_weight > best:
                    best = self.reverse_contain_weight

        # 分支 C：规则抽取的偏好键与中文指令对齐。
        if preference_key:
            tail = preference_key.rsplit(".", 1)[-1].lower()
            hints = _PREF_KEY_HINTS.get(tail, ())
            if any(hint.lower() in instr_l for hint in hints):
                matched.append(preference_key)
                if self.contain_weight > best:
                    best = self.contain_weight

        # 分支 B：指令主题词正向匹配候选内容。
        if not matched:
            for topic in topics:
                tl = topic.lower()
                if tl in content_l:
                    matched.append(topic)
                    if self.contain_weight > best:
                        best = self.contain_weight
                elif content_l and content_l in tl:
                    matched.append(topic)
                    if self.reverse_contain_weight > best:
                        best = self.reverse_contain_weight

        if not matched:
            # 仅做词元级弱匹配（几乎不应触发，分支 A 已覆盖大部分）。
            content_tokens = set(_split_tokens(content))
            hit = [t for t in topics if t in content_tokens]
            best = self.token_weight if hit else 0.0
            matched = hit

        score = best
        if type_intent is not None:
            score += self.type_weight

        if score <= 0.0:
            return 0.0, [], ""

        reason = ""
        if matched:
            reason = f"命中关键词：{ '/'.join(list(dict.fromkeys(matched))) }"
        if type_intent is not None:
            if reason:
                reason += "；"
            reason += f"类型意图：{type_intent}"
        return min(score, 1.0), list(dict.fromkeys(matched)), reason

    def _build_note(
        self,
        type_intent: str | None,
        topics: list[str],
        want_all: bool,
        target_count: int,
        allow_note: str,
    ) -> str:
        """生成给前端 / 日志的自然语言说明。"""
        if want_all and type_intent is not None:
            return f"将删除类型为 {type_intent} 的全部记忆，共 {target_count} 条"
        parts: list[str] = []
        if type_intent:
            parts.append(f"限定为 {type_intent}")
        if topics:
            parts.append("、".join(topics[:5]))
        if not parts:
            return allow_note
        return f"命中 {target_count} 条记忆（{' '.join(parts)}）"