"""Guard/report tests only; these are not V11 SDK acceptance evidence."""
import pytest
from src.common.kylin_runtime import enforce_acceptance
from scripts.kylin_v11_full_acceptance import check_health, final_status


@pytest.mark.parametrize("key,value", [("OS_AGENT_MEMORY_BACKEND", "memory"), ("OS_AGENT_MEMORY_BACKEND", "milvus"),
    ("OS_AGENT_EMBEDDING_BACKEND", "local"), ("OS_AGENT_EMBEDDING_BACKEND", "hash"),
    ("EMBEDDING_BACKEND", "BGE"), ("OS_AGENT_KYLIN_STRICT", "0")])
def test_reject_substitution(monkeypatch, key, value):
    monkeypatch.setenv("OS_AGENT_KYLIN_ACCEPTANCE", "1")
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        enforce_acceptance()


def test_explicit_embedding_override_rejected(monkeypatch):
    for key in ("OS_AGENT_MEMORY_BACKEND", "OS_AGENT_EMBEDDING_BACKEND", "OS_AGENT_KYLIN_STRICT", "EMBEDDING_BACKEND"):
        monkeypatch.setenv(key, "1" if key == "OS_AGENT_KYLIN_STRICT" else "kylin")
    monkeypatch.setenv("OS_AGENT_KYLIN_ACCEPTANCE", "1")
    from src.embedding.embedding_interface import create_embedding_backend
    with pytest.raises(RuntimeError, match="system embedding"):
        create_embedding_backend("hash")


def test_health_requires_probe_and_process_identity():
    healthy = dict(status="ok", memory_backend="kylin", embedding_backend="kylin", kylin_strict="1",
                   using_fallback=False, pid=123, run_id="test", vector_dim=768, vector_sdk={"backend": "kylin-vector-sdk"})
    check_health(healthy, 123, "test")
    for field in healthy:
        broken = healthy.copy()
        broken.pop(field)
        with pytest.raises(RuntimeError):
            check_health(broken, 123, "test")
    with pytest.raises(RuntimeError):
        check_health(healthy, 456, "test")


def test_incomplete_never_passes():
    assert final_status({"browser": {"status": "pending"}}) == ("incomplete", 2)
    assert final_status({"browser": {"status": "pending"}, "http": {"status": "failed"}}) == ("failed", 1)
    assert final_status({"http": {"status": "passed"}}) == ("passed", 0)
