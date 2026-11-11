# 3fba60e1 · Ingestion SQLite 中转库 — 完整建库说明

**From: efca9634（Orchestrator）→ To: 3fba60e1（ingestion）**  
**日期: 2026-08-03；需求落地复核: 2026-08-08**
**目的: 把「需建什么库、建哪些表、什么时候读写、怎么验收」一次说清楚**

---

## 0. 核心要点

1. **本模块建的是「ingestion 中转库」，不是最终记忆库。**
2. **最终记忆库只有 Milvus（cb0aeabe负责），本模块不写 Milvus，也不存 embedding。**
3. **中转库负责审计、持久化去重、默认脱敏归档、遗忘擦除、保留期清理和 benchmark 导出。**

洗完后仍然输出 `NormalizedRecord` 给 Orchestrator，**现有 `process()` 接口不变**。

---

## 1. 为什么要建这个库？（解决什么问题）

现在 `src/ingestion/cleaner.py` 里的去重是 **进程内 dict**：

- 服务重启 → 去重缓存清空，5 分钟内可能重复接收相同数据
- 无法查历史：不知道某条为什么被拒绝（low_quality / duplicate）
- da0f41a1 benchmark 无法统计：通过率、拒绝码分布、质量分分布
- 答辩无法演示：「这条数据什么时候进来的、为什么没进记忆」

**ingestion 中转库** 就是为了解决这些问题。

---

## 2. 本模块职责边界（避免走偏）

| 不在本模块内 | 原因 |
|--------|------|
| 存 embedding 向量 | cb0aeabe Milvus 负责 |
| 存 preference 版本链 | 26a9835a + Milvus metadata |
| 替代短期/中期记忆 | efca9634 `layer_manager` 内存负责 |
| 让前端直连本库 | 必须走 `api/server.py` |
| 引入 MySQL/PostgreSQL 等服务端数据库 | 当前单机智能体和比赛负载使用 SQLite 足够，保持部署简单 |

---

## 3. 文件放在哪

```
memory/
├── data/                              ← 运行时自动生成（gitignore）
│   └── ingestion_staging.db           ← SQLite 主库
├── src/ingestion/
│   ├── staging_store.py               ← 【已实现】SQLite 读写封装
│   ├── pipeline.py                    ← 【已接入】staging_store
│   └── cleaner.py                     ← 【保留】进程内去重 fallback
└── configs/default.yaml               ← 【已接线】staging 配置
```

**库文件路径（默认）：** `./data/ingestion_staging.db`  
**配置键：** `storage.staging_sqlite`

---

## 4. 建哪些表（完整 DDL，直接复制执行）

### 4.1 表 1：`ingestion_events`（主表，必做）

**作用：** 每走一次 `pipeline.process()`，无论成功/失败，都写一行。

```sql
CREATE TABLE IF NOT EXISTS ingestion_events (
    id                  TEXT PRIMARY KEY,          -- UUID，由模块生成
    user_id             TEXT NOT NULL,
    scene_id            TEXT,
    source              TEXT NOT NULL,             -- tool_result / user_behavior / manual_config / conversation
    stage               TEXT NOT NULL,             -- collect / clean / normalize / quality / deduplicate / completed
    accepted            INTEGER NOT NULL DEFAULT 0, -- 0=拒绝, 1=通过
    reject_code         TEXT,                      -- 见 src/ingestion/models.py RejectCode
    content             TEXT,                      -- NormalizedRecord.content；失败时可为空
    confidence          REAL,
    quality_score       REAL,
    quality_threshold   REAL,
    dedup_fingerprint   TEXT,                      -- SHA-256，正式字段
    record_key          TEXT,                      -- 稳定记录键，如 manual_config:u001:global:download_path
    source_event_id     TEXT,                      -- action_id / 来源事件 ID
    memory_id           TEXT,                      -- 短期/中期本地 memory ID，写入后不覆盖
    milvus_memory_id    TEXT,                      -- 成功持久化后补充的最终 Milvus ID
    metadata_json       TEXT NOT NULL DEFAULT '{}', -- JSON 字符串，其余 metadata
    quality_components_json TEXT NOT NULL DEFAULT '{}',
    reasons_json        TEXT NOT NULL DEFAULT '[]', -- 拒绝/评分原因，JSON 数组
    processing_ms       REAL NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL              -- ISO8601 UTC，如 2026-08-03T09:00:00+00:00
);

CREATE INDEX IF NOT EXISTS idx_events_user_created
    ON ingestion_events(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_fingerprint
    ON ingestion_events(dedup_fingerprint);

CREATE INDEX IF NOT EXISTS idx_events_record_key
    ON ingestion_events(user_id, record_key);

CREATE INDEX IF NOT EXISTS idx_events_accepted
    ON ingestion_events(accepted, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_source
    ON ingestion_events(source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_memory
    ON ingestion_events(user_id, memory_id);

CREATE INDEX IF NOT EXISTS idx_events_milvus_memory
    ON ingestion_events(user_id, milvus_memory_id);
```

