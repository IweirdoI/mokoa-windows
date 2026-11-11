"""动态键兜底（长尾偏好）回归测试——不触发 LLM 远程调用。

覆盖：
  - keys.normalize_preference_key 未知键降级为 custom_ 动态键（不再 raise）
  - keys.validate_value 未登记/动态键按自由文本 string 截断（不再 KeyError）
  - extractor.extract_by_rules 默认编程语言长尾规则
  - extract_preferences 未知键不再静默丢弃
  - PreferenceMemoryStore 端到端落库自定义键
"""
from __future__ import annotations

import pytest

from src.common.models import DataSource, RawRecord
from src.preference.extractor import extract_by_rules, extract_preferences
from src.preference.keys import normalize_preference_key, validate_value
from src.preference.store import PreferenceMemoryStore


def _rec(text: str, scene_id: str = "coding") -> RawRecord:
    return RawRecord(
        id="r_test",
        source=DataSource.CONVERSATION,
        user_id="u_test",
        payload={"content": text},
        scene_id=scene_id,
    )


class TestNormalizeDynamicKey:
    def test_known_key_passthrough(self) -> None:
        assert normalize_preference_key("habit", "download_path") == "habit.download_path"

    def test_unknown_key_to_custom(self) -> None:
        assert normalize_preference_key("habit", "coding_language") == "habit.custom_coding_language"

    def test_already_custom_kept(self) -> None:
        assert (
            normalize_preference_key("habit", "custom_coding_language")
            == "habit.custom_coding_language"
        )

    def test_invalid_category_fallback(self) -> None:
        assert normalize_preference_key("foobar", "unknown_thing") == "habit.custom_unknown_thing"

    def test_sanitize_special_chars(self) -> None:
        assert (
            normalize_preference_key("habit", "my cool-lang!") == "habit.custom_my_cool_lang"
        )

    def test_allow_dynamic_false_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_preference_key("habit", "definitely_not_a_key_xyz", allow_dynamic=False)


class TestValidateDynamicValue:
    def test_unknown_key_string_fallback(self) -> None:
        assert validate_value("habit.custom_coding_language", "Go") == "Go"

    def test_long_value_truncated(self) -> None:
        assert len(validate_value("habit.custom_note", "x" * 600)) == 512

    def test_none_value_no_crash(self) -> None:
        assert validate_value("habit.custom_note", None) == ""


class TestRuleLongTailLanguage:
    @pytest.mark.parametrize(
        "txt",
        [
            "写代码默认用 Go 吧",
            "编程语言默认是 Rust",
            "我平时开发用 TypeScript",
        ],
    )
    def test_extract_coding_language(self, txt: str) -> None:
        prefs = extract_by_rules(txt, scene_hint="coding")
        hits = [p for p in prefs if p.key == "custom_coding_language"]
        assert hits, f"未抽出默认编程语言: {txt}"
        assert hits[0].value


class TestExtractPreferencesNoDrop:
    def test_unknown_pref_not_dropped(self) -> None:
        prefs = extract_preferences([_rec("写代码默认用 Go")], use_llm=False, allow_rules=True)
        keys = [(p.category, p.key) for p in prefs]
        assert ("habit", "custom_coding_language") in keys

    def test_likes_still_works(self) -> None:
        prefs = extract_preferences(
            [_rec("我喜欢吃草莓、喝咖啡", scene_id="general")],
            use_llm=False,
            allow_rules=True,
        )
        likes = [p for p in prefs if p.key == "likes"]
        assert likes and "草莓" in likes[0].value


class TestStoreEndToEnd:
    def test_store_persists_custom_key(self, tmp_path) -> None:
        store = PreferenceMemoryStore(
            storage_dir=str(tmp_path), use_llm=False, allow_rules=True
        )
        entries = store.extract_from_text("写代码默认用 Go 吧", user_id="u1", scene_id="coding")
        keys = [e.metadata.get("preference_key") for e in entries]
        assert "habit.custom_coding_language" in keys

    def test_store_resolve_custom_key(self, tmp_path) -> None:
        store = PreferenceMemoryStore(
            storage_dir=str(tmp_path), use_llm=False, allow_rules=True
        )
        store.extract_from_text("写代码默认用 Go 吧", user_id="u1", scene_id="coding")
        rec = store.index.resolve_read("u1", "habit.custom_coding_language", "coding")
        assert rec is not None
        assert rec.value == "Go"
