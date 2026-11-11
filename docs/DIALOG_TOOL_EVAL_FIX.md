# 对话与工具专项复测

依据 `docs/evidence/dataset-eval-20260913/dialog_tool_20260913-221541.json`：
MultiWOZ 末句自检索 Hit@5=0.18，ToolBench 任务自检索=1.00。前者大量查询是
感谢/结束语，不能据此判断 embedding 损坏。本次不更换 embedding 或放宽隐私拦截。

修正内容：

- MultiWOZ 保留用户轮次，用初始任务和最近两条有效约束构造最多 500 字的查询；
  忽略纯感谢、确认和结束语。只用已收录正文中的用户话语，不加入助手答案。
- 同样本另跑 `last_user_control`，保留旧末句查询对照。`retrieval` 为新任务查询。
- 编排器摄取也使用任务上下文，避免将末尾寒暄作为整段对话的摄取代表。
- 自检索改用实际 memory_id 精确命中；正文前缀相同不算命中。仅已成功入库且查询
  非空的样本计入条件 Hit@5 分母，同时报告输入数、入库失败数、跳过数和空查询数。
- 查询不再回退到正文开头凑出正例；索引正文与加载器窗口一致，避免 1500→1200
  的二次截断。加载器记录截断标记；超出正文窗口的用户轮次不用于新查询。
- 保留远程加载器修复，并补齐旧版 MultiWOZ 双字段和 ToolBench 字符串/列表工具描述兼容。

复跑（数据与 seed 不变，核对 sampled_indices）：

```bash
git pull --ff-only origin master
export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
bash scripts/run_host_py.sh scripts/eval_dialog_tool_datasets.py --seed 42 --n 50
```

比较 `multiwoz.retrieval` 和 `multiwoz.last_user_control` 的 Hit@5、n_queries、
query_p95_ms，并同时检查 n_index_rejected / n_query_skipped / n_content_truncated。
旧报告的前缀命中和分母口径有缺陷，旧数值不能直接充当新口径基线。
ToolBench 的任务自检索成功不等于工具执行成功；本脚本仍是 create_memory/query_memory
组件自检索，不是 Orchestrator.recall 全链路或独立问答准确率评测。

本地测试覆盖查询构造、两种加载格式、末句对照所需元数据、精确 ID 计分与空输入。
本次未在麒麟或真实两库上跑新数字，真机结果以新生成的报告为准。