### 4.2 表 2：`dedup_cache`（必做）

**作用：** 替代 `DataCleaner` 进程内 `_seen` dict，实现 **TTL 去重持久化**。

```sql
CREATE TABLE IF NOT EXISTS dedup_cache (
    dedup_fingerprint     TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,   -- ISO8601 UTC
    expires_at            TEXT NOT NULL    -- first_seen_at + dedup_ttl_seconds
);

CREATE INDEX IF NOT EXISTS idx_dedup_expires
    ON dedup_cache(expires_at);
```

**语义（必须和现有代码一致）：**

- **只有 `accepted=true` 且通过质量门控后**，才写入 `dedup_cache`
- `low_quality` 的数据 **不写入** dedup_cache（现有 pipeline 设计：低质量不占坑）
- TTL 默认 300 秒（5 分钟），读 `configs/default.yaml` → `storage.dedup_ttl_seconds`
- 查询前先删除 `expires_at < now()` 的过期行

### 4.3 表 3：`raw_payload_archive`（已实现，默认脱敏）

**作用：** 默认存脱敏后的 payload，方便复现「为什么洗成这个结果」。

归档模式由配置控制：`redacted` 默认递归脱敏，`off` 不写 raw，`plaintext` 仅供
开发环境且必须显式二次确认。事件 content 与扩展 metadata 也会无状态脱敏，
staging 不会为了归档再保存一份敏感原文索引。

```sql
CREATE TABLE IF NOT EXISTS raw_payload_archive (
    event_id      TEXT PRIMARY KEY,       -- 对应 ingestion_events.id
    source        TEXT NOT NULL,
    raw_json      TEXT NOT NULL,          -- 默认是脱敏后的 payload JSON
    envelope_json TEXT NOT NULL DEFAULT '{}' -- user_id / scene_id / archive_mode
);

CREATE INDEX IF NOT EXISTS idx_raw_event
    ON raw_payload_archive(event_id);
```

---

## 5. 字段从哪来（和现有代码一一对应）

每次 `IngestionPipeline.process()` 结束时，你有完整的 `IngestionResult`：

| SQLite 字段 | 来源 |
|-------------|------|
| `id` | 新建 UUID |
| `user_id` | `result.record.user_id` 或 process 参数 |
| `scene_id` | `result.record.scene_id` |
| `source` | `result.record.source.value` 或 collect 阶段 source |
| `stage` | `result.stage.value` |
| `accepted` | `result.accepted` |
| `reject_code` | `result.reject_code.value` if any |
| `content` | `result.record.content` 经无状态敏感规则脱敏后的副本 |
| `confidence` | `result.record.confidence` |
| `quality_score` | `result.quality_score` |
| `quality_threshold` | `result.record.metadata["quality_threshold"]` |
| `dedup_fingerprint` | `result.dedup_fingerprint` |
| `record_key` | `result.record.metadata["record_key"]` |
| `source_event_id` | `result.record.metadata["source_event_id"]` 或 action_id |
| `memory_id` | Orchestrator 写入短期记忆后绑定，保留本地短期/中期 ID |
| `milvus_memory_id` | 长期持久化成功后补充，不覆盖 `memory_id`、`record_key` 或 event ID |
| `metadata_json` | `result.record.metadata` 去独立列并脱敏后的 JSON |
| `quality_components_json` | `result.record.metadata["quality_components"]` |
| `reasons_json` | `result.reasons` |
| `processing_ms` | `result.processing_ms` |

