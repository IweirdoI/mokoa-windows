# 2abac5a6 — 信息脱敏与上下文维护 技术文档

> 模块 Owner：2abac5a6
> 对应赛题要求：**(5) 集成敏感信息识别与过滤功能，支持自然语言指令驱动的精准遗忘操作，保障数据安全合规**
> 所属项目：麒忆智脑（MOKOA）— 银河麒麟 OS Agent 记忆优化系统（挑战杯 XA-202612）
> 代码目录：`src/security/`、`src/core/context_perception.py`
> 接口契约：`src/common/interfaces.py`（`SecurityService`）、`src/common/models.py`（`SensitiveEntry`）

---

## 一、模块定位与职责边界

本模块是 MOKOA 记忆系统的**安全与上下文合规层**，在数据与模型交互的边界上提供两层能力：

1. **信息脱敏（PII Detection & Masking）**：在系统输入进入模型/记忆库之前，于本地完成敏感信息识别与替换，实现「隐私与记忆的分离」——记忆库只存不可逆的占位符，真实敏感原文仅保存在本地加密索引中。
2. **精准遗忘（Precision Forgetting）**：支持用户用自然语言驱动地删除特定记忆（无论是敏感脱敏索引条目，还是普通偏好/知识记忆），并完成短/中/长期与向量库的级联清理。
3. **上下文维护（Context Maintenance）**：在上下文窗口逼近上限前主动压缩/重构历史对话，保证长期交互不触顶、体验连贯。

**协作边界（团队约定）**：
- 脱敏在 `Orchestrator.process_input()` 写入短期记忆前调用 `filter_sensitive()`；
- 遗忘对外接口 `forget()` 由 `Orchestrator` 暴露，本模块负责「解析目标 ID（含普通记忆/偏好/知识库）」「索引软删除」与「知识遗忘计划」（`plan_knowledge_forget`），正式级联删除由 Orchestrator 统一编排；
- 不直接写 Milvus（cb0aeabe负责），只通过接口调用 `delete_memory`。

---

## 二、整体数据流

```
用户原始输入
   │
   ▼
Orchestrator.process_input()
   │  ① filter_sensitive()  ←── 本模块（2abac5a6）
   │       ├─ 正则层（50+ 确定性规则）
   │       ├─ 语义层（可选 LLM，默认 LM Studio；不可用时降级正则）
   │       └─ 命中 → 写回 <HASH_xxxxxxxx> 占位符 + 写入本地加密索引
   │           （脱敏正文进入短期记忆 / 偏好 / 知识库）
   ▼
写入记忆层（短→中→长期 / Milvus）
   │
   ▼
用户发出遗忘指令（如「忘记我的手机号」「忘掉下载路径偏好」）
   │
   ▼
Orchestrator.forget(user_id, instruction)  ←── 编排（efca9634）
   ├─ ② security.forget()           → 扫描脱敏索引，返回 <HASH_...> 目标
   ├─ ② security.parse_forget_targets() → 扫描普通记忆/偏好候选，返回 memory_id 目标
   ├─ ② security.plan_knowledge_forget() → 命中知识意图时定向召回知识候选（见 4.4）
   ├─ ③ delete_sensitive_entries()  → 敏感索引软删除
   └─ ③ _purge_memory()             → 短期/中期/Milvus 级联删除 + 偏好版本索引同步删除
                                         （知识「全部删除」走 Milvus 按 filters 批量删除）
```

---

## 三、信息脱敏子系统

核心实现：`src/security/service.py`（`SecurityAndForgetService`、`SensitiveIndex`、`SensitivePattern`）、`src/security/index_crypto.py`、`src/security/semantic_detector.py`。

### 3.1 双层检测引擎

`filter_sensitive(text, user_id)` 返回 `(脱敏后文本, 检测类型列表, 占位符映射表)`：

- **正则层（确定性主力）**：内置 `_RULES` 规则集，覆盖 **50+ 种敏感类型**，按 DLP 优先级排序（密钥凭证 > 证件 > 金融 > 通讯 > 网络 > 其他），先匹配更具体的、后匹配更通用的，避免误伤。
- **语义层（可选增强）**：`SemanticPiiDetector` 调用本地 LLM（默认 LM Studio 原生 `/api/v1/chat` 端点）识别正则难以覆盖的隐式敏感信息（如工号、昵称、隐式地址）。模型不可用/超时/返回异常时**降级返回空列表**，由正则层兜底，绝不抛异常中断主流程。

