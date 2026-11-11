"""Preference domain models (local version index)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PreferenceVersion(BaseModel):
    version: int
    value: str
    description: str = ""
    source: str = "explicit_statement"
    confidence: float = 0.92
    changed_at: datetime = Field(default_factory=_utc_now)
    changed_by: str = "user"
    reason: str = ""
    memory_id: str | None = None  # filled after Orchestrator create_memory (optional)


class PreferenceRecord(BaseModel):
    """Local version-index record — mirrors preference-keys-enum.md §1.1."""

    id: str
    category: str
    key: str
    preference_key: str  # category.key
    scope: str = "global"
    scene_id: str | None = None
    value: str
    description: str = ""
    source: str = "explicit_statement"
    confidence: float = 0.92
    current_version: int = 1
    versions: list[PreferenceVersion] = Field(default_factory=list)
    n_observations: int = 1  # recommender-style frequency signal
    conflict_group: str | None = None
    ttl: int | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedPreference(BaseModel):
    """Intermediate extraction result before MemoryEntry conversion."""

    category: str
    key: str
    value: str
    description: str = ""
    scope: str = "scene"
    scene_id: str | None = "general"
    source: str = "explicit_statement"
    n_observations: int = 1
    statement_text: str | None = None
    reason: str = "extracted"
    confidence_source: str | None = None  # override for confidence formula
