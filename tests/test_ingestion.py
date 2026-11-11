"""多源摄取模块的功能测试。

测试重点覆盖正式规格中的外部行为，不依赖内部私有变量。这样后续即使把内存
去重换成 Redis、调整正文构造方式，也能通过稳定的输入输出契约判断兼容性。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.common.models import DataSource
from src.ingestion.models import PipelineStage, RejectCode
from src.ingestion.pipeline import IngestionPipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_manual_config_is_normalized_and_accepted() -> None:
    """用户明确配置应获得较高质量分，并保留用户、场景和配置键。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {
            "key": "reply_language",
            "value": "中文",
            "confidence": 0.8,
        },
        "manual_config",
        user_id="u001",
        scene_id="coding-session",
    )

    assert result.accepted is True
    assert result.stage == PipelineStage.COMPLETED
    assert result.record is not None
    assert result.record.source == DataSource.MANUAL_CONFIG
    assert result.record.content == "用户设置“reply_language”为“中文”"
    assert result.record.user_id == "u001"
    assert result.record.scene_id == "coding-session"
    assert result.record.metadata["config_key"] == "reply_language"
    assert result.record.metadata["quality_score"] == result.quality_score
    assert result.dedup_fingerprint == result.fingerprint
    assert result.record.metadata["dedup_fingerprint"] == result.dedup_fingerprint
    assert result.record.metadata["record_key"].endswith(":reply_language")
    assert result.quality_score is not None and result.quality_score >= 0.6


def test_structured_tool_failure_gets_readable_content() -> None:
    """结构化工具日志不能被直接转成 dict 字符串，应生成可读的失败经验。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {
            "action_id": "action-001",
            "tool_name": "pip",
            "status": "failed",
            "stderr": "连接超时",
            "duration_ms": 1250,
            "confidence": 0.8,
        },
        "tool_result",
        user_id="u001",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.content == "pip 执行失败：连接超时"
    assert result.record.metadata["tool_name"] == "pip"
    assert result.record.metadata["status"] == "failed"
    assert result.record.metadata["duration_ms"] == 1250.0
    assert result.record.metadata["action_id"] == "action-001"
    assert result.record.metadata["source_event_id"] == "action-001"
    assert result.record.metadata["record_key"] == "tool_result:u001:action-001"


def test_explicit_conversation_is_accepted_and_control_codes_are_removed() -> None:
    """明确偏好对话应通过；ANSI 颜色码和 NUL 控制符不得进入标准正文。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "\x1b[31m以后请用中文回答\x1b[0m\x00"},
        "conversation",
        user_id="u001",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.content == "以后请用中文回答"
    assert result.record.metadata["explicit"] is True


def test_attitude_conversation_is_explicit_and_accepted() -> None:
    """喜欢/讨厌的明确喜好应标 explicit，从而通过质量门。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "我喜欢吃螺蛳粉，但是我讨厌吃榴莲"},
        "conversation",
        user_id="u_food",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.metadata.get("explicit") is True
    keys = result.record.metadata.get("supported_preference_keys") or []
    assert "habit.likes" in keys
    assert "habit.dislikes" in keys


def test_html_tags_are_stripped_but_inner_text_kept() -> None:
    """QC-03：去掉 HTML 标签，保留标签内外正文。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "请记住：办公规范 <b>备份先开</b> 再改配置。"},
        "conversation",
        user_id="u_html",
    )

    assert result.accepted is True
    assert result.record is not None
    assert "<b>" not in result.record.content
    assert "</b>" not in result.record.content
    assert "办公规范" in result.record.content
    assert "备份先开" in result.record.content
    assert "再改配置" in result.record.content


def test_generic_type_brackets_are_not_stripped() -> None:
    """不要把代码里的 <T> 或比较式当成 HTML 删掉。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "请记住：泛型 List<T> 的比较应写成 a < b。"},
        "conversation",
        user_id="u_generic",
    )

    assert result.accepted is True
    assert result.record is not None
    assert "List<T>" in result.record.content
    assert "a < b" in result.record.content


def test_short_style_preference_passes_quality_without_lowering_threshold() -> None:
    """PREF-10：明确短偏好应入库；conversation 按类型阈值现为 0.44。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "回答请更简洁"},
        "conversation",
        user_id="u_pref10",
    )

    assert result.accepted is True
    assert result.quality_score is not None and result.quality_score >= 0.5
    assert result.record is not None
    assert result.record.metadata.get("explicit") is True
    # 实际生效的是按类型阈值 (conversation=0.44), 而非 pipeline 的全局兜底 0.5
    assert result.record.metadata.get("quality_threshold") == 0.44


