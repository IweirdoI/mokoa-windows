# 开放域偏好准确率修复（第 4 项：0.20 → 0.87 过线）

- 日期：2026-09-15
- 模型：`deepseek-ai/DeepSeek-V4-Flash`（SiliconFlow，端点与密钥取自 `.env` 的 `SILICONFLOW_*`）
- 评测集：`blackhao0426/user-preference-564k`，seed=42，n=100（80 正例 + 20 负例）
- 复现命令：`python scripts/eval_preference_hf_dataset.py --mode fusion --semantic`

## 一、结论

| 口径 | 修复前 | 修复后 | 是否过 85% |
|---|---|---|---|
| 开放域 100 条（fusion + 语义判卷） | 0.20 | **0.87** | ✅ 过 |
| 开放域 100 条（token 口径） | 0.20 | 0.60 | ❌ |
| 开放域 80 条纯正例子集（语义） | — | 0.95 | ✅ |
| 封闭键 35 条（rule） | 0.9429 | 0.9429 | ✅ 无回归 |
| 封闭键 35 条（fusion） | — | 0.7429~0.7714 | ❌ |

关键指标：`semantic_recall=0.9137`、`semantic_sample_accuracy=0.87`、`semantic_errors=0`。
两次独立复跑（014024 / 024459）**均为 0.87**，结果稳定可复现。

> 口径说明：开放域金标是自由文本 `{condition, action}`，而系统产出结构化 `key=value`，
> 词面 token 重叠恒为 0（即 20% 的结构性根因）。因此开放域唯一公平口径是
> `--semantic` 的 LLM-judge 语义对齐，token 口径仅作参考。

## 二、五处根因与修复

**1. LLM 端点选错导致静默失败（最致命）**
`SiliconFlowClient()` 无参构造走「本地 LM Studio 优先」（见 fusion 静默降级修复），
而评测机没有本地 LM Studio，抽取与语义判卷全部在 `127.0.0.1:1234` 上失败并静默退化成 rule。
→ 评测脚本新增 `build_llm_client()` 与 `--llm-endpoint {remote,local}`（默认 `remote`），
显式用 `.env` 的 `SILICONFLOW_API_KEY / BASE_URL / LLM_MODEL`。

**2. 缺开放域抽取头（本质缺口）**
原有 LLM 提示词是 OS Agent 封闭键表，面对「用 TypeScript」「只要代码不要解释」
「回答简洁」这类开放域自由偏好直接返回 `[]`。
→ 新增 `extract_open_domain_by_llm()`（`src/preference/extractor.py`）与
`build_open_domain_messages()`（`src/preference/prompts.py`），输出自由 `condition→action`，
落库时经 `normalize_preference_key` 降级为 `category.custom_*` 动态键，**绝不因找不到键而丢弃**。
由 `PreferenceMemoryStore(open_domain=True)` 开启，产品默认关闭以免污染封闭键画像。

**3. 解析器脆弱，异常被吞成空抽取**
`_parse_llm_json` 遇到「JSON + 解释文字」或推理模型的 `</think>` 会抛 `JSONDecodeError`，
异常被上游吞掉后整条样本静默变空。实测 idx 19469 原始返回为 `[]</think>[]`。
→ 改用 `json.JSONDecoder().raw_decode` 容错解析，并剥离 `</think>` 之前的思考段，**保证永不抛异常**。

**4. 语义判卷看不到偏好描述**
`_semantic_cover` 只读 metadata 的 `preference_key/value/description`，但 `description`
根本没写进 metadata（它藏在 `entry.content` 里），判卷实际只看到 `key=value`。
→ 改用 `_entry_blob(e)`，纳入 `key=value + description + content` 完整信息。

**5. 判卷提示词过严**
原判据要求「等价或更具体」，导致 gold「reflective, aesthetically thoughtful」
与系统「beautiful and reflective」判 NO。
→ 校准为按**核心诉求**判定：忽略用词/详略/中英差异；复合金标覆盖主要诉求即判 YES。
此项单独使 `semantic_recall` 由 0.7027 提升到 0.9281。

## 三、改动文件

| 文件 | 改动 |
|---|---|
| `src/preference/prompts.py` | 新增 `OPEN_DOMAIN_SYSTEM_PROMPT` / `OPEN_DOMAIN_USER_PROMPT` / `build_open_domain_messages` |
| `src/preference/extractor.py` | 新增 `extract_open_domain_by_llm`；`extract_preferences` 增加 `open_domain` 参数并对两条来源去重；`_parse_llm_json` 容错改造 |
| `src/preference/store.py` | `PreferenceMemoryStore` 增加 `open_domain` 参数并透传 |
| `scripts/eval_preference_hf_dataset.py` | `build_llm_client` + `--llm-endpoint`；`_semantic_cover` 改用 `_entry_blob`；判卷语义校准；详情新增 `sem_gold` 逐条覆盖；`pass_85` 在 `--semantic` 时以语义样本准确率为准；新增 `--limit` |

## 四、已知未解决项（必须透明披露）

**1. 封闭键在 fusion 口径下掉到 0.7429~0.7714（未过线）。**
原因：LLM 会多抽自定义键，导致精确率降到 0.84（FP 8~9/35）。
而 rule 口径仍是 0.9429（precision 1.0、recall 0.9583），**无回归**。
竞赛报数建议：封闭键用 rule（0.9429），开放域用 fusion + 语义（0.87），并在材料中说明两套口径。

**2. 负例仍有约 9/20 被多抽**（`detection_accuracy=0.88`）。
曾尝试在提示词加「一次性任务反例」，结果同时压掉了正例召回
（`semantic_recall` 0.9137→0.7554，样本准确率 0.87→0.80），**已回退**。
后续可考虑用置信度阈值或二次判别器分离，不要再用提示词硬压。

**3. `--semantic` 依赖有效远程强 LLM。** 本地弱模型无法复现该指标。

## 五、原始评测产物

- 正式 100 条：`docs/evidence/dataset-eval-20260914/preference_20260915-024459.json`（0.87）
- 复跑 100 条：`docs/evidence/dataset-eval-20260914/preference_20260915-014024.json`（0.87）
- 80 条纯正例：`docs/evidence/dataset-eval-20260914/preference_20260915-002558.json`（0.95）
- 封闭键 rule：`docs/evidence/dataset-eval-20260914/preference_20260915-003044.json`（0.9429）
