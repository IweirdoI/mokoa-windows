# 偏好存储动态键兜底：长尾偏好不再丢失（2026-09-14）

## 问题
偏好存储是封闭键（`PREFERENCE_KEYS`）驱动的键值库，无法覆盖长尾偏好。
例如用户说「写代码默认用 Go」，系统找不到 `coding_language` 这类键，原实现会：

- `keys.normalize_preference_key` 对未知键 `raise ValueError`；
- `keys.validate_value` 用 `PREFERENCE_KEYS[full_key]` 导致 `KeyError`；
- `extractor.extract_preferences` 直接 `if full not in PREFERENCE_KEYS: continue` 静默丢弃。

结果是「记住了您的偏好」却没结构化落库（即产品截图现象）。不能靠穷举键解决。

## 方案（多措并举，绝不向用户抛异常）
1. `keys.normalize_preference_key`：未知键降级为动态键 `category.custom_xxx`
   （category 合法时）或兜底 `habit.custom_{topic}`，不再 `raise`。
2. `keys.validate_value`：未登记/动态键按自由文本 `string` 截断 512，不再 `KeyError`。
3. `extractor.extract_preferences`：去掉静默丢弃，统一归一化后入库，并把
   `item.category/key` 同步为降级后的 `full_key`，保证元数据一致。
4. `extractor.extract_by_rules`：新增默认编程语言长尾规则 → `habit.custom_coding_language`，
   即使 LLM 不可用也能兜底该类偏好。
5. `prompts.build_extraction_messages`：引导 LLM 无法映射到已知键时主动用 `custom_` 键，
   而非丢弃真实偏好。
6. 向量语义兜底：`canonical_embed_text` 已对动态键生成 `key=value` 向量，agent 检索时
   可按语义命中这些自定义偏好。

## 验证（不触发 LLM 调用）
- `normalize_preference_key("habit","coding_language")` → `habit.custom_coding_language`；
  `normalize_preference_key("foobar","unknown_thing")` → `habit.custom_unknown_thing`；均不抛。
- `extract_by_rules("写代码默认用 Go")` → `[('habit','custom_coding_language','Go')]`。
- `PreferenceMemoryStore(use_llm=False).extract_from_text("写代码默认用 Go 吧")`
  → 落库 `habit.custom_coding_language = Go`。
- 开放列表键 `habit.likes/dislikes` 照常工作（如「我喜欢吃草莓、喝咖啡」→ `habit.likes`）。

## 影响模块
- `src/preference/keys.py`（键归一化 / 值校验降级）
- `src/preference/extractor.py`（去丢弃 + 长尾规则）
- `src/preference/prompts.py`（抽取提示词引导）

下游 `features.py` / `orchestrator.py` 已用 `PREFERENCE_KEYS.get(..., {}).get("type", "string")`
兜底，动态键不会引发崩溃。