def test_noise_and_preference_queries_still_rejected() -> None:
    """好的/哈哈仍过滤；问偏好句不得当新偏好入库。"""

    pipeline = IngestionPipeline()
    okay = pipeline.process({"content": "好的"}, "conversation", user_id="u_noise")
    laugh = pipeline.process({"content": "哈哈哈"}, "conversation", user_id="u_noise")
    query = pipeline.process(
        {"content": "你记得我的偏好吗？"},
        "conversation",
        user_id="u_noise",
    )

    assert okay.accepted is False
    assert laugh.accepted is False
    assert not (query.record and query.record.metadata.get("supported_preference_keys"))


def test_repeated_single_count_behavior_accumulates_to_acceptance() -> None:
    """调用方不累加 occurrence_count 时，服务端应按同指纹历史次数抬升质量。"""

    # 显式 0.6 阈值隔离默认阈值变化，专注验证"同指纹历史次数抬升质量"的累积逻辑。
    pipeline = IngestionPipeline(quality_threshold=0.6)
    payload = {
        "content": "用户打开了 VSCode",
        "occurrence_count": 1,
        "confidence": 0.7,
    }

    results = [
        pipeline.process(payload, "user_behavior", user_id="u001") for _ in range(5)
    ]

    # 引入按类型分阈值 + 信息密度后, behavior 真实日志约 0.36; 同指纹累加应使质量单调上升并越过阈值。
    # 注意: 一旦被接受即进入暂存区, 后续同指纹调用返回 DUPLICATE(accepted=False) 属正常去重,
    # 因此此处验证"质量随历史次数单调上升 + 至少一次被接受 + 计数累积", 而非固定某一次 accepted。
    scores = [r.quality_score for r in results]
    assert scores == sorted(scores), f"质量应随同指纹历史次数单调上升, 实际 {scores}"
    assert not results[0].accepted and results[0].reject_code == RejectCode.LOW_QUALITY
    assert any(r.accepted for r in results), "累积抬升质量后应至少有一次越过阈值被接受"
    assert results[4].record is not None
    assert results[4].record.metadata["occurrence_count"] == 5
    assert results[4].record.metadata["occurrence_prior_count"] == 4
    assert results[4].record.metadata["occurrence_accumulated"] is True
    other_user = pipeline.process(payload, "user_behavior", user_id="u002")
    assert other_user.accepted is False
    assert other_user.record is not None
    assert other_user.record.metadata["occurrence_count"] == 1


def test_high_risk_pii_is_marked_and_rejected_but_tool_ip_is_not() -> None:
    """高风险 PII 在评分前打标并扣分；工具日志中的 IP 不视为隐私风险。"""

    pipeline = IngestionPipeline()
    private = pipeline.process(
        {"content": "以后请记住我的邮箱是 user@example.com", "confidence": 0.9},
        "conversation",
        user_id="u001",
    )
    tool = pipeline.process(
        {
            "action_id": "action-ip-001",
            "tool_name": "ping",
            "status": "failed",
            "stderr": "连接 192.168.1.1 超时",
            "confidence": 0.9,
        },
        "tool_result",
        user_id="u001",
    )

    assert private.accepted is False
    assert private.reject_code == RejectCode.LOW_QUALITY
    assert private.record is not None
    assert private.record.metadata["privacy_risk"] is True
    assert tool.accepted is True
    assert tool.record is not None
    assert tool.record.metadata.get("privacy_risk") is not True


def test_release_dedup_allows_retry_after_downstream_failure() -> None:
    """下游失败释放指纹后，同一事件应能再次通过去重。"""

    pipeline = IngestionPipeline()
    payload = {"key": "reply_language", "value": "中文", "confidence": 0.9}
    first = pipeline.process(payload, "manual_config", user_id="u001")
    duplicate = pipeline.process(payload, "manual_config", user_id="u001")
    assert first.accepted is True
    assert duplicate.reject_code == RejectCode.DUPLICATE
    assert pipeline.release_dedup(first.dedup_fingerprint) is True
    retried = pipeline.process(payload, "manual_config", user_id="u001")
    assert retried.accepted is True


