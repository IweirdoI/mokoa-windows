"""Isolate acceptance state from the normal application's local files."""
import copy
import os
from pathlib import Path
from src.core import factory

state = Path(os.environ["OS_AGENT_ACCEPTANCE_STATE"])
state.mkdir(parents=True, exist_ok=True)
config = copy.deepcopy(factory._load_default_config())
config.setdefault("storage", {}).update(staging_backend="sqlite", staging_sqlite=str(state / "staging.db"))
config.setdefault("preference", {})["storage_dir"] = str(state / "preferences")
config["index_path"] = str(state / "security.json")
factory._load_default_config = lambda: config
from api.server import app  # noqa: E402,F401