> 关键防误脱敏设计：`filter_sensitive` 在每条规则执行前，先用哨兵符（`\x00PH{i}\x00`）把已有 `<HASH_xxxxxxxx>` 占位符保护起来，避免 `qq_number`/`imei` 等数字类规则把占位符内的 hex 数字段二次当成敏感内容。

### 3.2 覆盖的敏感类型（节选）

| 类别 | 代表规则 |
|------|----------|
| 密钥凭证 | SSH 私钥、X.509 证书、JWT、Bearer Token、OpenAI Key、GitHub PAT、Stripe Key、AWS Access Key、DB 连接串、`.env` 凭据、API Key |
| 证件 | 身份证、护照、港澳通行证、统一社会信用代码、外国人永居证、驾驶证 |
| 金融 | 银行卡、CVV2、银行卡有效期 |
| 通讯 | 手机（含+86/带分隔）、固定电话、短信验证码、邮箱 |
| 网络身份 | IPv4、MAC、IMEI、SessionId/Cookie、QQ 号、Tor Onion、加密货币钱包地址 |
| 其他 | 出生日期、车牌、美国 SSN、快递/订单号、工号、合同号、密保答案、登机牌 |

规则全部以 `SensitivePattern` 声明式定义，支持通过 `configs/default.yaml` 的 `custom_rules` 追加自定义规则（`from_config`）。

### 3.3 哈希占位符与敏感索引（隐私-记忆分离）

- 每条命中的敏感内容通过 `_generate_placeholder_id()` 生成唯一占位符：
  `SHA256(原文 + 类型 + SALT)[:8]` → 形如 `<HASH_7f3a9b>`。
- 占位符**不可逆**（单一哈希 + 随机盐，爆破成本极高），记忆库中只保留占位符，模型无法从占位符还原真实隐私。
- `SensitiveIndex` 维护 `placeholder_id → SensitiveEntry` 双向映射（`entries` 与 `reverse_map`），并提供：
  - `add_entry / get_original / get_placeholder / remove_entry`（软删除）
  - `get_stats()`：总量、活跃数、类型分布统计
  - `save_to_file / load_from_file`：JSON 持久化（按 `user_id` 隔离）

`SensitiveEntry` 数据模型（`src/common/models.py`）：

```python
class SensitiveEntry(BaseModel):
    placeholder_id: str      # 如 <HASH_7f3a9b>
    original_content: str    # 原始敏感内容
    sensitive_type: str      # phone / id_card / email ...
    user_id: str             # 所属用户（隔离）
    created_at: datetime
    is_deleted: bool = False # 软删除标记
```

### 3.4 索引加密（AES-GCM）

实现：`src/security/index_crypto.py`。

- **仅对 `original_content`（敏感原文）加密**，占位符 ID、类型等保持明文，保证索引查询/统计性能。
- 算法：AES-256-GCM，密文格式 `MOKOA1:<base64(nonce)>.<base64(ciphertext+tag)>`，前缀用于**版本识别与明文兼容**（旧明文索引可直接加载）。
- 密钥来源：环境变量 `MOKOA_SECURITY_KEY` 优先；兜底本地密钥文件 `~/.mokoa/security.key`（首次自动生成，非 Windows 平台限制 `0600` 权限）；文件不可写时退化为进程内随机密钥（仅当前进程可解密）。
- 容错：密钥变更/数据损坏导致解密失败时降级为空串，加载不中断。
- 专项测试覆盖：落盘无明文、往返还原、明文索引兼容、密钥变更降级。

> 关联点：`SensitiveIndex.to_dict()` 在落盘前调用 `encrypt_original()`，`from_dict()` 调用 `decrypt_original()`，加密逻辑与索引管理解耦。

### 3.5 还原（restore_sensitive）

`restore_sensitive(text)` 将正文中的 `<HASH_...>` 占位符按长度降序替换为原文（避免短占位符被部分替换），用于需要回显真实内容的受控场景。仅依赖本地索引文件，无索引则无法还原。

### 3.6 语义级识别器（可选）

`src/security/semantic_detector.py`（`SemanticPiiDetector`）：

- 调用 LM Studio 原生 `/api/v1/chat`（而非 OpenAI 兼容端点），以支持官方 `reasoning` 思考模式控制；语义识别场景默认 `reasoning="off"`，避免推理模型把输出写入 `reasoning_content` 导致正文为空。
- 内置少样本提示词，要求模型只输出原文精确片段的 JSON（`{"matches":[{"type","text","confidence"}]}`）。
- 兼容性：剥离 markdown 代码块围栏；兼容裸数组输出；兼容中文类型名（映射到英文别名）；尾部杂散字符做子串重试；置信度低于 `min_confidence`（默认 0.6）的匹配丢弃。
- 配置从 `configs/default.yaml` 顶层 `llm` 段读取（base_url/api_key/model/max_tokens/reasoning），`semantic_detection.enabled` 默认关闭，待按环境开启。

