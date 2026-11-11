# 2026-09-15 13:11 git/ 抽测（对照评选标准，17af2b3 并行评测）

书面结论见 `docs/测试结果.md` 第 23 节，以及本目录 `测试报告_20260915-1311.md`。
分钟级报告另存 `docs/评选标准抽测报告_20260915-1311.md`。

评测代码基线：`17af2b3f509ecea1ed10f1b02134277e530775ed`（偏好评测并行 + 封闭键 `restrict_llm_to_known_keys`）。
工作区 HEAD：`f7a5613` + 上述 17af2b3 产品/评测改动。seed=42，n=150（1.5× 对外抽测下限 100）。

麒麟 Embedding 768 维，`memory=memory`，strict=1。LLM：SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`，temperature=0.0，workers=8。

| 文件 | 内容 |
|---|---|
| `测试报告_20260915-1311.md` | 分钟级详细报告（含可复现变量与赛题四条线对照） |
| `reproducibility.json` | HEAD、SHA256、模型、解码参数、kylin so 哈希 |
| `preference_20260915-133012.json` | 564k 抽 150（120+30）fusion+rule+semantic；封闭键 35 |
| `conflict_longmemeval_20260915-133054.json` | LongMemEval 70 update + 72 control（8 条不足两轮跳过） |
| `dialog_tool_20260915-133305.json` | MultiWOZ + ToolBench 各 150 |
| `behavior_knowledge_20260915-134008.json` | PersonaChat 150；BPMN skipped |
| `dialog_quant_20260915-134250.json` | DailyDialog + DIALOGBENCH 各 150 |
| `retrieval_20260915-134740.json` | MS MARCO nq=150 / rc=2000；TREC 首次 embedding 超时 |
| `retrieval_20260915-140209.json` | TREC 97 问重跑成功 |
| `retrieval_20260915-140210.json` | 合并件（MARCO + TREC）；报告用这份 |
| `raw_io/` | 抽中样本输入、`sampled_indices`、评测 JSON 副本 |
| `prompts/` | 抽取提示词与判卷脚本副本 |
| `run_20260915-1311.log` | 主跑批日志 |
| `run_trec_retry_20260915-1311.log` | TREC 重跑日志 |

抽样缓存（gitignore）：`git/user-preference-564k/sampled_seed42_n150.jsonl`。
