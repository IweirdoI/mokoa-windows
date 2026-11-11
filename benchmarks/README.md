# 测试与 Benchmark 指南

> **模块负责人：da0f41a1** | 对应比赛要求 **(7) 量化评测机制** | 协助：`tests/`、`docs/` 效果验证章节

本文档说明：**评测内容、测试方法、模块对接关系、交付材料**。

> **当前代码状态（2026-08-06）：** `tests/test_metrics.py` 中四项指标测试仍全部
> `skip`；`benchmarks/` 目前已有 MemoryEntry 样例和3fba60e1三类 ingestion Demo。
> 下文其余标准数据集、跑分脚本和结果目录均为
> 规划项，不能作为已经完成的评测交付物。

---

## 一、测试模块核心任务

比赛初评 **性能指标占 25 分**，本模块需使用 **标准化数据集 + 自动化脚本** 验证以下 4 项硬性指标，并写入提交用的 **效果验证报告**：

| 指标 | 目标 | 测试文件 |
|------|------|----------|
| 偏好提取准确率 | ≥ **85%** | `tests/test_metrics.py::test_preference_extraction_accuracy` |
| 知识检索召回率 | ≥ **85%** | `tests/test_metrics.py::test_knowledge_retrieval_recall` |
| 知识检索响应时间 | ≤ **500ms** | `tests/test_metrics.py::test_retrieval_latency` |
| 知识冲突处理正确率 | ≥ **88%** | `tests/test_metrics.py::test_conflict_resolution_correctness` |

除上述 4 项外，还需做 **功能测试**（各模块能否正常工作）和 **对比实验**（如：有/无冲突消解），一并写入技术文档。

---

## 二、目录结构（当前与规划）

```
benchmarks/
├── README.md                 ← 本文件
├── run_benchmarks.py         ← 【规划】一键跑全部指标，输出 report.json
├── report_template.md        ← 【规划】效果验证报告模板
├── datasets/                 ← 标准测试数据集
│   ├── preference_memory_entries.json ← 【已有】MemoryEntry 样例
│   ├── ingestion_demo.jsonl           ← 【已有】三类 ingestion Demo 样例
│   ├── preference_extraction.jsonl    ← 【规划】
│   ├── knowledge_retrieval.jsonl      ← 【规划】
│   ├── conflict_resolution.jsonl      ← 【规划】
│   ├── security_forget.jsonl          ← 【规划】
│   └── ingestion_pipeline.jsonl       ← 【规划】
└── results/                  ← 【规划】每次跑分输出（gitignore，本地生成）
    └── report_20260729.json
```

规划中的自动化入口：

```bash
# 开发机联调
python benchmarks/run_benchmarks.py

# pytest 断言（CI / 回归）
pytest tests/test_metrics.py -v
```

---

## 三、四项硬性指标：测什么、怎么测、找谁

### 3.1 偏好提取准确率 ≥ 85%

**测什么：** 从用户输入 / 行为数据中，系统能否正确提取偏好（操作习惯、输出风格、安全策略等）。

**调用链：**

```
datasets/preference_extraction.jsonl
  → ingestion（可选）→ preference 模块 extract
  → 对比 expected_preferences 标注
```

**对接：偏好记忆模块（26a9835a）**

| 需要26a9835a提供 | 说明 |
|----------------|------|
| 稳定的 `extract()` 接口 | `src/preference/store.py` |
| 5～10 条真实样例 | 清洗前后 JSON，用于数据集 |
| 版本化/跨场景用例 | 同一偏好在不同 `scene_id` 下是否一致 |

当前主线中的 `PreferenceMemoryStore.extract()` 仍直接返回空列表，且尚无
`extract_from_text()`；因此本指标目前不能得到有效跑分。26a9835a的完整提取实现仍位于
`origin/feature/preference`，合并时需要先处理公共接口与配置冲突。

**数据集格式示例**（`datasets/preference_extraction.jsonl`，一行一条）：

