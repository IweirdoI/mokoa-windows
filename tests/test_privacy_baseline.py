"""Synthetic CPU-only regression corpus; no model or personal data required."""
import json
from pathlib import Path

import pytest

from src.security.service import redact_sensitive_text
from src.security.service import SecurityAndForgetService
from src.security.semantic_detector import SemanticDetectionUnavailable, SemanticPiiDetector

CASES = json.loads((Path(__file__).parent / "fixtures/privacy_baseline.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_privacy_baseline(case):
    result = redact_sensitive_text(case["text"])
    if "secret" in case:
        assert case["secret"] not in result
    else:
        assert result == case["text"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_storage_matches_export_and_restores(case, tmp_path):
    service = SecurityAndForgetService(index_path=str(tmp_path / "index.json"))
    result, _, _ = service.filter_sensitive(case["text"])
    if "secret" in case:
        assert case["secret"] not in result
    else:
        assert result == case["text"]
    assert service.restore_sensitive(result) == case["text"]


@pytest.mark.parametrize("output", ["", "not JSON", "{}", '{"matches":null}',
    '{"matches":[{"type":"name","text":"小明","confidence":NaN}]}',
    '{"matches":[{"type":"name","text":"小明","confidence":2}]}',
    '{"matches":[{"type":"name","text":"小明","confidence":0.1}]}',
    '{"matches":[{"type":"unexpected","text":"小明","confidence":0.9}]}',
    '{"matches":[{"type":"name","text":"不存在","confidence":0.9}]}'])
def test_invalid_detection_blocks_without_leaking(output, monkeypatch, caplog):
    detector = SemanticPiiDetector()
    monkeypatch.setattr(detector, "_call_llm", lambda _: output)
    with pytest.raises(SemanticDetectionUnavailable):
        detector.detect("小明", strict=True)
    assert "小明" not in caplog.text
    assert "不存在" not in caplog.text


def test_timeout_blocks_before_index_mutation(tmp_path, monkeypatch, caplog):
    detector = SemanticPiiDetector()
    def timeout(_):
        raise TimeoutError("private-request-body")
    monkeypatch.setattr(detector, "_call_llm", timeout)
    service = SecurityAndForgetService(index_path=str(tmp_path / "index.json"), semantic_detector=detector)
    with pytest.raises(SemanticDetectionUnavailable):
        service.filter_sensitive("手机13812345678")
    assert service._index.entries == {}
    assert "private-request-body" not in caplog.text


def test_valid_empty_is_distinct_from_failure(monkeypatch):
    detector = SemanticPiiDetector()
    monkeypatch.setattr(detector, "_call_llm", lambda _: '{"matches":[]}')
    assert detector.detect("正常内容", strict=True) == []


def test_export_semantic_has_no_raw_index(tmp_path, monkeypatch):
    detector = SemanticPiiDetector()
    monkeypatch.setattr(detector, "_call_llm", lambda _: '{"matches":[{"type":"name","text":"小明","confidence":0.9}]}')
    service = SecurityAndForgetService(index_path=str(tmp_path / "index.json"), semantic_detector=detector)
    assert service.redact_for_export("我叫小明") == "我叫[SEMANTIC_REDACTED]"
    assert service._index.entries == {}


def test_multiple_patterns_of_same_type_all_redacted(tmp_path):
    service = SecurityAndForgetService(index_path=str(tmp_path / "index.json"))
    original = "工号是NO.9527，员工编号: ABCD1234"
    filtered, types, _ = service.filter_sensitive(original)
    assert "NO.9527" not in filtered and "ABCD1234" not in filtered
    assert types.count("employee_id") == 1
    assert service.restore_sensitive(filtered) == original


def test_api_detection_failure_returns_safe_503(monkeypatch):
    from fastapi.testclient import TestClient
    from api import server
    def fail(_):
        raise SemanticDetectionUnavailable("private-backend-error")
    monkeypatch.setattr(server.orchestrator, "store_memory_entry", fail)
    response = TestClient(server.app).post("/store-entry", json={"entry": {"user_id":"privacy-test", "content":"hello", "memory_type":"project_context", "source":"manual"}})
    assert response.status_code == 503
    assert "private-backend-error" not in response.text


def test_export_preserves_existing_hash_placeholders():
    text = "编号 <HASH_12345678>"
    assert redact_sensitive_text(text) == text
