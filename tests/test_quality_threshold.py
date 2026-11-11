"""质量门控阈值与评分逻辑测试。

背景:
    评测发现 QualityEvaluator 对 ``conversation``/``document`` 等本就篇幅较长的
    真实语料施加了「>500 字 = 冗余日志」降权 (informativeness 0.8)，导致 PersonaChat
    等长对话在默认 confidence=0.7 下综合分停在 0.475，刚好卡在阈值 0.5 以下被误杀；
    而短噪声短语反而可能因长度 ≤500 拿到 0.505 通过 (MOKOA 评审记录)。

修复:
    ``_informativeness`` 仅对真正的行为日志类型 ``behavior`` 做超长降权；对话/文档/
    工具结果/手动配置等长文本视为信息密集，不降权。另补充常见填充短语到低信息集。

本测试同时包含:
    1) 不依赖外部数据的单元/合成用例 (CI 必跑)；
    2) 基于 git/ 真实语料的端到端用例 (数据集缺失时自动 skip)。

运行:
    pytest tests/test_quality_threshold.py -q
"""
from __future__ import annotations

import json
import os
import random
import zipfile
from pathlib import Path

import pytest

try:
    import pyarrow.parquet as pq
except ImportError:
    # 语料用例才需要。Windows 核心环境不安装 datasets/torch 链，缺 pyarrow 时跳过，不挡住其余测试。
    pq = None

from src.common.models import DataSource, NormalizedRecord
from src.ingestion.quality import QualityEvaluator

GIT = Path(__file__).resolve().parents[1] / "git"
HAS_DATASETS = pq is not None and (GIT / "PersonaChat" / "personachat.parquet").exists()

THRESHOLD = 0.46  # 真实语料底分0.505 与 噪声顶分0.415 的均值, 最大化分类间隔; 与 MemoryPolicy.SAVE_THRESHOLD / QualityEvaluator 默认保持一致


def _record(content: str, source: DataSource, confidence: float = 0.7) -> NormalizedRecord:
    type_map = {
        DataSource.CONVERSATION: "conversation",
        DataSource.DOCUMENT: "document",
        DataSource.TOOL_RESULT: "tool_result",
        DataSource.MANUAL_CONFIG: "manual_config",
        DataSource.USER_BEHAVIOR: "behavior",
    }
    return NormalizedRecord(
        type=type_map[source],
        content=content,
        confidence=confidence,
        source=source,
        user_id="bench",
        metadata={},
    )


# ---------------------------------------------------------------------------
# 1) 评分维度单元测试 (不依赖外部数据)
# ---------------------------------------------------------------------------

def test_informativeness_varied_long_text_scores_high():
    qe = QualityEvaluator(threshold=THRESHOLD)
    # 多样长文本 (信息密集) 应得高分, 不因长度被压低 (不再是固定 1.0 悬崖)
    varied = (
        "用户偏好使用 Vim 编辑器，常在终端运行 pytest 与 ruff，"
        "工作时长集中在上午，习惯用 Git 管理代码，喜欢阅读 RFC 文档。"
        "对于 Python 项目优先使用 Poetry 管理依赖，部署偏好容器化方案。"
    )
    assert qe._informativeness(varied, "conversation") > 0.8


def test_informativeness_repetitive_text_scores_lower():
    qe = QualityEvaluator(threshold=THRESHOLD)
    # 重复灌水 (长但低密度) 应显著低于多样长文本
    repetitive = "好的好的好的好的好的好的好的好的好的好的" * 10
    assert qe._informativeness(repetitive, "conversation") < 0.6


def test_informativeness_behavior_log_penalized_vs_conversation():
    qe = QualityEvaluator(threshold=THRESHOLD)
    log = "user opened terminal at 10:00 and ran git pull\n" * 20
    # behavior 超长应被轻微降权, 低于同等长度的文档/对话
    assert qe._informativeness(log, "behavior") < qe._informativeness(log, "document")
    assert qe._informativeness(log, "behavior") > 0.0


def test_informativeness_length_buckets():
    qe = QualityEvaluator(threshold=THRESHOLD)
    assert qe._informativeness("", "conversation") == 0.0
    assert qe._informativeness("你好", "conversation") == 0.3  # 过短 -> 低底分
    # 多样中等句仍得较高分 (不再是固定 1.0 悬崖)
    s = qe._informativeness("用户经常使用终端进行日常开发与运维工作。", "conversation")
    assert 0.7 < s <= 1.0


def test_memory_signal_detection():
    qe = QualityEvaluator(threshold=THRESHOLD)
    assert qe._detect_memory_signal("我住在上海。") == 1.0        # 个人事实
    assert qe._detect_memory_signal("我喜欢喝咖啡。") == 1.0       # 偏好
    assert qe._detect_memory_signal("禁止使用 root 登录。") == 1.0  # 约束
    assert qe._detect_memory_signal("好的没问题") == 0.0           # 噪声无信号