```json
{"id": "pref_001", "input": {"content": "以后尽量用中文，写代码要可直接运行", "source": "chat"}, "expected_preferences": ["中文回答", "可运行代码"], "task_type": "coding", "user_id": "u1"}
{"id": "pref_002", "input": {"content": "下载文件默认保存到 D 盘", "source": "user_behavior"}, "expected_preferences": ["下载路径偏好 D 盘"], "task_type": "os_operation", "user_id": "u1"}
```

这里的 `chat` 是偏好数据集标签；若样本先经过 `IngestionPipeline` 或 `/ingest`，
必须映射为 ingestion 支持的 `conversation`。

**准确率计算：**

```
准确率 = 正确提取的偏好条目数 / 标注偏好条目总数
```

匹配方式可用：关键词包含、集合 F1、或 LLM-as-judge（需在报告里说明方法）。

**相关代码：**

- `src/preference/store.py`
- `src/knowledge/memory_service/memory_schema.py`（`memory_type=preference`）

---

### 3.2 知识检索召回率 ≥ 85%

**测什么：** 给定 query，能否从 Milvus 中召回 **应该命中** 的记忆（工具经验、文档片段、项目上下文等）。

**调用链：**

```
先用 create_memory 写入种子记忆
  → datasets/knowledge_retrieval.jsonl 中的 query
  → query_memory(query + filters)
  → 检查 relevant_memory_ids 是否出现在 top_k 结果中
```

**对接：知识检索模块（cb0aeabe）**

| 需要cb0aeabe提供 | 说明 |
|----------------|------|
| 可用的 `create_memory` / `query_memory` | `src/knowledge/memory_service/` |
| Milvus 可连环境或 mock 说明 | 开发期 mock，答辩前真机 |
| 种子数据导入脚本（可选） | 批量写入测试记忆 |

当前 `MilvusClient` 是进程内字典 mock：能按字段过滤和限制 `top_k`，但不会根据
embedding 做相似度排序，也不会返回可供验收的真实向量召回结果。召回率指标必须在
真实 Milvus（或等价的确定性向量测试后端）接入后再计算。

**对接：编排核心模块（efca9634，协助）**

- Orchestrator 的 `recall()` 若走统一入口，联调时确认调用路径。

**数据集格式示例：**

```json
{"id": "ret_001", "user_id": "u1", "query": "pip 安装超时怎么办", "relevant_memory_ids": ["mem_tool_001"], "filters": {"memory_type": "tool_call"}, "top_k": 5}
{"id": "ret_002", "user_id": "u1", "query": "比赛记忆优化要求有哪些", "relevant_memory_ids": ["mem_doc_001"], "filters": {"memory_type": "document_chunk"}, "top_k": 5}
```

**召回率计算（常用）：**

```
Recall@K = |检索结果 ∩ 相关记忆| / |相关记忆|
```

对每条 query 算 Recall@5，再取平均。

**相关代码：**

- `src/knowledge/memory_service/query_memory.py`
- `src/knowledge/memory_service/create_memory.py`
- `src/knowledge/store.py`

---

### 3.3 检索响应时间 ≤ 500ms

**测什么：** 单次 **query → embedding → Milvus 检索 → 返回** 的端到端延迟。

**注意：**

- 比赛指标是 **检索响应时间**，建议测 **P50 / P95**，报告里写清楚。
- 当前 mock 的耗时只适合接口回归，不能作为真实 Milvus / 麒麟 embedding 的答辩性能数据。
- 需在 **两种环境** 各测一版（报告分栏）：

| 环境 | 用途 | 对接 |
|------|------|------|
| 开发机（Windows + local embedding + mock Milvus） | 日常回归 | cb0aeabe mock |
| 麒麟真机（Kylin SDK + Milvus Lite/集群） | **正式答辩数据** | efca9634 Embedding + cb0aeabe Milvus |

**对接：知识检索模块（cb0aeabe）+ 编排核心模块（efca9634）**

