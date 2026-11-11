# Knowledge Delivery

## 范围

`src/knowledge/memory_service/` 为主。

## 当前结论

- 这一块已经切到本地 `Milvus Lite` 路线。
- `MILVUS_URI` 使用本地 `.db` 文件路径。
- `EMBEDDING_BACKEND` 默认走 `local`。
- `VECTOR_DIM` 固定为 `1024`。
- 对外主入口是：
  - `create_memory`
  - `query_memory`
  - `update_memory`
  - `delete_memory`

## 代码职责

- `create_memory`：校验、评分、embedding、入库。
- `query_memory`：支持 `memory_id` 精查，以及 `query + filters` 检索。
- `update_memory`：内容变化时重算 embedding，metadata 变化时只改字段。
- `delete_memory`：默认软删，按 `memory_id` 或 `filters` 删除。
- `EmbeddingClient`：只封装统一 embedding 入口，不直接依赖具体 SDK。
- `MilvusClient`：负责 schema、写入、查询、更新、删除。

## 接口约定

- `create_memory` 只接标准化后的 `MemoryEntry`。
- `query_memory` 返回 `items` 和 `latency_ms`。
- `delete_memory` 的 `nl_condition` 只做预留，不在本层做自然语言解析。
- `filters` 里只放可检索的结构化字段。

## 对接注意

- 不要在业务层直接改 embedding SDK。
- 不要把 `content` 以外的大段文本塞进向量输入。
- `content` 变化一定要重算向量。
- 软删是默认行为，测试数据可直接清理，不需要上传数据库文件。

## 交接给后续的人

如果继续做正式 Milvus 接入，只需要确认这几件事：

1. `MilvusClient` 的真实库配置是否仍然是本地 `.db`
2. embedding 后端是否已经在 Linux/麒麟环境可用
3. `memory_schema.py` 的字段是否继续维持当前约定
4. `query_memory` / `delete_memory` 的 filters 是否还要扩展

