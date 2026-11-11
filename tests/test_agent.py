"""Run the real LangGraph with deterministic model/memory boundaries."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agent.service import (
    ChatModel,
    ChatRequest,
    MemoryAgent,
    ModelUnavailable,
    _exec_terminal,
    _read_file,
    _list_directory,
    _get_current_datetime,
    _calculate,
    _web_fetch,
    _search_files,
    _search_content,
    _write_file,
    _edit_file,
)
from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.security.service import SecurityAndForgetService
from src.security.semantic_detector import SemanticPiiDetector


@pytest.fixture
def memory():
    item = MemoryItem(id="m1", content="偏好中文", user_id="alice",
                      memory_type=MemoryType.PREFERENCE, layer=MemoryLayer.LONG)
    m = Mock()
    m.recall.return_value = SimpleNamespace(items=[item])
    m.get_preference_profile.return_value = {"features": []}
    m.process_input.return_value = SimpleNamespace(accepted=True, memory_id="m2", reasons=[], memory_type="preference")
    m.forget.return_value = ["m1"]
    return m


def model(*answers, tools=False):
    m = Mock(tools_enabled=tools)
    m.complete.side_effect = list(answers) or [{"content": "你好"}]
    return m


def test_recall_before_answer_and_save_user_only(memory):
    llm = model()
    def respond(messages, tools):
        memory.recall.assert_called_once()
        memory.process_input.assert_not_called()
        assert "偏好中文" in str(messages)
        assert "麒忆智脑" in str(messages)
        assert "中期" in str(messages)
        return {"content": "你好"}
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="你好呀"))
    assert result["answer"] == "你好"
    assert result["memory_write"]["status"] == "accepted"
    assert memory.process_input.call_args.args[0] == {"content": "你好呀", "role": "user"}


def test_ablation_skips_memory_and_write(memory):
    llm = model()
    def respond(messages, tools):
        blob = str(messages)
        assert "偏好中文" not in blob
        assert "没有参考记忆" in blob
        return {"content": "当前没有可用记忆"}
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="我的下载目录是什么？", use_memory=False, save_memory=False))
    memory.recall.assert_not_called()
    memory.get_preference_profile.assert_not_called()
    memory.process_input.assert_not_called()
    assert result["memories"] == []
    assert result["answer"] == "当前没有可用记忆"


def test_forget_bypasses_model_and_clears_history(memory):
    llm = model()
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="忘掉中文偏好"))
    assert result["reset_history"] is True
    llm.complete.assert_not_called()
    memory.process_input.assert_not_called()
    memory.forget.assert_called_once_with("alice", "忘掉中文偏好")


def test_model_failure_does_not_write(memory):
    llm = model()
    llm.complete.side_effect = ModelUnavailable("offline")
    with pytest.raises(ModelUnavailable):
        MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="你好"))
    memory.process_input.assert_not_called()


def test_privacy_failure_prevents_model_and_save(memory, tmp_path, monkeypatch):
    detector = SemanticPiiDetector()
    monkeypatch.setattr(detector, "_call_llm", lambda _: "invalid-output")
    memory.security = SecurityAndForgetService(index_path=str(tmp_path / "index.json"), semantic_detector=detector)
    llm = model()
    with pytest.raises(ModelUnavailable, match="敏感信息检测未完成"):
        MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="你好", use_memory=False))
    llm.complete.assert_not_called()
    memory.process_input.assert_not_called()


def test_semantic_protection_covers_history_context_and_tools(memory, tmp_path, monkeypatch):
    detector = SemanticPiiDetector()
    def detect(text):
        return '{"matches":[{"type":"name","text":"小明","confidence":0.9}]}' if "小明" in text else '{"matches":[]}'
    monkeypatch.setattr(detector, "_call_llm", detect)
    memory.security = SecurityAndForgetService(index_path=str(tmp_path / "index.json"), semantic_detector=detector)
    memory.recall.return_value.items[0].content = "联系人小明"
    llm = model({"tool_calls": [{"id":"t1", "type":"function", "function":{"name":"search_memory", "arguments":'{"query":"小明"}'}}]}, {"content":"完成"}, tools=True)
    MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="询问小明", history=[{"role":"user","content":"我叫小明"}], save_memory=False))
    assert len(llm.complete.call_args_list) == 2
    for call in llm.complete.call_args_list:
        assert "小明" not in str(call.args[0])
    assert memory.security._index.entries == {}


def test_write_failure_preserves_answer(memory):
    memory.process_input.side_effect = RuntimeError("private data")
    result = MemoryAgent(memory, model()).chat(ChatRequest(user_id="alice", message="你好"))
    assert result["answer"] == "你好"
    assert result["memory_write"]["status"] == "failed"
    assert "private data" not in str(result)


def test_tools_bound_to_request_user_and_limited(memory):
    call = {"id": "t1", "type": "function", "function": {"name": "search_memory", "arguments": '{"query":"偏好"}'}}
    llm = model({"tool_calls": [call]}, {"tool_calls": [call]}, {"content": "中文回答"}, tools=True)
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="有什么偏好", save_memory=False))
    assert result["answer"] == "中文回答"
    assert all(c.args[0].user_id == "alice" for c in memory.recall.call_args_list)
    assert llm.complete.call_args.args[1] is None


def test_tool_cannot_override_user(memory):
    call = {"id": "t1", "type": "function", "function": {"name": "search_memory", "arguments": '{"query":"偏好","user_id":"bob"}'}}
    llm = model({"tool_calls": [call]}, {"content": "无法检索"}, tools=True)
    MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="你好"))
    memory.recall.assert_called_once()


def test_calls_do_not_share_history(memory):
    llm = model({"content": "A"}, {"content": "B"})
    agent = MemoryAgent(memory, llm)
    agent.chat(ChatRequest(user_id="alice", message="alice-only", use_memory=False, save_memory=False))
    agent.chat(ChatRequest(user_id="bob", message="bob-only", use_memory=False, save_memory=False))
    assert "alice-only" not in str(llm.complete.call_args)


def test_api_validation_and_model_error(monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import app
    client = TestClient(app)
    assert client.post("/agent/chat", json={"user_id": " ", "message": "x"}).status_code == 422
    def fail(*args, **kwargs):
        raise ModelUnavailable("模型未启动")
    monkeypatch.setattr(MemoryAgent, "chat", fail)
    response = client.post("/agent/chat", json={"user_id": "a", "message": "x"})
    assert response.status_code == 503
    assert response.json()["detail"] == "模型未启动"


def test_local_model_config(monkeypatch):
    monkeypatch.setenv("AGENT_BASE_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("AGENT_MODEL", "local-test")
    cfg = ChatModel()
    assert cfg.base_url == "http://127.0.0.1:1234/v1"
    assert cfg.local


def test_placeholder_siliconflow_key_fails_before_network(monkeypatch):
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("AGENT_MODEL", "Pro/deepseek-ai/DeepSeek-V3.2")
    monkeypatch.setenv("AGENT_API_KEY", "sk-your-real-key-here")
    with pytest.raises(ModelUnavailable, match="占位"):
        ChatModel().complete([{"role": "user", "content": "你好"}])


def test_http_proxy_ignores_socks(monkeypatch):
    # Windows 下环境变量名大小写不敏感，delenv("HTTP_PROXY") 会连带删除
    # HTTPS_PROXY，故先统一清理所有代理变量再设置，避免互相干扰。
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897/")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897/")
    assert ChatModel._http_proxy() == "http://127.0.0.1:7897/"
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897/")
    assert ChatModel._http_proxy() is None


def test_retries_without_proxy_on_connect_error(monkeypatch):
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("AGENT_MODEL", "Pro/deepseek-ai/DeepSeek-V3.2")
    monkeypatch.setenv("AGENT_API_KEY", "sk-not-a-placeholder-key")
    # 先清理所有代理变量（Windows 大小写不敏感，delenv HTTP_PROXY 会连带删 HTTPS_PROXY），
    # 再设置目标 HTTPS_PROXY，避免误删。
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897/")
    calls: list[str | None] = []

    class APIConnectionError(Exception):
        pass

    def fake(self, messages, tools, proxy):
        calls.append(proxy)
        if proxy:
            raise APIConnectionError("proxy ssl failed")
        return {"content": "ok"}

    monkeypatch.setattr(ChatModel, "_complete_once", fake)
    assert ChatModel().complete([{"role": "user", "content": "你好"}])["content"] == "ok"
    assert calls == ["http://127.0.0.1:7897/", None]


def test_agent_api_uses_existing_orchestrator(monkeypatch, memory):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setattr(server, "orchestrator", memory)
    monkeypatch.setattr(ChatModel, "complete", lambda *args: {"content": "按中文偏好回答"})
    result = TestClient(server.app).post("/agent/chat", json={"user_id": "alice", "message": "给我建议"})
    assert result.status_code == 200
    assert result.json()["memories"][0]["id"] == "m1"
    assert result.json()["answer"] == "按中文偏好回答"
    memory.process_input.assert_called_once()


def test_knowledge_uses_existing_promotion(memory):
    memory.process_input.return_value.memory_type = "knowledge"
    memory.consolidate.return_value = SimpleNamespace(stored_memory_ids=["m2"])
    result = MemoryAgent(memory, model()).chat(ChatRequest(user_id="alice", message="项目配置"))
    memory.consolidate.assert_called_once_with("alice")
    assert result["memory_write"]["stored_memory_ids"] == ["m2"]


# ---------------------------------------------------------------------------
# exec_terminal 终端工具
# ---------------------------------------------------------------------------

def _py(code: str) -> str:
    """构造一条用当前解释器执行代码的命令（跨平台）。"""
    return f'"{sys.executable}" -c "{code}"'


def test_exec_terminal_runs_allowed_command(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_ALLOWED_COMMANDS", json.dumps([_py("print(42)")]))
    ok, text = _exec_terminal(_py("print(42)"))
    assert ok is True
    assert "42" in text
    assert "执行成功" in text


def test_exec_terminal_reports_nonzero_exit(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_ALLOWED_COMMANDS", json.dumps([_py("raise SystemExit(3)")]))
    ok, text = _exec_terminal(_py("raise SystemExit(3)"))
    assert ok is True
    assert "退出码 3" in text


def test_exec_terminal_rejects_command_chain():
    ok, text = _exec_terminal("echo hi && ls")
    assert ok is False
    assert "不支持命令链" in text


def test_exec_terminal_rejects_dangerous_command():
    ok, text = _exec_terminal("rm -rf /")
    assert ok is False
    assert "危险命令" in text


def test_exec_terminal_rejects_path_outside_sandbox():
    # 使用 '..' 而非 '/'，因为 Windows 上 os.path.isabs('/') 为 False，
    # 但两者都会触发「超出沙箱目录」的拒绝分支（跨平台一致）。
    ok, text = _exec_terminal(_py("print(1)") + " ..")
    assert ok is False
    assert "沙箱目录之外" in text


def test_exec_terminal_rejects_empty_and_long_command():
    assert _exec_terminal("   ")[0] is False
    assert _exec_terminal("x" * 2001)[0] is False


def test_exec_terminal_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_ALLOWED_COMMANDS", json.dumps([_py("__import__('time').sleep(5)")]))
    ok, text = _exec_terminal(_py("__import__('time').sleep(5)"), timeout=1)
    assert ok is False
    assert "超时" in text


def test_exec_tool_disabled_by_default(memory, monkeypatch):
    monkeypatch.delenv("AGENT_ENABLE_EXEC_TOOL", raising=False)
    assert MemoryAgent(memory, model(tools=True)).exec_enabled is False


def test_exec_tool_registered_and_executes(memory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLE_EXEC_TOOL", "1")
    call = {"id": "t1", "type": "function",
            "function": {"name": "exec_terminal", "arguments": json.dumps({"command": _py("print(7)")})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "已执行"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="帮我执行", save_memory=False))
    assert result["answer"] == "已执行"
    assert "7" in seen[1], "第二轮应看到终端工具返回的执行结果"
    assert "exec_terminal" in seen[0], "开启后系统提示应包含终端工具说明"


def test_exec_tool_rejected_when_not_enabled(memory, monkeypatch):
    monkeypatch.delenv("AGENT_ENABLE_EXEC_TOOL", raising=False)
    call = {"id": "t1", "type": "function",
            "function": {"name": "exec_terminal", "arguments": json.dumps({"command": _py("print(1)")})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "无法执行"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="帮我执行", save_memory=False))
    assert result["answer"] == "无法执行"
    assert "不支持的工具" in seen[1]


def test_unknown_tool_returns_error(memory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLE_EXEC_TOOL", "1")
    call = {"id": "t1", "type": "function",
            "function": {"name": "delete_everything", "arguments": json.dumps({"target": "/"})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "无此工具"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="帮我", save_memory=False))
    assert result["answer"] == "无此工具"
    assert "不支持的工具" in seen[1]


def test_exec_terminal_invalid_arguments_rejected(memory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLE_EXEC_TOOL", "1")
    call = {"id": "t1", "type": "function",
            "function": {"name": "exec_terminal", "arguments": json.dumps({"command": "  "})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "参数无效"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="执行", save_memory=False))
    assert result["answer"] == "参数无效"
    assert "工具参数无效" in seen[1]


# ---------------------------------------------------------------------------
# 基础只读/计算工具
# ---------------------------------------------------------------------------

def test_read_file_sandbox():
    ok, text = _read_file(".env.example")
    assert ok is True
    assert "AGENT_ENABLE_TOOLS" in text
    # 越界路径被拒
    assert _read_file("../outside.txt")[0] is False
    # 不存在的文件
    assert _read_file("no_such_file_xyz.txt")[0] is False


def test_list_directory_sandbox():
    ok, text = _list_directory(".")
    assert ok is True
    assert "src" in text
    # 越过沙箱根目录被拒
    assert _list_directory("..")[0] is False


def test_get_current_datetime_tool():
    ok, text = _get_current_datetime()
    assert ok is True
    assert "UTC 时间" in text
    assert "时间戳" in text


def test_calculate_safe_and_rejects():
    ok, text = _calculate("2 ** 10 + 1")
    assert ok is True
    assert text == "2 ** 10 + 1 = 1025"
    # 含字母（变量/函数调用）被拒
    ok2, text2 = _calculate("import os")
    assert ok2 is False
    # 除以零被拒
    assert _calculate("1/0")[0] is False


def test_web_fetch_rejects_non_http():
    ok, text = _web_fetch("ftp://example.com/x")
    assert ok is False
    assert "http" in text
    ok2, _ = _web_fetch("file:///etc/passwd")
    assert ok2 is False


def test_basic_tools_registered_and_run(memory):
    call = {"id": "t1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": ".env.example"})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "已读取"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="读配置", save_memory=False))
    assert result["answer"] == "已读取"
    assert ".env.example" in seen[1], "第二轮应包含工具读取到的文件内容"
    assert "read_file" in seen[0], "开启主开关后应追加基础工具说明"


def test_web_tool_rejected_when_not_enabled(memory, monkeypatch):
    monkeypatch.delenv("AGENT_ENABLE_WEB_TOOL", raising=False)
    call = {"id": "t1", "type": "function",
            "function": {"name": "web_fetch", "arguments": json.dumps({"url": "https://example.com"})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "无法抓取"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="抓网页", save_memory=False))
    assert result["answer"] == "无法抓取"
    assert "不支持的工具" in seen[1]


def test_search_files_sandbox():
    ok, text = _search_files("src/**/*.py")
    assert ok is True
    assert "service.py" in text
    # 越过沙箱根目录被拒
    assert _search_files("*.py", "..")[0] is False
    # 空模式被拒
    assert _search_files("")[0] is False


def test_search_content_sandbox():
    ok, text = _search_content("def _read_file", path="src/agent", glob="*.py")
    assert ok is True
    assert "def _read_file" in text
    assert "service.py" in text
    # 越界被拒
    assert _search_content("x", path="..")[0] is False
    # 非法正则被拒
    assert _search_content("[bad(", path="src/agent")[0] is False


def test_write_and_edit_file_sandbox():
    import os
    target = "tmp_agent_write_test.txt"
    try:
        # 写入新文件
        ok, text = _write_file(target, "hello world\n")
        assert ok is True
        # 不覆盖已存在文件
        ok2, _ = _write_file(target, "again")
        assert ok2 is False
        # 覆盖需显式
        ok3, _ = _write_file(target, "again", overwrite=True)
        assert ok3 is True
        # 读取回写结果
        ok4, content = _read_file(target)
        assert ok4 is True and "again" in content
        # 编辑：模糊匹配被拒
        ok5, _ = _edit_file(target, "a", "b")
        assert ok5 is False
        # 写入唯一串后编辑
        _write_file(target, "foo bar foo", overwrite=True)
        ok6, _ = _edit_file(target, "bar", "baz")
        assert ok6 is True
        ok7, content2 = _read_file(target)
        assert "baz" in content2 and "bar" not in content2
        # 越界被拒
        assert _write_file("../esc.txt", "x")[0] is False
        assert _edit_file("../esc.txt", "x", "y")[0] is False
    finally:
        if os.path.exists(target):
            os.remove(target)


def test_new_basic_tools_registered_and_run(memory):
    call = {"id": "t1", "type": "function",
            "function": {"name": "search_files",
                         "arguments": json.dumps({"pattern": "src/**/*.py"})}}
    seen = []

    def respond(messages, tools):
        seen.append(str(messages))
        names = {t["function"]["name"] for t in (tools or [])}
        return {"tool_calls": [call]} if len(seen) == 1 else {"content": "已搜索"}

    llm = model(tools=True)
    llm.complete.side_effect = respond
    result = MemoryAgent(memory, llm).chat(ChatRequest(user_id="alice", message="找文件", save_memory=False))
    assert result["answer"] == "已搜索"
    assert "search_files" in seen[0], "开启主开关后应注册 search_files 等新增工具"
    assert "service.py" in seen[1], "第二轮应包含 search_files 的检索结果"


import asyncio


def test_chat_stream_emits_tool_and_token_events(memory):
    """流式端点应实时产出 token、工具开始/结果、最终 done 事件。"""
    class StreamModel:
        tools_enabled = True

        def __init__(self):
            self.n = 0

        def complete_stream(self, messages, tools):
            # 真实模型每次 complete_stream 调用只产出一条最终消息；
            # 用调用次数模拟「第一轮规划工具、第二轮给出答案」。
            self.n += 1
            if self.n == 1:
                yield {"kind": "token", "text": "我"}
                yield {"kind": "message", "message": {
                    "role": "assistant", "content": "我",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": '{"path": ".env.example"}'}}]}}
            else:
                yield {"kind": "token", "text": "已读取。"}
                yield {"kind": "message", "message": {"role": "assistant", "content": "已读取。"}}

    agent = MemoryAgent(memory, StreamModel(), stream=True)
    events = []

    async def collect():
        async for chunk in agent.chat_stream(ChatRequest(user_id="alice", message="读配置", save_memory=False)):
            # chat_stream 产出 SSE 文本： "data: {json}\n\n"
            if isinstance(chunk, str) and chunk.startswith("data:"):
                events.append(json.loads(chunk[len("data:"):].strip()))

    asyncio.run(collect())
    types = [e["type"] for e in events]
    assert "token" in types
    assert "tool_start" in types and "tool_result" in types
    assert types[-1] == "done"
    start = next(e for e in events if e["type"] == "tool_start")
    assert start["name"] == "read_file"
    assert start["args"] == {"path": ".env.example"}
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["ok"] is True and ".env.example" in result["content"]
    done = events[-1]
    assert "已读取" in done["answer"]


def test_token_stream_emits_tokens_realtime():
    """移除节流器后，模型逐 token 产出的文本应原样实时进入事件流。"""
    from src.agent.service import MemoryAgent

    agent = MemoryAgent(Mock(), stream=True)
    agent._emit = lambda ev: agent._captured.append(ev)
    agent._captured = []
    agent._emit_token("你")
    agent._emit_token("好")
    agent._emit_token("世界")
    assert agent._captured == [
        {"type": "token", "text": "你"},
        {"type": "token", "text": "好"},
        {"type": "token", "text": "世界"},
    ]
