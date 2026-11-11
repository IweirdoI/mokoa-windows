# 偏好抽取评测：fusion 静默退化根因与修复（2026-09-14）

## 背景
评测脚本 `scripts/eval_preference_hf_dataset.py` 在 fusion 模式下，封闭键评测 `llm_calls`
出现 `succeeded=0, failed=35`，开放域分数也异常偏低（fusion 反而低于 rule）。
排查确认：这不是抽取质量问题，而是 LLM 调用整体失败后 fusion 路径静默降级为 rule，
制造「fusion 在正常运行」的假象。

## 根因
- `.env` 中的 `SILICONFLOW_API_KEY` 为无效凭证（调用返回 `401 Token is invalid`），
  但 `SiliconFlowClient` 的 `llm_available` 仅检查 key 非空，因此误判为可用。
- `SiliconFlowClient.__init__` 在无显式参数时，`api_key` / `base_url` 优先级将
  SiliconFlow（或 OpenAI）放在最前，即便本地 `LM_STUDIO_BASE_URL` 可用也会被忽略；
  且硬编码 base_url 缺少 `/v1` 后缀，本地 OpenAI 兼容端点请求会落到错误路径。
- 结果：所有 LLM 调用 401 失败 → fusion 静默降级为 rule。

## 修复内容
1. `src/preference/llm_client.py`：`SiliconFlowClient` 无参构造时优先选用本地
   LM Studio（`LM_STUDIO_BASE_URL` + 补全 `/v1`），保证「确定可用」的后端被选中；
   显式传参时保持原有 SiliconFlow/OpenAI 行为不变。
2. `scripts/eval_preference_hf_dataset.py`：
   - 修复 `_entry_blob` 因替换导致的引号损坏（此前 `return " ".join(parts"` 缺失右引号，编译报错）。
   - 新增 `--semantic` 语义对齐评测：用 LLM-judge 判定系统抽到的封闭键
     （`key=value` + `description`）是否语义覆盖 gold 自由文本偏好，弥补中英词面
     token 重叠恒为 0 的结构性盲区。
   - 修复 `extract_batch` 中 `has_got` 在 negative 样本语义分支先于定义被引用导致的
     `NameError`（将 `has_gold` / `has_got` 定义提前到语义分支之前）。
   - summary 输出新增 `semantic_recall` / `semantic_sample_accuracy`。

## 验证方式
- 封闭键评测：rule 模式 exact match 0.943（结构化偏好抽取质量可靠）；
  fusion 在弱本地模型 `phi-3.1-mini` 下 0.829（需强 LLM 才能体现 fusion 增益）。
- 修复后无参 `SiliconFlowClient` 已确认走本地 LM Studio 并能成功抽取结构化偏好
  （fusion 评测 `llm_calls.succeeded=35, failed=0`）。
- 开放域语义对齐指标需有效远程强 LLM 才能充分验证；本地弱模型仅作口径可行性验证，
  不消耗远程配额（本次提交不包含 LLM 调用型测试）。

## 影响模块
- `src/preference/llm_client.py`（LLM 后端选择逻辑）
- `scripts/eval_preference_hf_dataset.py`（评测口径与指标）
