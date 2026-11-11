# DailyDialog / DIALOGBENCH 窗口与字段隔离修复

基于远程 `dialog_quant_20260913-234930.json`，保留原始测试记录和历史数字。

1. DIALOGBENCH 的 `.json` / `.jsonl` 文件按 JSONL 逐行解析。坏 JSON 行继续跳过；
   报告 `parse_diagnostics` 记录坏行、非对象行、空 dialogue 数，样本记录 source_line。
2. 加载内容仅来自 dialogue，禁止用顶层 query、label、analysis 或 prompt 构造正文和问句。
   移除 leaky_concat 及其分类对照；评测索引采用元数据允许列表，防止旧元数据回流。
3. 两库正文仍保留前 1500 字，只有完整落在该窗口内的用户轮次可用于 task_query 和
   last_user_control。半截话语和窗口外轮次均排除，记录 n_user_turns_outside_window。
   DailyDialog 仍按偶数轮作为第一位说话者，不能把这一约定当成真实角色标注。
4. 报告与终端摘要明确标记 memory_id 自检索口径。这不是 DailyDialog BLEU、
   DIALOGBENCH 选择题准确率或赛题第 (7) 项的官方成绩。

麒麟复跑：

```bash
git pull --ff-only origin master
export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
bash scripts/run_host_py.sh scripts/eval_dialog_quant_datasets.py --seed 42 --n 50
```

比较两库 retrieval / last_user_control 的 Hit@5、n_queries、n_empty_queries、
n_content_truncated 和 n_user_turns_outside_window，同时检查 DIALOGBENCH 的
parse_diagnostics 与 sampled_indices。新的末轮对照指窗口内最后一个完整用户轮次，
与旧版全对话末轮不完全等价，不能只看 Hit@5 增减。

本次未跑真实麒麟数字，未扩展 data_zh；完整语料和长对话分块检索仍需独立评测。
