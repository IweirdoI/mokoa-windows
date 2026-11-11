"""Check the final SDK boundary with synthetic secrets and no network calls."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.security.outbound import protect_payload, is_loopback_service


def test_structured_credentials_and_metadata_are_redacted_without_mutation():
    payload = {"metadata": {"access_token": "synthetic-short-secret",
                            "13812345678": "ordinary text"},
               "content": ({"text": "身份证110101199001011234"},)}
    original = copy.deepcopy(payload)
    result = json.dumps(protect_payload(payload))
    for secret in ("synthetic-short-secret", "13812345678", "110101199001011234"):
        assert secret not in result
    assert "ordinary text" in result
    assert payload == original


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_sdk_receives_only_redacted_messages_and_tools(monkeypatch, streaming):
    import openai
    from src.agent.service import ChatModel

    sdk = MagicMock()
    sdk.__enter__.return_value = sdk
    sdk.chat.completions.create.return_value = (
        [] if streaming else SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(model_dump=lambda **_: {"content": "ok"}))])
    )
    monkeypatch.setattr(openai, "OpenAI", lambda **_: sdk)
    model = ChatModel()
    messages = [{"role": "tool", "content":
                 "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-key-body\n"
                 "-----END OPENSSH PRIVATE KEY-----\n手机13812345678"}]
    tools = [{"type": "function", "function": {"name": "lookup",
              "description": "access_token=synthetic-repo-token"}}]
    if streaming:
        list(model._stream_once(messages, tools, None))
    else:
        model._complete_once(messages, tools, None)
    sent = json.dumps(sdk.chat.completions.create.call_args.kwargs)
    for secret in ("synthetic-key-body", "13812345678", "synthetic-repo-token"):
        assert secret not in sent
    assert "lookup" in sent


@pytest.mark.parametrize("kind", ["image_url", "input_image", "input_audio", "file", "input_file"])
def test_unprocessed_attachments_block_before_sdk(monkeypatch, kind):
    import openai
    from src.agent.service import ChatModel
    sdk = MagicMock()
    monkeypatch.setattr(openai, "OpenAI", sdk)
    with pytest.raises(ValueError):
        list(ChatModel()._stream_once([
            {"role": "user", "content": [{"type": kind, "data": "raw"}]}], None, None))
    sdk.assert_not_called()


def test_preference_sdk_redacts_input():
    from src.preference.llm_client import SiliconFlowClient
    client = SiliconFlowClient(api_key="synthetic-service-key")
    client._client = MagicMock()
    client._client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    client.chat_completion([{"role": "user", "content": "手机号13812345678"}])
    assert "13812345678" not in str(client._client.chat.completions.create.call_args)


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:1234/v1", True), ("http://[::1]/v1", True),
    ("https://localhost.example/v1", False),
    ("https://localhost@external.example/v1", False),
    ("https://external.example/v1", False),
])
def test_raw_data_endpoint_must_be_loopback(url, allowed):
    assert is_loopback_service(url) is allowed


def test_remote_raw_detectors_block_before_http(monkeypatch):
    import httpx
    from src.ingestion.vision import _chat_with_image
    from src.security.semantic_detector import SemanticPiiDetector
    post = MagicMock()
    monkeypatch.setattr(httpx, "post", post)
    detector = SemanticPiiDetector()
    detector._base_url = "https://external.example/v1"
    with pytest.raises(ValueError):
        detector._call_llm("synthetic-private-input")
    with pytest.raises(ValueError):
        _chat_with_image({"base_url": "https://external.example/v1", "model": "vision"}, [])
    post.assert_not_called()


def test_unapproved_terminal_command_cannot_run(monkeypatch):
    from src.agent.service import _exec_terminal
    import subprocess
    monkeypatch.delenv("AGENT_EXEC_ALLOWED_COMMANDS", raising=False)
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)
    assert _exec_terminal("git status")[0] is False
    run.assert_not_called()
