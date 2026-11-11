"""Local preference version index (layer 1). Path B — not Milvus.

Industrial notes (avoid MemoryOS I/O anti-pattern):
  - Mutations mark dirty; disk write only on flush() / extract batch end
  - Atomic replace (tmp + os.replace) to avoid corrupt JSON on crash
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.preference.keys import MAX_VERSION_CHAIN, PreferenceScope, index_key, split_preference_key
from src.preference.models import PreferenceRecord, PreferenceVersion


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PreferenceVersionIndex:
    """
    In-memory + batched JSON persistence under data/preferences/{user_id}.json.

    Used by:
      - get_version_history() before Orchestrator create_memory
      - extract() to assign next version / is_latest
    """

    def __init__(self, storage_dir: str | Path | None = None, *, auto_persist: bool = False) -> None:
        root = Path(storage_dir) if storage_dir else Path("data") / "preferences"
        self.storage_dir = root
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # user_id -> { index_key -> PreferenceRecord }
        self._data: dict[str, dict[str, PreferenceRecord]] = {}
        self._dirty: set[str] = set()
        # auto_persist=False: industrial default (batch flush). True: legacy per-write (tests/debug).
        self.auto_persist = auto_persist

    def _path(self, user_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.storage_dir / f"{safe}.json"

    def load(self, user_id: str) -> None:
        if user_id in self._data:
            return
        path = self._path(user_id)
        if not path.exists():
            self._data[user_id] = {}
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        records: dict[str, PreferenceRecord] = {}
        for k, v in (raw.get("records") or {}).items():
            records[k] = PreferenceRecord.model_validate(v)
        self._data[user_id] = records

    def mark_dirty(self, user_id: str) -> None:
        self._dirty.add(user_id)

    def persist(self, user_id: str) -> None:
        """Write one user index atomically. Prefer flush() for batches.

        调用方负责保证 self._data[user_id] 已是最新内存状态（否则会落盘陈旧数据）；
        因此此处不再 self.load，避免用磁盘覆盖未落盘的改动。
        抽取结果为空时 extract() 仍会 flush，此时内存里没有该用户，直接跳过。
        """
        records = self._data.get(user_id)
        if records is None:
            self._dirty.discard(user_id)
            return
        payload = {
            "user_id": user_id,
            "updated_at": _utc_now().isoformat(),
            "records": {k: v.model_dump(mode="json") for k, v in records.items()},
        }
        path = self._path(user_id)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._dirty.discard(user_id)

    def flush(self, user_id: str | None = None) -> int:
        """Flush dirty users (or one user). Returns number of files written."""
        targets = [user_id] if user_id is not None else list(self._dirty)
        n = 0
        for uid in targets:
            if uid is None:
                continue
            if user_id is not None or uid in self._dirty:
                if uid not in self._data:
                    self._dirty.discard(uid)
                    continue
                self.persist(uid)
                n += 1
        return n

    def get_record(
        self,
        user_id: str,
        preference_key: str,
        scope: str = "global",
        scene_id: str | None = None,
    ) -> PreferenceRecord | None:
        self.load(user_id)
        return self._data[user_id].get(index_key(preference_key, scope, scene_id))

    def list_history(
        self,
        user_id: str,
        preference_key: str,
        scene_id: str | None = None,
    ) -> list[PreferenceRecord]:
        """Return matching records (scene first, then global if scene_id given)."""
        self.load(user_id)
        out: list[PreferenceRecord] = []
        if scene_id:
            rec = self.get_record(user_id, preference_key, PreferenceScope.SCENE.value, scene_id)
            if rec:
                out.append(rec)
        rec_g = self.get_record(user_id, preference_key, PreferenceScope.GLOBAL.value, None)
        if rec_g and rec_g not in out:
            out.append(rec_g)
        if scene_id is None:
            for rec in self._data[user_id].values():
                if rec.preference_key == preference_key and rec not in out:
                    out.append(rec)
        return out

    def apply_value(
        self,
        user_id: str,
        preference_key: str,
        value: str,
        *,
        description: str = "",
        source: str = "explicit_statement",
        confidence: float = 0.92,
        scope: str = "scene",
        scene_id: str | None = "general",
        changed_by: str = "user",
        reason: str = "extracted",
        memory_id: str | None = None,
        n_observations: int | None = None,
        persist: bool | None = None,
    ) -> PreferenceRecord:
        """Upsert value; bump version only when value changes. Default: dirty only, no disk I/O."""
        self.load(user_id)
        if scope == PreferenceScope.GLOBAL.value:
            scene_id = None
        ik = index_key(preference_key, scope, scene_id)
        category, key = split_preference_key(preference_key)
        now = _utc_now()
        existing = self._data[user_id].get(ik)
        do_persist = self.auto_persist if persist is None else persist

        if existing is None:
            ver = PreferenceVersion(
                version=1,
                value=value,
                description=description,
                source=source,
                confidence=confidence,
                changed_at=now,
                changed_by=changed_by,
                reason=reason,
                memory_id=memory_id,
            )
            rec = PreferenceRecord(
                id=f"{preference_key.replace('.', '_')}_v1_{uuid4().hex[:8]}",
                category=category,
                key=key,
                preference_key=preference_key,
                scope=scope,
                scene_id=scene_id,
                value=value,
                description=description,
                source=source,
                confidence=confidence,
                current_version=1,
                versions=[ver],
                n_observations=n_observations or 1,
                created_at=now,
                updated_at=now,
            )
            self._data[user_id][ik] = rec
            self.mark_dirty(user_id)
            if do_persist:
                self.persist(user_id)
            return rec

        # same value: reinforce observation count / confidence (no version bump)
        if existing.value == value:
            existing.description = description or existing.description
            existing.source = source
            existing.confidence = confidence
            existing.updated_at = now
            if n_observations is not None:
                existing.n_observations = max(existing.n_observations, n_observations)
            else:
                existing.n_observations += 1
            if existing.versions:
                existing.versions[-1].confidence = confidence
                existing.versions[-1].description = description or existing.versions[-1].description
            self.mark_dirty(user_id)
            if do_persist:
                self.persist(user_id)
            return existing

        new_version = existing.current_version + 1
        ver = PreferenceVersion(
            version=new_version,
            value=value,
            description=description,
            source=source,
            confidence=confidence,
            changed_at=now,
            changed_by=changed_by,
            reason=reason,
            memory_id=memory_id,
        )
        existing.versions.append(ver)
        if len(existing.versions) > MAX_VERSION_CHAIN:
            existing.versions = existing.versions[-MAX_VERSION_CHAIN:]
        existing.current_version = new_version
        existing.value = value
        existing.description = description
        existing.source = source
        existing.confidence = confidence
        existing.n_observations = n_observations or 1
        existing.updated_at = now
        existing.id = f"{preference_key.replace('.', '_')}_v{new_version}"
        self.mark_dirty(user_id)
        if do_persist:
            self.persist(user_id)
        return existing

    def delete(
        self,
        user_id: str,
        preference_key: str,
        scope: str = "scene",
        scene_id: str | None = None,
        *,
        persist: bool = True,
    ) -> bool:
        """按 index_key 删除偏好记录（含版本链），返回是否真实删除。

        供遗忘链路同步清理版本索引，保证偏好画像与向量存储一致。
        """
        self.load(user_id)
        ik = index_key(preference_key, scope, scene_id)
        removed = self._data.get(user_id, {}).pop(ik, None) is not None
        if removed:
            self.mark_dirty(user_id)
            if persist:
                self.persist(user_id)
        return removed

    def rollback(
        self,
        user_id: str,
        preference_key: str,
        to_version: int,
        *,
        scope: str = "scene",
        scene_id: str | None = "general",
        reason: str | None = None,
    ) -> PreferenceRecord:
        rec = self.get_record(user_id, preference_key, scope, scene_id)
        if rec is None:
            raise KeyError(f"No preference {preference_key} for user {user_id}")
        target = next((v for v in rec.versions if v.version == to_version), None)
        if target is None:
            raise KeyError(f"Version {to_version} not found for {preference_key}")
        out = self.apply_value(
            user_id,
            preference_key,
            target.value,
            description=target.description,
            source="explicit_statement",
            confidence=0.92,
            scope=scope,
            scene_id=scene_id,
            reason=reason or f"rollback to v{to_version}",
            persist=False,
        )
        self.flush(user_id)
        return out

    def resolve_read(
        self,
        user_id: str,
        preference_key: str,
        scene_id: str | None,
    ) -> PreferenceRecord | None:
        """scene > global read priority."""
        if scene_id:
            rec = self.get_record(user_id, preference_key, PreferenceScope.SCENE.value, scene_id)
            if rec:
                return rec
        return self.get_record(user_id, preference_key, PreferenceScope.GLOBAL.value, None)

    def export_user(self, user_id: str) -> dict[str, Any]:
        self.load(user_id)
        return {k: v.model_dump(mode="json") for k, v in self._data.get(user_id, {}).items()}

    def all_records(self, user_id: str) -> list[PreferenceRecord]:
        self.load(user_id)
        return list(self._data.get(user_id, {}).values())