**兼容字段：** `result.fingerprint` 与 `dedup_fingerprint` 相同，SQLite 只存 `dedup_fingerprint`。

---

## 6. 什么时候读写（流程图，按这个改 pipeline）

```text
process(raw, source, user_id, scene_id) 开始
    │
    ├─ collect 失败 → append_event(accepted=0, stage=collect, reject_code=...)
    │
    ├─ clean 失败   → append_event(accepted=0, stage=clean, ...)
    │
    ├─ normalize 失败 → append_event(accepted=0, stage=normalize, ...)
    │
    ├─ quality 失败 → append_event(accepted=0, stage=quality, reject_code=low_quality)
    │                 ※ 不写 dedup_cache
    │
    ├─ dedup 原子检查并登记：
    │     staging_store.register_if_new(dedup_fingerprint, user_id)?
    │         否 → append_event(accepted=0, stage=deduplicate, reject_code=duplicate)
    │         是 → append_event(accepted=1, stage=completed)
    │              返回 IngestionResult
    │
    └─ 无论哪条分支，都写 ingestion_events；raw 是否归档由模式决定
```

**重点：** 拒绝的数据也要进 `ingestion_events`，否则审计和 benchmark 做不起来。

---

## 7. 新建的 Python 类（接口定死）

**文件：** `src/ingestion/staging_store.py`

```python
class StagingStore:
    """Ingestion SQLite 中转库。Owner: 3fba60e1"""

    def __init__(
        self,
        db_path: str = "./data/ingestion_staging.db",
        dedup_ttl_seconds: float = 300.0,
        *,
        raw_archive_mode: str = "redacted",
        allow_plaintext_raw_archive: bool = False,
        raw_retention_days: int = 7,
        audit_retention_days: int = 90,
    ): ...

    def init_schema(self) -> None:
        """建表 + 建索引（幂等，启动时调一次）"""

    def append_event(
        self,
        result: IngestionResult,
        *,
        user_id: str,
        scene_id: str | None,
        source: str,
        raw: dict | None = None,
    ) -> str:
        """写入 ingestion_events（+ 可选 raw_payload_archive），返回 event_id"""

    def is_duplicate(self, dedup_fingerprint: str) -> bool:
        """查 dedup_cache；先清理过期行；存在且未过期返回 True"""

    def register_fingerprint(self, dedup_fingerprint: str, user_id: str) -> None:
        """INSERT OR REPLACE dedup_cache，expires_at = now + ttl"""

    def register_if_new(self, dedup_fingerprint: str, user_id: str) -> bool:
        """在同一 SQLite 写事务中完成检查和登记，首次登记返回 True"""

    def bind_memory_id(self, event_id: str, memory_id: str) -> bool: ...

    def bind_milvus_id(self, user_id: str, memory_id: str,
                       milvus_memory_id: str) -> int: ...

    def resolve_memory_ids(self, user_id: str, memory_id: str) -> dict[str, list[str]]: ...

    def forget_memory(self, user_id: str, memory_id: str) -> dict[str, int]:
        """用本地或 Milvus ID 定位，擦除内容、删除 raw/TTL 指纹，保留审计骨架"""

    def purge_expired(self, *, raw_retention_days: int | None = None,
                      audit_retention_days: int | None = None) -> dict:
        """手动执行 7d raw / 90d audit 清理"""

    def query_events(
        self,
        user_id: str,
        *,
        accepted: bool | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """审计/debug 用，给测试和 benchmark"""

    def export_jsonl(self, path: str, *, user_id: str | None = None, accepted: bool | None = None) -> int:
        """导出 JSONL，返回导出行数；给da0f41a1"""

    def stats(self, user_id: str | None = None) -> dict:
        """返回 {total, accepted, rejected, by_reject_code: {...}}"""
```

