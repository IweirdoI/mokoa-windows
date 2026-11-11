"""全局配置加载器 — 统一从 configs/default.yaml 读取全项目默认值.

Owner: efca9634（配置加载器，全队共用）

设计原则（三层优先级，高 -> 低）:
  1. 环境变量（OS_AGENT_* / LM_STUDIO_* / SILICONFLOW_* / AGENT_* 等）: 运行态覆盖，最高优先;
  2. configs/default.yaml: 全项目默认值的单一事实来源;
  3. 调用方传入的 default 形参: 代码兜底.

约定:
- ``load_default_config()``: 缓存式加载 YAML 原始字典.
- ``get(path, default)`` / ``get_int`` / ``get_float`` / ``get_bool`` / ``get_section``:
  支持点号路径（如 ``"memory.mid_term_capacity"``），并自动应用环境变量覆盖.
- ``get_llm_config(override)``: 解析 LLM 服务配置，支持局部覆盖段（保持原有语义层调用约定）.
- ``resolve_config_path(relative)``: 把 YAML 中的相对路径解析为项目根下的绝对路径.
- 各模块禁止再硬编码模型端点/密钥/模型名，统一走本加载器或对应 env 变量.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import os
import yaml

from .env import load_env

# 进程启动时加载 .env，使密钥从环境变量读取（不写死在代码/配置里）。
load_env()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "configs" / "default.yaml"


def _as_bool(raw: str) -> bool:
    """宽松地把环境变量字符串解析为布尔值。"""
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


# 环境变量 -> (配置点号路径, 类型) 映射；env 优先级高于 YAML。
# 这些变量原散落在 factory.py / embedding/* / knowledge/memory_service/config.py
# 等处直接 os.getenv，此处集中登记，使 config.get(...) 与运行态一致。
ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    # 存储 / 记忆后端
    "OS_AGENT_MEMORY_BACKEND": ("storage.memory_backend", str),
    "OS_AGENT_MILVUS_URI": ("storage.milvus_uri", str),
    "OS_AGENT_MILVUS_COLLECTION": ("storage.collection_name", str),
    "MILVUS_COLLECTION_NAME": ("storage.collection_name", str),
    # Embedding
    "OS_AGENT_EMBEDDING_BACKEND": ("embedding.backend", str),
    "EMBEDDING_BACKEND": ("embedding.backend", str),
    # 麒麟运行时
    "OS_AGENT_KYLIN_ACCEPTANCE": ("kylin.acceptance", _as_bool),
    "OS_AGENT_KYLIN_STRICT": ("kylin.strict", _as_bool),
    "OS_AGENT_KYLIN_VECTOR_LIBRARY": ("kylin.vector_library", str),
    "OS_AGENT_KYLIN_VECTOR_ENDPOINT": ("kylin.vector_endpoint", str),
    "OS_AGENT_KYLIN_VECTOR_TIMEOUT_MS": ("kylin.vector_timeout_ms", int),
    "MEMORY_VECTOR_DIM": ("kylin.vector_dim", int),
    # 顶层 LLM（安全语义层 / 视觉描述等）
    "LM_STUDIO_API_KEY": ("llm.api_key", str),
    "OPENAI_API_KEY": ("llm.api_key", str),
    "LM_STUDIO_BASE_URL": ("llm.base_url", str),
    "OPENAI_BASE_URL": ("llm.base_url", str),
    "LM_STUDIO_MODEL": ("llm.model", str),
    # Agent 对话服务
    "AGENT_BASE_URL": ("agent.base_url", str),
    "AGENT_MODEL": ("agent.model", str),
    "AGENT_API_KEY": ("agent.api_key", str),
    # 偏好提取 LLM（硅基流动）
    "SILICONFLOW_API_KEY": ("preference.llm.api_key", str),
    "SILICONFLOW_BASE_URL": ("preference.llm.base_url", str),
    "SILICONFLOW_LLM_MODEL": ("preference.llm.model", str),
    "PREFERENCE_LLM_MODEL": ("preference.llm.model", str),
}

# 路径 -> (env_var, type) 反向索引，便于按路径快速查找覆盖。
_ENV_BY_PATH: dict[str, tuple[str, type]] = {
    path: (env_var, caster) for env_var, (path, caster) in ENV_OVERRIDES.items()
}


@lru_cache(maxsize=1)
def load_default_config() -> dict[str, Any]:
    """加载项目默认配置（带缓存）。

    配置文件缺失时返回空字典，避免启动崩溃；由调用方按需取默认值。
    """
    if not _DEFAULT_CONFIG_PATH.exists():
        return {}
    with _DEFAULT_CONFIG_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _split(path: str) -> list[str]:
    return [p for p in path.split(".") if p != ""]


def _traverse(data: dict[str, Any], keys: list[str]) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def get(path: str, default: Any = None) -> Any:
    """按点号路径读取配置，环境变量优先于 YAML，最后回退 default。

    Args:
        path: 点号分隔的配置路径，如 ``"memory.mid_term_capacity"``。
        default: YAML 与 env 均未命中时返回的兜底值。
    """
    keys = _split(path)
    if not keys:
        return default
    # 1) 环境变量覆盖（最高优先级）
    if path in _ENV_BY_PATH:
        env_var, caster = _ENV_BY_PATH[path]
        raw = os.getenv(env_var)
        if raw is not None:
            try:
                return caster(raw)
            except (ValueError, TypeError):
                return raw
    # 2) YAML 默认值
    value = _traverse(load_default_config(), keys)
    return value if value is not None else default


def get_int(path: str, default: int = 0) -> int:
    value = get(path, default)
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def get_float(path: str, default: float = 0.0) -> float:
    value = get(path, default)
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def get_bool(path: str, default: bool = False) -> bool:
    value = get(path, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return _as_bool(value)
    return default


def get_section(name: str, default: Any = None) -> Any:
    """读取某个顶层配置段（如 ``"memory"`` / ``"kylin"``）。"""
    if default is None:
        default = {}
    return get(name, default)


def get_llm_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """获取 LLM 服务配置（保持原有语义层调用约定）。

    Args:
        override: 局部覆盖段（如 security.semantic_detection），
            其键值优先级高于顶层 ``llm`` 段。

    Returns:
        合并后的 LLM 配置字典，含 provider/base_url/api_key/model 等键。
    """
    config = load_default_config()
    merged: dict[str, Any] = dict(config.get("llm") or {})
    if override:
        merged.update({k: v for k, v in override.items() if v is not None})
    # Secrets/endpoints are runtime-only: env vars always override config.
    env_api_key = os.getenv("LM_STUDIO_API_KEY") or os.getenv("OPENAI_API_KEY")
    if env_api_key:
        merged["api_key"] = env_api_key
    else:
        merged.pop("api_key", None)
    # 端点/模型名也可通过 .env 覆盖（仅当显式设置时）。
    env_base_url = os.getenv("LM_STUDIO_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if env_base_url:
        merged["base_url"] = env_base_url
    env_model = os.getenv("LM_STUDIO_MODEL")
    if env_model:
        merged["model"] = env_model
    return merged


def resolve_config_path(relative_path: str) -> Path:
    """将配置中的相对路径解析为项目根下的绝对路径。"""
    path = Path(relative_path).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


class _Config:
    """便捷访问器：``CONFIG.get("memory.mid_term_capacity")`` 等。"""

    def get(self, path: str, default: Any = None) -> Any:
        return get(path, default)

    def get_int(self, path: str, default: int = 0) -> int:
        return get_int(path, default)

    def get_float(self, path: str, default: float = 0.0) -> float:
        return get_float(path, default)

    def get_bool(self, path: str, default: bool = False) -> bool:
        return get_bool(path, default)

    def section(self, name: str, default: Any = None) -> Any:
        return get_section(name, default)

    @property
    def raw(self) -> dict[str, Any]:
        return load_default_config()


CONFIG = _Config()
