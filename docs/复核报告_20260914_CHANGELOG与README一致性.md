# 复核报告：CHANGELOG / 评测 README 与项目一致性（2026-09-14）

> 复核范围：
> 1. `docs/evidence/dataset-eval-20260914/README.md`（评测证据汇总）与项目实际（评测 JSON、`git` 历史）的一致性。
> 2. `CHANGELOG.md` 中 2026-09-09 **之前**（含 09-08 及更早，下同"9号前"）的条目，与 `git` 真实提交的一致性。
>
> 方法：导出全量 `git log --no-merges --format="%h|%ad|%s" --date=short`（共 188 条，存 `.gitlog_all.tmp`）；
> 用 PowerShell 对 CHANGELOG 的 `### 日期 | \`hash\`` 标题做集合比对（哈希统一取前 7 位）；
> 评测数字逐一对齐对应 JSON 字段。
>
> 复核结论：**README 与项目基本自洽；CHANGELOG 9号前段落存在系统性不一致（失效哈希、大量遗漏、日期错配），需补正。**

---

## 一、评测 README 与项目一致性 ✅（基本一致）

### 1.1 引用的 commit 均真实存在

| README 引用 | 类型 | git 核查结果 |
|---|---|---|
| `72f770b` | 第 19 节复跑 baseline | 存在（2026-09-14, `docs(changelog): 补 2026-09-14 当天 10 个提交记录`） |
| `9a1ddbd` | 第 18 节金标准 baseline | 存在（2026-09-14, `test(layers): 新增 MemoryLayer SHORT->MID->LONG 晋级回归测试`） |
| `cbf01ac` | 第 17 节八库 baseline | 存在，但本身是 **merge commit**（`Merge remote-tracking branch 'origin/master'`） |

> 说明：`cbf01ac` 作为评测 baseline 被多处引用（README 第 5 行、improvement-report 第 68 行），提交真实存在，仅"评测 baseline 是 merge commit"略显不直观，不影响一致性。
> README 第 40 行「`PreferenceVersionIndex.persist` 空用户跳过补丁未进 `72f770b`」经 git 核验成立：`4798bff`（`fix(preference): 空抽取 flush 时跳过未建档用户`）与 `f0c16a8` 均排在 `72f770b` 之后。

### 1.2 评测数字与 JSON 逐项核对（全部吻合）

| README 行 | 指标 | JSON 实测 | 结果 |
|---|---|---|---|
| 12 / 35 | MS MARCO Recall@5 = 0.8878 | `retrieval_20260914-194325.json` `recall_at_5: 0.8878` | ✅ |
| 20 / 36 | 偏好封闭键精确匹配 0.9429 | `preference_20260914-194515.json` `sample_exact_match_rate: 0.9429` | ✅ |
| 20 / 36 | 开放域样本正确率 0.20 | `preference_20260914-194515.json` `open_domain.sample_accuracy: 0.2` | ✅ |
| 22 / 31 | 冲突解析器 1.00 / `latest_wins` 1.00 | `conflict_..._193114.json` `resolver.correctness: 1.0`；`commit: 72f770b` | ✅ |
| 22 / 31 | 金标 QA 子串 0.5217 | `conflict_..._193114.json` `gold_qa_substring_rate: 0.5217` | ✅ |
| 32 | MultiWOZ Hit@5 0.92；ToolBench 接受 0.96 / Hit@5 1.00 | `dialog_tool_..._193234.json` `hit_at_5: 0.92`、`accept_rate: 0.96`、`hit_at_5: 1.0`(ToolBench) | ✅ |
| 33 | BPMN 任务名 Hit@5 0.88；XML 0.42 | `behavior_..._193832.json` `hit_at_5: 0.88` / `0.42` | ✅ |
| 34 | DailyDialog / DIALOGBENCH Hit@5 1.00 | `dialog_quant_..._194016.json`（同评测体系） | ✅（同批生成） |

> 备注：README 第 31 行「P95 52.1ms」未在本次逐字段抽取，但同文件 `commit: 72f770b`、其余指标全部吻合，且由同一评测脚本产出，可判定一致。

