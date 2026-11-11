# 麒忆智脑 · cb0aeabe对接说明（偏好入库 & Orchestrator 联调）

**发起人：efca9634**  
**关联角色：26a9835a（偏好记忆模块负责人）、cb0aeabe（知识检索模块负责人）**  
**日期：2026-07-31；状态复核：2026-08-06（按当前代码更新）**

---

## 一、背景：核心决策回顾

在偏好模块实现初期，团队确立了以下关键架构决策，旨在解耦数据提取与存储逻辑：

1.  **入库路径决策**：采纳方案 B。偏好模块（26a9835a负责）仅负责从原始数据中提取结构化 `MemoryEntry`，**不直接操作数据库**。
2.  **存储架构**：采用两层存储模型。
    *   **本地版本索引**：由偏好模块维护，用于开发期测试及冲突消解前的历史查询。
    *   **Milvus 正式库（目标架构）**：由知识检索模块（cb0aeabe负责）管理，存储最终权威数据，支持 RAG 检索与前端展示；当前代码仍使用进程内 mock。

---

## 二、当前系统状态（联调起点）

### 1. 编排核心模块（efca9634负责）
- ✅ `Orchestrator.process_input`：已实现，负责接收输入并写入短期记忆，已对接多源接入模块。
- ✅ `POST /ingest` 与 `GET /short-term/{user_id}` API：已可用。
- ✅ `Orchestrator.resolve_and_store`：已通过适配器转换为 `MemoryEntry`，并调用 `create_memory()`；偏好条目走 `store_memory_entry()` 完成版本处理。
- ✅ `Orchestrator.recall`：已包装 `query_memory()`。
- ✅ `POST /recall`、`POST /store-entry`、`GET /preferences/{user_id}` 与 `POST /consolidate/{user_id}` API：已接线。
- ⚠️ 当前 `MilvusClient` 仍是进程内 mock，真实 pymilvus 连接、Collection 建表和向量检索尚未实现。

### 2. 偏好记忆模块（26a9835a负责）
- ⏳ `extract()` 方法：待实现，需输出 `list[MemoryEntry]`。
- ✅ `get_version_history()`：已支持按 `preference_key` / `config_key` 查询进程内版本历史。
- ✅ 当前版本链使用 `preference_key`、`version`、`is_latest`、`supersedes_memory_id`；`preference_value`、`category` 仍可按业务需要放入 metadata。

### 3. 知识检索模块（cb0aeabe负责）
- ✅ 基础骨架：`create_memory`, `query_memory`, `update_memory`, `delete_memory` 已就绪。
- ✅ Schema 定义：`memory_schema.py` 已定义五类 `memory_type`。
- ✅ 策略配置：`memory_policy.py` 已实现入库评分逻辑。

---

## 三、需知识检索模块确认的关键问题

### 3.1 MemoryEntry 与 Milvus Metadata 兼容性

偏好模块计划入库的 Metadata 结构如下：
```json
{
  "preference_key": "habit.download_path",
  "preference_value": "D:/Downloads",
  "category": "habit",
  "version": 2,
  "is_latest": true,
  "supersedes_memory_id": "uuid-of-v1",
  "scene_id": "coding-session"
}
```

当前结论：
1. `create_memory` 会通过 `**entry.metadata` 合并自定义字段，当前 mock 可保存上述字段。
2. 真实 Milvus Collection 尚未创建，生产 Schema、动态字段策略、字段白名单和长度限制仍需cb0aeabe确认。

### 3.2 偏好更新策略选择

当同一 `preference_key` 发生版本更新（v1 -> v2）时，采用何种存储策略？

| 方案 | 做法 | 优点 | 缺点 |
| :--- | :--- | :--- | :--- |
| **方案 1** | `update_memory` 原地更新，`version++` | 记录数少，检索简单 | 历史版本需额外存储或丢失 |
| **方案 2** | `create_memory` 插入新记录，旧记录置 `is_latest=false` | 完整保留版本链，适合前端展示 v1→v2 | 同 Key 多条记录，检索需过滤 |

**当前决策：已采用方案 2。** `store_memory_entry()` 插入新版本，并通过
`update_memory()` 将旧版本标记为 `is_latest=false`；新版本记录
`supersedes_memory_id`。`update_memory.py` 仍保留原地更新能力，但不是当前偏好版本链主路径。

### 3.3 测试用例需求

需由知识检索模块提供 2～3 条可直接跑通的 `MemoryEntry` 样例数据（`memory_type=preference`，覆盖不同 `task_type`），用于：
- 对齐偏好模块的 `extract` 输出格式
- 编排核心模块的联调测试
- 构建基准测试（Benchmark）的种子数据

**示例：**
```json
{
  "content": "用户偏好中文回答，代码要可直接运行",
  "memory_type": "preference",
  "task_type": "coding",
  "source": "chat",
  "user_id": "demo_user",
  "scene_id": "coding-session",
  "confidence": 0.85,
  "metadata": {
    "preference_key": "style.reply_language",
    "preference_value": "中文",
    "category": "style",
    "version": 1
  }
}
```

