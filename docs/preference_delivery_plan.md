# 偏好记忆模块 — 开发交付规划

> Owner: 26a9835a | 更新：2026-08-01 | 对应赛题 (2)

## 一、已交付（v1 / 本周）

| 能力 | 状态 | 说明 |
|------|------|------|
| 结构化 preference_key | ✅ | `keys.py` 对齐 preference-keys-enum.md |
| extract → MemoryEntry | ✅ | Path B，不写 Milvus |
| 本地版本索引 | ✅ | `data/preferences/{user_id}.json`，含回滚 |
| 置信度公式 | ✅ | `confidence.py` 对齐设计文档 |
| SiliconFlow LLM | ✅ | 环境变量配置；无 key 时规则兜底 |
| 规则抽取（中英） | ✅ | 覆盖 `测试用例.md` 10 条 |
| 跨场景 scope/scene_id | ✅ | scene > global 读取 |
| 单测 | ✅ | `tests/test_preference_extract.py` |

**你现在可以给da0f41a1：** `extract()` / `extract_from_text()` 稳定接口 + 10 条样例期望 key。  
**给efca9634：** MemoryEntry 样例（含 metadata）；`get_version_history(user_id, preference_key)`。

---

## 二、下一阶段（联调前 / v1.1）— 怎么做

**阶段目标（本周可验收）：** LLM 能稳定抽偏好；评测集 ≥30 条交给da0f41a1；给efca9634一份「挂接清单」可直接改 Orchestrator。  
**你不负责：** 真写 Milvus、改 lifecycle 热度堆（那是cb0aeabe / efca9634）。

### 2.0 本周顺序（按天）

| 天 | 你做什么 | 交付物 |
|----|----------|--------|
| D1 | 配好硅基流动，跑通 LLM 单条抽取 | `scripts/pref_llm_smoke.py` 输出 1～2 条 MemoryEntry |
| D2 | 规则 vs LLM 对比 10 条样例，记差异表 | `benchmarks/results/rule_vs_llm.md` |
| D3 | 补 few-shot + 收紧 prompt；修抽错的 key/value | `prompts.py` 更新；准确率主观 ≥ 规则 |
| D4 | 扩写 20～30 条 JSONL（中英、多场景、否定句） | `benchmarks/datasets/preference_extraction.jsonl` |
| D5 | 写对接说明 + 样例 Entry 发给efca9634/da0f41a1 | 群里一条消息 + 文件路径 |

### 2.1 P0-A：硅基流动实跑（先做这个）

```powershell
cd E:\code\MemaryOS\MOKOA-master
$env:PYTHONPATH="."
$env:SILICONFLOW_API_KEY="sk-..."   # 本地设，勿提交
$env:SILICONFLOW_BASE_URL="https://api.siliconflow.cn/v1"
$env:SILICONFLOW_LLM_MODEL="Pro/deepseek-ai/DeepSeek-V3.2"

python -c "
from src.preference import PreferenceMemoryStore
s = PreferenceMemoryStore(use_llm=True, allow_rules=False)
print(s.extract_from_text('把桌面上所有 .tmp 删了，直接删不用问我', user_id='llm_smoke'))
"
```

**验收：** `allow_rules=False` 仍能抽出 `security.confirm_before_rm=false`（或等价 key）。  
若失败：检查 key / base_url / 模型名；把原始 LLM 回复临时 print 出来看 JSON 是否被截断。

### 2.2 P0-B：规则 vs LLM 对比（做评测的前提）

对 `测试用例.md` 10 条各跑两遍：

| 模式 | 配置 |
|------|------|
| 规则 | `use_llm=False, allow_rules=True` |
| LLM | `use_llm=True, allow_rules=False` |
| 融合（目标态） | `use_llm=True, allow_rules=True`（LLM 主、规则补洞） |

差异表至少记：`漏抽` / `多抽` / `key 错` / `value 错` / `scene 错`。  
**你改代码的方向：** 漏抽 → 加 few-shot；多抽/错 key → 收紧 system prompt「只抽耐久偏好」；value 非法 → 已有 `validate_value`，在 prompt 里贴枚举。

Few-shot 建议加在 `prompts.py` 的 user 侧（3～5 条即可），例如：

```text
Input: 直接删不用问我
Output: [{"category":"security","key":"confirm_before_rm","value":"false","scope":"scene","scene_id":"os_operation",...}]
```

### 2.3 P0-C：给da0f41a1的 JSONL（格式对齐 benchmarks/README）

路径：`benchmarks/datasets/preference_extraction.jsonl`（一行一条）

```json
{"id":"pref_001","input":{"content":"把桌面上所有 .tmp 文件删了，直接删不用问我","source":"chat"},"expected_preferences":["security.confirm_before_rm=false","workflow.preview_before_execute=false"],"task_type":"os_operation","user_id":"u1","scene_id":"os_operation"}
```

**30 条怎么凑：**

