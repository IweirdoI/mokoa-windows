# 分类器修复与复测

旧实现存在两个独立偏置：conversation 默认加偏好分且平分也返回偏好；
英文 `like/style/policy/love` 使用子串匹配。评测脚本又将所有来源强制设为
conversation，因此工具任务和 BPMN 文档失去了来源信息。

现在只有明确偏好表达、持续性使用指令或 manual_config 用户设置才进入偏好类。
普通对话与单次行为没有默认偏好加分；`explicit` 代表明确表达/记忆请求，不代表偏好。
document / tool_result 保持知识类型，避免引文变成用户偏好。两类敏感占位符仍按知识处理。
分类器提供 `explain()` 返回类型和固定原因码，不输出原文。
编排器的并行偏好提取也限制了来源：工具和文档不能靠命中偏好抽取规则绕过分类器。

评测脚本为 ToolBench 使用 tool_result、BPMN 使用 document；对话数据保持 conversation。
同时输出 `classifier_conversation_control_distribution`，将同一批样本全部按对话再分类，
用于区分来源修正和正文判别的作用。分类分布不是准确率，入库成功率也不是分类准确率：
该脚本的入库标签仍由数据集配置指定。空数据不再当成有效评测样本。

本地验证包含中英正负例、技术关键词、临时请求、工具/文档引文和 process_input
写入短期记忆类型。普通内容默认 knowledge 仅是现有二分类接口的回退，是否入库仍由质量门控决定。
混合多轮对话、隐含偏好和引用归属仍需要进一步的句级抽取；本次没有训练语义分类模型。
已有存量记忆不会自动改类，避免丢失版本链及用户设置。

麒麟上保留原数据文件、使用同一参数复跑，并核对报告 sampled_indices 与旧报告一致：

```bash
python benchmarks/run_dataset_eval.py --seed 42 --n 20 --rq 50 --rc 1200 --out benchmarks/results
```

本机六库未解压，系统 tar 不支持现有 RAR 的字典大小，因此本次未复跑那 120 条真实样本，
不宣称真实六库准确率或分类分布已经验证。
