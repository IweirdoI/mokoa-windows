"""P0-A: SiliconFlow preference LLM smoke test.

Usage (PowerShell):
  cd E:\\code\\MemaryOS\\MOKOA-master
  $env:PYTHONPATH = "."
  $env:SILICONFLOW_API_KEY = "sk-..."   # required; do not commit
  # optional:
  # $env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
  # $env:SILICONFLOW_LLM_MODEL = "Pro/deepseek-ai/DeepSeek-V3.2"
  python scripts/pref_llm_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    # 优先从项目根 .env 加载密钥（不写死在脚本/配置里）
    try:
        from src.common.env import load_env
        load_env()
    except Exception:
        pass

    key = os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        print("FAIL: 未设置 SILICONFLOW_API_KEY（或 OPENAI_API_KEY）")
        print("PowerShell 示例：")
        print('  $env:SILICONFLOW_API_KEY = "sk-你的密钥"')
        return 1

    print("config:")
    print(f"  base_url = {os.getenv('SILICONFLOW_BASE_URL', 'https://api.siliconflow.cn/v1')}")
    print(f"  model    = {os.getenv('SILICONFLOW_LLM_MODEL', 'Pro/deepseek-ai/DeepSeek-V3.2')}")
    print(f"  api_key  = {key[:6]}...{key[-4:]} (len={len(key)})")
    print()

    from src.preference.llm_client import SiliconFlowClient
    from src.preference.store import PreferenceMemoryStore

    client = SiliconFlowClient()
    # raw ping — see model reply before JSON parsing
    try:
        ping = client.chat_completion(
            [
                {"role": "user", "content": '只回复 JSON：[{"ok": true}]'},
            ],
            max_tokens=64,
        )
        print("LLM ping raw:", ping[:300])
    except Exception as e:
        print("FAIL: LLM ping 失败:", e)
        return 2

    text = "把桌面上所有 .tmp 文件删了，直接删不用问我"
    store = PreferenceMemoryStore(
        storage_dir=ROOT / "data" / "preferences_smoke",
        llm_client=client,
        use_llm=True,
        allow_rules=False,  # 强制只走 LLM
    )
    print(f"\ninput: {text}")
    print("extracting (LLM only)...")
    try:
        entries = store.extract_from_text(text, user_id="llm_smoke", scene_id="os_operation")
    except Exception as e:
        print("FAIL: extract 异常:", e)
        return 3

    if not entries:
        print("FAIL: 未抽出任何偏好。可检查 prompts / 模型是否返回合法 JSON。")
        return 4

    print(f"OK: got {len(entries)} entries")
    for e in entries:
        print(
            json.dumps(
                {
                    "preference_key": e.metadata.get("preference_key"),
                    "preference_value": e.metadata.get("preference_value"),
                    "confidence": e.confidence,
                    "scene_id": e.scene_id,
                    "content": e.content,
                },
                ensure_ascii=False,
            )
        )

    keys = {e.metadata.get("preference_key") for e in entries}
    target = "security.confirm_before_rm"
    if target in keys:
        print(f"\nPASS: 命中验收 key `{target}`")
        return 0

    print(f"\nWARN: 未命中 `{target}`，但已有输出。请对照 keys 看是否抽到了等价安全偏好。")
    print("got keys:", sorted(k for k in keys if k))
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