def test_process_reports_per_stage_timings() -> None:
    """正式结果应给出各阶段耗时，且总量覆盖已执行阶段。"""

    pipeline = IngestionPipeline()
    accepted = pipeline.process(
        {"key": "reply_language", "value": "中文", "confidence": 0.9},
        "manual_config",
        user_id="u001",
    )
    rejected = pipeline.process(
        {"content": " \x00\t "},
        "conversation",
        user_id="u001",
    )

    assert accepted.accepted is True
    assert set(accepted.stage_ms) >= {
        "collect",
        "clean",
        "normalize",
        "quality",
        "deduplicate",
    }
    assert "staging" not in accepted.stage_ms
    assert all(elapsed >= 0 for elapsed in accepted.stage_ms.values())
    assert accepted.processing_ms >= sum(accepted.stage_ms.values()) - 0.001
    assert rejected.reject_code == RejectCode.EMPTY_CONTENT
    assert set(rejected.stage_ms) == {"collect", "clean"}


def test_single_behavior_is_rejected_but_repeated_behavior_is_accepted() -> None:
    """单次偶然行为价值低；同一类行为重复出现后可以进入记忆生命周期。"""

    pipeline = IngestionPipeline()

    single = pipeline.process(
        {
            "content": "用户打开了 VSCode",
            "occurrence_count": 1,
            "confidence": 0.7,
        },
        "user_behavior",
        user_id="u001",
    )
    repeated = pipeline.process(
        {
            "content": "用户打开了 VSCode",
            "occurrence_count": 5,
            "confidence": 0.7,
        },
        "user_behavior",
        user_id="u001",
    )

    assert single.accepted is False
    assert single.reject_code == RejectCode.LOW_QUALITY
    assert repeated.accepted is True
    assert repeated.record is not None
    assert repeated.record.metadata["occurrence_count"] == 5


def test_empty_content_and_unknown_source_have_stable_reject_codes() -> None:
    """坏数据应返回可机器处理的拒绝码，而不是抛出未解释异常。"""

    pipeline = IngestionPipeline()

    empty = pipeline.process(
        {"content": " \x00\t "},
        "conversation",
        user_id="u001",
    )
    unknown = pipeline.process(
        {"content": "任意正文"},
        "unknown_source",
        user_id="u001",
    )

    assert empty.accepted is False
    assert empty.stage == PipelineStage.CLEAN
    assert empty.reject_code == RejectCode.EMPTY_CONTENT
    assert unknown.accepted is False
    assert unknown.stage == PipelineStage.COLLECT
    assert unknown.reject_code == RejectCode.INVALID_SOURCE


def test_deduplication_is_isolated_by_user_and_scene() -> None:
    """同一用户同一场景重复写入应拒绝，不同用户的数据不能相互污染。"""

    pipeline = IngestionPipeline(dedup_ttl_seconds=300)
    payload = {"content": "以后默认使用中文", "confidence": 0.9}

    first = pipeline.process(payload, "manual_config", user_id="u001", scene_id="global")
    duplicate = pipeline.process(
        payload, "manual_config", user_id="u001", scene_id="global"
    )
    other_user = pipeline.process(
        payload, "manual_config", user_id="u002", scene_id="global"
    )
    other_scene = pipeline.process(
        payload, "manual_config", user_id="u001", scene_id="coding"
    )

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.stage == PipelineStage.DEDUPLICATE
    assert duplicate.reject_code == RejectCode.DUPLICATE
    assert duplicate.fingerprint == first.fingerprint
    assert other_user.accepted is True
    assert other_scene.accepted is True


def test_dedup_fingerprint_contains_structured_semantic_context() -> None:
    """相同错误出现在不同工具中仍是两条经验，不能只按错误正文去重。"""

    pipeline = IngestionPipeline()
    pip_error = pipeline.process(
        {"tool_name": "pip", "status": "failed", "stderr": "连接超时"},
        "tool_result",
        user_id="u001",
    )
    git_error = pipeline.process(
        {"tool_name": "git", "status": "failed", "stderr": "连接超时"},
        "tool_result",
        user_id="u001",
    )

    assert pip_error.accepted is True
    assert git_error.accepted is True
    assert pip_error.fingerprint != git_error.fingerprint


