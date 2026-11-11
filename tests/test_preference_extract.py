"""Preference module v1 tests — Owner: 26a9835a. Offline (rules only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.preference.confidence import calculate_confidence, has_negation
from src.preference.store import PreferenceMemoryStore

# From 测试用例.md
SAMPLES = [
    (
        "帮我把这个文件从桌面挪到 D 盘项目文件夹",
        {"habit.download_path", "habit.file_naming_rule"},
    ),
    (
        "帮我把这周的日报整理成表格发给我",
        {"tool_selection.data_processing_tool"},
    ),
    (
        "把桌面上所有 .tmp 文件删了，直接删不用问我",
        {"security.confirm_before_rm", "workflow.preview_before_execute"},
    ),
    (
        "帮我搜一下采购合同，上周改过的那个",
        {"tool_selection.file_search_tool"},
    ),
    (
        "用英文给我出一份这个 Excel 的分析报告",
        {"style.report_format", "style.reply_language"},
    ),
    (
        "把 downloads 按类型分类放到不同文件夹",
        {"habit.file_organize_rule", "workflow.backup_before_modify"},
    ),
    (
        "把这个目录打个压缩包，密码设一下",
        {"habit.archive_format", "security.confirm_before_highrisk"},
    ),
    (
        "帮我把这个 Word 转换成 PDF 发给客户",
        {"style.report_format", "workflow.auto_complete_steps"},
    ),
    (
        "帮我批量重命名这些图片，按日期顺序",
        {"habit.file_naming_rule", "tool_selection.batch_operation_tool"},
    ),
    (
        "把这份合同里的日期都改成 2026-08-01 格式",
        {"tool_selection.text_processing_tool", "workflow.backup_before_modify"},
    ),
]


@pytest.fixture()
def store(tmp_path: Path) -> PreferenceMemoryStore:
    return PreferenceMemoryStore(
        storage_dir=tmp_path / "preferences",
        use_llm=False,
        allow_rules=True,
    )


@pytest.mark.parametrize("text,expected_keys", SAMPLES)
def test_rule_extract_samples(store: PreferenceMemoryStore, text: str, expected_keys: set[str]) -> None:
    entries = store.extract_from_text(text, user_id="u_test", scene_id=None)
    got = {e.metadata["preference_key"] for e in entries}
    missing = expected_keys - got
    assert not missing, f"missing {missing}, got {got} for: {text}"
    for e in entries:
        assert e.memory_type.value == "preference"
        assert e.metadata.get("is_latest") is True
        assert "version" in e.metadata
        assert 0.0 <= e.confidence <= 1.0


def test_version_bump_and_history(store: PreferenceMemoryStore) -> None:
    e1 = store.extract_from_text("下载默认放 D 盘", user_id="u1", scene_id="general")
    assert e1
    assert e1[0].metadata["preference_key"] == "habit.download_path"
    assert e1[0].metadata["version"] == 1
    assert e1[0].metadata["preference_value"] == "D:/Downloads"

    e2 = store.extract_from_text("以后改到 E 盘下载", user_id="u1", scene_id="general")
    dl = [x for x in e2 if x.metadata["preference_key"] == "habit.download_path"]
    assert dl
    assert dl[0].metadata["version"] == 2
    assert dl[0].metadata["preference_value"] == "E:/Downloads"

    history = store.get_version_history("u1", "habit.download_path")
    versions = sorted({h.version for h in history})
    assert versions == [1, 2]


def test_rollback_creates_new_version(store: PreferenceMemoryStore) -> None:
    store.extract_from_text("下载默认放 D 盘", user_id="u2", scene_id="general")
    store.extract_from_text("以后改到 E 盘下载", user_id="u2", scene_id="general")
    rolled = store.rollback("u2", "habit.download_path", to_version=1, scene_id="general")
    assert rolled.metadata["preference_value"] == "D:/Downloads"
    assert rolled.metadata["version"] == 3
    history = store.get_version_history("u2", "habit.download_path")
    assert len({h.version for h in history}) == 3


def test_scene_isolation(store: PreferenceMemoryStore) -> None:
    store.extract_from_text(
        "帮我把这周的日报整理成表格发给我",
        user_id="u3",
        scene_id="office",
    )
    store.extract_from_text(
        "用 Python 跑数据",
        user_id="u3",
        scene_id="data_analysis",
    )
    # office excel should remain for office scene
    office = store.resolve_preference("u3", "tool_selection.data_processing_tool", "office")
    assert office is not None
    assert office.value == "excel"


def test_confidence_negation() -> None:
    assert has_negation("直接删不用问我")
    conf = calculate_confidence(
        source="explicit_statement",
        value="false",
        statement_text="直接删不用问我",
    )
    assert conf == pytest.approx(0.97)


def test_english_extract(store: PreferenceMemoryStore) -> None:
    entries = store.extract_from_text(
        "Never ask me before delete, just remove tmp files",
        user_id="u_en",
        scene_id="os_operation",
    )
    keys = {e.metadata["preference_key"] for e in entries}
    assert "security.confirm_before_rm" in keys


def test_batch_flush_single_file(store: PreferenceMemoryStore, tmp_path: Path) -> None:
    """Industrial: multi-key extract should leave one JSON file, not N rewrites mid-batch."""
    text = "把桌面上所有 .tmp 文件删了，直接删不用问我"
    entries = store.extract_from_text(text, user_id="u_io", scene_id="os_operation")
    assert len(entries) >= 2
    assert store.index._dirty == set()  # flushed
    path = store.index._path("u_io")
    assert path.exists()
    # dirty cleared after flush; reload should see both keys
    store.index._data.pop("u_io", None)
    store.index.load("u_io")
    assert len(store.index._data["u_io"]) >= 2


def test_canonical_embed_and_feature_profile(store: PreferenceMemoryStore) -> None:
    entries = store.extract_from_text("下载默认放 D 盘", user_id="u_feat", scene_id="general")
    assert entries
    assert entries[0].content.startswith("[preference] habit.download_path=")
    profile = store.get_feature_profile("u_feat", scene_id="general")
    assert profile["schema"] == "preference_feature_profile_v1"
    assert "habit.download_path" in profile["kv"]
    assert profile["kv"]["habit.download_path"] == "D:/Downloads"
    assert "agent_context" in profile


def test_observation_reinforce(store: PreferenceMemoryStore) -> None:
    store.extract_from_text("下载默认放 D 盘", user_id="u_obs", scene_id="general")
    store.extract_from_text("下载默认放 D 盘", user_id="u_obs", scene_id="general")
    rec = store.resolve_preference("u_obs", "habit.download_path", "general")
    assert rec is not None
    assert rec.n_observations >= 2
    assert rec.current_version == 1  # same value → no version bump


def test_negated_concise_updates_response_tone() -> None:
    from src.preference.extractor import extract_by_rules

    concise = extract_by_rules("以后回答请更加简洁")
    assert any(p.category == "style" and p.key == "response_tone" and p.value == "concise" for p in concise)

    detailed = extract_by_rules("回答可以复杂一些，不用那么简洁")
    assert any(p.category == "style" and p.key == "response_tone" and p.value == "detailed" for p in detailed)
    assert not any(p.key == "response_tone" and p.value == "concise" for p in detailed)


def test_attitude_likes_and_dislikes() -> None:
    from src.preference.extractor import extract_by_rules

    hits = extract_by_rules("我喜欢吃螺蛳粉，但是我讨厌吃榴莲")
    likes = [p for p in hits if p.key == "likes"]
    dislikes = [p for p in hits if p.key == "dislikes"]
    assert likes and likes[0].value == "螺蛳粉"
    assert dislikes and dislikes[0].value == "榴莲"

    both = extract_by_rules("我喜欢 Python，不喜欢 Java")
    assert any(p.key == "likes" and "Python" in p.value for p in both)
    assert any(p.key == "dislikes" and "Java" in p.value for p in both)

    love = extract_by_rules("我爱吃火锅")
    assert any(p.key == "likes" and p.value == "火锅" for p in love)

    assert extract_by_rules("我喜欢吃什么") == []
    assert not any(p.key == "likes" for p in extract_by_rules("以后请用中文回答"))


def test_attitude_likes_merge(store: PreferenceMemoryStore) -> None:
    first = store.extract_from_text("我喜欢吃螺蛳粉", user_id="u_food", scene_id="general")
    assert any(e.metadata["preference_key"] == "habit.likes" for e in first)
    second = store.extract_from_text("我也喜欢草莓", user_id="u_food", scene_id="general")
    likes = [e for e in second if e.metadata["preference_key"] == "habit.likes"]
    assert likes
    assert "螺蛳粉" in likes[0].metadata["preference_value"]
    assert "草莓" in likes[0].metadata["preference_value"]
