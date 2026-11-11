from src.common.kylin_runtime import discover_bridge_library, glibc_version, under_pytest


def test_pytest_is_detected():
    assert under_pytest() is True


def test_glibc_version_is_a_pair():
    major, minor = glibc_version()
    assert major >= 0 and minor >= 0


def test_discover_bridge_library_requires_real_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OS_AGENT_KYLIN_VECTOR_LIBRARY", raising=False)
    assert discover_bridge_library(tmp_path) is None
    missing = tmp_path / "missing.so"
    monkeypatch.setenv("OS_AGENT_KYLIN_VECTOR_LIBRARY", str(missing))
    assert discover_bridge_library(tmp_path) is None
    present = tmp_path / "bridge.so"
    present.write_bytes(b"x")
    monkeypatch.setenv("OS_AGENT_KYLIN_VECTOR_LIBRARY", str(present))
    assert discover_bridge_library(tmp_path) == present
