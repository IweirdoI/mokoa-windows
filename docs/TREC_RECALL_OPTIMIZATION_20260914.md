# TREC 麒麟测试反馈对应优化（2026-09-14）

依据：`docs/evidence/dataset-eval-20260913/retrieval_20260914-113522.json`。
同步远程基线：`26a5b9f`。保留历史结果，不覆盖原始证据。

## 变更

- 默认 `auto` 对英文开放域查询保留向量顺序，跳过查询扩写、最多 10000 条的词面扫描和办公重排；策略标记为 `query_memory:auto-semantic`。health、image、service 等通用词不再单独触发办公路径。
- 中文/中英混合查询、偏好查询、候选中有匹配项目别名的查询，以及明确办公短语，继续使用原有处理。用户、类型、场景和版本过滤仍在检索前执行。
- API 与核心模型均接受 `retrieval_mode=hybrid`，强制使用原有补召/重排逻辑。`semantic` 仍可显式选择。自动路由是启发式判断：英文项目别名未出现在向量候选、且没有明确办公短语时，调用方应选 `hybrid`；它不保证识别所有英文办公意图。
- TREC 评分使用完整正例 qrels，未入库正例作为漏召回，不能从 Recall/nDCG 的分母中删除；新增缺失正例 query–pid 对数。无正例标注的查询跳过，不计零分。
- 报告增加 `primary_metric=ndcg_at_10_graded` 和子库范围说明；二值 nDCG 保留供诊断，不作为分级 nDCG 替代。

## 本地验证

Windows 本地使用确定性向量/`hash` 与内存存储，150 项不同测试通过，覆盖检索策略、跨语言、完整 qrels 评分、API 参数转发、核心记忆、遗忘与指标。原默认模型环境的 API 运行等待较久后中止，改用显式离线后端完成验证。仅有已有弃用警告。

这些测试验证功能与调用次数，不代表麒麟模型质量或硬件延迟。尚未获得优化后的麒麟 P95/nDCG；不得将历史 semantic 的 386 ms 或 0.7306 当作本次实测结果。

## 麒麟复跑

在现有麒麟测试机项目目录执行（沿用其已有宿主 Python 包装脚本）：

```bash
git pull --ff-only origin master
export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --trec-only --trec-nq 0 --seed 42 --rc 1200 --trec-collection <repo>/git/MS_MARCO/passage_ranking/collection.tsv --out benchmarks/results/trec-auto-20260914
```

比较三条路径的分级 nDCG@10、P95、空结果数与策略分布；默认路径应减少词面补召，确认是否通过 500 ms 门槛及是否保留相关性。另将 `--rc` 改为 `9000`、输出改到另一目录，增加随机干扰评估，勿与原 7707 段子库混报。

两轮均为子库评测，非官方 8.8M 全集排名。Document track 缺正文仍跳过。Recall@5 不与每问正例数不同的 MS MARCO 横比；Hit@1 也不能代替分级相关性排序质量。