| 模块 | 提供 |
|------|------|
| cb0aeabe | `query_memory` 返回 `latency_ms` 字段；Milvus 部署方式 |
| efca9634 | 麒麟 Embedding 真机联调时间窗口；`docs/KYLIN_SDK.md` 环境 |

**测试方法：**

```python
# 对 knowledge_retrieval.jsonl 每条 query 跑 N 次，统计 P50/P95
assert p50_ms <= 500
assert p95_ms <= 500  # 建议也写进报告，答辩更稳
```

**相关代码：**

- `src/knowledge/memory_service/query_memory.py`（已有 `latency_ms`）
- `src/embedding/embedding_interface.py`

---

### 3.4 冲突处理正确率 ≥ 88%

**测什么：** 新旧记忆矛盾时，系统是否做出 **正确** 的 keep / replace / merge 决策。

**典型场景：**

| 旧记忆 | 新信息 | 期望 |
|--------|--------|------|
| 喜欢 Java（历史行为） | 偏好 Python（用户主动说） | **replace** → Python |
| 喜欢详细解释 | 回答要简短（同任务类型） | **冲突检测**，按 task_type 区分或 merge |
| 旧工具安装失败经验 | 新环境已安装成功 | **update** 或 merge |

**调用链：**

```
datasets/conflict_resolution.jsonl
  → conflict_resolver.resolve_*()
  或 update_memory() 全流程
  → 对比 expected_action / expected_content
```

**对接：编排核心模块（efca9634，主）+ 知识检索模块（cb0aeabe，辅）**

| 模块 | 提供 |
|------|------|
| efca9634 | `src/core/conflict_resolver.py` 稳定接口；Orchestrator `resolve_and_store` |
| cb0aeabe | `update_memory` 更新后 Milvus 中 content/version 是否正确 |

`ConflictResolver` 已有单元测试，但标准冲突数据集和
`test_conflict_resolution_correctness` 的实际计分逻辑尚未完成。

**数据集格式示例：**

```json
{
  "id": "conf_001",
  "old": {"content": "用户喜欢 Java 开发", "metadata": {"source_type": "behavior"}, "version": 1},
  "new": {"content": "用户偏好 Python 开发", "metadata": {"source_type": "chat"}},
  "expected_action": "replace",
  "expected_content_contains": "Python"
}
```

**相关代码：**

- `src/core/conflict_resolver.py`
- `src/core/orchestrator.py` → `resolve_and_store()`
- `src/knowledge/memory_service/update_memory.py`

---

## 四、功能测试清单（除 4 项指标外，建议全测）

这些写入 **功能测试报告**，证明「方案完整性」（复评 60 分相关）。

### 4.1 多源接入 — 对接 **3fba60e1**

| 测试项 | 通过标准 | 代码位置 |
|--------|----------|----------|
| 工具执行结果接入 | 输出符合 `NormalizedRecord` | `ingestion/collector.py` |
| 用户行为接入 | 同上 | `ingestion/collector.py` |
| 手动配置接入 | 同上 | `ingestion/collector.py` |
| 对话接入 | 同上 | `ingestion/collector.py` |
| 去重 / 去噪 | TTL 内重复输入被拒绝；正式 API 使用 SQLite，临时实例使用内存 fallback | `ingestion/pipeline.py`、`ingestion/staging_store.py` |
| 格式标准化 | 字段含 content、confidence、source | `ingestion/normalizer.py` |
| 质量门控 | 低价值数据被 `quality.py` 拒绝 | `ingestion/quality.py` |

当前已有 `datasets/ingestion_demo.jsonl`，提供工具结果、用户行为、手动配置三类可直接
演示的「输入 → 标准化结果」样例，并由 pytest 自动重放。完整 benchmark 后续仍需补
对话来源，并扩充为四类数据源各 3 条，落到规划的 `ingestion_pipeline.jsonl`。

---

