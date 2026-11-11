# 麒忆智脑 · 多源摄取（Ingestion）整体技术方案

> 负责人：3fba60e1  
> 模块路径：`src/ingestion/`  
> 规格版本：1.3（准上线）  
> 对应比赛要求：**(1) 多源接入 + 清洗 + 标准化 + 质量校验**

---

## 1. 目标与边界

本模块是「麒忆智脑」的可信数据入口。它把 OS Agent 的多源原始事件变成统一、可解释、可审计的 `NormalizedRecord`，再交给efca9634的 Orchestrator。

模块负责：

1. 接收工具结果、用户行为、手动配置、对话四类来源。
2. 校验来源与用户/场景信封。
3. 做不改变核心语义的字符清洗与截断。
4. 按来源构造稳定正文和白名单 metadata。
5. 用确定性规则评估是否值得进入记忆生命周期。
6. 在 TTL 窗口内抑制同一来源事件重放。
7. 把成功与拒绝都写入 SQLite staging，供审计、去重、遗忘和验收。

模块不负责：

| 职责 | 归属 |
|------|------|
| 偏好 / 知识分类 | `src/core/classifier.py` |
| 最终脱敏与 NL 遗忘执行 | `src/security/` |
| Embedding | `src/embedding/` |
| Milvus 入库与 RAG | `src/knowledge/memory_service/` |
| 短 / 中 / 长期晋升 | `src/core/layer_manager.py` |

Staging **不是**最终记忆库，也不存 embedding。最终长期记忆只有cb0aeabe的 Milvus。

---

## 2. 在系统中的位置

```
多源数据 ──→ ingestion（3fba60e1）──→ Orchestrator（efca9634）
                                      ├─ 敏感过滤
                                      ├─ 分类 / 短期写入
                                      └─ 短→中→长期 / 冲突 / 偏好入库
```

正式调用链：

```text
API POST /ingest
  → MemoryOrchestrator.process_input
    → IngestionPipeline.process
    → SecurityAndForgetService.filter_sensitive
    → LayerManager.write_short
    → StagingStore.bind_memory_id
```

`process()` 的 `accepted=True` 只表示通过清洗、标准化、质量门控和去重，**不表示已经写入短期记忆或 Milvus**。

---

## 3. 处理流程

```text
来源原始数据
    │
    ▼
DataCollector          来源校验、user/scene 信封、UTC 时间
    │
    ▼
DataCleaner            字符清洗、截断、SHA-256 指纹
    │
    ▼
DataNormalizer         来源专属正文、白名单 metadata、record_key
    │
    ▼
行为频次累计            同指纹历史次数抬升 occurrence_count
隐私风险标记            detect_privacy_risk → metadata.privacy_risk
    │
    ▼
QualityEvaluator       六维加权 − 惩罚，阈值默认 0.6
    │
    ▼
TTL 去重               仅登记已通过质量门控的指纹
    │
    ▼
SQLite Staging         成功/拒绝都审计；raw 默认脱敏
    │
    ▼
IngestionResult        accepted / reject_code / stage_ms / 评分明细
```

去重登记位于质量门控之后：低质量的偶然行为不占用指纹，后续同内容、更高频的数据仍可进入。

---

## 4. 四段式设计

### 4.1 采集 `collector.py`

只封装，不改业务语义。支持：

| source | 含义 |
|--------|------|
| `tool_result` | 工具执行结果 |
| `user_behavior` | 用户行为 |
| `manual_config` | 用户明确配置 |
| `conversation` | 对话 |
| `document` | 用户上传的 PDF / Word（.docx）/ 图片 |

`document` 由 `file_extract.py` 先抽文本，再走同一条四段式管线。图片：当前 `llm.model` 具备视觉能力则把原图交给模型描述；否则 OCR，再否则必须带说明。入口：`process_file()` 与 `POST /ingest/file`。

`user_id` 是生产隔离键。API 必须显式传入。完全缺失时兼容为 `default`，并写 `identity_defaulted=true`，仅供本地 Demo。空字符串直接拒绝（`invalid_user_id`）。

### 4.2 清洗 `cleaner.py`