| 来源 | 条数 |
|------|------|
| 测试用例.md 10 条（原句） | 10 |
| 同义改写 / 英文各 1 | 10 |
| 否定句、跨场景、版本变更（D盘→E盘） | 5 |
| 负例（一次性任务，不应抽偏好） | 5 |

负例示例：`"帮我把这份文件打印出来"` → `expected_preferences: []`。  
交给da0f41a1时附：调用方式 `PreferenceMemoryStore.extract_from_text` + 匹配规则（key 集合 F1 或 key=value 精确）。

### 2.4 P1：Orchestrator 挂接（你写清单，efca9634改代码）

你 **不改** `orchestrator.py` 业务逻辑也可以，但要发出下面契约（可直接转发）：

```
1. consolidate / 或 process_input 之后调用：
   entries = preference.extract(raw_records)   # list[MemoryEntry]

2. 冲突：get_version_history(user_id, metadata["preference_key"])
   不要用 item.content[:32]

3. 入库（方案 2，cb0aeabe已定）：
   - create_memory(新 Entry, is_latest=true)
   - 若有旧条：update_memory(旧 id, metadata.is_latest=false)
   - 偏好模块不调这两个函数

4. Agent 读偏好优先：
   preference.get_feature_profile(user_id, scene_id)
   而不是只靠向量 RAG
```

你额外准备 **3 条 MemoryEntry JSON**（cb0aeabe文档里那 3 条即可），放 `benchmarks/datasets/preference_memory_entries.json`，方便他单测 `create_memory`。

### 2.5 P2：冲突人工确认（可压到 W3）

本阶段只做后端信号即可，前端可后做：

- 已有 `confidence.detect_conflict_and_suggest`
- 在 `extract` 后若同 key 多值且 gap&lt;0.15：在 `MemoryEntry.metadata` 加 `"needs_confirm": true, "confirm_prompt": "..."`  
- 6714f379 Preference 页读该字段弹窗；你确认后 `source` 升为 `manual_config`、confidence→0.95

### 2.6 本阶段完成定义（DoD）

- [x] `use_llm=True, allow_rules=False` 对至少 8/10 条样例抽到正确 key → **已达成 10/10**（2026-08-01）  
- [x] 规则 vs LLM 差异表 → `python scripts/pref_rule_vs_llm.py`（fusion 亦 10/10）  
- [x] `preference_extraction.jsonl` ≥ 30 条（含 ≥5 负例）→ **35 条** `benchmarks/datasets/preference_extraction.jsonl`  
- [x] 发给efca9634：挂接契约 + 3 条 Entry 样例 → `docs/preference_orchestrator_contract.md` + `benchmarks/datasets/preference_memory_entries.json`  
- [x] 发给da0f41a1：JSONL + 接口说明 → `benchmarks/datasets/preference_extraction.jsonl` + `README_preference.md`  
- [x] 现有 `pytest tests/test_preference_extract.py` 仍全绿  

### 2.7 不要在本阶段做的事

- 不要自己 import Milvus / 调 `create_memory`  
- 不要改 `layer_manager` 热度堆（只提醒他看 `memoryos_antipattern_audit.md`）  
- 不要为冲准确率硬编码全部 30 条规则（规则保底，LLM 泛化）

| 优先级 | 任务 | 对接 |
|--------|------|------|
| P0 | 硅基流动实跑 + few-shot + 规则对比 | 自测 |
| P0 | 导出 ≥30 条 JSONL | da0f41a1 |
| P1 | Orchestrator 挂接契约 + Entry 样例 | efca9634 |
| P1 | create_memory 方案 2（他改，你供样例） | efca9634 + cb0aeabe |
| P2 | `needs_confirm` metadata（可延后） | 6714f379 |

---


## 三、中期（v2 / 答辩前）

| 任务 | 目标 |
|------|------|
| 行为类来源 `repeated_behavior` 计数器 | 工具日志 ≥3 次一致才升级 |
| 时间衰减定时刷新 confidence | Preference 页展示可信度 |
| 准确率冲 ≥85% | 规则+LLM 融合策略写进技术文档 |
| 真实场景 Demo | 系统操作主线 + 办公/编程支线各 1 条链路录屏 |
| 遗忘对接 | 2abac5a6 NL forget → 本地索引删除 + Orchestrator delete_memory |

---

## 四、建议周计划

| 周 | 产出 |
|----|------|
| W1（当前） | v1 代码 + 单测绿 + 对接样例 |
| W2 | LLM 调优 + 评测集首版 ≥30 条 |
| W3 | Orchestrator / Milvus 联调（create + 版本历史查询） |
| W4 | Demo 页面 Preference 版本链 + 效果数字 |
| 答辩前 | 真机回归、文档章节、对比实验（有/无规则兜底） |

---

## 五、环境变量

```
SILICONFLOW_API_KEY=...
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_LLM_MODEL=Pro/deepseek-ai/DeepSeek-V3.2
```

勿把 key 写入仓库；参考 MemoryOS `test.py` 的本地配置方式即可。
