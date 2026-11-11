"""Build retrieval queries from PersonaChat-style self-descriptions."""
from __future__ import annotations

import re

_CHITCHAT = re.compile(
    r"^(?:hey(?: there)?|hi|hello|how are you|what's up|whats up|"
    r"nice to meet you|goodbye|bye|ok(?:ay)?|thanks?(?: you)?)"
    r"[\s.!]*$",
    re.I,
)
_PERSONA_BLOCK = re.compile(
    r"\[PERSONA\]\s*(.*?)\s*(?:\[DIALOGUE\]|\Z)",
    re.I | re.S,
)
_DIALOGUE_MARK = re.compile(r"\[DIALOGUE\]", re.I)
_FACT_SPLIT = re.compile(r"[\n;]+|(?<=[.!?])\s+")
_BEHAVIOR_ACTION = re.compile(
    r"打开|vscode|终端|点击|mouse|git\s+pull|使用终端|opened|used the terminal",
    re.I,
)
_PERSONA_PROFILE = re.compile(
    r"\bi am (?:a|an)\b|\bi work (?:as|at|in)\b|\bi have (?!to\b)"
    r"|\bi (?:like|love|hate|prefer)\b"
    r"|我是(?:一名|一个|位)?|我在.{1,16}工作|我有(?!点)|我喜欢|我讨厌",
    re.I,
)
_PERSONA_BEHAVIOR_ACTIONS = frozenset(
    {"stated_persona", "persona", "self_description", "profile"}
)
PREFERENCE_PROBE = re.compile(
    r"(?:do|does|did)\s+(?:you|we|they|he|she)\s+(?:like|love|hate|prefer)\b"
    r"|(?:what|which|who)(?:\s+\w+){0,6}\s+do\s+(?:you|we|they)\s+(?:like|love|prefer|hate)"
    r"|喜欢(?:什么|哪|吗)|讨厌(?:什么|哪|吗)|偏好(?:什么|哪|吗)",
    re.I,
)


def persona_query(facts: list[str], *, max_chars: int = 400) -> str:
    """Keep the first two non-empty persona facts, in order.

    Empty means there is no user-trait evidence. Callers must not substitute
    chitchat turns or a document prefix to manufacture a retrieval hit.
    """
    chosen: list[str] = []
    for fact in facts:
        text = " ".join(str(fact).split()).strip()
        if not text or _CHITCHAT.fullmatch(text) or text in chosen:
            continue
        chosen.append(text)
        if len(chosen) >= 2:
            break
    if not chosen or max_chars <= 0:
        return ""
    return "\n".join(chosen)[:max_chars]


def last_persona_b_turn(turns: list[str]) -> str:
    """Last utterance attributed to Persona B, else the last non-empty turn."""
    last = ""
    last_b = ""
    for turn in turns:
        text = str(turn).strip()
        if not text:
            continue
        last = text
        lower = text.lower()
        if lower.startswith("persona b:"):
            last_b = text.split(":", 1)[-1].strip()
        elif lower.startswith("b:"):
            last_b = text.split(":", 1)[-1].strip()
    return last_b or last


def strip_preference_probes(text: str) -> str:
    """Drop interrogative like/love probes; keep first-person statements."""
    cleaned = PREFERENCE_PROBE.sub(" ", text or "")
    return " ".join(cleaned.split())


def persona_facts_from_content(content: str) -> list[str]:
    """Split ``[PERSONA]`` facts; ignore ``[DIALOGUE]`` chitchat."""
    text = content or ""
    match = _PERSONA_BLOCK.search(text)
    if match:
        block = match.group(1)
    elif _DIALOGUE_MARK.search(text):
        block = _DIALOGUE_MARK.split(text, 1)[0]
    else:
        return []
    facts: list[str] = []
    seen: set[str] = set()
    for part in _FACT_SPLIT.split(block):
        item = " ".join(part.split()).strip(" -•")
        if not item or _CHITCHAT.fullmatch(item):
            continue
        lowered = item.lower()
        if lowered.startswith("persona ") or lowered in seen:
            continue
        seen.add(lowered)
        facts.append(item)
    return facts


def persona_ingest_text(content: str) -> str:
    """Keep persona facts only when concat markers are present."""
    facts = persona_facts_from_content(content)
    if facts:
        return "\n".join(facts)
    text = content or ""
    match = _PERSONA_BLOCK.search(text)
    if match:
        collapsed = " ".join(match.group(1).split()).strip()
        if collapsed:
            return collapsed
    if _DIALOGUE_MARK.search(text):
        return " ".join(text.split()).strip()
    return text.strip()


def is_persona_as_user_behavior(content: str, *, action: str = "") -> bool:
    """True when a self-description was mis-tagged as ``user_behavior``."""
    if (action or "").strip().lower() in _PERSONA_BEHAVIOR_ACTIONS:
        return True
    text = content or ""
    if _BEHAVIOR_ACTION.search(text):
        return False
    return bool(_PERSONA_PROFILE.search(text))
