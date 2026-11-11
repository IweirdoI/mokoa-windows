# 多源数据摄取模块（Ingestion）

- 负责人：3fba60e1
- 模块路径：`src/ingestion/`
- 规格版本：1.2（SQLite staging）

最后更新：2026-08-08

## 1. 模块目标

本模块是“麒忆智脑”的可信数据入口，负责将 OS Agent 的多源原始数据转换为统一、
可解释、可测试的 `NormalizedRecord`。

模块完成以下工作：

1. 接收工具执行结果、用户行为、手动配置和对话数据。
2. 校验来源与基础信封字段。
3. 去除无意义字符、控制码和纯日志噪声。
4. 限制超长正文，防止日志无限膨胀。
5. 将来源数据标准化为团队统一 Schema。
6. 使用多维规则评价长期记忆价值。
7. 在时间窗口内抑制重复数据。
8. 返回稳定拒绝码、评分明细和中文原因。

本模块不负责：

- 偏好或知识分类：由 `src/core/classifier.py` 负责。
- 敏感信息最终脱敏：由 `src/security/` 负责。
- Embedding：由 `src/embedding/` 负责。
- Milvus 入库和 RAG 检索：由 `src/knowledge/memory_service/` 负责。
- 短、中、长期记忆晋升：由 `src/core/layer_manager.py` 负责。

## 2. 处理流程

```text
来源原始数据
    │
    ▼
DataCollector
    │  来源校验、用户/场景信封、UTC 时间
    ▼
DataCleaner
    │  字符清洗、日志截断、指纹生成
    ▼
DataNormalizer
    │  来源专属正文、统一 Schema、标准 metadata
    ▼
QualityEvaluator
    │  多维评分、拒绝原因
    ▼
TTL Deduplication
    │  只登记已经通过质量门控的数据
    ▼
SQLite Staging Audit（正式 API；直接构造 pipeline 时可关闭）
    │  成功与所有拒绝分支均记录
    ▼
NormalizedRecord / IngestionResult
```

去重登记位于质量门控之后。这样低质量的单次偶然行为不会占用指纹，后续相同正文
在出现次数增加后仍可被接收。

## 3. 文件说明

| 文件 | 职责 |
|---|---|
| `collector.py` | 四类来源采集、信封解析、UTC 时间 |
| `cleaner.py` | 确定性清洗、长度限制、SHA-256 指纹、进程内 TTL fallback |
| `normalizer.py` | 来源专属正文构造、置信度修正、metadata 白名单 |
| `quality.py` | 可解释的多维质量评分 |
| `models.py` | 管线阶段、拒绝码、单条/批量结果模型 |
| `pipeline.py` | 串联全流程、SQLite 去重/审计接线、正式及兼容接口 |
| `file_extract.py` | PDF / Word / 图片抽文本；图片可走视觉模型 |
| `vision.py` | 检测 LLM 视觉能力，并把图片交给模型描述 |
| `staging_store.py` | SQLite 事件审计、持久化 TTL 去重、查询/统计/JSONL 导出 |
| `__init__.py` | 模块公开导出 |

## 4. 支持的数据来源

### 4.0 文件上传 `document`

用户上传 PDF、Word（`.docx`）或常见图片。入口是 `IngestionPipeline.process_file()` 与 `POST /ingest/file`。图片优先走当前 LLM 的视觉描述（`ingestion.vision=auto` 按模型名判断）；没有视觉能力或调用失败时回退本地 OCR，再没有则必须带说明文字，否则拒绝。

### 4.1 工具执行结果 `tool_result`

推荐输入：

```json
{
  "action_id": "action-001",
  "tool_name": "pip",
  "status": "failed",
  "stderr": "连接超时",
  "duration_ms": 1250,
  "confidence": 0.8
}
```

工具正文读取优先级：

1. `content`
2. `error`
3. `stderr`
4. `result`
5. `output`
6. `stdout`
7. `message`

如果调用方已经提供 `content`，模块保持原文；否则生成：

```text
pip 执行失败：连接超时
```

