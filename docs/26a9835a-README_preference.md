# preference_extraction.jsonl — 对接说明（26a9835a → da0f41a1）

> **主线状态复核（2026-08-06）：** 本文描述的 35 条
> `preference_extraction.jsonl`、`extract_from_text()`、LLM / 规则融合实现目前仅在
> `origin/feature/preference`（提交 `723802f`）中，尚未合入当前 `master`。当前主线的
> `src/preference/store.py` 只有基础内存版本索引，`extract()` 仍返回空列表。下列调用
> 示例在合并该分支前不能直接运行。

## 文件

- `origin/feature/preference` 中的 `benchmarks/datasets/preference_extraction.jsonl`：**35 条**（≥30）

| 区间 | 内容 | 条数 |
|------|------|------|
| pref_001–010 | 测试用例原句 | 10 |
| pref_011–015 | 中文同义改写 | 5 |
| pref_016–020 | 英文改写 | 5 |
| pref_021–025 | 版本链 / 跨场景 | 5 |
| pref_026–030 | 负例（期望空） | 5 |
| pref_031–035 | 显式配置与扩展场景 | 5 |

## 字段

| 字段 | 说明 |
|------|------|
| `expected_preferences` | `preference_key=value` 列表；负例为 `[]` |
| `scene_id` / `task_type` | 场景标注 |
| `input.source` | `chat` / `manual_config` / `user_behavior` |

## 分支合并后的推荐匹配方式

1. 调用：`PreferenceMemoryStore(use_llm=True, allow_rules=True).extract_from_text(content, user_id=..., scene_id=...)`
2. 得到集合：`{f"{meta['preference_key']}={meta['preference_value']}" for ...}`
3. 对每条样本算 **key 级 recall**（至少命中 expected 的 key；value 可用精确或归一化匹配）
4. 负例：抽出 key 集合应为空（或忽略明显误抽后记 FP）

```python
from src.preference import PreferenceMemoryStore

store = PreferenceMemoryStore(use_llm=True, allow_rules=True)
entries = store.extract_from_text("直接删不用问我", user_id="bench", scene_id="os_operation")
got = {f"{e.metadata['preference_key']}={e.metadata['preference_value']}" for e in entries}
```

## 说明

- 线上默认 **Fusion**（LLM + 规则）；本数据集可用于 LLM-only / Fusion 对比。
- pref_021→022 为同用户同 key 版本演进；评测准确率时可按单条抽取计，版本链另测 `get_version_history`。
