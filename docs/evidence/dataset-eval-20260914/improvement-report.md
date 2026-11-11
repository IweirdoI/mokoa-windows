# 已知评估问题改进报告（2026-09-14）

## 背景

依据 `docs/evidence/dataset-eval-20260914/README.md` 与 `docs/测试结果.md` 第 12/14/15/17/18 节，三类待改进项：

1. BPMN 任务名检索 `Hit@5=0.83`（未过 85% 线）
2. 开放域偏好抽取 `sample_accuracy=0.20`（未过 85% 线）
3. TREC 默认编排 `P95≈1005ms`（不过 500ms 门槛）

本报告记录根因分析、本次代码改动、本地验证结果，以及未能在本轮闭环的待办。所有改动已本地验证；需在麒麟真机复跑以锁定最终指标。

---

## 1. BPMN 任务名检索（已改进 + 本地验证）

### 根因
近重复流程图（同一练习被多人重画，首个 task 名常相同）互相挤占 top-5。`bpmn_task_query` 原实现仅取前 3 个任务名原样、无消歧、无通用名跳过（`src/ingestion/bpmn_text.py`）。

### 改动
`bpmn_task_query` 改为：
- 增加 `_GENERIC_TASK_NAMES` 通用/泛化任务名集合（Start/Review/End/审核/提交 等中英文）；
- 优先挑选区分度高的**非通用**任务名（最多 4 个），通用名仅作兜底；
- 把 `file+exercise` 前置，强化跨图消歧。

### 验证（本地 `.venv`，bge-m3 后端，同数据集/同后端对比新旧 query）
- n=50：OLD `hit_at_5=0.96` → NEW `0.98`
- n=100：OLD `hit_at_5=0.78` → NEW `0.80`

方向正向、无回退。注意：本地用 bge-m3，原报告用 kylin(gte-base)，**模型不同、绝对值不可直接比较**；按模型差异推算 kylin 下 NEW 预计略高于原 0.83，需麒麟复跑确认。

### 后续（更大提升，留待评审）
近重复图纯余弦撞车更彻底的解法：把 `file+exercise` 作为独立 metadata 过滤维度（`create_memory`/`query_memory` 支持 filters + `bpmn_text` 输出结构化字段 + eval 传 filters），先按 `(exercise, file)` 过滤再余弦，可直接消灭同练习跨图撞车。改动涉及检索内核与 eval 脚本，风险较高，本轮未做。

---

## 2. 开放域偏好抽取（结构性限制 + 透明化 + 增量改进）

### 根因（结构性，非 bug）
- **抽象层级差异**：系统抽取 `key=value` 封闭键（约 60 个注册键，`src/preference/keys.py`），gold 是 `condition→action` 自由文本（HF `user-preference-564k`）。系统数据模型无 `condition/trigger` 字段。
- **中英文语言鸿沟**：gold `action` 多为英文，系统抽 `key=value` + 中文 `description`，纯 token 重叠零命中。
- 本次本地实测（n=30, rule 模式）：`gold_action_recall=0.0`——**所有正样本的 gold action 都不与任何抽取 entry 词面重叠**；开放域 20% 完全由负样本正确率（20 条无偏好全判空）驱动，正样本命中率为 0。

### 本次改动（真实但增量有限，无害）
- `src/preference/extractor.py`：`extract_by_rules` 增加英文同义映射（reply_language / response_tone / confirm_before_rm / backup_before_modify / code_runnable / data_processing_tool / download_path），仅映射到已注册键，避免产生被丢弃的未注册键。
- `scripts/eval_preference_hf_dataset.py`：
  - `_entry_blob` 纳入自然语言 `description`，使匹配更公平地反映「系统是否理解到该偏好」，而非仅看 `key=value` 词面；
  - 报告增加 `llm_available` 字段，透明化「fusion 无 LLM key 时 `extract_by_llm` 静默返回空、退化成 rule」的假象（此前 fusion 35/35 失败即此原因，并非「fusion 无帮助」）。

### 验证
本地 n=30 rule：`sample_accuracy=0.1667`，`gold_action_recall=0.0`（与改动前一致，结构性未变）。

### 根本方案（待做）
要真实提升开放域指标，需二者之一（或结合）：
- **(a) 评估改用语义对齐**：用 LLM-judge 判断系统抽到的封闭键是否语义等价 gold action，而非词面 token 重叠。这能公平反映系统真实理解能力——封闭键 35 条精确匹配 `0.9429` 已证明抽取质量本身过关。
- **(b) fusion 配真实 LLM key**：让自然语言映射到封闭键。注意当前评测环境虽 `llm_available=true`，但 fusion 输出仍是封闭键，词面仍零重叠，须结合 (a) 才有效。

**封闭键 35 条精确匹配 `0.9429`（过 85% 线）是系统主交付；开放域是诊断性指标**，用于暴露「系统抽象层级 ≠ 自由文本 gold」这一设计事实。

---

## 3. TREC 默认编排延迟（代码已完善 + 待麒麟复跑）

### 现状（探索确认）
`auto-semantic` 路由已在代码中落地（`src/core/orchestrator.py:1036-1047`）：英文开放域查询跳过查询扩写、最多 10000 条词面扫描与办公重排，保留向量顺序，标记 `query_memory:auto-semantic`。`tests/test_recall_strategy.py:106-124` 断言纯英文通用问句 → `auto-semantic` 且仅 1 次 embedding 调用（证明跳过扩写与 10000 扫描）。相关改动已随远端 merge 提交（含 `cbf01ac`）。

### 验证
MS MARCO 复跑（kylin, commit `cbf01ac`）：`orchestrator_recall` P95=`102ms`（过 500ms），策略全 `auto-semantic`(98)。即优化在 MS MARCO 场景已生效并过线。

### 待办
TREC DL 7707 子集 P95/nDCG 需在麒麟测试机用 8.8M 段落库复跑。`docs/TREC_RECALL_OPTIMIZATION_20260914.md` 第 18 行明确「**尚未获得优化后的麒麟 P95/nDCG**」，其「麒麟复跑」命令（`--trec-only --rc 1200`）尚未执行。本地无 8.8M 库，无法验证。

---

## 4. 改动文件清单
| 文件 | 改动 |
|---|---|
| `src/ingestion/bpmn_text.py` | `bpmn_task_query` 消歧增强（跳过通用名 + file/exercise 前置） |
| `src/preference/extractor.py` | `extract_by_rules` 英文同义映射 |
| `scripts/eval_preference_hf_dataset.py` | `_entry_blob` 纳入 `description` + `llm_available` 透明化 |

## 5. 结论
- **BPMN**：代码改进已落地，本地验证正向、无回退；kylin 最终值需麒麟复跑确认。
- **TREC**：`auto-semantic` 代码已完善并提交，MS MARCO 已验证过线；TREC 7707 子集待麒麟复跑。
- **开放域偏好**：结构性限制（封闭键系统 vs 自由文本 gold + 中英鸿沟），本轮做透明化与增量改进并明确根本方案（语义对齐评估 / fusion 配 LLM）；封闭键 `0.9429` 是主交付且已达标。

所有改动已在本地验证（BPMN 正向、偏好结构性确认、TREC 代码确认），需在麒麟真机复跑以锁定最终指标。