def test_memory_signal_boosts_conversation_reuse():
    qe = QualityEvaluator(threshold=THRESHOLD)
    fact = _record("我住在上海，工作在张江。", DataSource.CONVERSATION)
    plain = _record("今天天气不错，出去走了走。", DataSource.CONVERSATION)
    d_fact = qe.evaluate(fact)
    d_plain = qe.evaluate(plain)
    assert d_fact.components["reuse_value"] > d_plain.components["reuse_value"]
    assert any("提升复用价值" in r for r in d_fact.reasons)


def test_low_information_phrases_rejected():
    qe = QualityEvaluator(threshold=THRESHOLD)
    for phrase in ["好的", "知道了", "谢谢", "好的没问题", "没问题", "test", "测试", "点击"]:
        rec = _record(phrase, DataSource.CONVERSATION)
        d = qe.evaluate(rec)
        assert not d.accepted, f"填充短语 {phrase!r} 不应通过质量门控"
        assert d.score < THRESHOLD


def test_genuine_short_preference_passes():
    qe = QualityEvaluator(threshold=THRESHOLD)
    rec = _record("以后请用中文回答", DataSource.MANUAL_CONFIG, confidence=0.9)
    d = qe.evaluate(rec)
    assert d.accepted
    assert d.score >= THRESHOLD


def test_explicit_conversation_gets_explicitness_bonus():
    qe = QualityEvaluator(threshold=THRESHOLD)
    rec = _record("记住我喜欢深色主题", DataSource.CONVERSATION, confidence=0.7)
    rec.metadata["explicit"] = True
    d = qe.evaluate(rec)
    assert d.components["explicitness"] == 1.0
    assert d.accepted


# ---------------------------------------------------------------------------
# 2) 端到端：真实语料应全部通过，噪声应全部被拒 (数据集缺失则跳过)
# ---------------------------------------------------------------------------

def _load_personachat(n: int, rng: random.Random):
    rows = pq.read_table(str(GIT / "PersonaChat" / "personachat.parquet")).slice(0, 5000).to_pylist()
    idx = sorted(rng.sample(range(len(rows)), min(n, len(rows))))
    out = []
    for i in idx:
        r = rows[i]
        persona = " ".join(r.get("persona_b") or [])
        dialogue = " ".join(r.get("dialogue") or [])
        out.append((f"[PERSONA] {persona} [DIALOGUE] {dialogue}")[:1500])
    return out


NOISE = ["好的", "知道了", "谢谢", "好的没问题", "没问题", "test", "点击", "收到", "嗯。"]


@pytest.mark.skipif(not HAS_DATASETS, reason="git/ 测试集缺失 (被 .gitignore 忽略), 跳过真实语料用例")
def test_real_personachat_passes_threshold():
    qe = QualityEvaluator(threshold=THRESHOLD)
    rng = random.Random(42)
    contents = _load_personachat(40, rng)
    assert contents, "未能加载 PersonaChat 样本"
    scores = [qe.evaluate(_record(c, DataSource.CONVERSATION)).score for c in contents]
    # 引入信息密度(Step2)+记忆信号(Step3)后, 真实对话语料应为正分且全部高于按类型阈值 0.44
    assert min(scores) >= 0.44, f"真实语料最低分 {min(scores)} 不应卡在阈值下"
    assert all(s >= THRESHOLD for s in scores)


@pytest.mark.skipif(not HAS_DATASETS, reason="git/ 测试集缺失 (被 .gitignore 忽略), 跳过噪声对照用例")
def test_noise_rejected_after_fix():
    qe = QualityEvaluator(threshold=THRESHOLD)
    scores = [qe.evaluate(_record(c, DataSource.CONVERSATION)).score for c in NOISE]
    # 噪声最高分应低于真实语料最低分, 形成干净分隔带
    assert max(scores) < 0.505, f"噪声最高分 {max(scores)} 不应与真实语料重叠"
    assert all(s < THRESHOLD for s in scores)


@pytest.mark.skipif(not HAS_DATASETS, reason="git/ 测试集缺失 (被 .gitignore 忽略), 跳过端到端用例")
def test_dataset_driven_threshold_separation():
    """用真实语料 + 噪声验证 0.5 阈值位于干净分隔带内 (噪声<0.5<真实)。"""
    qe = QualityEvaluator(threshold=THRESHOLD)
    rng = random.Random(42)
    real = _load_personachat(40, rng)
    real_scores = [qe.evaluate(_record(c, DataSource.CONVERSATION)).score for c in real]
    noise_scores = [qe.evaluate(_record(c, DataSource.CONVERSATION)).score for c in NOISE]
    real_min, noise_max = min(real_scores), max(noise_scores)
    # 分隔带: 噪声顶 < 阈值 < 真实底
    assert noise_max < THRESHOLD <= real_min, (
        f"阈值 {THRESHOLD} 应位于分隔带内, 但 噪声顶={noise_max} 真实底={real_min}"
    )


# ---------------------------------------------------------------------------
# 3) 按类型分阈值: 不同来源自然分数分布不同, 不应共用单一全局阈值
# ---------------------------------------------------------------------------