`action_id` 是工具执行模块生成的唯一事件 ID。相同 `action_id` 和相同内容再次
投递时视为重放；不同 `action_id` 即使错误文本相同，也视为两次独立执行。

### 4.2 用户行为 `user_behavior`

推荐输入：

```json
{
  "action": "打开",
  "target": "VSCode",
  "occurrence_count": 5,
  "confidence": 0.7
}
```

用户行为必须提供 `content`，或至少提供 `action/event`。单次行为通常被质量门控
拒绝；重复行为达到足够质量后才进入记忆系统。

### 4.3 手动配置 `manual_config`

推荐输入：

```json
{
  "key": "reply_language",
  "value": "中文",
  "scope": "global",
  "confidence": 0.9
}
```

标准正文：

```text
用户设置“reply_language”为“中文”
```

手动配置代表用户明确意图，因此明确性和来源可靠度较高。

### 4.4 对话 `conversation`

推荐输入：

```json
{
  "content": "以后请用中文回答",
  "role": "user",
  "turn_id": "turn-12",
  "confidence": 0.8
}
```

模块会识别“以后、请、不要、必须、默认、偏好”等明确表达，但只将其作为质量
特征，不在 ingestion 中判断它属于偏好还是知识。

## 5. 正式接口

### 5.1 单条处理

```python
from src.ingestion import IngestionPipeline

pipeline = IngestionPipeline(
    quality_threshold=0.46,          # 全局兜底阈值
    quality_type_thresholds={        # 按类型分阈值；不传则用内置默认
        "conversation": 0.44,
        "document": 0.51,
        "tool_result": 0.54,
        "behavior": 0.41,
    },
    max_text_length=8000,
    dedup_ttl_seconds=300,
)

result = pipeline.process(
    {"content": "以后请用中文回答", "confidence": 0.9},
    "conversation",
    user_id="u001",
    scene_id="coding-session",
    metadata={"trace_id": "trace-001"},
)

if result.accepted:
    normalized_record = result.record
else:
    print(result.reject_code, result.reasons)
```

`user_id` 和 `scene_id` 推荐放在接口信封层，不要混入工具日志正文。为兼容现有
`IngestionAdapter`，如果没有显式参数，模块也会从原始字典读取。

当完全缺少 `user_id` 时，当前版本使用 `default` 并写入：

```json
{"identity_defaulted": true}
```

此行为只用于兼容本地 Demo。生产 API 必须注入真实用户标识，否则无法实现可靠
的数据隔离和精准遗忘。

### 5.2 批量处理

```python
batch = pipeline.process_batch(
    [
        {
            "raw": {"content": "以后请用中文回答"},
            "source": "conversation",
            "user_id": "u001"
        },
        {
            "raw": {"action": "打开", "target": "VSCode", "occurrence_count": 5},
            "source": "user_behavior",
            "user_id": "u001"
        }
    ]
)

print(batch.total)
print(batch.accepted_count)
print(batch.rejected_count)
```

批量接口按输入顺序处理，保证同一批中的重复数据具有确定性结果：第一条合格数据
通过，后续重复项被拒绝。

### 5.3 兼容接口

为了不破坏现有 Orchestrator，保留以下接口：

```python
pipeline.ingest(raw, source)          # 只采集，返回 RawRecord
pipeline.validate(raw_record)         # 清洗 + 标准化 + 质量检查，不登记去重
pipeline.run_pipeline(raw, source)    # 成功返回 NormalizedRecord，拒绝返回 None
```

新开发代码建议使用 `process()`，因为它能返回拒绝码和评分详情。

## 6. 标准输出

成功的 `IngestionResult` 示例：

```json
{
  "accepted": true,
  "stage": "completed",
  "record": {
    "type": "tool_result",
    "content": "pip 执行失败：连接超时",
    "time": "2026-07-29T09:20:00Z",
    "confidence": 0.8,
    "source": "tool_result",
    "user_id": "u001",
    "scene_id": "coding-session",
    "metadata": {
      "schema_version": "1.0",
      "dedup_fingerprint": "SHA-256",
      "fingerprint": "SHA-256",
      "source_event_id": "action-001",
      "action_id": "action-001",
      "record_key": "tool_result:u001:action-001",
      "tool_name": "pip",
      "status": "failed",
      "duration_ms": 1250,
      "quality_score": 0.635,
      "quality_threshold": 0.6,
      "quality_components": {},
      "quality_reasons": []
    }
  },
  "quality_score": 0.635,
  "dedup_fingerprint": "SHA-256",
  "fingerprint": "SHA-256",
  "processing_ms": 0.4
}
```