---

## 8. 需修改的现有文件

| 文件 | 改什么 |
|------|--------|
| `src/ingestion/pipeline.py` | 构造函数接收 `staging_store: StagingStore \| None`；通过 `_finalize_result()` 统一审计；dedup 调用 SQLite `register_if_new()` |
| `src/ingestion/cleaner.py` | 计算 fingerprint，并保留进程内 `register_if_new()` 作为 SQLite 不可用时的 fallback |
| `src/ingestion/__init__.py` | export `StagingStore` |
| `configs/default.yaml` | 使用下面配置节 |
| `tests/test_ingestion_staging.py` | SQLite 专项测试：持久化、过期、并发、查询/导出、工厂接线和连接关闭 |

**efca9634这边不会改你的 `staging_store.py`**，只要求 `process()` 返回的 `IngestionResult` 不变。

---

## 9. 配置（configs/default.yaml）

```yaml
storage:
  milvus_uri: http://localhost:19530
  collection_name: os_agent_memory
  # --- 以下由3fba60e1 ingestion staging 使用 ---
  staging_backend: sqlite        # sqlite | memory（memory=仅进程内，兼容旧行为）
  staging_sqlite: ./data/ingestion_staging.db
  staging_jsonl: ./data/staging.jsonl   # 推荐导出路径；当前不会自动 mirror
  staging_raw_archive_mode: redacted   # redacted | off | plaintext（仅 dev）
  staging_allow_plaintext_raw_archive: false
  staging_raw_retention_days: 7
  staging_audit_retention_days: 90
  dedup_ttl_seconds: 300
```

`src.core.factory.create_orchestrator()` 启动时读取配置并注入：

```python
if staging_backend == "sqlite":
    staging_store = StagingStore(config["staging_sqlite"], dedup_ttl_seconds=...)
else:
    staging_store = None  # 保持现有内存 dedup

ingestion = IngestionPipeline(
    staging_store=staging_store,
    dedup_ttl_seconds=...,
)
```

---

## 10. 验收标准（必须全部通过）

### 10.1 功能验收

| # | 场景 | 期望 |
|---|------|------|
| 1 | `manual_config` 合法写入 | `ingestion_events` 多 1 行，`accepted=1`，`stage=completed` |
| 2 | 同 fingerprint 5 分钟内再写 | 第 2 次 `accepted=0`，`reject_code=duplicate` |
| 3 | 低质量 `conversation "嗯"` | `accepted=0`，`reject_code=low_quality`，**dedup_cache 无此 fingerprint** |
| 4 | 服务重启后再写同 fingerprint | 若 TTL 未过期仍拒绝；过期后可再接收 |
| 5 | `download_path` D→E 改值 | 两条都 `accepted=1`，`record_key` 相同，`dedup_fingerprint` 不同 |
| 6 | `query_events(user_id)` | 能按时间倒序查到历史 |
| 7 | `export_jsonl` | 文件每行合法 JSON，字段与 `IngestionResult` 对齐 |
| 8 | raw 默认归档 | 手机号/邮箱/Token/敏感字段名不明文落盘 |
| 9 | `/forget` 命中 memory ID | content 被擦除、raw 行删除，质量审计仍保留 |
| 10 | `POST /staging/purge` | 默认删除 7 天前 raw、90 天前 audit |

### 10.2 性能验收

| 指标 | 目标 |
|------|------|
| `append_event` 单次 | ≤ 10ms（本地 SQLite WAL） |
| `is_duplicate` | ≤ 5ms |
| 不破坏现有 pytest | `tests/test_ingestion.py` 原用例全绿 |

### 10.3 边界约束