### 3.7 无状态脱敏与摄取标记

- `redact_sensitive_text()`：无状态脱敏（不写索引），供 ingestion staging 原始归档等无需反向恢复场景。
- `detect_privacy_risk()`：供 `IngestionPipeline` 在质量评分前打 `privacy_risk` 标记（仅识别高风险 PII/密钥集合 `_PRIVACY_RISK_RULE_NAMES`，IP/UUID/工号等运维上下文仍由 `filter_sensitive` 脱敏但不直接拒绝）。

---

## 四、精准遗忘子系统

### 4.1 敏感索引遗忘（forget）

`forget(user_id, nl_instruction)` 解析自然语言遗忘指令，扫描 `SensitiveIndex`，只返回匹配的 `placeholder_id` 列表（**解析阶段不执行删除**）。匹配策略：

1. 关键词直接匹配 `original_content`（包含关系，模糊）；
2. 关键词匹配 `sensitive_type`（内置「类型 → 关键词」映射表，如「手机」→ `phone`，「银行卡」→ `bank_card` 等）。

返回后由 `Orchestrator` 调用 `delete_sensitive_entries()` 统一软删除索引条目。

### 4.2 通用遗忘目标解析器（P0 修复，forget_parser）

实现：`src/security/forget_parser.py`（`ForgetTargetParser` / `ForgetTarget` / `ForgetResolution`）。

**背景（P0 根因）**：原 `forget` 只扫描脱敏索引，普通记忆/偏好（如下载路径偏好）不在索引中，导致 `POST /forget` 对非敏感内容恒返回 `count=0`。

**设计**：面向「候选记忆快照」（需具备 `id/content/memory_type/layer` 属性，如 `MemoryItem`）做多级文本匹配，输出带置信度与删除范围说明的目标列表，**解析阶段不执行删除**：

1. **类型意图识别**：指令是否点名某类记忆（`preference` / `knowledge`），用于收缩删除范围；
2. **主题词抽取**：抽取中文词组 + 英文单词，剔除动作词/虚词/类型意图词；过长（>6 字）中文块丢弃，主靠候选内容反向匹配；
3. **分级打分**（可注入权重，便于边界单测）：
   - 分支 A：候选内容词元反向命中指令（中文无标点整串主力路径）；
   - 分支 B：指令主题词正向命中候选内容；
   - 分支 C：标准偏好键别名（如 `download_path` ↔ 「下载路径」）；
4. **安全级联**：低于 `min_score=0.6` 不纳入；仅当指令显式出现「所有/全部/all」**且**限定类型时，才放行全量删除（提权词 `_ALL_WORDS`），防误删。

`ForgetTarget.preference_key` 携带标准键（如 `habit.download_path`），供前端展示与审计，并驱动偏好版本索引同步删除。

### 4.3 级联删除与版本索引同步（Orchestrator 联调）

`Orchestrator.forget()`（efca9634编排，本模块提供解析与索引删除能力）流程：

1. `security.forget()` → 敏感索引 `<HASH_...>` 目标；
2. `security.parse_forget_targets()` → 普通记忆/偏好 `memory_id` 目标；
3. `security.delete_sensitive_entries()` → 敏感索引软删除；
4. `_purge_memory()` → 从短期/中期/Milvus 级联删除；
5. `_sync_pref_index_delete()` → 删除偏好后同步清除本地版本索引（`version_index.delete()`），保证画像/版本索引与短中长期/向量存储一致。

候选来源 `_forget_candidates()` 汇总：短期 + 中期 + 长期偏好 + 长期召回，全程按 `user_id` 隔离（用户 A 遗忘不删用户 B）。

---

### 4.4 知识库按自然语言条件批量定位遗忘（新增）

实现：`src/security/knowledge_forget.py`（`KnowledgeForgetResolver` / `KnowledgeForgetPlan`）。

**背景**：原 `forget` 已能处理脱敏索引与普通记忆/偏好，但「按自然语言条件在知识库（Milvus）中批量定位并遗忘」此前未打通（详见 4.1/4.2）；该功能现已于 2026-09-09 由 `knowledge_forget.py` 实现并接入 `Orchestrator.forget`，见第十节。

**设计**：把知识库遗忘指令解析为一份计划（`KnowledgeForgetPlan`）：

