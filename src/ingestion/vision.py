"""检测当前 LLM 是否具备视觉能力，并把图片交给模型转成记忆正文。

检测顺序：``ingestion.vision`` 开关 → 模型名启发式 → 可选 ``/v1/models`` 探测。
调用失败时返回空串，由 ``file_extract`` 回退到本地 OCR 或用户说明。

Owner: 3fba60e1
"""

from __future__ import annotations

import base64
import logging
import re
from io import BytesIO
from typing import Any

import httpx

from src.common.config import get_llm_config, load_default_config
from src.security.outbound import is_loopback_service


logger = logging.getLogger(__name__)

_VISION_NAME_MARKERS = (
    "-vl",
    "vl-",
    "vl.",
    "vision",
    "multimodal",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "gemini",
    "llava",
    "pixtral",
    "internvl",
    "minicpm-v",
    "glm-4v",
    "moondream",
    "cogvlm",
    "bakllava",
    "qwen-vl",
    "phi-3-vision",
    "phi-4-multimodal",
    "llama-4",
)
_CLAUDE_VISION_MARKERS = ("claude-3", "claude-4", "sonnet", "opus", "haiku")
_DESCRIBE_PROMPT = (
    "请用中文客观描述这张图片中对记忆有用的事实："
    "可见文字原样抄录，界面、场景或操作结果用一两句话说明。"
    "不要猜测看不到的内容，不要寒暄。"
)
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)

_capability_cache: dict[str, bool] = {}


def reset_vision_cache() -> None:
    """清空进程内探测缓存，供测试隔离。"""

    _capability_cache.clear()


def model_name_suggests_vision(model: str) -> bool:
    """仅根据模型名判断，不访问网络。"""

    name = (model or "").strip().lower()
    if not name:
        return False
    if "claude" in name and any(marker in name for marker in _CLAUDE_VISION_MARKERS):
        return True
    return any(marker in name for marker in _VISION_NAME_MARKERS)


def vision_mode() -> str:
    """读取 ``ingestion.vision``：``auto`` / ``on`` / ``off``。"""

    ingestion_cfg = load_default_config().get("ingestion") or {}
    raw = str(ingestion_cfg.get("vision", "auto")).strip().lower()
    aliases = {"true": "on", "1": "on", "yes": "on", "false": "off", "0": "off", "no": "off"}
    normalized = aliases.get(raw, raw)
    return normalized if normalized in {"auto", "on", "off"} else "auto"


def model_supports_vision() -> bool:
    """当前配置的模型是否应按视觉路径处理图片。"""

    mode = vision_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True

    llm_cfg = get_llm_config()
    model = str(llm_cfg.get("model") or "")
    if model_name_suggests_vision(model):
        return True
    ingestion_cfg = load_default_config().get("ingestion") or {}
    if bool(ingestion_cfg.get("vision_probe")):
        return _probe_model_vision(llm_cfg)
    return False


def describe_image(
    data: bytes,
    *,
    caption: str | None = None,
    filename: str = "",
) -> str:
    """把图片发给具备视觉能力的 LLM，返回可入库的中文描述。

    模型不可用、超时或返回空内容时返回空串，不抛给主流程。
    """

    if not data or not model_supports_vision():
        return ""

    try:
        mime, encoded = _encode_image(data)
    except Exception:
        logger.debug("图片无法编码为视觉请求", exc_info=True)
        return ""

    llm_cfg = get_llm_config()
    prompt = _DESCRIBE_PROMPT
    hint = (caption or "").strip()
    if hint:
        prompt = f"{prompt}\n用户补充说明：{hint}"
    if filename:
        prompt = f"{prompt}\n文件名：{filename}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ],
        }
    ]
    try:
        raw = _chat_with_image(llm_cfg, messages)
    except Exception as exc:
        logger.warning("视觉模型不可用，回退本地 OCR/说明 (%s)", type(exc).__name__)
        return ""
    return _clean_model_text(raw)


def _probe_model_vision(llm_cfg: dict[str, Any]) -> bool:
    """查询 OpenAI 兼容 ``/models``，看当前模型是否声明视觉能力。"""

    base_url = str(llm_cfg.get("base_url") or "").rstrip("/")
    model = str(llm_cfg.get("model") or "").strip()
    if not base_url or not model:
        return False

    cache_key = f"{base_url}|{model}"
    cached = _capability_cache.get(cache_key)
    if cached is not None:
        return cached

    supported = False
    try:
        payload = _get_json(
            f"{_openai_root(base_url)}/models",
            api_key=str(llm_cfg.get("api_key") or ""),
            timeout=2.0,
        )
        entry = _select_model_entry(payload, model)
        supported = _entry_has_vision(entry) if entry is not None else False
    except Exception:
        logger.debug("视觉能力探测失败，按无视觉处理", exc_info=True)
        supported = False

    _capability_cache[cache_key] = supported
    return supported


def _select_model_entry(payload: Any, model: str) -> dict[str, Any] | None:
    items: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            items = data
        elif payload.get("id") or payload.get("name"):
            items = [payload]
    elif isinstance(payload, list):
        items = payload

    model_l = model.lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or item.get("name") or "").lower()
        if ident == model_l or model_l in ident or ident in model_l:
            return item
    return None


def _entry_has_vision(entry: dict[str, Any]) -> bool:
    caps = entry.get("capabilities") or entry.get("capability")
    if isinstance(caps, (list, tuple, set)):
        lowered = {str(item).lower() for item in caps}
        if lowered & {"vision", "multimodal", "image"}:
            return True
    if entry.get("supports_vision") is True:
        return True

    modality = entry.get("modality") or entry.get("modalities")
    chunks: list[str] = []
    if isinstance(modality, str):
        chunks.append(modality.lower())
    elif isinstance(modality, (list, tuple)):
        chunks.extend(str(item).lower() for item in modality)
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        arch_mod = architecture.get("modality") or architecture.get("modalities")
        if isinstance(arch_mod, str):
            chunks.append(arch_mod.lower())
    joined = " ".join(chunks)
    return any(token in joined for token in ("vision", "image", "multimodal"))


def _encode_image(data: bytes) -> tuple[str, str]:
    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image.load()
        rgb = image.convert("RGB")
        max_side = 1280
        if max(rgb.size) > max_side:
            rgb.thumbnail((max_side, max_side))
        buffer = BytesIO()
        rgb.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "image/jpeg", encoded


def _chat_with_image(llm_cfg: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    base_url = str(llm_cfg.get("base_url") or "").rstrip("/")
    model = str(llm_cfg.get("model") or "").strip()
    if not base_url or not model:
        return ""

    if not is_loopback_service(base_url):
        raise ValueError("原始图片仅允许交给本机视觉服务，请使用本地OCR")

    timeout = float(llm_cfg.get("timeout_seconds") or 60.0)
    max_tokens = min(int(llm_cfg.get("max_tokens") or 512), 512)
    headers = {"Content-Type": "application/json"}
    api_key = str(llm_cfg.get("api_key") or "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = httpx.post(
        f"{_openai_root(base_url)}/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        },
        headers=headers,
        timeout=timeout,
        trust_env=False,
    )
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _get_json(url: str, *, api_key: str, timeout: float) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _openai_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _clean_model_text(text: str) -> str:
    cleaned = _THINK_BLOCK.sub("", text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned
