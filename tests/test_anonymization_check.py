"""匿名化自检规则单测：只验证纯函数 scan_text，不依赖 git 与真实仓库文件。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_anonymization.py"
_spec = importlib.util.spec_from_file_location("check_anonymization", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
check_anonymization = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_anonymization)

scan_text = check_anonymization.scan_text


def test_detects_windows_user_absolute_path() -> None:
    hits = scan_text(r'sys.path.insert(0, r"c:\Users\alice\code\MOKOA")')
    assert any(hit.startswith("windows-user-path:") for hit in hits)


def test_detects_unix_home_paths() -> None:
    linux = scan_text("--trec-collection /home/alice/projects/MOKOA/git/TREC")
    assert any(hit.startswith("unix-home-path:") for hit in linux)
    macos = scan_text("path: /Users/alice/work/collection.tsv")
    assert any(hit.startswith("unix-home-path:") for hit in macos)


def test_template_and_placeholder_paths_are_not_flagged() -> None:
    """模板变量与占位符不是个人信息，自检不应误报。"""

    assert scan_text('"default": "C:/Users/{user}/Downloads"') == []
    assert scan_text("--trec-collection <repo>/git/MS_MARCO/passage_ranking/collection.tsv") == []
    assert scan_text("见 `/home/<linux-user>/projects/MOKOA` 与 `/Users/<user>/x`") == []


def test_forbidden_host_is_injected_not_hardcoded() -> None:
    """主机名必须由调用方注入，仓库内不得硬编码。"""

    assert scan_text("run on host-a only") == []
    assert "forbidden-host: host-a" in scan_text("run on host-a only", ("host-a",))


def test_clean_text_produces_no_findings() -> None:
    assert scan_text("普通说明：调用 /api/ingest，默认下载目录 C:/Users/{user}/Downloads。") == []
