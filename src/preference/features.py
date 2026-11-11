"""Structured preference features — recommender-system inspired.

Avoid MemoryOS free-text profile blobs. Agent / policy engines should consume
typed feature maps (key→value, confidence, recency) rather than RAG prose alone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.preference.keys import PREFERENCE_KEYS
from src.preference.models import PreferenceRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def canonical_embed_text(preference_key: str, value: str, description: str = "") -> str:
    """
    Stable, structured content for vector stores.

    Prefer key=value over free-form description so embedding refresh tracks
    value changes (MemoryOS session summary stale-embedding anti-pattern).
    """
    base = f"[preference] {preference_key}={value}"
    if description and description.strip() and description.strip() != base:
        return f"{base} | {description.strip()[:400]}"
    return base


def days_since(ts: datetime | None) -> float:
    if ts is None:
        return 0.0
    now = _utc_now()
    return max(0.0, (now - _ensure_utc(ts)).total_seconds() / 86400.0)


def record_to_feature(rec: PreferenceRecord) -> dict[str, Any]:
    """Single preference as a recommender feature row."""
    return {
        "feature_id": f"{rec.preference_key}::{rec.scope}::{rec.scene_id or 'global'}",
        "preference_key": rec.preference_key,
        "category": rec.category,
        "value": rec.value,
        "value_type": PREFERENCE_KEYS.get(rec.preference_key, {}).get("type", "string"),
        "confidence": rec.confidence,
        "n_observations": rec.n_observations,
        "recency_days": round(days_since(rec.updated_at), 3),
        "version": rec.current_version,
        "scope": rec.scope,
        "scene_id": rec.scene_id,
        "source": rec.source,
        # soft score for ranking: confidence × log-ish frequency × recency decay proxy
        "rank_score": round(
            rec.confidence * (1.0 + 0.05 * min(rec.n_observations, 20)) * max(0.4, 1.0 - 0.005 * days_since(rec.updated_at)),
            4,
        ),
    }


def build_user_feature_profile(
    records: list[PreferenceRecord],
    *,
    scene_id: str | None = None,
) -> dict[str, Any]:
    """
    Build a structured user preference profile for OS Agent injection.

    Resolution: for each preference_key, prefer scene match over global
    (same rule as version_index.resolve_read).
    """
    by_key: dict[str, PreferenceRecord] = {}
    for rec in records:
        # 解析优先级：scene（同场景）> global；与 version_index.resolve_read 一致。
        # （下方第三分支「scene_id 且 global」为不可达死代码，已删除，避免维护误导。）
        if scene_id and rec.scope == "scene" and rec.scene_id == scene_id:
            by_key[rec.preference_key] = rec
        elif rec.preference_key not in by_key and (
            rec.scope == "global" or (scene_id is None and rec.scope == "scene")
        ):
            by_key[rec.preference_key] = rec

    # second pass: fill globals for missing keys when scene_id set
    if scene_id:
        for rec in records:
            if rec.scope == "global" and rec.preference_key not in by_key:
                by_key[rec.preference_key] = rec

    features = [record_to_feature(r) for r in by_key.values()]
    features.sort(key=lambda x: x["rank_score"], reverse=True)

    # dense-ish categorical map for policy engines
    kv: dict[str, str] = {f["preference_key"]: f["value"] for f in features}
    conf: dict[str, float] = {f["preference_key"]: f["confidence"] for f in features}

    return {
        "schema": "preference_feature_profile_v1",
        "scene_id": scene_id,
        "n_features": len(features),
        "kv": kv,
        "confidence": conf,
        "features": features,
        # prompt-friendly structured block (NOT MemoryOS prose blob)
        "agent_context": {
            k: {"value": kv[k], "confidence": conf[k]} for k in sorted(kv)
        },
    }
