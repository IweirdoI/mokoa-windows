# preference_extraction.jsonl — 对接说明（26a9835a → da0f41a1）

## 文件

- `benchmarks/datasets/preference_extraction.jsonl`：**35 条**（≥30）

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

## 推荐匹配方式

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
- **计分约定**：本轮保留 `expected_preferences` 原标签计分，不为抬分改标签或硬补规则。准确率定义与独立验证集由评测侧（da0f41a1）固定后再对比 rule / LLM / fusion。

## 标注待审（不改标签，仅记录争议）

| id | 原文要点 | 当前标签 | 争议 |
|----|----------|----------|------|
| pref_011 | 「以后下载默认放到 D 盘的项目目录里」 | `habit.download_path=D:/Downloads` + `habit.file_naming_rule=project_prefix` | 「项目目录」未必等于 Downloads；原文未明确「文件命名前缀」 |
| pref_016 | 「Move the file from Desktop to the D: drive project folder」 | 同上 | 单次移动文件，未明确持久默认下载偏好；project folder ≠ Downloads |

漏提取整改（规则侧已补，仍按原 expected 计分）：`pref_025`→`style.code_runnable`；`pref_032`→`tool_selection.web_scraping_tool`；`pref_034`→`workflow.backup_before_modify`；`pref_035`→`style.report_format=pdf`。