### 3.4 查询接口兼容性

前端偏好页面需要展示"同一偏好的历史版本 v1 → v2"。

当前结论：
1. 当前 mock 支持按 `memory_type`、`preference_key`、`is_latest` 等顶层字段精确过滤；`GET /preferences/{user_id}` 已封装这些过滤条件。
2. `/recall` 返回顶层 `items`、`latency_ms`、`strategy`；每条 item 包含 `MemoryItem` 字段。Milvus 命中的 `score` 当前未透传，前端不能依赖相似度字段。
3. `GET /preferences/{user_id}` 默认 `latest_only=true`，传 `latest_only=false` 可查询版本历史。

### 3.5 冲突消解与更新逻辑衔接

编排核心模块的 `conflict_resolver` 在处理偏好冲突时会决定 `replace`、`keep_old` 或 `merge`。

当前结论与待办：
1. 偏好冲突当前走新插版本，并把旧记录标记为非最新，不做软删。
2. `update_memory` 在 content 实际变化时会重新生成 embedding；仅更新 metadata 时不会重算。
3. "已知种子记忆 + 期望替换结果"对照数据集仍需补充，用于稳定衡量冲突处理正确率。

### 3.6 检索接口联调细节

当前结论与待办：
1. `recall()` 已直接包装 `query_memory()`，并传入 `user_id`、记忆类型、场景和额外 filters。
2. 当前 mock 可用于功能联调，但不能代表真实 Milvus 的向量检索质量或 ≤500ms 性能，需要在答辩环境单独压测。
3. `/recall` 已提供列表与总耗时；相似度 score 尚未透传，前端字段仍需在真实检索接入后复核。

### 3.7 Embedding 对接

`embedding_client.py` 目前调用编排核心模块提供的 `embedding_interface`（支持麒麟 SDK 或本地 Fallback）。

当前结论与待办：
1. 开发环境可显式使用本地 embedding backend；`auto` 会按平台选择 Linux/Kylin 或本地实现。
2. 当前仅对 `MemoryEntry.content` 生成 embedding。
3. 尚未看到 embedding 调用失败后的降级和统一错误码，需要在生产接入前补充。

### 3.8 开发与部署环境

当前结论与待办：
1. 代码当前默认使用进程内 mock；Milvus Lite / Docker / 集群模式尚未接线。
2. `configs/default.yaml` 已有 `milvus_uri` 和 `collection_name`，但工厂尚未把这两个配置传给 `MilvusClient`，不能视为生产配置已生效。
3. 当前未发现种子记忆批量导入脚本，可先通过 `/store-entry` 或 `create_memory()` 逐条导入；是否补脚本由 benchmark 需求决定。

---

## 四、后续行动项（编排核心模块）

当前完成情况：
1. [x] `resolve_and_store()` 接入 `create_memory()` / `update_memory()`。
2. [x] 完成 `MemoryItem` 与 `MemoryEntry` 双向转换适配器。
3. [x] 实现 `POST /recall` API。
4. [x] 实现 `POST /consolidate/{user_id}` 与 `GET /preferences/{user_id}`。
5. [ ] 接入真实 Milvus，并验证 Schema、向量检索、配置加载与性能。
6. [ ] 根据前端需要决定是否透传相似度 score。

**注：编排核心模块不会直接修改知识检索模块的目录结构，仅通过公开 API 接口交互。**

---

## 五、建议联调顺序

1. [x] **开发链路打通**：编排核心通过 `create_memory` 写入单条偏好记录。
2. [x] **版本策略落地**：采用方案 2，保留 v1→v2 版本链。
3. [x] **查询 API 接线**：`query_memory`、`POST /recall` 和偏好历史查询已接线。
4. [ ] **真实 Milvus 联调**：落地 Collection Schema、连接配置、向量检索与相似度返回。
5. [ ] **基准测试准备**：补种子数据导入方式，并在目标环境验证召回质量和延迟。

---

## 六、相关文件索引

| 文件 | 说明 |
| :--- | :--- |
| `src/knowledge/memory_service/memory_schema.py` | MemoryEntry 定义（知识检索模块维护） |
| `src/knowledge/memory_service/create_memory.py` | 入库入口 |
| `src/knowledge/memory_service/update_memory.py` | 更新入口 |
| `src/knowledge/memory_service/query_memory.py` | 检索入口 |
| `src/core/orchestrator.py` | 核心调度与冲突消解（编排核心模块） |
| `src/core/conflict_resolver.py` | 冲突消解逻辑 |
| `src/preference/store.py` | 偏好提取与存储（偏好记忆模块） |
| `src/embedding/embedding_interface.py` | Embedding 统一入口 |

---

## 七、沟通渠道

- **编排/冲突/联调节奏** → efca9634（编排核心模块）
- **存储细节/查询格式** → cb0aeabe（知识检索模块）
- **偏好提取逻辑** → 26a9835a（偏好记忆模块）

---

请知识检索模块重点关注 **第三节** 的 8 组问题，尤其是 3.1（Schema 兼容性）和 3.2（更新策略），以便编排核心模块尽快完成重构联调。