def test_action_id_distinguishes_new_execution_from_event_redelivery() -> None:
    """相同 action_id 的重复投递应去重，不同 action_id 代表两次独立执行。"""

    pipeline = IngestionPipeline()
    first_payload = {
        "action_id": "action-001",
        "tool_name": "pip",
        "status": "failed",
        "stderr": "连接超时",
    }
    second_payload = {**first_payload, "action_id": "action-002"}

    first = pipeline.process(first_payload, "tool_result", user_id="u001")
    redelivery = pipeline.process(first_payload, "tool_result", user_id="u001")
    second_execution = pipeline.process(second_payload, "tool_result", user_id="u001")

    assert first.accepted is True
    assert redelivery.accepted is False
    assert redelivery.reject_code == RejectCode.DUPLICATE
    assert redelivery.dedup_fingerprint == first.dedup_fingerprint
    assert second_execution.accepted is True
    assert second_execution.dedup_fingerprint != first.dedup_fingerprint


def test_record_key_is_stable_when_manual_config_value_changes() -> None:
    """配置值发生变化时去重指纹应改变，但来源记录键必须保持稳定。"""

    pipeline = IngestionPipeline()
    old_value = pipeline.process(
        {"key": "download_path", "value": "D:/Downloads", "confidence": 0.9},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )
    new_value = pipeline.process(
        {"key": "download_path", "value": "E:/Downloads", "confidence": 0.9},
        "manual_config",
        user_id="u001",
        scene_id="global",
    )

    assert old_value.accepted is True
    assert new_value.accepted is True
    assert old_value.record is not None
    assert new_value.record is not None
    assert old_value.dedup_fingerprint != new_value.dedup_fingerprint
    assert old_value.record.metadata["record_key"] == new_value.record.metadata["record_key"]


def test_low_quality_record_does_not_occupy_dedup_cache() -> None:
    """低质量候选不登记指纹，后续同正文的高频行为仍有机会被接收。"""

    pipeline = IngestionPipeline()
    low_quality = pipeline.process(
        {"content": "用户经常使用终端", "occurrence_count": 1},
        "user_behavior",
        user_id="u001",
    )
    high_quality = pipeline.process(
        {"content": "用户经常使用终端", "occurrence_count": 5},
        "user_behavior",
        user_id="u001",
    )

    assert low_quality.reject_code == RejectCode.LOW_QUALITY
    assert high_quality.accepted is True
    assert low_quality.fingerprint == high_quality.fingerprint


