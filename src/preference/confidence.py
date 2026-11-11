"""Preference confidence scoring — from confidence-score-formula.md v1.0."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


def _ensure_utc(dt: datetime | None) -> datetime:
    """Normalize to timezone-aware UTC (tolerate legacy naive values from disk)."""
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

NEGATION_PATTERNS_ZH = [
    r"不用",
    r"不要",
    r"别",
    r"不需要",
    r"禁止",
    r"不允许",
    r"无需",
    r"直接.*不用",
    r"跳过",
]
NEGATION_PATTERNS_EN = [
    r"never",
    r"don'?t",
    r"do not",
    r"no need",
    r"skip",
    r"without",
]

BASE_SCORES = {
    "manual_config": 1.00,
    "explicit_statement": 0.92,
    "repeated_behavior": 0.80,
    "single_observation": 0.55,
    "cross_scene_transfer": 0.45,
    "system_default": 0.30,
}

INDEPENDENT_GROUPS = {
    "manual_config": "group_a",
    "explicit_statement": "group_b",
    "repeated_behavior": "group_c",
    "single_observation": "group_c",
    "cross_scene_transfer": "group_d",
    "system_default": "group_e",
}

# 收敛增益：多个独立来源佐证同一取值时提升置信度。
# 3 个及以上独立来源（+0.05）视为强佐证；2 个（0.03）为弱佐证。
CONVERGE_BOOST_STRONG = 0.05  # n_independent >= 3
CONVERGE_BOOST_WEAK = 0.03    # n_independent == 2
# 冲突惩罚：多条来源指向不同取值时下调置信度（含"否定类"冲突权重更高）。
CONFLICT_PENALTY_WITH_NEGATION = 0.20
CONFLICT_PENALTY_PLAIN = 0.15
# 冲突确认阈值：top-2 置信度差距 < 该值则触发人工确认，避免自动采用摇摆值。
CONFLICT_CONFIRM_GAP = 0.15


def has_negation(statement_text: str | None) -> bool:
    if not statement_text:
        return False
    for pattern in NEGATION_PATTERNS_ZH + NEGATION_PATTERNS_EN:
        if re.search(pattern, statement_text, flags=re.IGNORECASE):
            return True
    return False


def calculate_confidence(
    source: str,
    value: str,
    n_observations: int = 1,
    updated_at: datetime | None = None,
    statement_text: str | None = None,
    all_sources_for_key: list[dict[str, Any]] | None = None,
    prev_confidence: float | None = None,
    ema_lambda: float = 0.7,
) -> float:
    """confidence = clamp(S_base + B_freq - D_time + B_neg - P_conflict + B_converge, 0, 1)."""
    base = BASE_SCORES.get(source, 0.30)

    freq_boost = 0.0
    if source == "repeated_behavior":
        freq_boost = min((n_observations - 3) * 0.03, 0.15)
    elif source == "single_observation":
        freq_boost = min((n_observations - 1) * 0.03, 0.15)
    freq_boost = max(freq_boost, 0.0)

    time_decay = 0.0
    if source != "system_default":
        ts = _ensure_utc(updated_at)
        days = (datetime.now(UTC) - ts).days
        if days > 30:
            time_decay = min((days - 30) * 0.005, 0.30)

    negation_boost = 0.0
    if source == "explicit_statement" and has_negation(statement_text):
        negation_boost = 0.05

    conflict_penalty = 0.0
    if all_sources_for_key:
        values = {str(s.get("value")) for s in all_sources_for_key}
        if len(values) > 1:
            has_negation_conflict = any(
                s.get("negation") or has_negation(s.get("statement_text")) for s in all_sources_for_key
            )
            conflict_penalty = CONFLICT_PENALTY_WITH_NEGATION if has_negation_conflict else CONFLICT_PENALTY_PLAIN

    converge_boost = 0.0
    if all_sources_for_key and len(all_sources_for_key) >= 2:
        same_value_sources = [s for s in all_sources_for_key if str(s.get("value")) == str(value)]
        independent_groups = {
            INDEPENDENT_GROUPS.get(str(s.get("source")), str(s.get("source"))) for s in same_value_sources
        }
        n_independent = len(independent_groups)
        if n_independent >= 3:
            converge_boost = CONVERGE_BOOST_STRONG
        elif n_independent == 2:
            converge_boost = CONVERGE_BOOST_WEAK

    raw = base + freq_boost - time_decay + negation_boost - conflict_penalty + converge_boost

    # 指数滑动平均（EMA）平滑：同值重复观察时置信度平滑趋稳（参考 PAMU SW+EMA），
    # 改口（value 变化）时调用方应传 prev_confidence=None，从新原始分重新计，
    # 从而平滑过渡到新偏好而非被旧值硬抬高。
    if prev_confidence is not None and 0.0 <= float(prev_confidence) <= 1.0:
        raw = ema_lambda * raw + (1.0 - ema_lambda) * float(prev_confidence)

    return max(0.0, min(1.0, raw))


def detect_conflict_and_suggest(key: str, sources: list[dict[str, Any]]) -> str | None:
    """Return confirmation prompt when top-2 confidence gap < 0.15."""
    if len(sources) < 2:
        return None

    scored: list[tuple[float, str, str]] = []
    for s in sources:
        conf = calculate_confidence(
            source=str(s["source"]),
            value=str(s["value"]),
            n_observations=int(s.get("n_observations", 1)),
            updated_at=_ensure_utc(s.get("updated_at")),
            statement_text=s.get("statement_text"),
            all_sources_for_key=sources,
        )
        scored.append((conf, str(s["value"]), str(s["source"])))

    scored.sort(reverse=True, key=lambda x: x[0])
    best_conf, best_val, best_src = scored[0]
    second_conf, second_val, second_src = scored[1]
    gap = best_conf - second_conf
    if gap < CONFLICT_CONFIRM_GAP:
        return (
            f"偏好 '{key}' 存在冲突信号：\n"
            f"  - 来源 '{best_src}' 推荐值 '{best_val}'（置信度 {best_conf:.2f}）\n"
            f"  - 来源 '{second_src}' 推荐值 '{second_val}'（置信度 {second_conf:.2f}）\n"
            f"  差距 {gap:.2f} < {CONFLICT_CONFIRM_GAP:.2f}，请确认使用哪个值。"
        )
    return None


def map_data_source_to_confidence_source(data_source: str, n_observations: int = 1) -> str:
    """Map ingestion / RawRecord source to confidence source tiers."""
    s = (data_source or "").lower()
    if s in {"manual_config", "manual"}:
        return "manual_config"
    if s in {"conversation", "chat", "explicit_statement"}:
        return "explicit_statement"
    if s in {"user_behavior", "behavior", "tool_result", "agent_log"}:
        return "repeated_behavior" if n_observations >= 3 else "single_observation"
    if s in {"cross_scene_transfer"}:
        return "cross_scene_transfer"
    if s in {"system_default"}:
        return "system_default"
    return "explicit_statement"
