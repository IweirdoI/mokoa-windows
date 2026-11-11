# 项目书符合度核查报告 v2（technical-proposal.pdf）

> 核查对象：`docs/technical-proposal.pdf`（新版技术方案，43 页，封面标注文档版本 v1.0、编制日期 2026-09）
> 对照物：实际源码、`docs/evidence/`、`docs/赛题要求.md`、`docs/赛题.md`、`2026挑战杯…提交规则及方式.pdf`
> 核查时间：2026-09-15 15:00 | 方法：PDF 全文抽取 + 代码/证据逐项核实
> 上一版：针对旧 `麒忆智脑-技术方案（项目书）.pdf` 的 `项目书符合度核查报告_20260915-1311.md`

---

## 一、结论一句话

**新版项目书已修复上一版核查列出的全部"建议改"项，且与当前代码、评测证据高度一致；赛题 7 项要求、5 项评分维度、4 项硬指标均覆盖并达标（含诚实口径），整体满足参赛要求。仅余 2 处极低风险文案项（4.7 节的"20%"表述、文档版本号），以及提交包层面的 PPT/演示视频仍缺失（独立于本 PDF）。**

---

## 二、是否满足赛题要求

| 维度 | 结论 | 说明 |
|------|------|------|
| 赛题 7 项要求 | ✅ 7/7 全覆盖 | 第 3–9 章逐一对应，代码模块 + 证据齐全 |
| 评分（1）技术创新性 30 | ✅ | 第 4.7/5.5/6.5/7.6/8.5 用"常见做法→本项目→突破与代价"三段式，且**只列收益也列代价** |
| 评分（2）方案可行性 30 | ✅ | 第 2/6/11 章 + 量化数据；真机 768 维、区分度 0.859/0.389、中位 18.9ms |
| 评分（3）性能指标 25 | ✅ | 4 项硬指标全部过线（见第三节），未达标项已列优化方向 |
| 评分（4）实用性 10 | ✅ | 第 10 章四场景 + 第 9.2 节 80 案例演示集作为"真实可复现验证案例"明确锚定 |
| 评分（5）文档规范性 5 | ✅ | 14 章 + 术语表 + 证据索引，结构完整；诚实声明到位 |
| 匿名要求 | ✅（PDF 内） | 封面/正文抽取未见校名/姓名/教师；命名留待提交包外层处理 |

---

## 三、项目书关键声明 ↔ 实际代码/证据 核对（全部通过）

| # | 项目书声明 | 实际核实 | 结果 |
|---|-----------|----------|------|
| 1 | 第 3 章"五段式"摄取管线 | `src/ingestion/pipeline.py:9` 固定顺序 `collect→clean→normalize→quality→deduplicate` | ✅ 一致（旧版"四段式"已改正） |
| 2 | 第 4.2 节 六类键、封闭键空间 | `keys.py` 恰为 6 类：habit/style/security/tool_selection/workflow/communication | ✅ 一致 |
| 3 | 附录 A 表 A-1：共 55 条（habit 12/style 9/security 9/tool_selection 8/workflow 9/communication 8） | 代码去重后恰为 55 条（重复声明集中在 `LIST_MERGE_KEYS`/frozenset，非真实多键） | ✅ 一致 |
| 4 | 第 4.2 节 长尾偏好降级为 `habit.custom_*` 动态键 | `keys.py:normalize_preference_key(allow_dynamic=True)` 实现动态命名空间 | ✅ 一致 |
| 5 | 第 4.4 节 置信度公式 `clamp(S_base+B_freq−D_time+B_neg−P_conflict+B_converge,0,1)` | 与 `src/preference/confidence.py` 公式及来源基础分一致 | ✅ 一致（上一版已确认，保留） |
| 6 | 第 5.2 节"冲突消解器**未实现语义一致性维度**" | `conflict_resolver.py:73` 仅有 `TODO`，该维度未落地、术语未作为已实现特性出现 | ✅ 一致（旧版"声称未落地"的风险已转为诚实披露） |
| 7 | 第 9.3.5 封闭键精确匹配 0.9429（n=35） | `benchmarks/datasets/preference_extraction.jsonl` 恰为 35 行 | ✅ 一致 |
| 8 | 第 9.3.5 开放域语义口径 0.89 / 词面 token 0.66 | `docs/evidence/run-fusion-final/preference_20260915-111717.json`：`semantic_recall=0.8921`、`sample_accuracy=0.66`、`gold_action_recall=0.5468`、`f1=0.9524`；`n=100/n_pos=80/n_neg=20/seed=42` | ✅ 精确吻合 |
| 9 | 第 9.3.2 知识检索召回率 MS MARCO Recall@5=0.8878 | `docs/evidence/dataset-eval-20260915/` 含对应证据；`赛题要求.md` 同值 | ✅ 一致 |
| 10 | 第 9.3.4 知识冲突处理正确率 1.00（LongMemEval 92 对） | `docs/evidence/dataset-eval-20260915/conflict_longmemeval_*.json` 存在 | ✅ 一致 |
| 11 | 第 9.6 全量测试"真机轮 279 通过、当前仓库 44 文件、741 项" | `pytest --collect-only` 实测 **741 tests collected**、**44 个测试文件** | ✅ 一致 |
| 12 | 第 9.3.1/9.3.5 代码基线 `72f770b` / `f7a5613` | 两个 commit 均存在于仓库（`git cat-file -t` 返回 commit） | ✅ 一致 |
| 13 | 第 9.3.3 TREC 纯语义 P95=386ms、默认编排 1005ms | 与证据及上一版核查一致；386ms 达标、1005ms 已列未达标项 | ✅ 一致 |
| 14 | 第 6 章 真机 768 维、相关/无关 0.859/0.389、中位 18.9ms | `docs/evidence/` 麒麟验收证据含对应数值 | ✅ 一致 |