机械处理：NFKC、去 ANSI/控制符、统一换行、压缩空白、去掉纯 DEBUG/TRACE 噪声行、单字段默认最长 8000 字。截断前长度写入 `truncated_fields`。

指纹字段：

```text
user_id + scene_id + source + 来源事件 ID + 语义正文相关字段
SHA-256(canonical JSON)
```

`occurrence_count` 不参与指纹，因此同一行为提高频次不会变成另一条事件。不同 `action_id` 即使错误文本相同，也视为两次独立执行。

### 4.3 标准化 `normalizer.py`

显式 `content` 优先保持原文；否则按来源拼接稳定中文正文。只传白名单 metadata。

`record_key` 是与正文改值无关的来源层稳定键，不是 `memory_id`，也不是去重指纹。例如配置项从 D 盘改到 E 盘：

- `dedup_fingerprint` 变
- `record_key` 不变，如 `manual_config:u001:global:download_path`

是否用 `record_key` 做版本合并，由 Orchestrator 决定。

### 4.4 质量 `quality.py`

确定性加权，保证测试、演示、正式环境分数一致：

```text
score =
  0.25 × 来源可靠度
+ 0.20 × 用户明确性
+ 0.20 × 重复出现程度
+ 0.15 × 正文信息量
+ 0.10 × 后续复用价值
+ 0.10 × 输入置信度
− 临时信息 0.35
− 隐私风险 0.40
− 低信息短语 0.20
```

默认阈值 `0.6`。权重可在 `configs/default.yaml` 的 `memory.quality_weights` 覆盖，无需改代码。

设计原则：

- 手动配置单次即可通过。
- 普通对话默认不保存，明确意图才容易过。
- 工具失败经验复用价值高。
- 单次用户行为按误触处理，需累计出现。

---

## 5. 准上线增强

### 5.1 行为频次服务端累计

规格要求：单次 `user_behavior` 拒绝，重复出现后准入。若 Agent 每次都传 `occurrence_count=1`，旧实现会永远拒绝。

现行规则：

```text
effective = max(调用方频次, 同指纹历史条数 + 1)
```

有 staging 时读 `ingestion_events`；无 staging 时用进程内计数。用户隔离。遗忘擦除指纹后重新累计。调用方已提供更大频次时仍以调用方为准。

### 5.2 下游失败释放去重

`process()` 在质量通过后立即登记指纹，以防止并发双写。若随后 `filter_sensitive` 或 `write_short` 失败，Orchestrator 调用 `IngestionPipeline.release_dedup()`，同一事件可在 TTL 内重试。审计行保留，便于排查。

### 5.3 隐私风险协议

ingestion **不替代**最终脱敏。评分前调用 `src.security.service.detect_privacy_risk()`，命中高风险规则时打 `privacy_risk=true` 并扣 0.40。

计入风险：手机号、邮箱、身份证、银行卡、私钥、JWT、云厂商密钥等。

不计入风险：IPv4、UUID、MAC、工号等运维上下文，避免「连接 192.168.1.1 超时」这类工具经验被误拒。

最终 `filter_sensitive` 仍在 Orchestrator 中执行，可脱敏更广的规则集并维护敏感索引。

### 5.4 自动 purge

| 项 | 默认 |
|----|------|
| raw 保留 | 7 天 |
| 审计保留 | 90 天 |
| 自动间隔 | 3600 秒（`staging_auto_purge_interval_seconds`） |
| 手动接口 | `POST /staging/purge` |

`0` 关闭后台线程，测试和脚本默认关闭。`StagingStore.close()` 会停止线程。

### 5.5 分阶段耗时与监控

`IngestionResult.stage_ms` / `ProcessInputResult.stage_ms` 记录：

`collect` → `clean` → `normalize` → `quality` → `deduplicate` → `staging`

`GET /staging/stats` 返回最近 1000 条的拒绝码分布和 avg / p50 / p95 / max。可带 `user_id`。

---

## 6. SQLite 中转库

三张表：

| 表 | 用途 |
|----|------|
| `ingestion_events` | 每次 `process()` 的成功或拒绝审计 |
| `dedup_cache` | 已通过质量门控的 TTL 指纹 |
| `raw_payload_archive` | 默认脱敏 raw；可 `off`；`plaintext` 必须 dev 双确认 |

