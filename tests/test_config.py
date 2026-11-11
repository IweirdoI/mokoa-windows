"""公共配置加载器测试 — src/common/config.py 及各模块配置接入.

Owner: 2abac5a6

验证：
  - load_default_config / get_llm_config 合并与覆盖逻辑
  - SemanticPiiDetector 从配置构造（端点/密钥/模型名）
  - SecurityAndForgetService.from_config 启用语义层
  - memory_service 配置接入（Milvus 地址/集合名）
  - ContextAnalyzer summary_model 默认读取 llm 段
"""

from __future__ import annotations

from src.common.config import get_llm_config, load_default_config
from src.security.semantic_detector import SemanticPiiDetector
from src.security.service import SecurityAndForgetService


# ---------------------------------------------------------------------------
# 公共配置加载器
# ---------------------------------------------------------------------------


def test_load_default_config_has_llm_section() -> None:
    config = load_default_config()
    llm = config.get("llm") or {}
    assert llm.get("base_url"), "llm.base_url 应存在"
    # api_key 仅经环境变量注入（LM_STUDIO_API_KEY / OPENAI_API_KEY），不写进配置文件
    # （见 configs/default.yaml 顶部约定），因此不应硬编码在配置里。
    assert "api_key" not in llm, "llm.api_key 不应硬编码在配置文件中"
    assert llm.get("model"), "llm.model 应存在"


def test_get_llm_config_defaults_from_top_level() -> None:
    cfg = get_llm_config()
    assert cfg["base_url"] == "http://127.0.0.1:1234"
    # 默认配置使用本地 LM Studio 已加载的非思考模型（qwen3.5 强制思考、content 恒空，不可用）
    assert cfg["model"] == "phi-3.1-mini-128k-instruct"
    # reasoning 仅在语义检测器内部有默认值 off，顶层 llm 段不强制包含该键


def test_get_llm_config_override_merges(monkeypatch) -> None:
    """模块覆盖段应优先于顶层 llm 段（环境变量优先级最高，故此处先隔离环境）。"""
    for var in ("LM_STUDIO_BASE_URL", "OPENAI_BASE_URL", "LM_STUDIO_MODEL",
                "LM_STUDIO_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    override = {"min_confidence": 0.8, "base_url": "http://example.com:8080"}
    merged = get_llm_config(override)
    assert merged["base_url"] == "http://example.com:8080"
    assert merged["min_confidence"] == 0.8
    # 未覆盖的键仍来自顶层 llm 段
    assert merged["model"] == "phi-3.1-mini-128k-instruct"


def test_get_llm_config_ignores_none_override(monkeypatch) -> None:
    monkeypatch.setenv("LM_STUDIO_API_KEY", "env-secret")
    merged = get_llm_config({"api_key": None})
    # None 覆盖被忽略，保留环境变量中的真实密钥
    assert merged["api_key"] == "env-secret"
    merged2 = get_llm_config({"reasoning": None})
    # 配置与环境中均无 reasoning 时，None 不会凭空注入该键
    assert "reasoning" not in merged2


# ---------------------------------------------------------------------------
# SemanticPiiDetector 配置接入
# ---------------------------------------------------------------------------


def test_detector_defaults_read_from_config(monkeypatch) -> None:
    monkeypatch.setenv("LM_STUDIO_API_KEY", "test-key")
    detector = SemanticPiiDetector()
    assert detector._base_url == "http://127.0.0.1:1234"
    assert detector._model == "phi-3.1-mini-128k-instruct"
    assert detector._api_key == "test-key"  # 密钥经环境变量注入
    assert detector._reasoning == "off"


def test_detector_explicit_params_override_config() -> None:
    detector = SemanticPiiDetector(
        base_url="http://other:9999",
        api_key="custom-key",
        model="custom-model",
    )
    assert detector._base_url == "http://other:9999"
    assert detector._model == "custom-model"
    assert detector._api_key == "custom-key"


def test_detector_from_config_uses_semantic_section() -> None:
    detector = SemanticPiiDetector.from_config(
        {"min_confidence": 0.75, "reasoning": "low"}
    )
    assert detector._min_confidence == 0.75
    assert detector._reasoning == "low"
    # 端点/模型回退顶层 llm 段
    assert detector._base_url == "http://127.0.0.1:1234"
    assert detector._model == "phi-3.1-mini-128k-instruct"


# ---------------------------------------------------------------------------
# SecurityAndForgetService.from_config 语义层接线
# ---------------------------------------------------------------------------


def test_service_from_config_disabled_semantic() -> None:
    service = SecurityAndForgetService.from_config(
        {"semantic_detection": {"enabled": False}}
    )
    assert service.semantic_detector is None


def test_service_from_config_enables_semantic_from_llm(monkeypatch) -> None:
    """enabled=true 时语义层应启用，且端点/密钥继承顶层 llm 段。"""
    monkeypatch.setenv("LM_STUDIO_API_KEY", "test-key")
    service = SecurityAndForgetService.from_config(
        {
            "semantic_detection": {
                "enabled": True,
                "min_confidence": 0.7,
            }
        }
    )
    assert service.semantic_detector is not None
    assert service.semantic_detector._base_url == "http://127.0.0.1:1234"
    assert service.semantic_detector._model == "phi-3.1-mini-128k-instruct"
    assert service.semantic_detector._api_key == "test-key"
    assert service.semantic_detector._reasoning == "off"


def test_service_from_config_full_default_yaml() -> None:
    """用完整 default.yaml 构造，验证语义层默认关闭、正则仍生效。"""
    config = load_default_config()
    service = SecurityAndForgetService.from_config(config)
    # 默认配置 semantic_detection.enabled=false
    assert service.semantic_detector is None
    # 正则层正常工作
    result, detected, _ = service.filter_sensitive("手机 13812345678")
    assert "13812345678" not in result
    assert "phone" in detected


# ---------------------------------------------------------------------------
# memory_service 配置接入
# ---------------------------------------------------------------------------


def test_memory_service_config_reads_storage() -> None:
    """memory_service 配置：默认使用本地 milvus-lite 文件，环境变量可覆盖。"""
    from src.knowledge.memory_service.config import (
        get_collection_name,
        get_milvus_uri,
    )

    # 远端 knowledge 分支将默认 Milvus 地址改为本地 lite 数据库文件（可用 MILVUS_URI 覆盖）
    assert get_milvus_uri().endswith("os_agent_memory.db")
    assert get_collection_name() == "os_agent_memory"


# ---------------------------------------------------------------------------
# ContextAnalyzer summary_model 配置接入
# ---------------------------------------------------------------------------


def test_context_analyzer_summary_model_from_config() -> None:
    from src.core.context_perception import ContextAnalyzer

    analyzer = ContextAnalyzer(keywords_path="nonexistent_keywords.yaml")
    assert analyzer._summary_model == "phi-3.1-mini-128k-instruct"  # 继承 llm.model


def test_context_analyzer_summary_model_explicit_override() -> None:
    from src.core.context_perception import ContextAnalyzer

    analyzer = ContextAnalyzer(
        summary_model="explicit-model",
        keywords_path="nonexistent_keywords.yaml",
    )
    assert analyzer._summary_model == "explicit-model"
