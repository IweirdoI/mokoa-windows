"""偏好系统稳健性回归测试——覆盖审查发现的低危项（#6-#11），不触发真实 LLM/git。

- #6 features 解析优先级（scene > global），删除死代码后行为不变
- #7 eval 旧版 list 缓存补 n_total_sampled，summary 不缺样本数
- #8 中文职业抽取否定前瞻增强（想/说/要/来… 不误抽，正常职业仍抽）
- #9 extract_preferences 空文本记录单次求值、不报错
- #10 version_index.persist 删除 self.load 后仍正确落盘
- #11 extract_by_llm 在 debug 下跳过坏行且不整批崩溃
"""
from __future__ import annotations

from pathlib import Path

from src.common.models import DataSource, RawRecord
from src.preference.extractor import (
    _extract_profile_facts,
    extract_by_llm,
    extract_preferences,
)
from src.preference.features import build_user_feature_profile
from src.preference.models import PreferenceRecord
from src.preference.store import PreferenceMemoryStore
from src.preference.version_index import PreferenceVersionIndex
from unittest.mock import MagicMock

import scripts.eval_preference_hf_dataset as E


def _rec(text: str, scene_id: str = "coding") -> RawRecord:
    return RawRecord(
        id="r_test",
        source=DataSource.CONVERSATION,
        user_id="u_test",
        payload={"content": text},
        scene_id=scene_id,
    )


def _prec(preference_key: str, value: str, scope: str, scene_id=None) -> PreferenceRecord:
    return PreferenceRecord(
        id=f"{preference_key}:{scope}",
        category=preference_key.split(".", 1)[0],
        key=preference_key.split(".", 1)[1] if "." in preference_key else preference_key,
        preference_key=preference_key,
        scope=scope,
        scene_id=scene_id,
        value=value,
    )


# --------------------------------------------------------------------------- #6
class TestFeatureResolutionPriority:
    def test_scene_overrides_global(self) -> None:
        recs = [
            _prec("habit.likes", "草莓", "global"),
            _prec("habit.likes", "咖啡", "scene", scene_id="coding"),
        ]
        profile = build_user_feature_profile(recs, scene_id="coding")
        assert profile["kv"]["habit.likes"] == "咖啡"

    def test_global_fallback_when_no_scene(self) -> None:
        recs = [_prec("habit.likes", "草莓", "global")]
        profile = build_user_feature_profile(recs, scene_id="coding")
        assert profile["kv"]["habit.likes"] == "草莓"

    def test_global_only_no_scene_id(self) -> None:
        recs = [_prec("habit.likes", "苹果", "global")]
        profile = build_user_feature_profile(recs)
        assert profile["kv"]["habit.likes"] == "苹果"


# --------------------------------------------------------------------------- #7
class TestCachedListStats:
    def test_list_cache_reports_total(self, tmp_path) -> None:
        p = tmp_path / "cache.json"
        p.write_text('[{"input":"a"},{"input":"b"}]', encoding="utf-8")
        rows, stats = E.load_cached_samples(p)
        assert len(rows) == 2
        assert stats.get("n_total_sampled") == 2


# --------------------------------------------------------------------------- #8
class TestChineseOccupationRobustness:
    def test_normal_occupation_extracted(self) -> None:
        occ, _ = _extract_profile_facts("我是一名工程师")
        assert occ == "工程师"

    def test_want_to_study_not_extracted(self) -> None:
        occ, _ = _extract_profile_facts("我想学编程")
        assert occ is None

    def test_saying_not_extracted(self) -> None:
        occ, _ = _extract_profile_facts("我说喜欢喝咖啡")
        assert occ is None

    def test_come_to_interview_not_extracted(self) -> None:
        occ, _ = _extract_profile_facts("我是来面试的")
        assert occ is None

    def test_not_a_teacher_not_extracted(self) -> None:
        occ, _ = _extract_profile_facts("我不是老师")
        assert occ is None


# --------------------------------------------------------------------------- #9
class TestEmptyTextHandling:
    def test_blank_record_skipped_not_crash(self) -> None:
        recs = [_rec(""), _rec("   "), _rec("我喜欢吃草莓", scene_id="general")]
        prefs = extract_preferences(recs, use_llm=False, allow_rules=True)
        likes = [p for p in prefs if p.key == "likes"]
        assert likes and "草莓" in likes[0].value

    def test_all_blank_returns_empty(self) -> None:
        prefs = extract_preferences([_rec(""), _rec("  ")], use_llm=False, allow_rules=True)
        assert prefs == []


# --------------------------------------------------------------------------- #10
class TestVersionIndexPersistNoLoad:
    def test_persist_writes_memory_state_without_reload(self, tmp_path) -> None:
        idx = PreferenceVersionIndex(storage_dir=str(tmp_path))
        # 直接写入内存（模拟 extract 后的状态），不经由 load
        idx._data["u1"] = {
            "habit.likes": _prec("habit.likes", "草莓", "global")
        }
        idx.persist("u1")
        disk = Path(tmp_path / "u1.json")
        assert disk.exists()
        payload = __import__("json").loads(disk.read_text(encoding="utf-8"))
        assert "habit.likes" in payload["records"]
        assert payload["records"]["habit.likes"]["value"] == "草莓"

    def test_store_extract_then_persist(self, tmp_path) -> None:
        store = PreferenceMemoryStore(
            storage_dir=str(tmp_path), use_llm=False, allow_rules=True
        )
        store.extract_from_text("我喜欢吃草莓", user_id="u1", scene_id="general")
        store.index.persist("u1")
        assert (tmp_path / "u1.json").exists()

    def test_flush_skips_user_with_no_extracted_prefs(self, tmp_path) -> None:
        store = PreferenceMemoryStore(
            storage_dir=str(tmp_path), use_llm=False, allow_rules=True
        )
        rec = _rec("What is the capital of France?")
        rec.user_id = "empty_user"
        entries = store.extract([rec])
        assert entries == []
        assert not (tmp_path / "empty_user.json").exists()


# --------------------------------------------------------------------------- #11
class TestExtractByLlmSkipBadRows:
    def test_bad_row_skipped_in_debug(self, monkeypatch) -> None:
        client = MagicMock()
        # 混合：一条合规、一条缺键（会触发 KeyError 被行级 except 捕获）
        client.chat_completion.return_value = (
            '[{"category":"habit","key":"likes","value":"草莓"},'
            '{"foo":"bar"}]'
        )
        items = extract_by_llm(
            ["我喜欢吃草莓"], "u1", "general", "conversation",
            client=client, debug=True,
        )
        assert len(items) == 1
        assert items[0].value == "草莓"
