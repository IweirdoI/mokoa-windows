# 2abac5a6 — 信息脱敏与上下文维护

## 一、分工概述

负责 MOKOA 项目的信息脱敏与上下文维护模块。核心目标：**让 OS Agent 的记忆系统在长期交互中既能保护用户隐私安全，又能维持对话体验的连贯性。**

## 二、负责目录

`src/security/service.py`

## 三、工作内容

> **当前实现复核（2026-08-09）：** 正则敏感信息检测已接入
> `Orchestrator.process_input()`；脱敏正文已写回 `<HASH_...>` 加盐哈希占位符，
> `restore_sensitive()` 可通过本地索引完整还原；自然语言遗忘解析已实现，
> `forget()` 可按类型/原文关键词匹配 SensitiveIndex 并只返回目标 ID，正式软删除
> 统一由 Orchestrator 调用 `delete_sensitive_entries()` 执行；索引持久化
> 已加密（AES-GCM，仅原文加密，密钥走环境变量/本地密钥文件）。剩余差距：
> LLM 语义级敏感识别代码已就绪（Ollama 适配），配置默认关闭待按环境开启；
> 用户隔离（多用户实例管理）与持久化安全仍需专项测试。

### 1. 信息脱敏

**涉及题目要求：** (5) 敏感信息识别与精准遗忘

**核心问题：**

大部分人在使用模型服务时走的是云端 API，数据会经过第三方供应商。我们不能假设供应商不会利用这些语料训练自有模型，也无法完全排除中间人攻击等风险。因此在数据进入记忆系统（至少是模型输入）之前，必须在本地完成敏感信息的识别与替换。

**目标设计：**

- 在数据入库前，对输入文本执行本地敏感数据识别，覆盖手机号、身份证号、邮箱、银行卡号、地址、密码等常见类型；目标是把敏感内容替换为占位符（如 `<HASH_7f3a9b>`），并加密保存原始内容与占位符的映射关系
- 识别到敏感内容后，将其替换为不可逆的哈希占位符（如 `<HASH_7f3a9b>`），并通过加盐哈希的组合方式建立敏感内容到占位符的单向映射
- 在本地维护一个引用索引文件，记录占位符与实际敏感数据的关联关系；当占位符数量较多时，索引文件支持快速判断某段内容是否已被索引、快速定位到对应位置与关系链
- 仅通过占位符无法反向还原原始内容（单一哈希不可逆，爆破成本极高），还原需要依赖本地索引文件，从而实现隐私与记忆的分离

**回退方案：**

- 主方案优先使用本地大语言模型做语义级敏感识别
- 回退方案走模式识别（正则表达式等确定性规则），确保在模型不可用时仍能提供基础脱敏保护

**Phase 1 完成情况（2026-08-04）：**

- ✅ 实现了基于正则表达式的高精度敏感信息检测，覆盖 50+ 种敏感类型（手机号、身份证、邮箱、银行卡、护照、统一社会信用代码、固定电话、IPv4、SSH 私钥、JWT Token、API Key、加密货币钱包地址、Tor Onion 地址等）
- ✅ 使用 SHA256+盐生成唯一哈希 ID，并已写回脱敏正文（`<HASH_...>` 占位符），索引维护原文与哈希 ID 的双向映射
- ✅ 已实现敏感信息索引管理器（`SensitiveIndex`），持久化已加密：AES-GCM 仅加密原文，placeholder_id/type 留明文；密钥走环境变量 `MOKOA_SECURITY_KEY` 或本地密钥文件 `~/.mokoa/security.key`；兼容历史明文索引，密钥变更时旧条目降级为空串不中断加载
- ✅ 已提供 `restore_sensitive()`，脱敏正文含哈希 ID，可通过索引还原 `filter_sensitive()` 的实际输出
- ✅ 实现了索引管理功能：支持软删除（`delete_sensitive_entries()`）、统计查询（`get_sensitive_stats()`）、持久化（`save_index()`/`load_index()`）
- ✅ 更新了 `SecurityService` 接口，新增 `restore_sensitive()`、`delete_sensitive_entries()`、`get_sensitive_stats()`、`save_index()`、`load_index()` 方法
- ✅ `tests/test_security.py` 当前包含 92 个测试，覆盖正则检测、HASH 占位符脱敏、NL 遗忘、语义层识别与索引加密
- ✅ 自然语言遗忘解析已实现：`forget()` 按类型关键词/原文关键词匹配 SensitiveIndex，只返回 placeholder_id，不在解析阶段修改索引
- ✅ 索引加密已实现并通过专项测试（落盘无明文、往返还原、明文索引兼容、密钥变更降级）
- ⏳ 用户隔离（多用户实例管理）与持久化安全仍需专项测试
- 涉及文件：`src/security/service.py`、`src/security/semantic_detector.py`、`src/security/index_crypto.py`、`src/common/interfaces.py`、`src/common/models.py`、`tests/test_security.py`