### 4.2 短/中/长期记忆流转 — 对接 **编排核心模块（efca9634）**

| 测试项 | 通过标准 | 代码位置 |
|--------|----------|----------|
| 短期写入 | 新交互进入 short-term | `core/layer_manager.py` |
| 中期晋升 | 达热度阈值后 promote | `core/layer_manager.py` |
| 长期分流 | 偏好 → preference；知识 → knowledge | `core/orchestrator.py` |
| 记忆分类 | 「喜欢 Python」→ preference | `core/classifier.py` |

**对接efca9634：** 一条完整链路 Demo 脚本（输入 → 短期 → 晋升 → 入库）。

---

### 4.3 记忆 CRUD — 对接 **知识检索模块（cb0aeabe）**

| 测试项 | 通过标准 | 代码位置 |
|--------|----------|----------|
| create_memory | 返回 memory_id，Milvus 可查 | `create_memory.py` |
| query_memory | 当前按 id / filter 可做 mock 联调；真实向量排序待实现 | `query_memory.py` |
| update_memory | content 变更后 embedding 更新 | `update_memory.py` |
| delete_memory | 软删 / 硬删生效 | `delete_memory.py` |
| memory_policy | 低分条目拒绝入库 | `memory_policy.py` |

**对接cb0aeabe：** 五类 `memory_type` 各 1 条可写入的 `MemoryEntry` 样例（见 `memory_schema.py`）。

---

### 4.4 安全与遗忘 — 对接 **安全遗忘模块（2abac5a6）**

| 测试项 | 通过标准 | 代码位置 |
|--------|----------|----------|
| 敏感信息过滤 | 手机号/Token 被脱敏或拦截 | `security/service.py` |
| NL 精准遗忘 | 删除级联已接线；NL 指令到 memory ID 的解析已实现（forget_parser，P0 验收通过）；知识库按 NL 条件批量定位遗忘已实现（knowledge_forget.py） | `security/` + `delete_memory.py` |

**规划数据集：** `datasets/security_forget.jsonl`（当前尚未创建）

```json
{"id": "sec_001", "setup_memory": "这是临时测试内容", "nl_instruction": "忘掉刚才的测试", "expect_deleted": true}
{"id": "sec_002", "input": "我的手机号是 13800138000", "expect_filtered": true, "expect_not_stored_raw": true}
```

---

### 4.5 演示与 API — 对接 **前端模块（6714f379）+ 编排核心模块（efca9634）**

| 测试项 | 对接 |
|--------|------|
| Web Demo 与测试场景一致 | 6714f379：Demo 走同一套 query |
| REST API `/ingest` `/recall` `/forget` | efca9634：`api/server.py` |

---

## 五、对比实验建议（写进技术文档）

| 实验 | 对比什么 | 目的 |
|------|----------|------|
| A/B-1 | 有 vs 无 **冲突消解** | 证明 conflict_resolver 价值 |
| A/B-2 | 有 vs 无 **quality 门控** | 证明智能筛选 |
| A/B-3 | **Kylin embedding** vs local embedding | 国产化适配与延迟 |
| A/B-4 | 仅向量检索 vs 向量 + filter | 证明 memory_type 分类有效 |

---

## 六、交付物清单（提交前核对）

| 交付物 | 路径 / 形式 | 截止时间参考 |
|--------|-------------|--------------|
| 标准数据集 + 说明 | `benchmarks/datasets/` + 文档章节 | 8 月中旬 |
| 自动化跑分脚本 | `benchmarks/run_benchmarks.py` | 8 月中旬 |
| pytest 指标测试 | `tests/test_metrics.py`（去掉 skip） | 8 月下旬 |
| 效果验证报告 | 填入 `report_template.md` → 并入 Word/PDF | 9 月上旬 |
| 功能测试报告 | checklist 结果表 | 9 月上旬 |
| 对比实验章节 | 技术文档一节 | 9 月上旬 |
| 麒麟适配测试记录 | 与 `docs/KYLIN_SDK.md`  checklist 对齐 | 答辩前 |