def test_behavior_uses_lower_per_type_threshold():
    """behavior 的区分维度是频次: occ>=2 的重复模式应放行, 但仍低于全局 0.46。"""
    # 真实行为模式: 已重复出现 2 次 (recurrence+reuse 由 occurrence_count 驱动)
    rec = _record("user opened VSCode", DataSource.USER_BEHAVIOR)
    rec.metadata["occurrence_count"] = 2
    qe_per_type = QualityEvaluator()  # 默认带 _DEFAULT_TYPE_THRESHOLDS
    qe_global = QualityEvaluator(type_thresholds={"behavior": THRESHOLD})  # behavior 强制走全局 0.46
    d_per = qe_per_type.evaluate(rec)
    d_glob = qe_global.evaluate(rec)
    assert d_per.score == d_glob.score  # 分数相同, 仅阈值不同
    assert not d_glob.accepted, "occ=2 的行为模式分数仍低于全局 0.46, 会被误杀"
    assert d_per.accepted, "按类型阈值 0.41 应放行 occ>=2 的重复行为模式"
    assert d_per.threshold == 0.41


def test_behavior_one_off_trivial_event_rejected():
    """一次性琐碎事件 (occ=1, 如 mouse_move) 无记忆价值, 应被拒。"""
    rec = _record("mouse_move", DataSource.USER_BEHAVIOR)
    qe = QualityEvaluator()
    d = qe.evaluate(rec)
    assert d.threshold == 0.41
    assert not d.accepted, "一次性琐碎事件不应进记忆"


def test_document_uses_higher_per_type_threshold():
    rec = _record(
        "本文档描述系统架构：前端采用 React，后端采用 FastAPI，数据层使用 PostgreSQL 与 Redis。",
        DataSource.DOCUMENT,
    )
    qe = QualityEvaluator()
    d = qe.evaluate(rec)
    assert d.threshold == 0.51
    assert d.accepted


def test_unlisted_type_falls_back_to_global_threshold():
    rec = _record("用户偏好: 终端使用 zsh", DataSource.MANUAL_CONFIG)
    qe = QualityEvaluator()
    d = qe.evaluate(rec)
    assert d.threshold == THRESHOLD  # manual_config 未列于 type_thresholds, 回落全局
    assert d.accepted


# ---------------------------------------------------------------------------
# 4) 配置驱动: 阈值来自 configs/default.yaml, 缺省回落内置默认
# ---------------------------------------------------------------------------

# 各类型安全区间 (噪声顶, 真实底] —— 与 configs/default.yaml 中注释的取值说明保持一致。
# 阈值必须落在此区间才有效: 低于下界会漏放噪声, 高于上界会误杀真实语料。
SAFE_RANGES = {
    "conversation": (0.400, 0.475),
    "document": (0.504, 0.5164),
    "tool_result": (0.500, 0.5737),
    "behavior": (0.3616, 0.4524),
}


def _load_default_yaml() -> dict:
    yaml = pytest.importorskip("yaml")
    path = Path(__file__).resolve().parents[1] / "configs" / "default.yaml"
    assert path.exists(), f"配置文件缺失: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_pipeline_forwards_type_thresholds_and_falls_back():
    """显式传入的配置应生效；不传则回落到 QualityEvaluator 内置默认。"""
    from src.ingestion.pipeline import IngestionPipeline

    per_type = {"conversation": 0.40, "document": 0.52, "tool_result": 0.56, "behavior": 0.35}
    pipeline = IngestionPipeline(quality_type_thresholds=per_type)
    assert pipeline.quality.type_thresholds == per_type

    fallback = IngestionPipeline()
    assert fallback.quality.type_thresholds == QualityEvaluator._DEFAULT_TYPE_THRESHOLDS


def test_default_config_declares_type_thresholds():
    """configs/default.yaml 应声明按类型阈值，作为配置的单一来源。"""

    cfg = _load_default_yaml()
    per_type = (cfg.get("memory") or {}).get("quality_type_thresholds") or {}
    assert per_type, "configs/default.yaml 缺少 memory.quality_type_thresholds"
    # 配置为主源、代码内置值仅作兜底, 故只要求类型集合一致, 数值允许被配置覆盖
    assert set(per_type) == set(QualityEvaluator._DEFAULT_TYPE_THRESHOLDS)


def test_configured_thresholds_within_documented_safe_ranges():
    """防止误配置：每个类型的阈值都必须落在文档所述的安全区间内。"""

    cfg = _load_default_yaml()
    per_type = (cfg.get("memory") or {}).get("quality_type_thresholds") or {}
    for name, value in per_type.items():
        assert name in SAFE_RANGES, f"未知类型 {name}，请先在代码中定义其安全区间"
        low, high = SAFE_RANGES[name]
        assert low < value <= high, (
            f"{name} 阈值 {value} 超出安全区间 ({low}, {high}]: "
            f"低于 {low} 会漏放噪声, 高于 {high} 会误杀真实语料"
        )

