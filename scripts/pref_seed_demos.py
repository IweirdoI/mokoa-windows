"""Seed 3–5 fixed frontend demo preference users via extract → store path.

Offline by default (rule mode). Optionally hit a running API:

  python scripts/pref_seed_demos.py
  python scripts/pref_seed_demos.py --via-api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_PATH = ROOT / "benchmarks" / "datasets" / "preference_demo_users.json"


def seed_local() -> dict:
    """Offline seed via PreferenceMemoryStore (no Milvus required)."""

    from src.preference.store import PreferenceMemoryStore

    demos = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="pref_demo_") as tmp:
        store = PreferenceMemoryStore(
            storage_dir=tmp,
            use_llm=False,
            allow_rules=True,
        )
        report = []
        for demo in demos:
            user_id = demo["user_id"]
            scene_id = demo.get("scene_id")
            stored = []
            for text in demo.get("seed_texts") or []:
                entries = store.extract_from_text(
                    text,
                    user_id=user_id,
                    scene_id=scene_id,
                    source="conversation",
                )
                stored.append(
                    {
                        "keys": [
                            {
                                "preference_key": e.metadata.get("preference_key"),
                                "preference_value": e.metadata.get("preference_value"),
                                "version": e.metadata.get("version"),
                            }
                            for e in entries
                        ]
                    }
                )
            history = []
            hist_key = demo.get("history_key")
            if hist_key:
                history = [
                    {
                        "version": item.version,
                        "preference_value": item.metadata.get("preference_value"),
                        "is_latest": item.metadata.get("is_latest"),
                    }
                    for item in store.get_version_history(user_id, hist_key)
                ]
            report.append(
                {
                    "user_id": user_id,
                    "label": demo.get("label"),
                    "stored": stored,
                    "profile": store.get_feature_profile(user_id, scene_id=scene_id),
                    "history": history,
                }
            )
        return {"mode": "local_rule", "users": report}


def seed_via_api(base_url: str) -> dict:
    import urllib.error
    import urllib.request

    demos = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    report = []
    for demo in demos:
        user_id = demo["user_id"]
        scene_id = demo.get("scene_id")
        stored = []
        for text in demo.get("seed_texts") or []:
            body = json.dumps(
                {
                    "user_id": user_id,
                    "text": text,
                    "scene_id": scene_id,
                    "source": "conversation",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/preferences/extract",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    stored.append(json.loads(resp.read().decode("utf-8")))
            except urllib.error.URLError as exc:
                stored.append({"error": str(exc)})
        report.append({"user_id": user_id, "label": demo.get("label"), "stored": stored})
    return {"mode": "api", "base_url": base_url, "users": report}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--via-api", default=None, help="API base URL")
    args = parser.parse_args()
    result = seed_via_api(args.via_api) if args.via_api else seed_local()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