关键约束：

- 单连接 + `RLock`，`WAL` + `busy_timeout=5s`。
- raw 上限 16 KiB，metadata 上限 4 KiB，超限仍保持合法 JSON。
- `identity_defaulted` 等自有字段按 token 白名单保留，不会被 `identity` 子串误伤。
- 事件先绑本地 `memory_id`，Milvus 成功后再补 `milvus_memory_id`，三者不互相覆盖。
- `/forget` 擦除 content / metadata / raw / TTL 指纹，保留阶段、状态、质量分、耗时等骨架。

SQLite 故障对主业务 fail-open：去重回退内存，审计失败不改 `accepted`。

---

## 7. 拒绝码

拒绝是正常业务结果，不是 HTTP 500。

| 码 | 阶段 | 含义 |
|----|------|------|
| `invalid_source` | collect | 来源不在四类枚举中 |
| `invalid_payload` | collect | 载荷或批量信封非法 |
| `invalid_user_id` | collect | 用户标识为空字符串 |
| `empty_content` | clean | 清洗后没有正文 |
| `normalization_failed` | normalize | 无法形成标准正文 |
| `low_quality` | quality | 质量分低于阈值 |
| `duplicate` | deduplicate | TTL 窗口内已接收 |

---

## 8. 对外接口

### 8.1 正式 Python API

```python
from src.ingestion import IngestionPipeline, StagingStore

result = pipeline.process(
    raw, source, user_id="u001", scene_id="coding-session"
)
# result.accepted / reject_code / reasons / quality_score
# result.stage_ms / processing_ms / staging_event_id
```

兼容旧接口：`ingest` / `validate` / `run_pipeline`。新代码只用 `process()`。

### 8.2 HTTP

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/ingest` | 完整接入；返回 `processing_ms` / `stage_ms` |
| GET | `/staging/stats` | 拒绝分布与耗时分位数 |
| POST | `/staging/purge` | 手动保留期清理 |

生产必须由 API 注入真实 `user_id`。

---

## 9. 配置

`configs/default.yaml`：

```yaml
memory:
  quality_threshold: 0.6
  quality_weights:
    source_reliability: 0.25
    explicitness: 0.20
    recurrence: 0.20
    informativeness: 0.15
    reuse_value: 0.10
    input_confidence: 0.10

storage:
  staging_backend: sqlite
  staging_sqlite: ./data/ingestion_staging.db
  staging_raw_archive_mode: redacted
  staging_raw_retention_days: 7
  staging_audit_retention_days: 90
  staging_auto_purge_interval_seconds: 3600
  dedup_ttl_seconds: 300
```

相对路径以项目根目录解析，不依赖启动时的 cwd。

---

## 10. 验收与核验

| 项 | 命令 / 位置 | 结果 |
|----|-------------|------|
| 管线单测 | `pytest tests/test_ingestion.py` | 通过 |
| Staging 单测 | `pytest tests/test_ingestion_staging.py` | 通过 |
| 编排/API 回归 | `pytest tests/test_core.py tests/test_api.py` | 通过 |
| 验收数据集 | `benchmarks/datasets/ingestion_acceptance.jsonl` | 14 条期望与管线一致 |
| 吞吐脚本 | `python scripts/ingestion_acceptance.py --repeat 80` | 开发机 p95 < 1ms 量级 |

真机（麒麟）仍需跑同一脚本，确认批量吞吐和 SQLite 文件增长。这是当前唯一未在本机完成的准上线项。

---

## 11. 准上线结论

ingestion 已达到**准上线**：主链、审计、脱敏、遗忘、自动清理、失败可重试、隐私协议和监控均已闭合，并有可重复的数据集与验收脚本。

上线前仍建议：

1. 在银河麒麟真机跑 `scripts/ingestion_acceptance.py --repeat 500`，记录 p95 与数据库体积。
2. 用真实 Agent 日志替换/扩充 JSONL 后，再微调 `quality_weights`。
3. 多 worker 部署时确认只有一份 SQLite 文件，并观察 `database is locked` 是否被 5s busy timeout 消化。

整系统上线还依赖：真实 Milvus、麒麟 Embedding SDK 真机验收。这两项不属于本模块。