1. **意图识别**：命中知识触发词（知识库 / 文档 / 资料库 / 笔记 / knowledge / doc / wiki…）或 `forget_parser` 的 `knowledge` 类型意图；
2. **定向召回查询**：从指令抽取主题词，作为 `recall(memory_type=knowledge)` 的查询串，提升知识命中率（突破空查询 `top_k` 限制，避免「近期对话冲掉文档事实」）；
3. **批量删除过滤**：`want_all`（「删除所有文档」类提权指令）时直接构造 `delete_memory(filters={memory_type, nl_condition})`，走 Milvus `delete_by_filters` 一次批量软删，覆盖 recall 之外的全量条目；
4. **候选打分**：非全量指令复用 `ForgetTargetParser` 对定向召回的知识候选打分（`min_score=0.6` 安全门控），命中目标并入 `Orchestrator.forget` 的级联删除流程。

`Orchestrator.forget` 在原有「敏感索引解析 → 普通记忆/偏好解析」之后调用 `security.plan_knowledge_forget(instruction)`：命中知识意图时把定向召回的知识候选并入候选集供目标解析，并在 `want_all` 时补一次按 `filters` 的批量删除。全程按 `user_id` 隔离，与短/中/长期、向量库删除共用 `_purge_memory` 链路。

测试：`tests/test_security_isolation_persistence.py`（解析层：意图 / 主题 / `want_all` / 候选定位）、`tests/test_knowledge_forget.py`（集成层：Orchestrator + FakeMilvus 验证定位、级联删除与批量删除）。

---

## 五、上下文维护子系统

实现：`src/core/context_perception.py`（`ContextAnalyzer`、`ContextBlock`、`KeywordRule`）。

**目标**：在上下文窗口逼近上限（阈值 70%~80%）前主动压缩重构，实现「热插拔」式无感知更新，避免截断/滚动窗口造成的信息丢失与对话断层。

**分层架构**：
- `system` / `user_prompt` / `long_term` 标记为 `is_cached`，**永不压缩**（系统指令、用户设定、长期记忆已高度提炼）；
- 最近几轮对话保持不变（核心意图）；
- 工具调用过程压缩为概括（如「已执行 3 个命令：安装依赖、启动服务、验证端口」）；
- 中间对话的重复/无关/已解决部分压缩为要点列表。

**能力**：
- `ContextAnalyzer.analyze()`：评估 Token 压力、语义活跃度（话题变化频率）、信息密度（事实 vs 闲聊），动态选择策略 `restructure`（>0.9）/ `summary`（>0.6 或高活跃）/ `prune`（低压力）；
- `stream_compress()`：按 `block_size`（默认 5 轮）流式分块逐步压缩；
- 三层处理策略：关键词快速过滤 → 模型摘要 → 语义检索兜底（模型不可用时降级关键词摘要）；
- 关键词规则可从 `configs/keywords.yaml` 加载，缺失/解析失败时回退内置默认规则（工具输出、问候语、确认语）。

---

## 六、接口契约与配置

### 6.1 接口（SecurityService，`src/common/interfaces.py`）

| 方法 | 说明 |
|------|------|
| `filter_sensitive(text)` | 检测并掩码 PII，返回 `(filtered, detected_types, placeholder_map)` |
| `restore_sensitive(text)` | 从占位符按索引还原原文 |
| `forget(user_id, instruction)` | 解析 NL 遗忘目标（仅敏感索引），返回 `placeholder_id` 列表 |
| `parse_forget_targets(instruction, candidates)` | 解析 NL 遗忘目标（普通记忆/偏好），返回 `ForgetResolution` |
| `delete_sensitive_entries(user_id, ids)` | 索引软删除，返回已删除 id 列表 |
| `get_sensitive_stats(user_id)` | 统计（总量/活跃/类型分布） |
| `save_index / load_index` | 索引持久化与加载 |

### 6.2 配置项（configs/default.yaml）

- `security.custom_rules`：追加自定义脱敏规则；
- `security.semantic_detection.enabled`：语义层开关（默认关闭）；
- `security.index_path`：索引文件路径；
- `security.user_id`：默认用户隔离 ID；
- `llm.*`（base_url / api_key / model / max_tokens / reasoning）：语义识别与上下文摘要共用；
- `MOKOA_SECURITY_KEY` 环境变量 / `~/.mokoa/security.key`：索引加密密钥。

---

## 七、与上下游协作

