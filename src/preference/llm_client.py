"""SiliconFlow OpenAI-compatible LLM client (same pattern as MemoryOS test.py)."""

from __future__ import annotations

import os
import re
from typing import Any
from src.security.outbound import protect_payload

try:
    from src.common.env import load_env
    load_env()  # 确保 .env 中的密钥已注入环境变量
except Exception:  # pragma: no cover - 非关键路径，导入失败不阻断
    pass


def _clean_reasoning_output(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


class SiliconFlowClient:
    """Thin wrapper around openai.OpenAI pointing at SiliconFlow."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        if api_key or base_url or model:
            # 显式传参时尊重调用方，保持既有 SiliconFlow/OpenAI 行为不变。
            self.api_key = (
                api_key
                or os.getenv("SILICONFLOW_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            )
            self.base_url = (
                base_url
                or os.getenv("SILICONFLOW_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or "https://api.siliconflow.cn/v1"
            )
            self.model = (
                model
                or os.getenv("SILICONFLOW_LLM_MODEL")
                or os.getenv("PREFERENCE_LLM_MODEL")
                or "Pro/deepseek-ai/DeepSeek-V3.2"
            )
        else:
            # 无参时按 .env 自动选「确定可用」的后端：本地 LM Studio 优先，
            # 避免无效远程 key 被静默选中、导致 fusion 整批 401 退化成 rule。
            lm_base = os.getenv("LM_STUDIO_BASE_URL")
            if lm_base:
                self.api_key = os.getenv("LM_STUDIO_API_KEY") or "lm-studio"
                self.base_url = lm_base.rstrip("/") + "/v1"
                self.model = (
                    os.getenv("LM_STUDIO_MODEL") or "phi-3.1-mini-128k-instruct"
                )
            else:
                self.api_key = (
                    os.getenv("SILICONFLOW_API_KEY")
                    or os.getenv("OPENAI_API_KEY")
                    or ""
                )
                self.base_url = (
                    os.getenv("SILICONFLOW_BASE_URL")
                    or os.getenv("OPENAI_BASE_URL")
                    or "https://api.siliconflow.cn/v1"
                )
                self.model = (
                    os.getenv("SILICONFLOW_LLM_MODEL")
                    or os.getenv("PREFERENCE_LLM_MODEL")
                    or "Pro/deepseek-ai/DeepSeek-V3.2"
                )
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError(
                "No API key. Set SILICONFLOW_API_KEY (or OPENAI_API_KEY) for preference LLM extraction."
            )
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=model or self.model,
            messages=protect_payload(messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _clean_reasoning_output(raw)