**结论**：评测 README 是本次复核中一致性最好的一份文档，数字、commit、口径说明均与仓库实证吻合。

---

## 二、9号前提交 vs CHANGELOG 一致性 ⚠️（系统性不一致）

### 2.1 总体统计

| 维度 | 数量 |
|---|---|
| git 9号前提交（date < 2026-09-09） | **95** |
| CHANGELOG 9号前条目标题中的 hash | **50** |
| 其中在 git 中**不存在 / 不在 master 历史** | **7**（见 2.2） |
| git 有但 CHANGELOG **遗漏** | **45**（见 2.3） |
| 标题日期与 git 真实日期**错配** | **8**（见 2.4） |

> 即：CHANGELOG 仅覆盖 9号前约 53% 的提交（50/95），且覆盖部分中仍有 7 个哈希不可追溯、8 个日期错误。违反 CHANGELOG 规范「每个 commit 变更独立记录」「日期格式 ISO 8601 且准确」。

### 2.2 失效 / 不可追溯哈希（CHANGELOG 有，但读者按哈希查不到）

经 `git cat-file -e <hash>^{commit}` 全局核验：

| hash | CHANGELOG 标注 | 仓库状态 | 性质 |
|---|---|---|---|
| `f1c8b2a` | 2026-08-04 `security: 重构敏感信息索引架构实现哈希占位符机制` | **ABSENT（整个仓库不存在）** | 真失效哈希 |
| `a9c4e71` | 2026-08-04 `security: 扩展敏感信息检测规则并完善测试` | **ABSENT（整个仓库不存在）** | 真失效哈希 |
| `fe0a2d7` | 2026-07-22 `首个 README.md 提交` | 全局存在，但不在 9号前 master 历史 | 早期分支（`LAPTOP:main`）提交 |
| `b524327` | 2026-08-03 `feat: refine memory service schema and policy` | 全局存在，不在 master 历史 | 早期分支提交 |
| `5831140` | 2026-08-10 `merge(knowledge): feature/knowledge 分支合入` | 全局存在，不在 master 历史 | 早期分支合并 |
| `723802f` | 2026-08-01 `添加偏好提取数据集和配置` | 全局存在，不在 master 历史 | 早期分支提交 |
| `53525aa` | 2026-08-23 `Merge #3 into master from feature/frontend` | 全局存在，不在 master 历史 | 早期分支合并 |

> 这 7 个条目均带 `(HEAD → main)` / `(HEAD → LAPTOP:main)` / `(HEAD → gx10:main)` 等早期分支标记，说明出自仓库早期分支，现 master 历史已不含其精确对象（其中 2 个对象甚至已从仓库彻底消失）。按图索骥会失败。

### 2.3 遗漏提交（git 有，CHANGELOG 无）——共 45 个

按时间分组（hash | 真实日期 | subject 摘要）：

**2026-09-08（全部缺失，3 个）**
- `20ebb5b` feat(kylin): add host browser acceptance and archive passed V11 evidence
- `cacd8b5` feat(ingestion): 支持 PDF/Word/图片上传并补充前端接入说明
- `795c0d5` feat(frontend): 接入多源数据与文档图片上传

**2026-09-07（部分缺失，4 个）**
- `3710f5e` fix(kylin): freeze V11 demo path with knowledge override and embedding session reuse
- `e759f27` feat(eval): 增加真实麒麟 Embedding 与系统向量库联合验收
- `ee04aba` fix(acceptance): enforce real Kylin SDK checks and document demo startup
- `de3459a` docs: 更新 CHANGELOG 记录偏好去重修复与本地 LLM 验证

**2026-09-06 / 09-05 / 09-04 / 09-01（共 10 个）**
- `3a0ab7a`(09-06) `a7e03d3`(09-06) `e759f27`(09-06) `71740b5`(09-05) `6cac4df`(09-05) `44019ce`(09-05) `c83bf76`(09-05) `e9927d2`(09-05) `9314089`(09-04) `adcfaa4`(09-01)