注意：示例分数用于说明字段，实际值以当前评分规则计算结果为准。

## 7. 质量评分规格

质量分采用确定性加权：

```text
score =
  0.25 × 来源可靠度
+ 0.20 × 用户明确性
+ 0.20 × 重复出现程度
+ 0.15 × 正文信息量
+ 0.10 × 后续复用价值
+ 0.10 × 输入置信度
- 临时信息惩罚
- 隐私风险惩罚
- 低信息量短语惩罚
```

默认准入阈值为 `0.46`（取真实语料底分 0.505 与噪声顶分 0.415 的均值，最大化分类间隔）。

### 来源可靠度

| 类型 | 分值 |
|---|---:|
| 手动配置 | 0.95 |
| 对话 | 0.80 |
| 工具结果 | 0.70 |
| 用户行为 | 0.45 |

### 设计原则

- 手动配置是用户明确声明，单次即可具有较高价值。
- 普通对话不会全部保存，只有明确要求或其他高价值内容才容易通过。
- 工具失败经验通常具有较高复用价值。
- 单次用户行为容易是误触，需通过累计出现次数提高分数。
- `temporary=true` 会扣 0.35。
- `privacy_risk=true` 会扣 0.40，但最终安全处理仍由 security 模块负责。

每次评分都会返回：

- `quality_score`
- `quality_components`
- `quality_reasons`
- `quality_threshold`

因此答辩时可以展示“为什么保存”或“为什么拒绝”，而不是只展示黑盒结果。

## 8. 清洗与截断规格

Cleaner 执行：

1. Unicode NFKC 标准化。
2. 删除终端 ANSI 颜色控制码。
3. 删除 NUL 等不可见控制字符。
4. 统一 Windows/Linux 换行。
5. 合并行内多余空格。
6. 删除只有时间戳和 DEBUG/TRACE 标记的噪声行。
7. 限制正文相关字段长度。

截断发生时，标准 metadata 会记录：

```json
{
  "truncated_fields": {
    "stderr": 18342
  }
}
```

数值表示截断前字符数，便于审计和调整配置。

## 9. 事件身份、记录键与去重规格

本模块明确区分三种身份：

| 字段 | 含义 | 维护者 |
|---|---|---|
| `source_event_id` | 来源系统的事件 ID，如 action_id | 工具/行为/对话来源 |
| `dedup_fingerprint` | 同一来源事件的精确去重哈希 | ingestion |
| `record_key` | 与正文改值无关的来源层稳定键 | ingestion 提示，core 决定是否采用 |

`record_key` 不是最终 `memory_id`，也不负责偏好/知识分类。例如同一个配置项从
D 盘改成 E 盘时：

```text
dedup_fingerprint：发生变化，因为正文不同
record_key：保持不变，例如 manual_config:u001:global:download_path
```

这样 core 后续可以用 `record_key` 识别版本更新，而不会把正文哈希错误地当成
逻辑记忆主键。

### 9.1 去重指纹

指纹计算字段：

```text
user_id + scene_id + source + 来源事件 ID + 语义正文相关字段
```

计算方式：

```text
SHA-256(canonical JSON)
```

特性：

- 不同用户互相隔离。
- 不同场景互相隔离。
- 字典键顺序不影响指纹。
- 缓存只保存哈希，不保存正文。
- 使用线程锁，适用于单进程 FastAPI 并发。
- 只登记已通过质量门控的数据。
- 同一 action_id 的相同事件重投会被拒绝。
- 不同 action_id 的相同错误文本会被分别接收。

### 9.2 兼容字段

当前 core/API 已经读取 `fingerprint`，因此 1.1 版本同时输出：

```text
dedup_fingerprint == fingerprint
```

