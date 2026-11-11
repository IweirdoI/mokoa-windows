# 麒麟 CPU 小模型诊断包

这是独立测试包，不启动前后端，不修改项目或系统配置，不连接外部模型服务，不启用生产敏感检测。
模型权重已经包含在包内；第一次安装 Python 依赖需要可用的网络/软件源。建议在麒麟现有 Python 3.12 上使用独立虚拟环境。

## 运行

从仓库拉取后，独立包位于 `artifacts/privacy-cpu-probe.zip`。在仓库目录执行
`unzip artifacts/privacy-cpu-probe.zip -d /tmp/mokoa-privacy-probe`，然后
`cd /tmp/mokoa-privacy-probe`。请选择尚不存在的解压目录，避免混用旧版文件。

解压到新的普通用户目录，进入该目录后执行：

```bash
python3 --version
free -h
vmstat 1 5
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/privacy_ner_eval.py --model-dir model --rules-file rules.json --threads 1 --output result-1thread.json
.venv/bin/python scripts/privacy_ner_eval.py --model-dir model --rules-file rules.json --threads 2 --output result-2threads.json
free -h
vmstat 1 5
```

如果 python3 不是 3.12，可把命令中的 python3 换成现有 python3.12。若没有 venv 或依赖安装失败，保留完整报错后反馈，不需要升级系统 Python。
如果运行前 `vmstat` 持续出现明显的 si/so，先记录这一状态：该环境下延迟会受到换页影响。已有 swap 占用本身不证明当前还在持续换页。

请回传 result-1thread.json、result-2threads.json 和运行前后的 free/vmstat 输出。还需要在实际前后端、Embedding 和向量库同时运行时复测；本脚本自身不启动它们。

## 指标解释

- 输入仅包含 28 条虚构短文本，不提供真实个人资料；报告不含输入原文。
- 比较规则匹配位置、模型 PER/LOC 位置和两者并集。完整地址只遮盖城市时计为漏检。
- 规则指标来自原文正则匹配位置的并集，不是完整入库或 API 测试；多遮盖的字符也可能只是“密码”等字段前缀。
- 模型使用 CPUExecutionProvider，限制为 1 或 2 个推理线程，关闭空转；不把宿主 i9 全部核心当成虚拟机资源。
- 内存字段是运行末尾的进程 RSS 采样及相对导入前增量，**不是峰值内存**。
- load_including_imports_ms 包括导入/载入；median_ms/P95/max 为 140 次短文本热推理（含分词和实体解码）。
- 这些是敏感信息候选检测指标，**不是知识检索 500 ms 指标，也不是比赛 Recall@5**。
- 工具对超长输入报错，不静默截断。尚未测试长文档、并发和长时间驻留。
- 示例集是诊断集，尚未形成独立盲测集，不能用其结果宣称实际隐私识别率。

## 模型来源

- 原模型：[IcyKallen/bert-mini-finetuned-ner-chinese](https://huggingface.co/IcyKallen/bert-mini-finetuned-ner-chinese)，模型卡标注 Apache-2.0；训练数据说明不完整。
- ONNX 导出：[TomatoMTL/bert-mini-finetuned-ner-chinese-onnx](https://huggingface.co/TomatoMTL/bert-mini-finetuned-ner-chinese-onnx)，模型卡标注 Apache-2.0。
- 固定版本：`2e6990291be3ec0407ef0d559e8346893b8ff4e9`。
- 权重 SHA256：`80db3cf7b9edc7d1ba4cc20561a46efda957b1f07a27ac911d31304be8baf5f3`。
- 模型只提供 PER/ORG/LOC/MISC 标签，未提供“私人地址”或“敏感/公开”的标签。本包故意使用简单 PER/LOC 策略暴露此限制，尚不适合直接作为生产隐私门控。
- [ONNX Runtime 线程设置依据](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)。
