# 银河麒麟 AI SDK 接入说明

> 来源：`kylinos_dev_manual(1).pdf` — KylinSdk V3.0 开发指南，第 9 章 AI SDK

> **当前代码状态（2026-08-12）：** 已有 `kylin_ctypes.py`、
> `KylinEmbeddingAdapter`、预检与严格 smoke test，但尚无麒麟真机验证记录。开发环境的
> `LocalEmbeddingAdapter` 会加载 BGE-M3；模型不可用时会明确告警并降级为 1024 维
> 确定性哈希向量，避免零向量污染召回，但降级模式仍不能用于正式召回质量验收。
> 普通模式下麒麟初始化或运行失败会清理会话并降级到本地 backend；真机验收必须设置
> `OS_AGENT_KYLIN_STRICT=1`，失败时直接报错，禁止以降级结果冒充SDK验收。

## 1. 文档定位

这份手册是 **银河麒麟自研开发者套件（kysdk）** 的总文档，包含：

| 模块 | 与记忆项目的关系 |
|------|------------------|
| **9 AI SDK** | **核心**：文本向量化（embedding）、文本生成 |
| 系统能力 SDK | 可选：进程/资源监控 |
| 系统安全 SDK | 可选：与2abac5a6安全模块联动 |
| 应用支撑 SDK | 6714f379前端 Qt 控件可参考 |

比赛要求的 **「调用银河麒麟 embedding SDK」** 对应 **9.4.3 向量化 → 9.4.3.1 文本向量化**。

---

## 2. 部署前提

1. **操作系统**：银河麒麟 **2503** 版本  
2. **AI 子系统**：在「设置 → AI 模块管理」中下载安装  
3. **安装开发包**：

```bash
sudo apt install libkylin-coreai-embedding-dev
```

4. **模型配置**：「设置 → AI 模块管理」中配置（全局生效）  
5. **端侧适配机型**（文档列出的 x86/arm 整机，其他机型效果不保证）  
   - 联想开天 M90f G1s（D3000）  
   - 联想开天 M90h G1t（海光）  
   - 联想开天 P90z G1t（兆芯）

**开发阶段（非麒麟环境）**：使用 `LocalEmbeddingAdapter` 加载 BGE-M3；仅在依赖或
模型不可用时使用带告警的哈希向量降级。联调/答辩时切换并验证
`KylinEmbeddingAdapter`，正式指标不得使用降级向量。

---

## 3. 文本向量化 C API（核心）

**头文件：**

```c
#include <coreai/embedding/embedding.h>
```

**CMake 链接：**

```cmake
pkg_check_modules(KYSDK_COREAI_EMBEDDING kysdk-coreai-embedding)
target_link_libraries(demo PRIVATE ${KYSDK_COREAI_EMBEDDING_LIBRARIES})
```

> 注意：手册安装命令为 `libkylin-coreai-embedding-dev`，pkg-config 名为 `kysdk-coreai-embedding`（以真机为准）。

### 会话生命周期

| 函数 | 说明 |
|------|------|
| `text_embedding_create_session()` | 创建会话 |
| `text_embedding_init_session(session)` | 初始化，0=成功 |
| `text_embedding(session, text, &result)` | **同步**向量化 |
| `text_embedding_async(session, text, callback, user_data)` | 异步向量化 |
| `text_embedding_destroy_session(&session)` | 销毁会话 |

### 结果解析

| 函数 | 说明 |
|------|------|
| `embedding_result_get_vector_data(result)` | `float*` 向量数组 |
| `embedding_result_get_vector_length(result)` | 向量维度 |
| `embedding_result_get_error_code(result)` | 错误码 |
| `embedding_result_get_error_message(result)` | 错误信息 |
| `embedding_result_destroy(&result)` | 释放结果 |

### 错误码