其中 `dedup_fingerprint` 是正式字段，`fingerprint` 是迁移期兼容别名。本模块不会
自行删除兼容字段，待团队完成公共契约迁移后统一处理。

直接构造 `IngestionPipeline()` 时使用进程内缓存，适合单元测试和临时脚本。
正式 API 由 `src/core/factory.py` 读取 `configs/default.yaml`，注入 SQLite
`StagingStore`；此时 TTL 指纹会跨 pipeline 实例和服务重启保留。

## 10. 拒绝码

| 拒绝码 | 阶段 | 含义 |
|---|---|---|
| `invalid_source` | collect | 来源不在 DataSource 枚举中 |
| `invalid_payload` | collect | payload 或批量信封结构不合法 |
| `invalid_user_id` | collect | 用户标识为空字符串 |
| `empty_content` | clean | 清洗后没有正文 |
| `normalization_failed` | normalize | 来源字段无法形成标准正文 |
| `low_quality` | quality | 质量分低于阈值 |
| `duplicate` | deduplicate | TTL 窗口内已接收相同数据 |

拒绝属于正常业务结果，不应记录成服务器 500 错误。

## 11. 与 Orchestrator 联调

当前 Orchestrator 已使用正式接口：

```python
result = ingestion.process(
    raw,
    source,
    user_id=user_id,
    scene_id=scene_id,
)
```

Orchestrator 会读取 `accepted`、`reject_code`、`quality_score` 和标准化记录，随后
执行敏感过滤、分类并写入短期记忆。`run_pipeline()` 仅作为旧调用方兼容入口保留。

特别注意：现有 API 请求把 `user_id` 放在 `IngestRequest` 外层，而不是 payload
内部。API 接线时必须显式传给 `process()`，否则会使用兼容身份 `default`。

## 12. SQLite 中转库

SQLite 中转库只负责 ingestion 审计和去重，不是最终记忆库，也不存 embedding。
最终长期记忆仍由 `src/knowledge/memory_service/` 写入 Milvus。

默认配置：

```yaml
storage:
  staging_backend: sqlite
  staging_sqlite: ./data/ingestion_staging.db
  staging_raw_archive_mode: redacted
  staging_allow_plaintext_raw_archive: false
  staging_raw_retention_days: 7
  staging_audit_retention_days: 90
  dedup_ttl_seconds: 300
```

正式启动时，`create_orchestrator()` 会以项目根目录解析相对路径、自动创建父目录
并幂等建表。需要恢复旧的纯内存行为时，将 `staging_backend` 改为 `memory`。

中转库包含三张表：

| 表 | 用途 |
|---|---|
| `ingestion_events` | 审计每次 `process()` 的成功或拒绝结果 |
| `dedup_cache` | 保存已通过质量门控的 TTL 指纹 |
| `raw_payload_archive` | 默认保存有大小上限的脱敏 payload；可关闭，plaintext 仅允许 dev 显式双确认 |

查询和导出示例：

```python
from src.ingestion import StagingStore

store = StagingStore("./data/ingestion_staging.db")
accepted = store.query_events("u001", accepted=True, limit=50)
summary = store.stats("u001")
count = store.export_jsonl("./data/u001-ingestion.jsonl", user_id="u001")
```

`query_events()` 返回已经解析的 `metadata`、`quality_components` 和 `reasons`，
不要求调用方再次解析数据库内部的 JSON 字符串。`export_jsonl()` 按事件时间正序
输出，适合 benchmark 重放；配置中的 `staging_jsonl` 仅作为推荐导出路径，不会
在每次 ingestion 时同步镜像，避免 SQLite 和 JSONL 两条写入路径产生不一致。

并发去重由单个 SQLite 写事务完成。若 SQLite 因文件锁或磁盘故障暂时不可用，
pipeline 会记录异常并回退到进程内去重，不改变原有 `process()` 返回契约。

staging 在 Orchestrator 执行 `filter_sensitive()` 之前写入，但事件 `content`、扩展
metadata 和 raw payload 会先经过无状态规则脱敏，不额外保存敏感值映射。raw 模式：

