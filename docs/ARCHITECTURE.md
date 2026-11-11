# 架构设计文档 — 麒忆智脑

Owner: efca9634 | 协助: da0f41a1

## 1. 设计原则：收缩而非堆叠

**一个月可交付** 的前提下，架构遵循：

- **一个核心**：自研 Memory Orchestrator  
- **三类记忆**：Preference / Knowledge / Lifecycle  
- **三个参考**：MemoryOS、Mem0、Cognee（设计借鉴，非并行部署）  
- **自研不可替代**：分类、冲突消解、多源清洗、麒麟 Embedding 适配、混合存储  

不再接入 Supermemory、Zep、LangMem 等并行 adapter，避免功能重叠和答辩解释成本。

## 2. 三层架构

```
┌──────────────────────────────────────────────────┐
│  Layer 3: API / Frontend                         │
├──────────────────────────────────────────────────┤
│  Layer 2: 自研核心                                │
│  Orchestrator | Classifier | ConflictResolver    │
│  Ingestion(4段) | Preference | Knowledge         │
│  Security                                        │
├──────────────────────────────────────────────────┤
│  Layer 1: 可插拔基础设施                          │
│  Embedding(统一接口) | Storage(混合)              │
│  Adapters(仅3个参考)                              │
└──────────────────────────────────────────────────┘
```

## 3. 开源参考映射

| 参考 | 角色 | 自研落点 |
|------|------|----------|
| MemoryOS | 记忆生命周期 | `core/layer_manager.py` |
| Mem0 | 偏好抽取与检索思路 | `preference/store.py` + `adapters/mem0_adapter.py` |
| Cognee | 知识图谱与结构化 | `knowledge/store.py` + `adapters/cognee_adapter.py` |

**为何不用 Zep？** Cognee 已覆盖知识图谱 + 结构化；ingestion 的多源模式在 `ingestion/collector.py` 自研即可。

## 4. Memory Orchestrator 接口

`src/core/orchestrator.py` 对外提供：

| 方法 | 职责 |
|------|------|
| `process_input()` | 接入 → 清洗链 → 分类 → 写入生命周期 |
| `classify()` | 偏好 vs 知识 |
| `consolidate()` | 短 → 中 → 长 晋升 |
| `resolve_and_store()` | 冲突检测 + 更新 |
| `recall()` | 包装 `query_memory()` 检索长期记忆，可按偏好/知识过滤 |
| `forget()` | 遗忘删除链路；NL 目标解析仍待安全模块完成 |

## 5. Ingestion 四段式（题目要求 1）

| 模块 | 文件 | 职责 |
|------|------|------|
| 采集 | `collector.py` | 工具日志、用户行为、手动配置、对话 |
| 清洗 | `cleaner.py` | 去噪、无效信息过滤、长度限制、去重指纹生成 |
| 标准化 | `normalizer.py` | 统一为 NormalizedRecord JSON |
| 质量 | `quality.py` | 置信度/价值评分，决定是否入库 |

质量门控通过后，`pipeline.py` 使用 `staging_store.py` 进行 SQLite TTL 原子去重；
直接构造 `IngestionPipeline()` 时使用 cleaner 内的进程内 fallback。

标准化输出示例：

```json
{
  "type": "behavior",
  "content": "用户经常使用 VSCode",
  "time": "2026-07-28",
  "confidence": 0.85,
  "source": "user_behavior",
  "user_id": "u001"
}
```

## 6. 存储方案（题目要求 4）

| 层级 | 方案 | 负责人 |
|------|------|--------|
| 最终记忆库 | **Milvus（生产目标）**；当前为进程内 mock | cb0aeabe `memory_service/` |
| 摄取中转库 | SQLite staging（审计、TTL 去重、按需 JSONL 导出） | 3fba60e1 |
| Embedding | 麒麟 SDK / 本地 fallback | efca9634 `embedding/` |

> 已移除早期 `src/storage/` SQLite 双库 stub，统一由 Milvus 承担向量存储。
> staging 在 Orchestrator 敏感过滤之前写入，但审计 content、扩展 metadata 和 raw
> 默认会先做无状态规则脱敏。raw 可关闭，plaintext 仅允许 dev 显式开启；`/forget`
> 可通过本地 memory ID 或最终 `milvus_memory_id` 命中并同步擦除 staging 内容；两种
> ID 分字段保留，不覆盖 event ID / `record_key`。手动 purge 默认执行 7d raw / 90d audit。
> 自动定时 purge 仍待接入；staging 不替代短期/中期记忆存储。

## 7. Embedding 统一接口（降低答辩风险）

```
embedding_interface.py   ← 后端工厂
├── kylin_adapter.py     ← 银河麒麟 SDK ctypes 适配骨架（待真机验证）
└── local_embedding.py   ← BGE-M3 本地模型；不可用时显式告警并降级为 1024 维哈希向量
```

当前可表述为：**「系统预留多种 Embedding 后端，已实现统一接口、麒麟 ctypes
适配骨架，并在开发机接入 BGE-M3 本地模型。」** 在完成麒麟真机测试前，
不应宣称麒麟 SDK 端侧向量性能已验收。

详见 `docs/KYLIN_SDK.md`。

## 8. 记忆流转（题目要求 6）

| 层级 | 内容 | 晋升条件 |
|------|------|----------|
| 短期 | 原始交互、工具结果 | 会话结束或容量满 |
| 中期 | 聚合片段 | 热度 ≥ threshold |
| 长期-偏好 | 习惯、风格、策略 | 质量分 + 分类为 preference |
| 长期-知识 | 工作流、案例、模板 | 质量分 + 分类为 knowledge |

## 9. 接口契约

见 `src/common/interfaces.py`。模块间禁止跨目录直接 import 实现类。

## 10. 待办

- [x] 麒麟 SDK 文档整理（KYLIN_SDK.md）
- [ ] Cognee adapter 最小 POC（cb0aeabe）
- [ ] 冲突消解评测集（da0f41a1）
- [ ] 麒麟真机 embedding 延迟测试（efca9634）
