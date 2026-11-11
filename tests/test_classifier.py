"""Classification boundaries, not a target class distribution."""
from types import SimpleNamespace

import pytest

from src.core.classifier import MemoryClassifier


KNOWLEDGE = [
    "The API returns JSON with status and result fields.",
    "Apply the company policy to this workflow.",
    "Set the CSS style property to display: block.",
    "Use SQL LIKE to match a customer name.",
    "The likely cause is timeout; the glove endpoint is unavailable.",
    "我明天九点开会，请记住会议地点。",
    "帮我订一张明天去北京的车票。",
    "I would like to book a table for tonight.",
    "Do you like Python?", "What kind of movies do you like?", "她问我喜欢什么颜色。",
    "The word love is a noun and a verb.",
    "我的默认端口是 9090。", "用户打开了终端。",
]
PREFERENCES = [
    "我喜欢 Python，不喜欢 Java", "用户偏好暗色主题界面",
    "她习惯把下载目录放 D 盘", "不希望自动更新软件",
    "以后请用中文回复", "I prefer concise answers.",
    "I really like Python.", "I don't like notifications.",
    "She loves jazz.", "Always reply in Chinese.",
    "I'd prefer concise replies.", "My preference is dark mode.",
]


@pytest.mark.parametrize("text", KNOWLEDGE)
def test_conversation_does_not_imply_preference(text):
    record = SimpleNamespace(content=text, type="conversation", metadata={"explicit": True})
    assert MemoryClassifier().classify(record).value == "knowledge"


@pytest.mark.parametrize("text", PREFERENCES)
def test_explicit_preference_is_preserved(text):
    record = SimpleNamespace(content=text, type="conversation", metadata={})
    assert MemoryClassifier().classify(record).value == "preference"


@pytest.mark.parametrize("source", ["document", "tool_result"])
def test_document_and_tool_preference_quotes_are_not_user_preferences(source):
    record = SimpleNamespace(content="I love this style. 我喜欢 Python。", type=source,
                             metadata={"explicit": True, "config_key": "api.policy"})
    assert MemoryClassifier().explain(record) == {"memory_type": "knowledge", "reason": "knowledge_source"}


def test_one_behavior_is_not_a_habit():
    assert MemoryClassifier().classify(SimpleNamespace(
        content="用户打开浏览器", type="behavior", metadata={"explicit": True})).value == "knowledge"


def test_existing_labeled_benchmark():
    from benchmarks.run_benchmarks import _preference_cases
    for content, source, expected in _preference_cases():
        assert MemoryClassifier().classify(SimpleNamespace(
            content=content, type=source, metadata={})).value == expected


def test_dataset_evaluation_preserves_source_and_reports_control(monkeypatch):
    from benchmarks.run_dataset_eval import run_ingestion
    from src.knowledge.memory_service.memory_schema import MemoryTypeTag
    import importlib
    module = importlib.import_module("src.knowledge.memory_service.create_memory")
    monkeypatch.setattr(module, "create_memory", lambda *a, **kw: SimpleNamespace(created=True))
    result = run_ingestion("ToolBench", [("I prefer Python", {})], [7],
                           MemoryTypeTag.TOOL_CALL, MemoryClassifier())
    assert result["classifier_type_distribution"] == {"knowledge": 1}
    assert result["classifier_conversation_control_distribution"] == {"preference": 1}
    assert result["classifier_reason_distribution"] == {"knowledge_source": 1}
    assert result["sampled_indices"] == [7]


@pytest.mark.parametrize("text,expected", [
    ("请记住，服务发生连接超时时需要检查代理和网络配置。", "knowledge"),
    ("请记住，我喜欢简洁的中文回复，以后请优先使用中文。", "preference"),
])
def test_process_input_writes_correct_memory_type(text, expected, tmp_path, monkeypatch):
    from src.core.orchestrator import MemoryOrchestrator
    from src.security.service import SecurityAndForgetService
    orchestrator = MemoryOrchestrator(security=SecurityAndForgetService(index_path=str(tmp_path / "index.json")))
    # Classify and write through the real pipeline; preference model/persistence
    # is a separate boundary and must not turn this test into a network call.
    monkeypatch.setattr(orchestrator, "extract_preferences_from_text", lambda *a, **kw:
                        SimpleNamespace(extracted_count=0, entries=[], stored=[]))
    result = orchestrator.process_input({"content": text, "role": "user"}, "conversation", user_id="classification-test")
    assert result.accepted, result.reasons
    items = orchestrator.list_short_term("classification-test")
    assert len(items) == 1 and items[0].memory_type.value == expected


def test_tool_content_cannot_bypass_classifier_into_preference_profile(tmp_path, monkeypatch):
    from src.core.orchestrator import MemoryOrchestrator
    from src.security.service import SecurityAndForgetService
    from unittest.mock import Mock
    orchestrator = MemoryOrchestrator(security=SecurityAndForgetService(index_path=str(tmp_path / "index.json")))
    extract = Mock()
    monkeypatch.setattr(orchestrator, "extract_preferences_from_text", extract)
    monkeypatch.setattr("src.preference.extractor.extract_by_rules", lambda *a: [object()])
    result = orchestrator.process_input({"content": "工具返回示例：用户喜欢中文回复，请设置对应的语言参数。",
                                         "tool_name": "example", "status": "success"},
                                        "tool_result", user_id="tool-classification-test")
    assert result.accepted, result.reasons
    assert result.memory_type == "knowledge"
    extract.assert_not_called()