def test_non_finite_confidence_falls_back_to_safe_default() -> None:
    """NaN/Infinity 不能绕过阈值或让 Pydantic 结果模型校验失败。"""

    pipeline = IngestionPipeline()
    result = pipeline.process(
        {"content": "以后默认使用中文", "confidence": float("nan")},
        "manual_config",
        user_id="u001",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.confidence == 0.7


def test_long_text_is_truncated_with_original_length_audit() -> None:
    """超长正文必须有界，同时记录截断前长度，便于审计和调整阈值。"""

    pipeline = IngestionPipeline(max_text_length=20)
    original = "以后请记住：" + "中文回答" * 20
    result = pipeline.process(
        {"content": original, "confidence": 0.9},
        "conversation",
        user_id="u001",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.content.endswith("…")
    assert len(result.record.content) <= 20
    assert result.record.metadata["truncated_fields"]["content"] == len(original)


def test_batch_processing_reports_each_result_and_summary() -> None:
    """批量接口既要给汇总数字，也要保留每条失败原因。"""

    pipeline = IngestionPipeline()
    batch = pipeline.process_batch(
        [
            {
                "raw": {"content": "以后请使用中文回答"},
                "source": "conversation",
                "user_id": "u001",
            },
            {
                "raw": {"content": ""},
                "source": "conversation",
                "user_id": "u001",
            },
            {
                # 故意缺少 source，验证坏信封不会导致整批任务中断。
                "raw": {"content": "invalid envelope"},
                "user_id": "u001",
            },
        ]
    )

    assert batch.total == 3
    assert batch.accepted_count == 1
    assert batch.rejected_count == 2
    assert batch.results[0].accepted is True
    assert batch.results[1].reject_code == RejectCode.EMPTY_CONTENT
    assert batch.results[2].reject_code == RejectCode.INVALID_PAYLOAD


def test_validate_does_not_change_dedup_state() -> None:
    """兼容接口 validate 只能检查质量，不能让后续正式处理被误判为重复。"""

    pipeline = IngestionPipeline()
    raw_record = pipeline.ingest(
        {
            "content": "以后默认使用中文",
            "confidence": 0.9,
            "user_id": "u001",
        },
        "manual_config",
    )

    assert pipeline.validate(raw_record) is True
    assert pipeline.validate(raw_record) is True
    assert pipeline.run_pipeline(
        {
            "content": "以后默认使用中文",
            "confidence": 0.9,
            "user_id": "u001",
        },
        "manual_config",
    ) is not None


def test_acceptance_dataset_matches_current_pipeline() -> None:
    """准上线验收 JSONL 的期望结果必须与当前管线一致。"""

    dataset = PROJECT_ROOT / "benchmarks" / "datasets" / "ingestion_acceptance.jsonl"
    samples = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(samples) >= 10
    pipeline = IngestionPipeline()
    for sample in samples:
        result = pipeline.process(
            sample["payload"],
            sample["source"],
            user_id=sample["user_id"],
            scene_id=sample.get("scene_id"),
        )
        expected = sample["expected"]
        assert result.accepted is expected["accepted"], sample["id"]
        actual_code = result.reject_code.value if result.reject_code else None
        assert actual_code == expected.get("reject_code"), sample["id"]
        if "privacy_risk" in expected and result.record is not None:
            assert (
                bool(result.record.metadata.get("privacy_risk"))
                is expected["privacy_risk"]
            ), sample["id"]


def test_three_source_demo_samples_match_current_pipeline() -> None:
    """3fba60e1 Demo 的三类样例必须能被当前 pipeline 稳定重放。"""

    dataset = PROJECT_ROOT / "benchmarks" / "datasets" / "ingestion_demo.jsonl"
    samples = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {sample["source"] for sample in samples} == {
        "tool_result",
        "user_behavior",
        "manual_config",
    }

    for sample in samples:
        result = IngestionPipeline().process(
            sample["payload"],
            sample["source"],
            user_id=sample["user_id"],
            scene_id=sample["scene_id"],
        )
        assert result.accepted is sample["expected"]["accepted"]
        assert result.record is not None
        assert result.record.type == sample["expected"]["type"]
        assert result.record.content == sample["expected"]["content"]


def test_persona_concat_is_ingested_as_facts_not_dialogue() -> None:
    result = IngestionPipeline().process(
        {
            "content": (
                "[PERSONA] I like cats. I work as a nurse. [DIALOGUE] "
                "Persona A: What kind of movies do you like? Persona B: Hey there!"
            ),
            "role": "user",
        },
        "conversation",
        user_id="u_persona",
    )
    assert result.accepted is True
    assert result.record is not None
    assert "I like cats" in result.record.content
    assert "nurse" in result.record.content
    assert "DIALOGUE" not in result.record.content
    assert "movies" not in result.record.content
    assert result.record.metadata.get("persona_facts")


def test_persona_statement_is_not_accepted_as_user_behavior() -> None:
    pipeline = IngestionPipeline()
    once = pipeline.process(
        {
            "action": "stated_persona",
            "target": "I like cats.",
            "content": "I like cats.",
            "occurrence_count": 1,
        },
        "user_behavior",
        user_id="u_persona_beh",
    )
    repeated = pipeline.process(
        {
            "action": "stated_persona",
            "target": "I am a nurse.",
            "content": "I am a nurse.",
            "occurrence_count": 5,
        },
        "user_behavior",
        user_id="u_persona_beh",
    )
    assert once.accepted is False
    assert once.reject_code == RejectCode.INVALID_PAYLOAD
    assert repeated.accepted is False
    assert repeated.reject_code == RejectCode.INVALID_PAYLOAD