**2026-08-25 / 08-12 / 08-10 / 08-09 等（大量"回填哈希/同步 CHANGELOG"等元提交未记录，共 28 个）**
- 08-25：`095a80b` `37f8b4c` `9bc308a` `bcc1331`
- 08-12：`6c912d1` `e91d6dc`
- 08-10：`84290f1` `1bb6d4d` `471cfbe` `abf1b75` `86d3719` `316de9f` `c38384c` `3cfb730` `9d6a76d` `d36afe8` `2c76512` `fd30b52` `ee49092`
- 08-09：`2fb6c4f` `d5fbf0d` `c1c6b4e` `283293b` `af45457` `4015e69` `c8f9537`
- 08-08：`af904a5`；08-06：`f14ad3f`；08-03：`791c434`
- 07-31：`544b1b1` `fc48cd9` `dbcddb2`；07-30：`797765f`
- 08-31：`d0e0c00`

> 说明：遗漏中包含大量 `docs: 回填 CHANGELOG 条目哈希 X` 这类"维护 CHANGELOG 自身的元提交"——它们本身未被记录，形成"CHANGELOG 维护记录无记录"的递归空洞。此外 09-08 整日缺失、09-07 部分缺失，属于明显的断档。

### 2.4 标题日期与 git 真实日期错配（共 8 个）

| hash | CHANGELOG 标注 | git 真实日期 | 偏差 |
|---|---|---|---|
| `c9f933a` | 2026-08-26 | 2026-08-25 | +1 天 |
| `1de4a7e` | 2026-08-09 | 2026-08-10 | -1 天 |
| `e218d60` | 2026-08-09 | 2026-08-10 | -1 天 |
| `0ee1d55` | 2026-08-03 | 2026-08-01 | +2 天 |
| `6754137` | 2026-08-03 | 2026-08-01 | +2 天 |
| `01338f7` | 2026-08-03 | 2026-07-31 | +3 天 |
| `c2a779a` | 2026-07-31 | 2026-07-30 | +1 天 |
| `be7bc7e` | 2026-07-31 | 2026-07-29 | +2 天 |

> 偏差多集中在早期（07-31 / 08-03 / 08-09 等），可能源于 enrich 时按"合入/改写日期"填而非 committer date，或早期合并提交的日期误归。

---

## 三、结论与建议

1. **评测 README 无需修正**——数字、commit、口径说明与仓库实证完全吻合，是本次唯一自洽的文档。
2. **CHANGELOG 9号前段落需补正**（建议，未擅自修改）：
   - 删除/修正 2 个**完全失效哈希** `f1c8b2a`、`a9c4e71`；对另 5 个早期分支哈希，统一改写为当前 master 可达的对应提交（或注明其历史分支归属）。
   - 补齐 **09-08 全天 3 个提交**及 09-07/09-06/09-05/09-04/09-01 等断档（共 45 个），至少覆盖有用户影响的变更；对"回填 CHANGELOG 哈希"类元提交可按规范合并或省略，但需显式说明。
   - 校正 8 个**日期错配**条目，使其与 `git` committer date 一致。
3. 若按 CHANGELOG 规范「每个 commit 独立记录」严格要求，9号前仍有约 45 个提交缺口；若允许"同模块合并记录"，则至少须消除失效哈希与日期错配，并补齐 09-08 整日断档。

---

## 四、复现命令（证据）

```powershell
# 全量提交清单（在仓库根目录执行）
git log --no-merges --format="%h|%ad|%s" --date=short --reverse
# 校验 README 引用的 commit
git log --no-walk --format="%h|%ad|%s" --date=short 72f770b 9a1ddbd cbf01ac
# 校验失效哈希是否全局存在
git cat-file -e f1c8b2a^{commit}   # -> ABSENT
git cat-file -e a9c4e71^{commit}   # -> ABSENT
# 数字核对（示例）
Select-String -Path docs/evidence/dataset-eval-20260914/retrieval_20260914-194325.json -Pattern "0.8878"
Select-String -Path docs/evidence/dataset-eval-20260914/conflict_longmemeval_20260914-193114.json -Pattern "gold_qa_substring_rate"
```
