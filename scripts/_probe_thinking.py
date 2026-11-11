"""临时探测 3：对比非推理模型的 content 输出（完整提示词 + 4096 tokens）。

注意：仅用于本地 LM Studio 探测，密钥从环境变量 LM_STUDIO_API_KEY 读取，
不要把真实密钥提交进仓库。运行前请先 export LM_STUDIO_API_KEY="sk-lm-..."。
"""
import os
import time

import httpx

# 本地 LM Studio OpenAI 兼容端点（127.0.0.1，非真实远端服务）。
BASE = "http://127.0.0.1:1234/v1/chat/completions"
# 探测请求超时（秒）：本地小模型推理较重，留足余量；非业务逻辑阈值。
REQUEST_TIMEOUT = 60
LM_STUDIO_KEY = os.getenv("LM_STUDIO_API_KEY")
if not LM_STUDIO_KEY:
    raise SystemExit(
        "请先设置环境变量 LM_STUDIO_API_KEY（本地 LM Studio 密钥）后再运行本探测脚本，"
        "切勿把真实密钥写入仓库。"
    )
KEY = LM_STUDIO_KEY
PROMPT = (
    "你是敏感信息识别专家。请从文本中找出个人敏感信息，"
    '以JSON输出：{"matches":[{"type":"employee_id","text":"NO.9527","confidence":0.9}]}'
    "\n\n文本：我的工号是NO.9527，地址在北京海淀区中关村大街1号"
)

CANDIDATES = [
    "qwen2.5-1.5b",
    "qwen2.5-coder-3b-instruct",
    "qwen3-0.6b",
    "gemma-3-1b-it-qat",
]


def probe(model: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 1024,
        "stream": False,
        "temperature": 0.0,
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(BASE, json=payload, headers={"Authorization": f"Bearer {KEY}"}, timeout=REQUEST_TIMEOUT)
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content", "")
        has_reasoning = bool(msg.get("reasoning_content"))
        print(
            f"[{model}] {(time.perf_counter()-t0)*1000:.0f}ms | "
            f"content={content[:90]!r} | reasoning={has_reasoning}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{model}] 异常: {type(exc).__name__} {exc}")


if __name__ == "__main__":
    for model in CANDIDATES:
        probe(model)