```
COREAI_EMBEDDING_SUCESS = 0
COREAI_EMBEDDING_INPUT_ERROR
COREAI_EMBEDDING_INIT_ERROR
COREAI_EMBEDDING_CONNECTION_ERROR
COREAI_EMBEDDING_RUNTIME_ERROR
COREAI_EMBEDDING_ERROR_UNKNOWN = 99
```

### 并发注意

- **每个会话同时只能处理一个任务**；并发需创建多个 session  
- 批量 embed 时建议：session 池 + 队列，或同步接口 + 批处理

---

## 4. 同步调用示例（来自手册）

```cpp
TextEmbeddingSession *session = text_embedding_create_session();
if (text_embedding_init_session(session) != 0) { /* handle error */ }

EmbeddingResult *result = nullptr;
bool ok = text_embedding(session, "要向量化的文本", &result);

float *vec = embedding_result_get_vector_data(result);
int dim = embedding_result_get_vector_length(result);

embedding_result_destroy(&result);
text_embedding_destroy_session(&session);
```

---

## 5. Python 项目接入方案（推荐）

SDK 为 **C 接口**，Python 记忆模块需增加薄封装：

```
src/embedding/
├── embedding_interface.py # 统一工厂入口
├── kylin_adapter.py       # 麒麟 SDK（真机）
├── kylin_ctypes.py        # ctypes 绑定 C API
├── local_embedding.py     # BGE-M3；不可用时告警并使用哈希向量降级
└── native/                # 可选：C 扩展或独立 .so 服务
    └── kylin_embed_bridge.c
```

**方案 A（推荐答辩）：** Python → ctypes → `libkysdk-coreai-embedding.so`  
**方案 B：** 独立 C++ 微服务暴露 HTTP/gRPC，Python 调用（便于隔离崩溃）  
**方案 C：** 开发期 BGE-M3，麒麟机仅替换 `EmbeddingProvider` 实现

---

## 6. 与记忆模块的对接点

| 模块 | 如何使用 embedding |
|------|-------------------|
| `knowledge/memory_service/create_memory.py` | 通过 `EmbeddingClient` 对 content 向量化 |
| `knowledge/memory_service/query_memory.py` | 通过 `EmbeddingClient` 对 query 向量化 |
| `preference/store.py` | 当前不直接调用 embedding；跨场景相似匹配尚未实现 |
| `tests/test_metrics.py` | 真机测 latency ≤ 500ms（含 embed + search） |

**500ms 预算建议：**

- 单次 query embed：目标 < 100ms（端侧）  
- 向量检索（SQLite/本地索引）：< 50ms  
- 预留 LLM 重排：可选，需压测  

---

## 7. 其他可复用 AI 能力

| 能力 | 章节 | 用途 |
|------|------|------|
| 文本生成 | 9.5.1 | 偏好抽取、冲突融合（端侧 LLM，降云端依赖） |
| OCR | 9.4.1 | 工具结果含截图时的文字抽取 |
| 图像向量化 | 9.4.3.2 | 多模态知识记忆扩展 |

---

## 8. 适配测试报告 checklist（比赛交付）

- [ ] 麒麟 2503 + AI 子系统已安装  
- [ ] `libkylin-coreai-embedding-dev` 安装成功  
- [ ] 同步/异步 embed 单条延迟  
- [ ] 向量维度与 Milvus Collection Schema / 实际 provider 一致
- [ ] 记忆检索端到端 ≤ 500ms  
- [ ] 指定适配机型测试截图  

Owner: **efca9634** | Review: cb0aeabe（检索）、da0f41a1（测试报告）

### 自动化入口

```bash
# 环境、架构、安装包和动态库预检
python scripts/kylin_preflight.py --require-kylin --output data/reports/preflight.json

# 真实SDK严格测试（绝不降级）
OS_AGENT_KYLIN_STRICT=1 python scripts/smoke_kylin_embedding.py \
  --runs 20 --output data/reports/embedding.json

# 非麒麟环境的主链接线检查
python scripts/smoke_e2e.py
```
