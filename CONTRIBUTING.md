# 贡献指南 — 麒忆智脑 (MOKOA)

> OS Agent 记忆优化系统 | 挑战杯 XA-202612 | 麒麟软件有限公司

首先感谢你参与这个项目！以下为团队协作规范，请所有成员阅读。

---

## 一、目录结构与负责人

| 目录 | 负责人 | 职责 |
|------|--------|------|
| `src/core/` | efca9634 | Orchestrator、短/中/长期记忆、冲突消解、Embedding |
| `src/ingestion/` | 3fba60e1 | 多源接入四段式：collector → cleaner → normalizer → quality |
| `src/preference/` | 26a9835a | 偏好提取、版本化、entry_adapter |
| `src/knowledge/memory_service/` | cb0aeabe | Milvus CRUD、RAG、memory_schema |
| `src/security/` | 2abac5a6 | 敏感信息识别于替换、NL遗忘、上下班维护 |
| `frontend/` | 6714f379 | Web 演示 |
| `tests/`, `benchmarks/`, `docs/` | da0f41a1 | 测试、文档、benchmark |

## 二、协作规则

1. **只改自己负责的目录**。跨模块调用通过 `src/common/interfaces.py` 或 `memory_schema.py` 进行。
2. **26a9835a不直接写 Milvus**：产出 `MemoryEntry` JSON → 调用 `create_memory()`。
3. **Embedding 统一入口**：`src/embedding/embedding_interface.py`（efca9634维护），其他人不要修改。
4. `src/adapters/` 是参考 stub，答辩前可不实现。
5. **最终记忆库只有一个**：cb0aeabe的 Milvus（`memory_service/`），当前开发后端为 mock。

## 三、数据流简要

```
数据源 → ingestion(3fba60e1) → Orchestrator(efca9634)
    → 敏感过滤(2abac5a6) → 分类 → 短期记忆
    → 晋升(short→mid→long) → 冲突消解 → preference(26a9835a) / knowledge(cb0aeabe)
    → 查询: recall(query) → query_memory → 长期记忆返回
```

详见 [README.md](./README.md) 第七节「数据流」。

## 四、Commit 规范

使用 Conventional Commits 格式，一行不超过 72 字符：

- `feat:` 新功能
- `fix:` Bug 修复
- `docs:` 文档更新
- `refactor:` 重构
- `test:` 测试相关
- `chore:` 构建/工具/杂项

示例：
```
feat(core): 实现短期记忆热度晋升逻辑
fix(security): 修复手机号正则漏匹配 IPv4 段
```

> 项目使用 `git-cliff` 自动生成 `CHANGELOG.md`，请勿手动维护。

## 五、测试

- 完成新功能或修改后，补充对应测试。
- 运行测试：
  ```bash
  cd MOKOA
  pip install -r requirements.txt
  python -m pytest tests/ -v
  ```
- 快速验证环境：
  ```bash
  python -c "from src.core.factory import create_orchestrator; o=create_orchestrator(); print('OK')"
  ```

## 六、常见问题

**Q: 我该从哪里开始看代码？**
A: 先看 [README.md](./README.md)，了解整体架构和分工。然后查 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。

**Q: 如何理解记忆分类？**
A: 见 [src/knowledge/memory_service/memory_schema.py](./src/knowledge/memory_service/memory_schema.py)，定义了 5 种 `memory_type`。

**Q: 性能指标是什么？**
A: 见 README.md 第八节 — 偏好提取 ≥ 85%、知识检索召回 ≥ 85%、检索响应 ≤ 500ms、冲突处理正确 ≥ 88%。

## License

Apache 2.0