- `redacted`：默认，递归脱敏后归档。
- `off`：保留事件审计，不写 raw 行。
- `plaintext`：仅供开发环境；必须同时设置
  `staging_allow_plaintext_raw_archive: true`，避免误开。

Orchestrator 会先把 staging event 绑定到本地短期/中期 `memory_id`；成功写入 Milvus
后，再额外填写 `milvus_memory_id`，不会覆盖 event ID、`record_key` 或本地 ID。
`/forget` 可使用本地 ID 或最终 Milvus ID 定位同一事件，擦除对应 content、metadata、
来源标识，删除 raw 行和 TTL 指纹，同时保留 stage、accepted、时间、质量分与处理
耗时等审计骨架。Milvus 写入成功但 staging 绑定失败时只记录异常，不阻塞长期队列释放。

手动保留期清理接口为 `POST /staging/purge`，默认执行 **7 天 raw / 90 天审计**；
自动定时清理尚未接入。

```bash
# 使用配置中的 7d / 90d
curl -X POST http://localhost:8000/staging/purge \
  -H 'Content-Type: application/json' \
  -d '{}'

# 临时覆盖保留期
curl -X POST http://localhost:8000/staging/purge \
  -H 'Content-Type: application/json' \
  -d '{"raw_retention_days":14,"audit_retention_days":120}'
```

## 13. 测试

安装依赖后运行：

```bash
python -m pytest tests/test_ingestion.py -v
python -m pytest tests/test_ingestion_staging.py -v
```

完整回归：

```bash
python -m pytest tests/ -v
```

当前测试覆盖：

- 手动配置标准化
- 结构化工具失败日志
- 明确对话与 ANSI 控制码清理
- 单次/重复用户行为质量差异
- 空正文和未知来源拒绝码
- 用户与场景隔离去重
- 结构化工具上下文参与去重指纹
- action_id 重投与不同工具执行的区分
- 配置值变化时 record_key 保持稳定
- dedup_fingerprint 与旧 fingerprint 兼容
- 低质量数据不占用指纹
- NaN 等非法置信度安全回退
- 超长正文截断审计
- 批量处理汇总
- 兼容 `validate()` 不污染去重状态
- SQLite 跨实例持久化去重和 TTL 过期
- 所有成功/拒绝分支的事件审计
- 查询、统计及 JSONL 导出
- metadata/raw JSON 限长后仍可解析
- 多连接并发登记的原子性
- raw 默认脱敏、关闭归档及 dev plaintext 双确认
- event → memory ID 绑定与 `/forget` 精确擦除
- 7 天 raw / 90 天审计的手动 purge
- 三类数据源 Demo 样例重放

## 14. 生产化检查清单

- [x] API 显式传入真实 `user_id`
- [x] `quality_threshold` 从 `configs/default.yaml` 注入
- [x] `quality_type_thresholds`（按类型分阈值）从 `configs/default.yaml` 注入，
      缺省回落到 `QualityEvaluator._DEFAULT_TYPE_THRESHOLDS`；各类型推荐值与安全区间
      见该配置文件中的详细说明
- [x] 根据真实数据校准评分权重（权重已配置化；验收集可对照分布，真机样本到位后再微调）
- [x] 使用 SQLite 共享 TTL 去重后端
- [x] raw 默认脱敏归档，并支持 `off` / dev-only `plaintext`
- [x] `/forget` 联动 staging 内容擦除、raw 删除和去重释放
- [x] 采用 7 天 raw / 90 天审计，并提供手动 purge API
- [x] 接入自动定时 purge（`staging_auto_purge_interval_seconds`，默认 1h）
- [x] 处理去重登记成功但下游短期记忆写入失败的一致性（`release_dedup`）
- [x] 对拒绝码和各阶段耗时建立监控（`GET /staging/stats` + `stage_ms`）
- [x] 与 security 模块确认 `privacy_risk` 标记协议（`detect_privacy_risk`）
- [x] 建立真实工具日志和用户行为 JSONL 数据集（`ingestion_acceptance.jsonl`）
- [ ] 在麒麟真机执行批量吞吐与内存稳定性测试（脚本：`scripts/ingestion_acceptance.py`）
