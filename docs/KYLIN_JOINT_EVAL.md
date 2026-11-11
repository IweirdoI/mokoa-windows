# 真实麒麟 Embedding + 系统向量库联合验收

适用阶段：两个 SDK 已分别实跑通过，接下来验证真实文本向量写入系统库，并测量带标注查询的召回率与延迟。实现基于远程 `3a0ab7a`，保留其麒麟实测桥接修复。

## 麒麟执行

先同步本轮代码，并按 `KYLIN_VECTOR_BRIDGE.md` 构建桥接。使用此前已验证可工作的宿主机 Python、SDK库和用户环境。

```bash
export OS_AGENT_KYLIN_VECTOR_LIBRARY="$PWD/build/kylin-vector/libmokoa_kylin_vector_bridge.so"
mkdir -p data/reports
# 如果之前需指定此变量，继续使用原来实跑通过的实际路径
# export KYLIN_EMBEDDING_LIBRARY=/actual/path/libkysdk-coreai-embedding.so.1
python scripts/eval_kylin_joint.py \
  --output data/reports/kylin-joint-01.json \
  --top-k 5 --repeats 3 > data/kylin-joint-01.log 2>&1
```

脚本直接创建严格模式 `KylinEmbeddingAdapter(allow_fallback=False)` 和 `KylinVectorClient`，不依赖 factory 默认后端，不会使用 BGE/哈希/内存替身。从实际向量探测维度，用该维度新建独立集合，不固定写死768或1024。

每轮创建唯一集合和用户，输出每条测试记忆ID。默认结束时只物理删除本轮写入的记录，不删除集合、不清理其他业务库。失败时也尝试清理；清理失败会记入报告且退出码为1。可用 `--keep-data` 保留本轮数据供排查。不要并发运行到同一输出文件；脚本拒绝覆盖现有证据文件。

每次使用新的报告及日志名称，保留 SDK 的 protocol 警告，避免只截取最后的通过信息。

## 指标和通过条件

- 入库：每条知识经过真实 Embedding 编码后写入系统 SDK；逐条读回核对正文，入库失败不能继续计算一个虚高的召回率。
- 查询：查询文本实时编码，再调用系统向量SDK，按用户限制只检索本轮集合。
- Recall@K：每个查询命中的相关文档数 ÷ 该查询标注的全部相关文档数，再对查询测量取平均。不是“命中任意一条就计100%”。
- Hit@1：第一条结果是否相关，单独报告，不与Recall@K混淆。
- 延迟：查询编码开始至SDK返回结果，包含编码、ctypes/JSON桥接与数据库检索；报告编码、检索分项和整体均值/P50/P95/最大值、超过500ms次数。
- 初始化探测耗时另报；查询阶段的模型已经由初始化和语料入库预热。首个搜索请求额外标出，不把它误称为模型冷启动。
- `passed=true` 要求宏平均Recall@K≥85%、本轮所有查询端到端耗时≤500ms、SDK无降级且无运行/清理错误。失败退出1。

**此处测量是检索子系统端到端，不包含 HTTP 网络、前端、ingestion 质量门和自动晋升。** 它补齐截图中“真实Embedding写入系统库”的联合验收；不替代整套OS Agent功能、安全、偏好准确率或知识冲突验收。

## 数据规模与替换

默认 `benchmarks/datasets/kylin_joint_retrieval.json` 为人工编写诊断集：12条知识、15个查询，含多个相关答案的查询。3次重复得到45次测量，仍只有15个独立查询，不能写成45个独立样本或官方标准数据集成绩。

千条规模使用自建标注集 `benchmarks/datasets/kylin_scale_recall_1000.json`（1000 文档、35 查询：诊断集 12/15 + 20 条唯一实体知识）。干扰项为跨主题桌面运维知识，不是近重复灌水。项目书写「自建千条标注集 Recall@5」，不要写成官方赛题集。在 V11 宿主执行：

```bash
bash scripts/eval_kylin_scale_recall.sh
```

重新生成标注文件：`python scripts/build_kylin_scale_recall_dataset.py`。更换数据集无需改评测代码，传 `--dataset /path/to/dataset.json`。格式：

```json
{
  "description": "数据来源和标注方式",
  "documents": [{"id": "doc1", "text": "知识正文"}],
  "queries": [{"id": "q1", "text": "查询文本", "relevant_ids": ["doc1"]}]
}
```

这是格式示意，实际文档数必须大于top-k，避免全库返回导致无意义的100%召回率。ID不得重复、相关文档ID必须存在。报告记录数据文件SHA-256、代码提交号、实际维度、SDK端点、逐查询排名和分数。修改标注后视为新的数据集版本，不能直接与旧成绩混比。

## 本机验证边界

Windows只验证评测逻辑、数据校验、指标计算和严格失败路径，不生成麒麟性能成绩。将脚本带回麒麟执行后，请提供JSON与完整stderr/stdout日志；若失败，优先查看 `error`、逐查询结果和 `cleanup`，不要改用替代后端来凑通过。