比赛方案还要求：**至少 1 个真实场景应用案例** — 与6714f379 Demo 对齐，在报告中写同一场景的测试数据。

截至 2026-08-08，已有 `preference_memory_entries.json` 和三类 ingestion Demo 样例；
标准指标数据集、跑分脚本和报告当时尚未完成。**更新（2026-09-14）：** 现
`benchmarks/datasets/` 已含多类数据集（jsonl/json），`run_benchmarks.py` /
`run_dataset_eval.py` 跑分脚本与 `benchmarks/results/` 多份真实报告（含 Recall@5、
延迟 P95 等）均已落地，请同步勾选状态并把实际命令、环境与原始结果写入报告。

---

## 七、对接关系总表（速查）

| 测试内容 | 对接模块 | 所需内容 |
|------------|----------|----------|
| 偏好准确率 | **偏好记忆（26a9835a）** | extract 接口 + 样例 |
| 检索召回率 / 延迟 | **知识检索（cb0aeabe）** | query/create 可用 + Milvus |
| 延迟（Embedding） | **Embedding（efca9634）** | 麒麟真机联调 |
| 冲突正确率 | **编排核心（efca9634）** | conflict_resolver 稳定 |
| 入库/更新正确性 | **知识检索（cb0aeabe）** | update/delete 行为 |
| 多源清洗 | **多源接入（3fba60e1）** | 四类数据源样例 |
| 敏感/遗忘 | **安全遗忘（2abac5a6）** | filter + NL 解析 |
| 记忆流转/分类 | **编排核心（efca9634）** | layer_manager + classifier |
| Demo 一致性 | **前端模块（6714f379）** | 演示脚本与测试 query 统一 |
| 架构/联调问题 | **编排核心（efca9634）** | 项目负责人 |

**建议：每周例会前跑一遍 `pytest tests/ -v`，把失败项对接给对应模块同学。**

---

## 八、推荐工作顺序（按周）

| 阶段 | 任务 |
|------|------|
| **第 1 周** | 创建 `datasets/` 目录结构；写好 JSONL 格式；`report_template.md` |
| **第 2 周** | 向各模块要样例；手工标注首批 30 条/类 |
| **第 3 周** | 实现 `run_benchmarks.py`；去掉 `test_metrics.py` 的 skip |
| **第 4 周** | 功能测试 checklist 全跑；出第一版报告 |
| **答辩前** | 麒麟真机 + Milvus 正式环境复测；更新最终数字 |

---

## 九、常见问题

**Q：模块还没写完，能先测吗？**  
可以。用 mock（cb0aeabe的 `milvus_client.py` 已支持）测流程；模块就绪后替换为真实调用，数据集不用改。

**Q：达不到 85% 怎么办？**  
比赛方案写「未达基础指标需 **明确优化方向**」— 报告里如实写当前值 + 改进计划，不要编造数据。

**Q：数据集从哪来？**  
优先：团队手工标注 OS Agent 场景 → 问发榜单位是否提供 → 参考 LoCoMo 改写（报告注明来源）。

**Q：能改业务代码吗？**  
以测试为主；发现 bug 提 issue 给对应模块，Fix 由模块负责人改。

---

## 十、相关文件索引

| 文件 | 说明 |
|------|------|
| `tests/test_metrics.py` | 4 项硬性指标 pytest |
| `src/knowledge/memory_service/memory_schema.py` | 入库 JSON 格式 |
| `src/knowledge/memory_service/README.md` | cb0aeabe CRUD 说明 |
| `docs/KYLIN_SDK.md` | 麒麟 Embedding 适配 checklist |
| `docs/ARCHITECTURE.md` | 全队架构 |
| 项目根 `README.md` | 分工与数据流 |

API / 联调问题 → **编排核心模块（efca9634）** | 业务细节 → 对应模块同学
