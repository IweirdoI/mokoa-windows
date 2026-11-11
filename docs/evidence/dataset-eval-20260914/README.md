# 2026-09-14 git/ 加大抽测（对照评选标准）

麒麟 Embedding，seed=42。书面结论见 `docs/测试结果.md` 第 17–19 节。

## 八库 n=100（第 17 节，commit `cbf01ac`）

| 文件 | 内容 |
|---|---|
| `dialog_tool_20260914-132107.json` | MultiWOZ + ToolBench 各 100 |
| `behavior_knowledge_20260914-132707.json` | PersonaChat + BPMN 各 100 |
| `dialog_quant_20260914-132845.json` | DailyDialog + DIALOGBENCH 各 100 |
| `retrieval_20260914-133354.json` | MS MARCO Recall@5=0.8878；本文件 TREC 因评测跳过重编，TREC 数字用 09-13 目录的 `retrieval_20260914-113522.json` |

TREC 97 问 pid 子库见第 16 节，不要用本目录 retrieval JSON 里的 TREC skipped 当「段落库丢失」。

## 偏好 / 冲突金标准（第 18 节，commit `9a1ddbd`）

| 文件 | 内容 |
|---|---|
| `preference_20260914-154041.json` | user-preference-564k 抽 100 条 **rule**；封闭键 35 条精确匹配 0.9429；开放域样本正确率 0.20 |
| `preference_20260914-154211.json` | 同集 **fusion**；LLM 35/35 失败，回退规则，与上一份同数字 |
| `conflict_longmemeval_20260914-154214.json` | LongMemEval 70 更新 + 22 对照；解析器 1.00，端到端 latest_wins 1.00；金标 QA 子串 0.5217 仅诊断 |
| `conflict_longmemeval_20260914-154028.json` | 作废：当时把 QA 子串当成 e2e 正确率（0.5217） |

抽样缓存（gitignore，不进仓库）：`git/user-preference-564k/sampled_seed42_n100.jsonl`，`git/longmemeval_oracle.json`。

## HEAD `72f770b` 复跑（第 19 节，2026-09-14 19:30–19:45）

| 文件 | 内容 |
|---|---|
| `conflict_longmemeval_20260914-193114.json` | 解析器 1.00，latest_wins 1.00，金标 QA 子串 0.5217，P95 52.1ms |
| `dialog_tool_20260914-193234.json` | MultiWOZ Hit@5=0.92；ToolBench 接受 0.96 Hit@5=1.00 |
| `behavior_knowledge_20260914-193832.json` | BPMN 任务名 Hit@5=**0.88**（第 17 节 0.83）；XML 0.42 |
| `dialog_quant_20260914-194016.json` | DailyDialog / DIALOGBENCH Hit@5=1.00 |
| `retrieval_20260914-194325.json` | MS MARCO Recall@5=0.8878；TREC skipped |
| `preference_20260914-194515.json` | rule：开放域样本正确率 0.20；检测 0.24（TP=4）；封闭键 0.9429 |
| `run_n100_rerun_72f770b.log` | 八库+冲突；偏好首次 KeyError 中断 |
| `run_preference_rule_72f770b.log` | persist 跳过空用户后的 rule 复跑 |

偏好评测依赖工作区对 `PreferenceVersionIndex.persist` 的空用户跳过，该补丁当时未进 `72f770b`。