### 2. 上下文维护

**核心问题：**

智能体在实现长期交互时，对话上下文会不断累积。每个模型的上下文窗口是有限的，如果不做维护，最终一定会触达上限导致对话中断或其他异常。

模型初期（同期上下文4k左右）的常见的处理方式是达到上限后截断或滚动窗口，但这些做法体验较差——会丢失关键信息或造成对话断层。

**设计思路：**

在上下文还没有触达上限之前，主动对已有的上下文进行压缩重构，实现「热插拔」式的无感知更新：

1. **长期记忆设定文件保持不变** — 用户偏好、系统指令、角色设定等通过用户可感知与读写的文件注入上下文，这类信息本身已经是高度提炼的，不需要压缩
2. **最近的几轮对话保持不变** — 最近的用户意图和系统回复是当前对话的核心，直接保留（思维链按需决定（有的模型确实允许将思维链注入上下文：
enable_thinking：是否开启本轮思考模式
preserve_thinking：是否把历史思考存入上下文））
3. **工具调用压缩为概括** — 中间的工具执行过程（多次文件读写、命令运行等）压缩成一句摘要，如「已执行 3 个命令：安装依赖、启动服务、验证端口」
4. **中间对话过程压缩** — 早期对话中重复、无关、已解决的部分压缩为要点列表，如「用户曾咨询 Python 虚拟环境配置，已推荐 conda 方案」

**触发机制：**

压缩过程在上下文达到预定阈值（如窗口容量的 70%~80%）时主动触发，而不是等撑爆才处理。等实际到达上限时，系统自动将压缩好的消息替换原有上下文，对用户而言整个过程无感知。

**Phase 2 完成情况（2026-08-05）：**

- ✅ 实现上下文压缩引擎（`src/core/context_perception.py`）：
  - 分层架构设计：系统提示词/用户提示词/长期记忆永不压缩
  - 支持流式压缩：按轮数分块，逐步压缩
  - 三层处理策略：关键词快速过滤 → 模型摘要 → 语义检索兜底
  - 动态策略选择：基于 Token 压力、语义活跃度、信息密度自动选择压缩策略
  - 支持关键词规则配置（可从 YAML 文件加载）
  - 内置默认关键词规则（工具输出、问候语、确认语等）
- ✅ 实现上下文感知分析器（`ContextAnalyzer`）：
  - 语义活跃度评估（话题变化频率）
  - 信息密度评估（事实 vs 闲聊比例）
  - Token 压力监控
- ✅ 实现上下文分块管理（`ContextBlock`）：
  - 缓存标记（system/user_prompt/long_term 永不压缩）
  - 压缩状态追踪
- 涉及文件：`src/core/context_perception.py`

### 2. 精准遗忘

**涉及题目要求：** (5) 敏感信息识别与精准遗忘

**核心问题：**

用户需要能够自然语言驱动地删除或遗忘特定的记忆内容，而不仅仅是删除所有记忆。

**设计思路：**

- 解析自然语言遗忘指令（如"忘记关于我的生日和手机的记忆"）
- 将自然语言指令转化为结构化查询（按类型、关键词、时间范围）
- 执行实际删除操作（短期记忆、中期记忆、长期知识库）

**Phase 3 进度（2026-08-09 更新）：**

