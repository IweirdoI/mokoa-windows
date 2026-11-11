"""Evidence-based preference classification. Owner: efca9634.

Knowledge is the fallback of the two-class interface, not an admission decision.
Ingestion quality gates still decide whether a record is worth saving.
"""
from __future__ import annotations

import re

from src.common.models import MemoryType, NormalizedRecord
from src.core.persona_query import strip_preference_probes
# 脱敏占位符模式由安全脱敏层统一定义（src/security/placeholders），
# 此处直接复用，避免与 security/service.py 两处正则漂移。
from src.security.placeholders import PLACEHOLDER_RE


class MemoryClassifier:
    # 匹配安全层产出的占位符（<HASH_8hex> 常规 / <_HASH_16hex_> LLM 上下文），
    # 分类时需跳过它们，不能把占位符里的 hex 当成真实内容特征。
    _HASH_RE = PLACEHOLDER_RE
    _EN_PREFERENCE = re.compile(
        r"\b(?:i|we|the user|user|he|she|they)\s+"
        r"(?:(?:really|usually|always|generally|personally|strongly)\s+)?"
        r"(?:(?:do not|don't|does not|doesn't)\s+)?"
        r"(?:prefer(?:s)?|like(?:s)?|dislike(?:s)?|love(?:s)?|hate(?:s)?)\b"
        r"(?!\s+(?:to know|to book|to reserve|to order|to ask)\b)", re.I
    )
    _EN_EXPLICIT = re.compile(
        r"\b(?:i would|i'd|we would|we'd)\s+prefer\b"
        r"|\b(?:my|our|the user's)\s+preference\s+is\b", re.I
    )
    _ZH_PREFERENCE = re.compile(
        r"(?:我|我们|用户|他|她)(?:平时|通常|一直|更|比较|很|非常|最)?"
        r"(?:不喜欢|喜欢|偏好|习惯|讨厌|厌恶|喜爱|爱吃)(?!什么|哪|吗)"
        r"|^(?:不喜欢|喜欢|偏好|习惯|讨厌|厌恶|喜爱|爱吃)(?!什么|哪|吗)"
        r"|(?:我|用户)?不希望(?:自动|以后|每次|总是)"
    )
    _PERSISTENT_INSTRUCTION = re.compile(
        r"(?:以后|今后|每次|始终|默认)(?:都)?(?:请)?(?:用|使用|把|将|保存|下载|回答|回复)"
        r"|\b(?:always|by default|from now on)\s+(?:please\s+)?"
        r"(?:reply|respond|answer|use|save|download)\b", re.I
    )

    def classify(self, record: NormalizedRecord) -> MemoryType:
        return MemoryType(self.explain(record)["memory_type"])

    def explain(self, record: NormalizedRecord) -> dict[str, str]:
        """Stable decision reason without copying personal content."""
        raw = record.content or ""
        text = strip_preference_probes(raw)
        source = record.type
        if self._HASH_RE.search(raw):
            kind, reason = MemoryType.KNOWLEDGE, "protected_content"
        elif source in {"document", "tool_result"}:
            kind, reason = MemoryType.KNOWLEDGE, "knowledge_source"
        elif source == "manual_config":
            kind, reason = MemoryType.PREFERENCE, "user_configuration"
        elif self._EN_PREFERENCE.search(text) or self._EN_EXPLICIT.search(text) or self._ZH_PREFERENCE.search(text):
            kind, reason = MemoryType.PREFERENCE, "expressed_preference"
        elif self._PERSISTENT_INSTRUCTION.search(text):
            kind, reason = MemoryType.PREFERENCE, "persistent_instruction"
        else:
            kind, reason = MemoryType.KNOWLEDGE, "no_preference_evidence"
        return {"memory_type": kind.value, "reason": reason}
