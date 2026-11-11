# 偏好记忆模块（26a9835a）

对应赛题 **(2)**：操作习惯 / 输出风格 / 安全策略等偏好的自动提取、版本化、跨场景适配。

## 入库路径（Path B）

```
extract() → list[MemoryEntry]
         → 本地版本索引 data/preferences/{user_id}.json
Orchestrator.store_memory_entry / extract_preferences_from_text
         → 长期偏好画像（与 short→mid 热度门独立）
```

本模块 **不** `import MilvusClient`，**不**直接调用底层 `create_memory()`（由编排层 `store_memory_entry` 写入）。

### 短期候选 vs 已生效偏好

| 层级 | 如何产生 | 面板/API |
|------|----------|----------|
| 短期候选 | `/ingest` → `process_input` 写入 short；总览 `preference_count` 可计入 | 总览短期层；**不是**偏好页「当前偏好」 |
| 已生效长期偏好 | `POST /preferences/extract`，或 ingest 时规则命中后并行 Path B 提取入库 | `GET /preferences/{user}`、版本历史、`get_preference_profile` |

聊天句如「以后回答请更加简洁」：仅 `/ingest`+`/recall` 时短期会有候选；编排在规则命中后会自动 Path B 提取，写入长期画像（**不**跳过 short→mid 质量/热度门；知识晋升仍走 consolidate）。前端亦可显式调 `/preferences/extract` 做确认流。

## 规则 / LLM / 降级

| 模式 | 配置 | 行为 |
|------|------|------|
| 规则 | `use_llm=false, allow_rules=true` | 只走 `extract_by_rules`，离线可复现 |
| LLM | `use_llm=true, allow_rules=false` | 只走硅基流动；失败则空结果（无规则兜底） |
| 融合（默认） | `use_llm=true, allow_rules=true` | 先 LLM；无密钥/超时/空结果时**降级规则**；规则可补漏 |

配置见 `configs/default.yaml` 的 `preference:` 段（与顶层 `llm` 安全语义分离）。

主线接线：`Orchestrator.extract_preferences_from_text` → `store_memory_entry`；API：`POST /preferences/extract`，历史：`GET /preferences/{user}?latest_only=false`。

## 快速试用

```bash
# 可选：硅基流动（与 MemoryOS test.py 相同配置方式）
set SILICONFLOW_API_KEY=sk-xxx
set SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
set SILICONFLOW_LLM_MODEL=Pro/deepseek-ai/DeepSeek-V3.2

pytest tests/test_preference_extract.py -v
python scripts/pref_eval_dataset.py --mode rule
python scripts/pref_seed_demos.py
```

```python
from src.preference import PreferenceMemoryStore

store = PreferenceMemoryStore(use_llm=False)  # 离线规则
entries = store.extract_from_text("把桌面上所有 .tmp 文件删了，直接删不用问我")
for e in entries:
    print(e.model_dump())
```

## 文件

| 文件 | 职责 |
|------|------|
| `keys.py` | preference_key 枚举注册表 |
| `confidence.py` | 置信度公式 |
| `extractor.py` | LLM + 规则抽取 |
| `llm_client.py` | SiliconFlow 客户端 |
| `version_index.py` | 本地版本链 |
| `store.py` | `PreferenceMemoryStore` 对外 API |
| `models.py` / `prompts.py` | 模型与提示词 |

## MemoryEntry.metadata 约定

与cb0aeabe对接文档一致：`preference_key` / `preference_value` / `category` / `version` / `is_latest` / `supersedes_memory_id` / `scene_id` / `scope`。

## 工业优化（相对 MemoryOS 弊端）

| MemoryOS 问题 | 本模块做法 |
|---------------|-----------|
| 每次更新整文件 IO | dirty + `extract()` 末尾一次 `flush()`；原子写 |
| 自由文本用户画像 | `get_feature_profile()` 结构化 kv/confidence/rank_score |
| session 向量创建后不刷新 | 偏好 `content` 用 `[preference] key=value`，值变则向量输入变 |
| 中期热度 peek 不淘汰 | **不在本模块**；见 `docs/memoryos_antipattern_audit.md` 给efca9634的红线 |

```python
profile = store.get_feature_profile("demo_user", scene_id="os_operation")
# profile["kv"] / profile["agent_context"] 注入 Agent，不要拼散文画像
```
