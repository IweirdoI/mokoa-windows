"""临时验证：本地 LM Studio (phi-3.1-mini) 跑通 偏好抽取→入库→去重 完整链路。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import openai  # noqa: F401
except ImportError:
    pytest.skip("未安装 openai，请先 pip install openai", allow_module_level=True)

from src.preference.llm_client import SiliconFlowClient
from src.preference.store import PreferenceMemoryStore

KEY = os.getenv("SILICONFLOW_API_KEY")
if not KEY:
    pytest.skip("未设置 SILICONFLOW_API_KEY，跳过本地 LLM 偏好验证", allow_module_level=True)

client = SiliconFlowClient(
    api_key=KEY,
    base_url="http://127.0.0.1:1234/v1",
    model="phi-3.1-mini-128k-instruct",
)
store = PreferenceMemoryStore(
    storage_dir=Path("./data/pref_test_local"),
    llm_client=client,
    use_llm=True,
    allow_rules=True,
)


def summ(e):
    m = getattr(e, "metadata", {}) or {}
    return (m.get("preference_key"), m.get("preference_value"), getattr(e, "id", None))


text = "以后回答我都用中文，报告导出成 PDF。"
print("=== 第一次抽取 + 入库 ===")
e1 = store.extract_from_text(text, user_id="test_user", scene_id="office", source="conversation")
for e in e1:
    print("  ", summ(e))

print("\n=== 重复输入（应复用，不新建）===")
e2 = store.extract_from_text(text, user_id="test_user", scene_id="office", source="conversation")
for e in e2:
    print("  ", summ(e)) 

# 严谨去重验证：重复同值不应新增版本
hist = store.get_version_history("test_user", "style.report_format")
n_versions = len(hist)
latest_obs = hist[-1].metadata.get("n_observations") if hist else None
print(f"\n去重验证: style.report_format 版本数={n_versions} (期望1), 最新n_observations={latest_obs}")
print("结论:", "OK 去重生效（未新建版本）" if n_versions == 1 else "WARN 产生了重复版本")