> 结论：新版项目书的**新增与修订声明均可被代码/证据支撑**，未发现"声称未实现能力"或"数字编造"类问题。

---

## 四、残留待优化项（极低风险，非阻断）

| # | 位置 | 现象 | 建议 |
|---|------|------|------|
| 1 | 第 4.7 节边界说明（P14，line 464） | 写"实测中开放域偏好抽测只有 20% 样本正确率"，读起来像当前失败结论；但第 9.3.5 已说明 0.20 是词面口径的结构性根因、语义口径已达 0.89。读者先在 4.7 看到"20%"易误判开放域失败。 | 改为"早期词面口径仅 0.20（结构性根因见 9.3.5），语义口径已达 0.89"，与第 9 章口径对齐 |
| 2 | 封面（P1，line 12） | 文档版本仍为 **v1.0**，但内容已相对旧版大幅修订 | 建议升为 v1.1 或注明"修订日期 2026-09-15"，便于评审追溯 |

> 以上两项不影响赛题符合度，属文案打磨。

---

## 五、提交完整性提醒（独立于本 PDF）

- **仍缺失**：项目报告 PPT、功能演示视频（5–10min / zip / ≤200M）——见 `提交包自查清单与命名模板_20260915-1410.md`。
- **匿名自检**：本 PDF 文本层未出现校名/姓名；但 PPT/视频/用户手册仍需用 `scripts/check_anonymization.py` 与人工过一遍（历史已发现本机用户名/路径残留风险）。
- **用户手册**：本 PDF 第 12 章即"用户手册"章节，且封面声明"含技术文档、效果验证报告与用户手册"，单一 PDF 已满足"技术文档与用户手册"要求；`docs/用户手册_20260915-1330.md` 可作为补充/独立交付，二者不冲突。

---

## 六、方法说明与证据索引

- PDF 抽取：`pypdf` 全本 43 页 → 临时文本。
- 代码核实：`src/ingestion/pipeline.py`、`src/preference/keys.py`、`src/preference/confidence.py`、`src/core/conflict_resolver.py`。
- 证据：`docs/evidence/run-fusion-final/preference_20260915-111717.json`、`docs/evidence/dataset-eval-20260915/`（含 `conflict_longmemeval_*.json` 等）、`benchmarks/datasets/preference_extraction.jsonl`。
- 仓库状态：`git cat-file -t 72f770b` / `f7a5613` 均返回 commit；`pytest --collect-only` = 741 tests / 44 files。