- ✅ 已实现 NL 遗忘解析器：按类型关键词（手机/邮箱/密码等）与原文关键词匹配 SensitiveIndex，只返回 placeholder_id
- ⚠️ 多源删除部分完成：Orchestrator 统一按 placeholder_id 软删索引、按本地或 Milvus memory ID 级联删除短期/中期/长期待入库/Milvus 与 staging；"按占位符定位并删除记忆正文条目"的完整闭环仍待联调
- ✅ 与 Orchestrator 的 `forget()` 方法对接完成（efca9634侧已处理占位符与 memory_id 的分离逻辑）

**Phase 3 P0 修复（2026-08-25，`c9f933a`）：普通记忆/偏好的精准遗忘打通**

> 背景：原 `/forget` 只扫描脱敏索引（SensitiveIndex），普通记忆/偏好（如下载路径偏好）不在索引中，导致非敏感内容的遗忘指令恒返回 `count=0`。

- ✅ 新增通用 NL 遗忘目标解析器（`src/security/forget_parser.py`）：类型意图识别 + 话题词抽取 + 两级打分（候选词元反向命中指令为核心）+ 置信度门控 `min_score=0.6` 防误删；全量删除需显式表达"所有"且限定类型才放行。
- ✅ `Orchestrator.forget` 对候选偏好做同步清理：删除后同步清除本地偏好版本索引，保证画像/版本索引与短期/中期/向量存储一致（`src/preference/version_index.py` 新增 `delete()`）。
- ✅ 解析目标携带 `preference_key` 标准键（如 `habit.download_path`），便于前端展示与审计（`src/security/forget_parser.py`）。
- ✅ P0 六条验收标准全部通过（见下"验收"）；本模块测试 `test_security.py` 121 项、专项 + 集成 15 项（Windows 14 passed / 1 skipped）无回归。

## 四、与上下游协作

| 模块 | 协作方式 | 说明 |
|------|----------|------|
| 编排核心（efca9634） | 输入 + 输出 | 脱敏在 `process_input()` 写入短期记忆前调用 `filter_sensitive()` 执行；遗忘接口 `forget()` 由 Orchestrator 对外暴露 |
| 短期记忆（efca9634） | 输出 | `/forget` 的短期/中期/Milvus 级联删除已接线；NL 遗忘解析已返回 placeholder_id，Orchestrator 对占位符执行敏感索引软删除、对 memory_id 执行级联删除 |
| 知识检索模块（cb0aeabe） | 输出 | 已接 `delete_memory()` 按 memory_id / filters 删除；按自然语言条件批量定位已由本模块 `knowledge_forget.py` 打通并接入 `Orchestrator.forget` |

ingestion staging 虽然在 `filter_sensitive()` 之前写入，但现在会对审计 content、
metadata 和 raw payload 做无状态规则脱敏；raw 也可关闭，plaintext 仅供 dev 显式
双确认。`/forget` 命中本地或最终 Milvus memory ID 后会同步擦除 staging content、
删除 raw 和 TTL 指纹；手动 purge 默认采用 7d raw / 90d audit。staging 使用不可恢复
固定标签且不写 SensitiveIndex，已与安全设计对齐。

## 五、P0 验收（2026-08-25）

针对自然语言精准遗忘的六条验收标准实测结论：

| # | 验收标准 | 结果 | 说明 |
|---|----------|------|------|
| ① | "下载路径偏好"映射到 `habit.download_path` | ✅ 通过 | `ForgetTarget.preference_key` 携带标准键 |
| ② | 返回真实删除 ID | ✅ 通过 | `forget()` 返回真实 `memory_id` 列表 |
| ③ | 偏好画像/版本索引/短中长期/向量存储同步删 | ✅ 通过 | 新增版本索引 `delete()` 并接线 `forget` 同步清理 |
| ④ | 删除后 `/recall` 返回 0 条 | ✅ 通过 | 依赖 Milvus 删除 + 版本索引同步 |
| ⑤ | 用户 A 遗忘不删用户 B | ✅ 通过 | 全链路按 `user_id` 隔离 |
| ⑥ | 审计不保留未脱敏原文 | ✅ 通过 | staging `raw_archive_mode=redacted`、元数据预脱敏 |

新增验收测试：`tests/test_security.py` 的 `test_parser_maps_download_path_preference_key`、
`test_acceptance_forget_preference_syncs_all_layers`。完整测试套件 284 passed / 1 skipped。