| 字段 | 上限 |
|------|------|
| `content` | 8000 字符（与 cleaner 一致） |
| `metadata_json` | 4KB |
| `raw_json` | 16KB |

---

## 11. 建议开发顺序（按天做）

```
Day 1  staging_store.py：init_schema + append_event + query_events
Day 2  dedup_cache：is_duplicate + register_fingerprint + TTL 清理
Day 3  pipeline.py 接入；原 test_ingestion 全过
Day 4  raw_payload_archive + export_jsonl + stats
Day 5  写 SQLite 专项测试 + README 更新
```

---

## 12. 测试覆盖（已落地到 tests/test_ingestion_staging.py）

```python
def test_staging_persists_accepted_event(tmp_path):
    # process 一次 manual_config → query_events 能查到 accepted=1

def test_staging_duplicate_survives_new_pipeline_instance(tmp_path):
    # 同 fingerprint 写两次；第二个 StagingStore 实例仍判 duplicate

def test_low_quality_does_not_register_dedup_cache(tmp_path):
    # "嗯" 被拒绝；dedup_cache 表为空

def test_export_jsonl_matches_ingestion_result(tmp_path):
    # export 行数 >= 1；json.loads 不报错
```

当前测试文件还覆盖 TTL 过期、多连接并发原子登记、超限 JSON 合法截断、工厂配置
注入，以及 `StagingStore.close()`/上下文退出释放连接。

---

## 13. 与队友的接口关系（明确协作边界）

```text
工具/前端/API
    ↓
POST /ingest  →  Orchestrator.process_input()
    ↓
IngestionPipeline.process()     ← 在此写入 SQLite
    ↓
NormalizedRecord
    ↓
ShortTermMemory（efca9634，内存）
    ↓
consolidate → create_memory（cb0aeabe，Milvus）
```

**其他模块协作说明：**

- **26a9835a** 将来如需批量读已接受记录：`staging_store.query_events(user_id, accepted=True)`  
- **da0f41a1** benchmark 所需数据：`staging_store.stats()` + `export_jsonl()`  
- **cb0aeabe** 不直接读取本模块的数据库  

---

## 14. 常见问题

**Q：和 Milvus 有什么区别？**  
A：本模块存储「洗之前/洗之后的事件审计」；Milvus 存储「值得长期记住且已 embedding 的记忆」。

**Q：accepted=1 是不是就等于进了 Milvus？**  
A：不是。accepted=1 只表示通过 ingestion；进 Milvus 还要走 consolidate / store-entry。

**Q：JSONL 还要吗？**  
A：SQLite 为主；需要样例或 benchmark 时调用 `export_jsonl()` 按需导出。当前不会在每次
ingestion 时自动 mirror，避免 SQLite 与 JSONL 两条写路径不一致。

**Q：表结构能改吗？**  
A：上面列是 v1 定稿；要加列先与efca9634对齐，Orchestrator 不依赖本模块表结构，但 benchmark 会依赖。

---

## 15. 交付清单（PR 中应包含）

- [x] `src/ingestion/staging_store.py`
- [x] `pipeline.py` 接入 + 配置读取
- [x] `configs/default.yaml` staging 配置
- [x] `tests/test_ingestion_staging.py`
- [x] `src/ingestion/README.md` 增加「§ SQLite 中转库」一节
- [x] `.gitignore` 忽略 `data/` 和 `*.db`

### 当前待完善

- [x] raw 默认脱敏，并支持关闭归档或 dev-only plaintext
- [x] `/forget` 联动 staging 内容擦除、raw 删除和去重释放
- [x] 采用 7 天 raw / 90 天审计，提供手动 purge API
- [ ] 自动定时执行 purge
- [ ] 处理去重登记与下游短期记忆写入失败的一致性
- [ ] 完成 `append_event`、`is_duplicate/register_if_new` 性能验收

---

有问题 @efca9634（Orchestrator / 联调）或 @da0f41a1（benchmark 导出格式）。
