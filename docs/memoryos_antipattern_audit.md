# MemoryOS 弊端对照审计（MOKOA）

> 2026-08-01 | 偏好负责人视角 + 生命周期移植风险

## 对照矩阵

| MemoryOS 弊端 | MOKOA 偏好模块 | MOKOA 短/中/长期 (`layer_manager`) |
|---------------|----------------|-------------------------------------|
| ① 每次节点/元数据更新写整文件，IO 爆炸 | **曾有，已修**：`apply_value` 只标 dirty，`extract()` 结束 `flush()` 一次；原子 `os.replace` | **尚未实现**（内存 stub，无 JSON 风暴；勿照搬 MemoryOS `save()`） |
| ② 用户画像是自由文本 blob | **未延续**：`preference_key` 枚举 + `get_feature_profile()` 结构化特征 | N/A（长期分流后偏好走结构化） |
| ③ session 嵌入/摘要关键词创建创建时写死，page 并入不刷新 | **不涉及**（偏好无 session 合并）；`content` 用 `key=value` 规范串，值变则内容变 | **高风险**：stub TODO 写「参考 MemoryOS mid_term」——移植时必须强制 refresh summary/embedding |
| ④ 中→长热度堆只 peek 不 pop，淘汰仅靠 LFU | **不涉及** | **高风险**：`heat_threshold` 已配置但未用；实现时必须 pop/归档已晋升 segment |

## 偏好侧已做的工业优化

1. **批量落盘**：`PreferenceVersionIndex` dirty-set + `flush()`  
2. **原子写**：tmp + `os.replace` + `fsync`  
3. **结构化特征**：`features.build_user_feature_profile`（kv / confidence / rank_score）  
4. **频率信号**：`n_observations`，行为源 ≥3 升为 `repeated_behavior`  
5. **规范 embedding 文本**：`[preference] key=value | desc`，避免「描述不变、值已变」的陈旧向量  

## 给efca9634的移植红线（lifecycle）

实现 `promote_to_mid` / `promote_to_long` 时 **禁止** 原样拷贝 MemoryOS：

1. page 并入 session → **必须** 更新 `summary` / `summary_keywords` / `summary_embedding`（或维护 page 级向量 + 可刷新 centroid）  
2. 热度触发晋升后 → **必须** `heappop` / 归档 / TTL 降权，不能只 reset 计数留在堆顶  
3. 淘汰策略 → heat × recency × LFU 混合，禁止仅 LFU  
4. 短期容量 → `write_short` 必须执行 `short_capacity` 淘汰  
5. 落盘 → 脏页批量 flush，禁止每条 page 整文件 rewrite  

## 联调仍待完成（非本模块缺陷但相关）

- Orchestrator 尚未调用 `preference.extract()`  
- `resolve_and_store` 仍走内存 `save`，未接 `create_memory`  
- `recall` 未走 `get_feature_profile` / preference filter  
