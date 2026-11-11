"""Malformed public requests should fail validation before model execution."""
import pytest
from fastapi.testclient import TestClient
from api.server import app


@pytest.mark.parametrize('path', ['/short-term/validation_user', '/mid-term/validation_user'])
def test_invalid_layer_memory_type_is_validation_error(path):
    with TestClient(app) as client:
        assert client.get(path, params={'memory_type': 'invalid'}).status_code == 422


@pytest.mark.parametrize('extra', [
    {'memory_type': 'invalid'}, {'top_k': 0}, {'top_k': -1}, {'top_k': 101}, {'retrieval_mode': 'invalid'},
])
def test_invalid_recall_arguments_are_validation_errors(extra):
    with TestClient(app) as client:
        assert client.post('/recall', json={'user_id': 'validation_user', **extra}).status_code == 422


def test_hybrid_mode_is_forwarded_to_orchestrator(monkeypatch):
    from api import server
    from src.common.models import RetrievalResult
    seen = []
    def recall(query, **kwargs):
        seen.append(query.retrieval_mode)
        return RetrievalResult(items=[], latency_ms=0, strategy="query_memory+lexical")
    monkeypatch.setattr(server.orchestrator, "recall", recall)
    with TestClient(app) as client:
        response = client.post('/recall', json={
            'user_id': 'validation_user', 'query': 'printer', 'retrieval_mode': 'hybrid',
        })
    assert response.status_code == 200
    assert seen == ['hybrid']
