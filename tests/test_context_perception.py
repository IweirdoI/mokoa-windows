"""Context perception engine tests — 上下文压缩引擎.

Owner: 2abac5a6 | Ref: src/core/context_perception.py

覆盖 ContextBlock / ContextAnalyzer 的分层压缩策略：
关键词快速过滤 → 模型摘要 → 语义检索兜底，以及流式压缩。
"""

from __future__ import annotations

import pytest

from src.core.context_perception import ContextAnalyzer, ContextBlock, KeywordRule


# ---------------------------------------------------------------------------
# ContextBlock 分块测试
# ---------------------------------------------------------------------------


def _message(role: str, content: str, **extra) -> dict:
    msg = {"role": role, "content": content}
    msg.update(extra)
    return msg


def test_block_is_cached_for_system_prompt() -> None:
    block = ContextBlock([_message("user", "hi")], priority="system")
    assert block.is_cached is True
    assert block.is_compressible is False


def test_block_is_cached_for_long_term() -> None:
    block = ContextBlock([_message("user", "hi")], priority="long_term")
    assert block.is_cached is True


def test_block_compressible_for_normal_dialogue() -> None:
    block = ContextBlock([_message("user", "hi")], priority="normal")
    assert block.is_cached is False
    assert block.is_compressible is True


def test_block_not_compressible_after_compression() -> None:
    block = ContextBlock([_message("user", "hi")], priority="normal")
    block.is_compressed = True
    assert block.is_compressible is False


# ---------------------------------------------------------------------------
# ContextAnalyzer 分析测试
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer() -> ContextAnalyzer:
    return ContextAnalyzer(keywords_path="nonexistent_keywords.yaml")


def test_analyzer_loads_default_rules(analyzer) -> None:
    """配置文件不存在时回退到内置默认规则。"""
    names = [rule.name for rule in analyzer._keywords]
    assert "tool_output" in names
    assert "repetitive_greeting" in names
    assert "confirmation" in names


def test_analyze_low_pressure_selects_prune(analyzer) -> None:
    result = analyzer.analyze(
        messages=[_message("user", "你好")],
        token_usage=100,
        context_window=128000,
    )
    assert result["strategy"] == "prune"
    assert result["needs_summary"] is False
    assert result["needs_pruning"] is True


def test_analyze_high_pressure_selects_restructure(analyzer) -> None:
    result = analyzer.analyze(
        messages=[_message("user", "hi")] * 10,
        token_usage=120000,
        context_window=128000,
    )
    assert result["strategy"] == "restructure"
    assert result["deep_restructure_needed"] is True


def test_analyze_high_volatility_selects_summary(analyzer) -> None:
    """高话题活跃度应触发轻量摘要。"""
    messages = [
        _message("user", "a"),
        _message("assistant", "b"),
        _message("user", "c"),
        _message("assistant", "d"),
        _message("user", "e"),
        _message("assistant", "f"),
    ]
    result = analyzer.analyze(messages=messages, token_usage=1000)
    # 交替角色 -> volatility 高，即使 token 压力低也应 summary
    assert result["volatility"] > 0.5
    assert result["needs_summary"] is True


def test_analyze_empty_messages_default_density(analyzer) -> None:
    result = analyzer.analyze(messages=[], token_usage=0)
    assert result["info_density"] == 0.5


# ---------------------------------------------------------------------------
# 流式压缩测试
# ---------------------------------------------------------------------------


def test_stream_compress_blocks_messages(analyzer) -> None:
    """block_size=5 时，12 条消息应压缩为 3 块。"""
    analyzer._block_size = 5
    messages = [_message("user", f"第{i}条") for i in range(12)]
    compressed = analyzer.stream_compress(messages)
    assert len(compressed) == 3  # 5 + 5 + 2
    assert all("【上下文压缩】" in block["content"] for block in compressed)


def test_stream_compress_empty(analyzer) -> None:
    assert analyzer.stream_compress([]) == []


def test_compress_block_records_original_count(analyzer) -> None:
    messages = [_message("user", "a"), _message("assistant", "b")]
    block = analyzer._compress_block(messages)
    assert block["compressed_from"] == 2


def test_keyword_filter_discards_greetings(analyzer) -> None:
    """命中 discard 规则的消息应被过滤。"""
    greeting_rule = KeywordRule("greeting", ["你好"], action="discard")
    analyzer._keywords = [greeting_rule]
    messages = [
        _message("user", "你好"),
        _message("user", "帮我看看这段代码"),
    ]
    filtered = analyzer._keyword_filter(messages)
    assert len(filtered) == 1
    assert filtered[0]["content"] == "帮我看看这段代码"


def test_keyword_filter_keeps_unmatched(analyzer) -> None:
    analyzer._keywords = []
    messages = [_message("user", "任意内容")]
    assert analyzer._keyword_filter(messages) == messages


def test_llm_summary_fallback_to_keyword_summary(analyzer) -> None:
    """模型不可用时应回退到关键词摘要，不崩溃。"""
    messages = [_message("user", "安装依赖")] * 4
    summary = analyzer._generate_summary(messages)
    assert isinstance(summary, str)
    assert len(summary) > 0


def test_keyword_summary_contains_preview(analyzer) -> None:
    messages = [_message("user", "pip 安装失败需要换源")]
    summary = analyzer._keyword_summary(messages)
    assert "pip 安装失败需要换源" in summary


# ---------------------------------------------------------------------------
# 压缩统计测试
# ---------------------------------------------------------------------------


def test_compression_stats_counts_cached(analyzer) -> None:
    messages = [
        _message("user", "系统提示", priority="system"),
        _message("user", "普通对话"),
        _message("assistant", "回复"),
    ]
    stats = analyzer.get_compression_stats(messages)
    assert stats["total_messages"] == 3
    assert stats["cached_blocks"] == 1
    assert stats["compressible_blocks"] == 2
