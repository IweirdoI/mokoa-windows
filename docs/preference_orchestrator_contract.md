# 偏好模块 → Orchestrator 挂接契约

**From: 26a9835a（preference）→ To: efca9634（Orchestrator）**  
**抄送参考: cb0aeabe（create_memory 方案 2）**  
**日期: 2026-08-01**

---

## 1. 调用点（请你改）

```text
ingestion → process_input → short-term
                ↓
         consolidate（短→中→长）之后，或独立 preference 路径：
                ↓
         entries = preference.extract(raw_records)   # list[MemoryEntry]
                ↓
         对每条 entry：冲突消解 → create_memory / update_memory
```

**偏好模块不 import Milvus，也不直接调 `create_memory`。**

已接线（2026-09 起）：
- `process_input`：分类为偏好 **或** `extract_by_rules` 命中时，并行 `extract_preferences_from_text` → `store_memory_entry`（Path B）；响应含 `preference_extracted` / `preference_keys`
- `POST /preferences/extract`：显式提取入库
- `recall` 默认排除 `is_latest=false` 的旧偏好版本
- 短期候选与长期已生效偏好分离；**不要**为演示跳过 short→mid 热度门

---

## 2. 接口一览（已就绪）

| API | 位置 | 说明 |
|-----|------|------|
| `extract(records) → list[MemoryEntry]` | `PreferenceMemoryStore` | Path B 主出口 |
| `extract_from_text(text, user_id=, scene_id=)` | 同上 | Demo / 单测 |
| `get_version_history(user_id, preference_key)` | 同上 | 本地版本链 |
| `get_feature_profile(user_id, scene_id)` | 同上 | Agent 注入用结构化画像 |
| `resolve_preference(user_id, key, scene_id)` | 同上 | scene > global |

默认构造：`PreferenceMemoryStore(use_llm=True, allow_rules=True)`（Fusion）。

---

## 3. 入库策略（cb0aeabe已定 · 方案 2）

同一 `preference_key` 从 v1 → v2：

1. `create_memory(新 Entry)`，`metadata.is_latest=true`，`version=N`
2. `update_memory(旧 memory_id)`，只改 metadata：`is_latest=false`（不必改 content）
3. `metadata.supersedes_memory_id` 可指向旧 id（extract 输出里若本地有记录会带）

查询当前偏好：`filters={"memory_type":"preference","preference_key":"...","is_latest":true}`  
查历史：去掉 `is_latest`，按 `preference_key` 拉多条。

---

## 4. MemoryEntry 样例（可直接 create_memory）

文件：`benchmarks/datasets/preference_memory_entries.json`（3 条）

覆盖：
- `style.code_runnable` · coding
- `habit.download_path` · os_operation · manual
- `workflow.preview_before_execute` · os_operation

`content` 使用规范串：`[preference] key=value | 描述`（便于 embedding 随值刷新）。

---

## 5. 伪代码（建议落点）

```python
def resolve_and_store_preference(self, entry: MemoryEntry) -> None:
    key = entry.metadata["preference_key"]
    history = self.preference.get_version_history(entry.user_id, key)
    latest = next((h for h in reversed(history) if h.metadata.get("is_latest")), None)

    if latest and self.conflict_resolver.detect(latest, memory_item_from(entry)):
        decision = self.conflict_resolver.resolve_preference(latest, memory_item_from(entry))
        # map decision back to entry if needed

    # 方案 2
    resp = create_memory(CreateMemoryRequest(entry=entry))
    if latest and latest.id:
        update_memory(UpdateMemoryRequest(
            memory_id=latest.id,
            metadata={"is_latest": False},
        ))
```

Agent 读偏好（优先于纯向量）：

```python
profile = self.preference.get_feature_profile(user_id, scene_id)
# 使用 profile["kv"] / profile["agent_context"]
```

---

## 6. 生命周期红线（实现 mid/long 时）

见 `docs/memoryos_antipattern_audit.md`：勿原样拷贝 MemoryOS（session 向量不刷新、热度 peek 不 pop、整文件 IO）。

---

## 7. 联调检查清单

- [x] Path B：`process_input` 规则命中 / 偏好分类时并行 extract；亦可 `POST /preferences/extract`  
- [x] 冲突与版本链按 `preference_key`  
- [x] v1→v2：旧条 `is_latest=false`，新条可查  
- [ ] 前端偏好页区分短期候选 vs `GET /preferences` 已生效（6714f379）  
- [ ] 面板确认流（可选）与 Agent 个性化闭环联调  

有问题 @26a9835a（extract / metadata）或 @cb0aeabe（Milvus schema）。
