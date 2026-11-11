# 麒忆智脑 — 量化评测效果验证报告（模板）

> 模块负责人：da0f41a1 | 对应比赛要求 **(7) 量化评测**
> 运行方式：`python benchmarks/run_benchmarks.py [--real]`
> 本报告填写说明：用 `benchmarks/results/report_*.json` 与 `summary_*.md` 的真实数字替换下方占位；真机数字来自麒麟 V11 宿主（kylin 后端）的 `--real` 跑分。

---

## 1. 概述

- 作品名称：麒忆智脑（MOKOA）— OS Agent 记忆优化及高效应用研究
- 参赛赛题：XA-202612 麒麟软件有限公司
- 评测目标：验证赛题初评 4 项硬性指标是否达标（性能指标占初评 25 分）
- 评测口径：功能覆盖实跑 + 标准化数据集自动化跑分；**不包装为大规模评测**，不把失败项写成已验收

## 2. 测试环境

| 项 | 开发/CI（baseline） | 真机验收（real） |
|---|---|---|
| 系统 | Windows / 开发机 | 银河麒麟 V11 2603 |
| 记忆后端 | 进程内 mock / memory | 麒麟 AI 子系统向量引擎 `mokoa_kylin_sdk` |
| Embedding | 本地 BGE-M3（sentence-transformers） | 麒麟端侧 Embedding SDK（768 维，严格模式） |
| 向量维度 | 1024（BGE-M3） | 768（麒麟 SDK） |
| 严格模式 | 关 | `OS_AGENT_KYLIN_STRICT=1` |
| 跑分时间 | <填写> | <填写> |

## 3. 四项硬性指标结果

| 指标 | 目标 | baseline 数值 | 真机数值 | 是否达标 |
|---|---|---|---|---|
| 偏好提取准确率 | ≥ 85% | <填 recall_json> | <填 real_json> | <✅/❌> |
| 知识检索召回率（Recall@5） | ≥ 85% | <填 recall_json> | <填 real_json> | <✅/❌> |
| 检索响应时间（P95） | ≤ 500ms | <填 p95> | <填 real_p95> | <✅/❌> |
| 冲突处理正确率 | ≥ 88% | <填 conflict_json> | 同 baseline（规则无关后端） | <✅/❌> |

> 说明：冲突处理正确率依赖 `ConflictResolver` 规则，与存储后端无关，真机与 baseline 一致。

## 4. 分指标详述

### 4.1 偏好提取准确率（≥85%）
- 方法：MemoryClassifier 对标注语料分类，准确率 = 正确/总数
- 样本量：<填 sample_size>
- 结论：<填 value> ，<达标/未达标说明>

### 4.2 知识检索召回率（≥85%，Recall@5 / Hit@1）
- 方法：播种知识库 → 按 query 检索 top-5 → 检查相关记忆是否命中
- 样本量：<填 sample_size>
- Recall@5=<填> ，Hit@1=<填>
- 结论：<达标/未达标说明；若未达标，列出英文办公文档中文问句等弱项>

### 4.3 检索响应时间（≤500ms，建议报 P50/P95）
- baseline 平均 <填>ms；P95 <填>ms
- 真机 P50 <填>ms；P95 <填>ms
- 结论：<达标/未达标说明>

### 4.4 冲突处理正确率（≥88%）
- 方法：ConflictResolver 对 (old, new, expected) 用例判定
- 样本量：<填 sample_size>
- 结论：<填 value> ，<达标/未达标说明>

## 5. 对比实验（写入技术文档）

| 实验 | 对比 | 目的 | 结论 |
|---|---|---|---|
| A/B-1 | 有 vs 无 冲突消解 | 证明 conflict_resolver 价值 | <填> |
| A/B-2 | 有 vs 无 quality 门控 | 证明智能筛选 | <填> |
| A/B-3 | Kylin embedding vs local | 国产化适配与延迟 | <填> |
| A/B-4 | 仅向量 vs 向量+filter | 证明 memory_type 分类有效 | <填> |

## 6. 结论与改进方向

- 总体：<是否达到初评指标要求>
- 未达标项（如有）：<明确写当前值与改进计划，不编造数据>
- 知识/冲突检索弱项根因：中文问句对英文办公 PDF 常被词面过滤打空；建议放宽中英文对齐检索或演示固定句式。
- 演示建议：正式演示只串已 PASS 的 query（见 `docs/演示固定脚本_只串PASS.md`），不把失败项写成已验收。

## 7. 附录

- 原始结果：`benchmarks/results/report_<ts>.json`
- 摘要：`benchmarks/results/summary_<ts>.md`
- 功能覆盖实跑：`docs/测试结果.md`（2026-09-12 00:20，65 PASS / 12 FAIL / 3 MANUAL）
- 跑分命令：
  ```bash
  python benchmarks/run_benchmarks.py            # 组件级基线
  # 真机：先导出 OS_AGENT_* 环境变量，再
  python benchmarks/run_benchmarks.py --real
  ```