| 模块 | 负责人 | 协作方式 |
|------|--------|----------|
| 编排核心 `orchestrator` | efca9634 | 脱敏在 `process_input()` 写短期记忆前调用；`forget()` 对外暴露，级联删除由其编排 |
| 短期/中期记忆 | efca9634 | `/forget` 短期/中期/Milvus 级联删除已接线 |
| 知识检索 `memory_service` | cb0aeabe | 已接 `delete_memory()` 按 `memory_id` / `filters` 删除；自然语言条件批量定位已由本模块（`knowledge_forget.py`）打通并接入 `Orchestrator.forget` |
| ingestion staging | 3fba60e1 | staging 在 `filter_sensitive` 前写入，对审计/元数据/raw 做无状态规则脱敏；命中 memory_id 后同步擦除 content/metadata/raw 与 TTL 指纹 |

staging 使用不可恢复的固定标签独立脱敏，不写 `SensitiveIndex`，已与安全设计对齐。

---

## 八、测试与验收

### 8.1 测试覆盖

- `tests/test_security.py`：包含 **121 项单元测试**，覆盖正则检测、HASH 占位符脱敏、NL 遗忘、语义层识别、索引加密（落盘无明文、往返还原、明文兼容、密钥变更降级）、通用遗忘目标解析（P0 验收六条）。
- 全项目可收集测试用例 **209 项**（其中本模块相关：`test_security.py` 121 项，`test_security_isolation_persistence.py` 与 `test_knowledge_forget.py` 共 15 项）。本模块专项测试已验证：后两者在 Windows 下 **14 passed / 1 skipped**（POSIX 密钥文件权限测试在 Windows 跳过），无新增回归。

### 8.2 P0 精准遗忘六条验收（2026-08-25）

| # | 验收标准 | 结果 |
|---|----------|------|
| ① | 「下载路径偏好」映射到 `habit.download_path` | ✅ |
| ② | 返回真实删除 `memory_id` | ✅ |
| ③ | 偏好画像/版本索引/短中长期/向量存储同步删 | ✅ |
| ④ | 删除后 `/recall` 返回 0 条 | ✅ |
| ⑤ | 用户 A 遗忘不删用户 B（全链路 `user_id` 隔离） | ✅ |
| ⑥ | 审计不保留未脱敏原文（staging `redacted`、元数据预脱敏） | ✅ |

---

## 九、运行与部署

```bash
# 安装依赖
pip install -r requirements.txt

# 运行本模块测试
python -m pytest tests/test_security.py -v

# 开启语义层（可选，需本地 LM Studio 就绪）
# 在 configs/default.yaml 设置 security.semantic_detection.enabled: true
# 并设置 llm.base_url / model 等

# 索引加密密钥
export MOKOA_SECURITY_KEY="<your-passphrase>"   # 或依赖 ~/.mokoa/security.key 自动生成
```

**注意事项**：
- 索引文件默认落盘于进程工作目录 `security_index.json`，生产建议置于受保护路径并通过 `index_path` 指定；
- 多用户隔离（按 `user_id`）已打通，相关「多用户实例管理」与「持久化安全」专项测试已完成（见 `tests/test_security_isolation_persistence.py`，第十节）；
- 语义层默认关闭；向量检索默认使用进程内 `InMemoryMilvusClient`（mock）开发后端，真实 Milvus 接入由 `src/knowledge/memory_service/milvus_client.py`（cb0aeabe）经部署配置启用。

---

## 十、完成情况与展望

- [x] 正则脱敏 + 加盐哈希占位符 + 索引加密（AES-GCM）
- [x] 语义级识别（LM Studio，可降级）
- [x] 敏感索引 NL 遗忘 + 通用遗忘目标解析（P0 普通记忆/偏好打通）
- [x] 上下文压缩引擎（分层 + 流式 + 动态策略）
- [x] 用户隔离（多用户实例管理）专项测试 — `tests/test_security_isolation_persistence.py`（按 `user_id` 隔离：filter/forget/stats/restore 互不串扰）
- [x] 持久化安全专项测试 — `tests/test_security_isolation_persistence.py`（落盘无明文、往返还原、明文索引兼容、密钥轮换降级、密钥文件权限）
- [x] 知识库按自然语言条件批量定位遗忘 — `src/security/knowledge_forget.py` + `Orchestrator.forget` 定向召回/批量删除，`tests/test_knowledge_forget.py` 集成验证

> 说明：上述三项原为「待办」，已于 2026-09-09 实现并以专项测试覆盖：对应 `test_security_isolation_persistence.py`（13 项）与 `test_knowledge_forget.py`（2 项）共 15 项，Windows 下 **14 passed / 1 skipped**（POSIX 密钥文件权限测试在 Windows 跳过）；既有 `tests/test_security.py`（121 项）无回归。

