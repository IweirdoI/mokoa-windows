"""临时探测：验证 qwen3.5-0.8b 原生端点 reasoning=off 的 content 输出。

注意：本脚本仅用于本地 LM Studio 探测，密钥从环境变量 LM_STUDIO_API_KEY 读取，
不要把真实密钥提交进仓库。运行前请先 export LM_STUDIO_API_KEY="sk-lm-..."。
"""
import os
import time

import httpx

# 本地 LM Studio 探测端点（127.0.0.1，非真实远端服务）；密钥必须由环境变量提供。
BASE = "http://127.0.0.1:1234/api/v1/chat"
# 探测请求超时（秒）：本地小模型推理较重，留足余量；非业务逻辑阈值。
REQUEST_TIMEOUT = 90
LM_STUDIO_KEY = os.getenv("LM_STUDIO_API_KEY")
if not LM_STUDIO_KEY:
    raise SystemExit(
        "请先设置环境变量 LM_STUDIO_API_KEY（本地 LM Studio 密钥）后再运行本探测脚本，"
        "切勿把真实密钥写入仓库。"
    )
KEY = LM_STUDIO_KEY
PROMPT = (
    "你是敏感信息识别专家。请从文本中找出个人敏感信息，"
    '只输出JSON：{"matches":[{"type":"employee_id","text":"NO.9527","confidence":0.9}]}'
    "\n\n文本：我的工号是NO.9527，地址在北京海淀区中关村大街1号"
)


def probe(label: str, reasoning: str) -> None:
    payload = {
        "model": "qwen3.5-0.8b",
        "input": PROMPT,
        "reasoning": reasoning,
        "max_output_tokens": 1024,
        "temperature": 0,
        "store": False,
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(BASE, json=payload, headers={"Authorization": f"Bearer {KEY}"}, timeout=REQUEST_TIMEOUT)
        data = r.json()
        outputs = data.get("output", [])
        messages = [o.get("content", "") for o in outputs if o.get("type") == "message"]
        reasoning_count = sum(1 for o in outputs if o.get("type") == "reasoning")
        stats = data.get("stats", {})
        content = messages[0] if messages else ""
        print(
            f"[{label} reasoning={reasoning}] {(time.perf_counter()-t0)*1000:.0f}ms | "
            f"messages={len(messages)} reasoning_items={reasoning_count} | "
            f"content={content[:90]!r}"
        )
        if stats:
            print(f"    stats: total_out={stats.get('total_output_tokens')} reasoning_tok={stats.get('reasoning_output_tokens')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] 异常: {type(exc).__name__} {exc}")


if __name__ == "__main__":
    for reasoning in ("off", "low", "high"):
        probe("A", reasoning)
