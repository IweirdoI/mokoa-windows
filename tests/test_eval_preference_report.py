"""评测脚本健壮性回归测试——不触发真实 LLM / git。

覆盖：
  - _git_short_head 在 git 缺失/仓库不存在时返回 "unknown"，避免报告丢失
  - _semantic_cover 在 LLM 调用异常时返回 None（哨兵），而非被误算成「未覆盖」
  - extract_batch 在 --semantic 下把调用失败计入 semantic_errors，且 semantic_recall
    分母只用有效判定（不让 LLM 失败污染召回率）
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.eval_preference_hf_dataset as E


class TestGitShortHeadFallback:
    def test_returns_unknown_when_git_missing(self, monkeypatch) -> None:
        # 模拟环境无 git 可执行
        def _boom(*a, **k):
            raise FileNotFoundError()

        monkeypatch.setattr(subprocess, "check_output", _boom)
        assert E._git_short_head(Path(".")) == "unknown"

    def test_returns_unknown_on_nonzero_exit(self, monkeypatch) -> None:
        # 模拟不在 git 仓库内（subprocess 抛 CalledProcessError）
        def _boom(*a, **k):
            raise subprocess.CalledProcessError(128, "git")

        monkeypatch.setattr(subprocess, "check_output", _boom)
        assert E._git_short_head(Path("/tmp/not_a_repo_xyz")) == "unknown"

    def test_returns_hash_when_available(self, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "a593907\n")
        assert E._git_short_head(Path(".")) == "a593907"


class TestSemanticCoverNoFalseNegative:
    def test_returns_none_on_llm_error(self, monkeypatch) -> None:
        client = MagicMock()
        client.chat_completion.side_effect = RuntimeError("401 Token is invalid")
        fake_entries = [MagicMock()]
        assert E._semantic_cover(client, "我喜欢喝咖啡", fake_entries) is None

    def test_returns_bool_on_success(self, monkeypatch) -> None:
        client = MagicMock()
        client.chat_completion.return_value = "YES\n覆盖该偏好"
        fake_entries = [MagicMock()]
        assert E._semantic_cover(client, "我喜欢喝咖啡", fake_entries) is True
        client.chat_completion.return_value = "NO\n无关"
        assert E._semantic_cover(client, "我喜欢喝咖啡", fake_entries) is False

    def test_no_entries_returns_false_not_none(self) -> None:
        # 没有抽出的偏好条目时应为「未覆盖」(False)，而非 None
        assert E._semantic_cover(MagicMock(), "我喜欢喝咖啡", []) is False


class TestExtractBatchSemanticErrors:
    def _fake_store(self, monkeypatch, n_entries: int = 1):
        store = MagicMock()
        entries = [
            MagicMock(metadata={"preference_key": "habit.likes", "preference_value": "咖啡"})
            for _ in range(n_entries)
        ]
        store.extract.return_value = entries

        def _make(*a, **k):
            return store

        monkeypatch.setattr(E, "PreferenceMemoryStore", _make)
        return store

    def test_semantic_errors_counted_not_polluting_recall(self, monkeypatch) -> None:
        self._fake_store(monkeypatch)
        # 让语义判定全部失败（LLM 不可达）
        client = MagicMock()
        client.chat_completion.side_effect = RuntimeError("network unreachable")

        rows = [
            {
                "index": 0,
                "input": "我喜欢喝咖啡",
                "output": None,
                "source": "x",
                "gold_preferences": [{"action": "喜欢喝咖啡"}],
            }
        ]
        # monkeypatch RawRecord / DataSource 已为真实模块，extract_batch 会用真正 store
        report = E.extract_batch("fusion", rows, semantic=True, client=client)
        assert report["semantic_errors"] == 1
        # 失败样本不计入分母 → semantic_recall 应为 None 而非 0.0
        assert report["semantic_recall"] is None

    def test_semantic_recall_uses_valid_denominator(self, monkeypatch) -> None:
        self._fake_store(monkeypatch)
        client = MagicMock()
        # 一条成功判定 YES，另一条 LLM 失败
        client.chat_completion.side_effect = [
            "YES\n覆盖",
            RuntimeError("network unreachable"),
        ]
        rows = [
            {
                "index": 0,
                "input": "我喜欢喝咖啡",
                "output": None,
                "source": "x",
                "gold_preferences": [
                    {"action": "喜欢喝咖啡"},
                    {"action": "喜欢喝茶"},
                ],
            }
        ]
        report = E.extract_batch("fusion", rows, semantic=True, client=client)
        assert report["semantic_errors"] == 1
        # 仅 1 条有效且命中 → recall = 1.0
        assert report["semantic_recall"] == 1.0
