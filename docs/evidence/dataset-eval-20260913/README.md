# 2026-09-13 公开语料评测证据

给队友改分类器 / 质量门 / 对话召回用。原始语料在本机 `git/`（gitignore，约 3G），**没有**打进仓库。

## 文件

| 文件 | 环境 | 说明 |
|---|---|---|
| `dataset_eval_20260913-174834.*` | Windows BGE-M3 | 50 问 / 1200 段；6 库分类全是 preference；P95 880ms |
| `dataset_eval_20260913-210337.*` | 麒麟 Embedding | **分类器修复前**；同样 6/6 全 preference；P95 73ms |
| `dataset_eval_20260913-215909.*` | 麒麟 Embedding | **分类器修复后**（`66aeaf9`）；ToolBench/BPMN 已是 knowledge |
| `dialog_tool_20260913-221541.json` | 麒麟 Embedding | MultiWOZ + ToolBench 各 50 条；加载器修好后正文非空；查询仍用末轮用户句，Hit@5=0.18 |
| `dialog_tool_20260913-224246.json` | 麒麟 Embedding | **`7bbbfaf` 复跑**：`task_query` + 精确 memory_id Hit@5；MultiWOZ 0.98 / 末轮对照 0.24；ToolBench 仍 1.00 |
| `behavior_knowledge_20260913-230446.json` | 麒麟 Embedding | PersonaChat + BPMN 各 50 条；画像拆条 / BPMN 抽任务名；见第 12 节 |
| `dialog_quant_20260913-234930.json` | 麒麟 Embedding | DailyDialog + DIALOGBENCH 各 50 条；JSONL 坏行跳过、只索引 dialogue；见第 13 节 |
| `retrieval_20260914-001250.json` | 麒麟 Embedding | MS MARCO 50 问/1200 段 Recall@5=0.8646；TREC 官方 Recall 跳过；见第 14 节 |
| `dialog_tool_20260914-015405.json` | 麒麟 Embedding | **`21967f0` 八库复跑**：MultiWOZ Hit@5=0.98 / 接受 50/50；ToolBench Hit@5=1.00 / 接受 48/50 |
| `behavior_knowledge_20260914-015710.json` | 麒麟 Embedding | concat 接受 50/50；人设→behavior 0/50（产品拦截）；BPMN 任务名 Hit@5=0.92 |
| `dialog_quant_20260914-015759.json` | 麒麟 Embedding | DailyDialog / DIALOGBENCH 接受 50/50、Hit@5=1.0；末轮对照 0.88 / 0.96 |
| `retrieval_20260914-015120.json` | 麒麟 Embedding | MS MARCO Recall@5=0.8646（含 semantic 对照）；TREC 仍 skip |
| `retrieval_20260914-113522.json` | 麒麟 Embedding | **TREC 抽样**：97 judged 问，7707 段正例子库；Recall@5=0.1591，分级 nDCG@10=0.7306；官方 8.8M 仍 skip |

书面结论见仓库 `docs/测试结果.md` 第 11–16 节。

## 口径注意

- 174834 / 210337 的 MultiWOZ、ToolBench 数字**作废**：加载器字段对不上，正文大量为空。
- 215909 仍是各库 20 条抽测，不是全量。
- `dialog_tool_*` 才是对话/工具专项。221541 的 MultiWOZ Hit@5=0.18 是末轮「Thank you」对整段对话；224246 用 `task_query` 后为 0.98，同批末轮对照仍 0.24，不是向量引擎坏了。
- `behavior_knowledge_*` 才是用户行为/知识专项。PersonaChat concat 17/50 质量门 0.475；单次 behavior 0/50。BPMN 原始 XML Hit@5=0.58，抽任务名后 0.82。
- `dialog_quant_*` 才是量化评测对话专项。DIALOGBENCH 整文件 JSON 会 Extra data，按 JSONL 跳过坏行后 Hit@5=1.0；不是官方选项准确率。
- `retrieval_*` 才是知识检索专项。TREC 因缺 8.8M 段落库曾跳过官方 Recall；`retrieval_20260914-113522` 已按 pid 在 7707 段正例子库上实跑，仍不是 8.8M 全库 nDCG。MS MARCO v1.1 抽样 Recall@5=0.8646 不是 8.8M 全库成绩。
- `*_20260914-015*` 是 `21967f0` 对 git/ 八库的复跑。共享 staging TTL 会把编排接受率打成 0，以隔离临时 sqlite 的那几份 JSON 为准（015405 / 015710 / 015759 / 015120）。

## 复跑

```bash
# 七库抽测
export OS_AGENT_EMBEDDING_BACKEND=kylin
export OS_AGENT_MEMORY_BACKEND=memory
export OS_AGENT_KYLIN_STRICT=1
bash scripts/run_host_py.sh benchmarks/run_dataset_eval.py --seed 42 --n 20 --rq 50 --rc 1200

# 对话 + 工具专项
bash scripts/run_host_py.sh scripts/eval_dialog_tool_datasets.py --seed 42 --n 50

# 用户行为 + 知识专项
bash scripts/run_host_py.sh scripts/eval_behavior_knowledge_datasets.py --seed 42 --n 50

# 量化评测对话专项
bash scripts/run_host_py.sh scripts/eval_dialog_quant_datasets.py --seed 42 --n 50

# 知识检索专项（TREC 无段落库会 skip 官方全库；有 collection.tsv 则按 pid 抽样）
bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --seed 42 --nq 50 --rc 1200
bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --seed 42 --trec-only --rc 1200
```
