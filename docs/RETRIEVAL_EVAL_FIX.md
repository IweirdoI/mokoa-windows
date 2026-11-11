# 2026-09-14 检索复测修正

本次基于远程 `retrieval_20260914-001250.json`，保留原始证据，不改历史跑分。

- 统一二值 Recall/MRR/nDCG 实现：Recall@5 分母为全部 gold；nDCG 理想排序
  也以全部 gold 计算。重复结果只贡献一次相关性，无 gold 的问题跳过。
- `--rc` 是干扰语料目标，不能截掉已抽中问题的候选/金标准；超出目标会明确记录。
- 两个评测入口直接插入的语料都写入 `is_latest=true`，编码数量不匹配时拒绝部分入库。
- 新增 `RetrievalQuery.retrieval_mode="semantic"`，HTTP `/recall` 同名字段可用。
  该模式保留向量顺序，不套办公主题词面过滤，仍保留用户、类型、场景与版本过滤。
  默认 `auto` 的项目隔离、词面补召和空结果保护不变。
- 专项脚本同时输出 `query_memory`、原有 `orchestrator_recall` 和新的
  `orchestrator_semantic`，无需改数据或样本即可对比。

麒麟复跑：

```bash
git pull --ff-only origin master
export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --seed 42 --nq 50 --rc 1200
```

TREC 可增加 `--trec-collection /path/to/collection.tsv`，流式按 `pid<TAB>正文`
核对 qrels 正例覆盖（沿用当前 min_rel=1）。不会按 QA query_id 或相似正文猜 passage_id。
报告给出缺失 PID 数及示例。此入口只检查正文对齐，不运行 8.8M 全库 TREC 排名；
即使正例齐全，也不能用只有正例的子库宣称官方成绩。未提供正文仍明确跳过。

本机未运行新麒麟指标，也未下载 8.8M 全库。下一步比较三条路径的 Recall@5、
nDCG@10、空结果数、策略分布和 P95，核对 sampled_indices 与旧报告一致。

---

## 2026-09-14（续）补修评测口径遗留两项

来自检索复测五点复查（图例 #1、#3）。#2/#4/#5 此前已修复，本轮补齐剩余两项。

### #1 评测子库无随机负例，Hit@1=1.0 被放大

- 根因：`scripts/eval_retrieval_datasets.py` 的 `run_trec_sampled` 中
  `n_distractors = max(0, rc - len(required))`。`--rc` 原语义是「总语料上限」，默认 1200，
  而 judged 正例 `required` 约 7707，于是 `1200 - 7707 < 0` → `max(0, ...) = 0`，
  子库只剩正例，每个分段都是某问的正例，没有任何真实负例，Hit@1 恒为 1.0（虚高）。
- 修复：把 `--rc` 语义改为「叠加在 judged 金标准正例之上的随机干扰段数量」（从 8.8M
  全库 reservoir 抽样）。新增纯函数 `distractors_target(rc, n_required)` 返回 `max(0, rc)`，
  与正例数无关；默认 `--rc` 提到 5000。scope 文案现在明确标注 `plus N random
  distractors sampled from 8.8M collection`，并在 `n_distractors==0` 时提示 Hit@1 仅供对照。
- 影响：现在跑出的 Hit@1 是在「正例 + 真实随机负例」子库上的结果，不再被语料结构放大；
  若显式传 `--rc 0` 仍回退到仅正例子库（scope 同步标注夸大警示）。

### #3 默认编排 P95 破 500ms：英文开放域仍走办公词面补召

- 根因：`src/core/orchestrator.py` 默认 `auto` 路径
  `should_lexical = memory_type != PREFERENCE and (know or missing_terms)`。
  英文开放域知识问句既命中 `knowledge_intent`（含 install/configure/docker 等英文提示词），
  又因向量池里找不到英文词而触发 `missing_terms`，从而补一轮「办公主题中文词面门」：
  对 1 万级全表做词面扫描（`self._milvus.search(None, …)`），既拖慢延迟（默认编排
  P95≈1005ms，破 500ms），又常把无关中文文档塞进结果。
- 修复（合并远端 cbf01ac 的更精细方案）：`recall_ranker` 新增
  `open_domain_semantic(query, items)`——纯英文（含 ascii、不含中文）且无偏好意图、
  未点名已知项目别名、不含办公短语（printer share / meeting room / office wifi /
  default port / project manual 等）的通用问句才判定为开放域语义，交给向量排序。
  `orchestrator` 在 `auto` 路径新增 `auto_semantic` 提前返回（`strategy="query_memory:semantic"`），
  不触发办公词面全表扫描。命中已知项目别名（如 "XingHeNotes default port"）或含办公短语的
  英文仍走词面补召（`retrieval_mode="hybrid"` 亦显式支持），避免误伤需要词面找回的英文查询。
  中文办公词面门逻辑完全保留。
- 影响：英文开放域通用问句默认不再触发全表词面扫描，P95 回落到向量语义排序量级
  （此前 `orchestrator_semantic` 路径 P95≈386ms PASS）；中文知识问句与「英文+项目/办公短语」
  查询召回行为不变。

### 回归测试

- `tests/test_retrieval_eval.py::test_distractors_target_adds_random_negatives_despite_more_required`
  （`distractors_target(1200, 7707) == 1200`，验证正例数不再截掉干扰段）。
- `tests/test_recall_strategy.py::test_open_domain_semantic_routes_english_general_questions`
  （纯英文通用问句→语义；含中文→词面；英文项目别名/办公短语→词面）。
- `tests/test_recall_strategy.py::test_english_open_domain_skips_lexical_fallback`
  （英文通用问句 `strategy == "query_memory:semantic"`，且 `store.calls` 中无向量为 None 的全表词面扫描）。
- `tests/test_recall_strategy.py::test_english_project_keeps_lexical_recovery`（远端）
  （"XingHeNotes default port" 在 auto/hybrid 下仍走词面，`strategy == "query_memory+lexical"`）。
- `tests/test_recall_strategy.py::test_explicit_hybrid_can_recover_english_office_document`（远端）
  （`retrieval_mode="hybrid"` 下 "printer" 能找回 office 文档）。
- 关联测试：`test_retrieval_eval.py` + `test_recall_strategy.py` + `test_metrics.py` 37 passed；
  `test_core.py` + `test_api.py` 71 passed；lint 零错误。
