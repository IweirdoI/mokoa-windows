> **个人工程化版本。** 发布者赵琳（GitHub [@IweirdoI](https://github.com/IweirdoI)）。在麒麟原版 `220bdd1` 上补充 Windows 安装、哈希向量接线和 CI。上游仓库：阿里云 Codeup `git@codeup.aliyun.com:6a60620d8c4f3693e22016a1/MOKOA.git`。原版快照：[mokoa-kylin](https://github.com/IweirdoI/mokoa-kylin)。本人分工是偏好记忆 `src/preference/`。说明见 [docs/发布说明.md](docs/发布说明.md)。偏好方案见 [赵琳-偏好记忆整体技术方案.md](赵琳-偏好记忆整体技术方案.md)。

# 仓库说明：

## LangGraph 单智能体

现有聊天页已接入 `POST /agent/chat`，支持模型回答、记忆召回与摄取、遗忘以及关闭记忆的对照测试。
安装依赖后启动模型服务和 API；Windows 启动、云端模型配置、麒麟迁移及验证步骤见 [LangGraph 接入说明](docs/LANGGRAPH_AGENT.md)。



# 麒忆智脑 — OS Agent 记忆优化系统

> **挑战杯 XA-202612** | 麒麟软件有限公司 | 自研 Memory Orchestrator 为核心

---

## 一、我们在做什么？

为 **银河麒麟 OS Agent** 开发一套 **多源融合偏好与知识记忆** 模块（项目名称：**麒忆智脑**）。

**不是**堆叠 Mem0 / MemoryOS / Cognee 等开源框架，**而是**自研 **Memory Orchestrator（记忆编排核心）**，参考 3 个开源项目的设计思路，实现比赛要求的 7 项功能。

---

## 二、与比赛题目对照

| 比赛要求 | 我们的实现 | 目录 | 负责人 | 状态 |
|----------|-----------|------|--------|------|
| **(1) 多源接入 + 清洗 + 标准化 + 质量校验** | 四段式 ingestion 流水线（含 SQLite staging） | `src/ingestion/` | 3fba60e1 | 主链、默认脱敏、遗忘清理和手动保留期 purge 已完成；性能待验收 |
| **(2) 偏好记忆：提取、版本化、跨场景** | preference 模块 + Orchestrator 分类 + entry_adapter | `src/preference/` | 赵琳 / 26a9835a | 规则+LLM 提取已合入主线；**动态键兜底**（未知/长尾偏好不再静默丢弃，落为 `habit.custom_*` 等自由文本键）；LLM 后端无参优先本地 LM Studio；偏好 HF 数据集评测脚本已就绪；版本链/跨场景已接；真机 12/12 PASS |
| **(3) 知识记忆：结构化、冲突、RAG 检索** | memory_service（麒麟向量引擎 CRUD） | `src/knowledge/memory_service/` | cb0aeabe | 已切麒麟原生向量引擎（768 维、严格模式）；知识检索词面过滤过严已修复（中文问句 vs 英文文档不再被误杀，见 `src/core/recall_ranker.py`/`orchestrator.py`），待真机复测；冲突 5/8 为独立 resolver 逻辑问题待修 |
| **(4) 端侧 + 麒麟 Embedding SDK + ≤500ms** | embedding 统一接口 + Kylin SDK | `src/embedding/` | efca9634 | Windows/开发机已接入 BGE-M3（sentence-transformers）；**麒麟 V11 真机 SDK 已验证**（768 维，严格模式无降级） |
| **(5) 敏感信息 + 自然语言精准遗忘** | security + delete_memory | `src/security/` | 2abac5a6 | 脱敏 + NL 遗忘解析 + 索引加密 + 语义识别（LM Studio）；遗忘已覆盖普通记忆/偏好（版本索引同步删），并支持知识库按自然语言条件批量定位遗忘（knowledge_forget.py）；真机 10/10 PASS |
| **(6) 短/中/长期记忆流转** | layer_manager + mid_term 晋升 | `src/core/layer_manager.py` | efca9634 | 主链完成（heat 晋升、相似度去重、质量门控、`GET /stats`）；FLOW 重启持久化补测全 PASS |
| **(7) 量化评测 + 测试报告** | tests + benchmarks | `tests/`, `benchmarks/` | da0f41a1 | 4 项硬指标组件级基线已落地（`tests/test_metrics.py` + `benchmarks/run_benchmarks.py`）；真机/麒麟后端跑分待补 |

**结论：没有跑题。** 架构与题目一一对应。需注意答辩时说明：

- **验收形态**：正式真机验收使用宿主机原生 FastAPI + 麒麟 Embedding SDK + 麒麟 AI 子系统向量引擎（`mokoa_kylin_sdk`，768 维，严格模式 `kylin_strict=1`），不再依赖 Milvus；Milvus 仅作兼容/可选档。开发/CI 仍可走进程内 mock 或 memory 后端。
- **偏好 vs 知识**：Orchestrator 分类后，统一写入向量库，用 `memory_type` 字段区分（见 `memory_schema.py`）。

---

## 三、系统架构（给团队成员的一页纸）

```
多源数据 ──→ ingestion（3fba60e1）──→ Orchestrator（efca9634）
                                      ├─ 记忆分类（classify）
                                      ├─ 短期记忆写入
                                      ├─ 短→中→长期晋升（layer_manager）
                                      ├─ 冲突消解（conflict_resolver）
                                      └─ 偏好入库（entry_adapter → create_memory）
                ┌─────────────────────┴─────────────────────┐
                ▼                                           ▼
         preference（赵琳 / 26a9835a）                    memory_service（cb0aeabe）
         产出候选 MemoryEntry                        麒麟向量引擎 增删改查 + RAG
         不调库，产出 JSON                           create/query/update/delete
                └─────────────────────┬─────────────────────┘
                                      ▼
                              security（2abac5a6）
                              敏感过滤 + NL 遗忘 → delete_memory
```

### 开源参考（仅借鉴，不并行部署）

| 参考 | 借鉴点 | 落点 |
|------|--------|------|
| MemoryOS | 短/中/长期、热度晋升 | `core/layer_manager.py` |
| Mem0 | 偏好事实抽取 | `preference/store.py` |
| Cognee | 知识结构化 | `knowledge/` + adapter |

---

## 四、团队分工与成员负责的文件

| 成员 | 负责模块 | 主要目录 | 不要改 |
|------|----------|----------|--------|
| **efca9634** | 编排核心、短/中/长期、冲突、联调、Embedding | `src/core/`, `src/embedding/` | 别人模块 |
| **3fba60e1** | 多源接入四段式 | `src/ingestion/` | — |
| **赵琳（26a9835a）** | 偏好提取、版本化、动态键兜底 | `src/preference/` | 不调向量库，产出 JSON 调 `create_memory`；LLM 客户端无参优先本地 LM Studio |
| **cb0aeabe** | 记忆库 CRUD + RAG | `src/knowledge/memory_service/` | — |
| **2abac5a6** | 敏感信息、NL 遗忘 | `src/security/` | 解析后调 `delete_memory` |
| **6714f379** | Web 演示 | `frontend/` | 调 `api/server.py` |
| **da0f41a1** | 测试、文档、benchmark | `tests/`, `benchmarks/`, `docs/` | — |

### 模块间调用规则

1. **只通过接口协作**：`src/common/interfaces.py`、`memory_schema.py`
2. **最终记忆库目标只有一个**：cb0aeabe的 **麒麟向量引擎**（`memory_service/`）；开发/CI 后端为进程内 mock/memory，真机验收走 kylin
3. **26a9835a不直接写向量库**：产出 `MemoryEntry` JSON → 调 `create_memory()`
4. **Embedding 统一入口**：`src/embedding/embedding_interface.py`（efca9634维护）
5. **`src/adapters/` 是参考 stub**，答辩前可不实现，不影响主流程

---

## 五、目录结构

```
memory/
├── README.md                          ← 本文件
├── docs/
│   ├── ARCHITECTURE.md                ← 架构详述
│   └── KYLIN_SDK.md                   ← 麒麟 Embedding SDK 接入
├── configs/default.yaml
├── src/
│   ├── common/                        ← 全队共用模型与接口
│   ├── core/                          ← ★ Orchestrator（efca9634）
│   │   ├── entry_adapter.py           ← MemoryItem ↔ MemoryEntry 转换
│   │   ├── mid_term.py                ← 中期记忆池（50 条容量）
│   │   ├── orchestrator.py            ← 记忆编排核心
│   │   ├── layer_manager.py           ← 短/中/长期流转
│   │   ├── short_term.py              ← 短期记忆池
│   │   ├── conflict_resolver.py       ← 冲突消解
│   │   └── classifier.py              ← 记忆分类
│   ├── ingestion/                     ← 3fba60e1
│   │   ├── pipeline.py                ← 四阶段摄取管线
│   │   ├── cleaner.py                 ← 字符层清洗
│   │   ├── normalizer.py              ← 来源专属标准化器
│   │   ├── collector.py               ← 数据采集
│   │   └── quality.py                 ← 质量评分
│   ├── preference/                    ← 26a9835a（规则+LLM 提取已合入主线）
│   │   ├── keys.py                    ← 偏好键定义 + 动态键兜底（未知键→custom_）
│   │   ├── extractor.py               ← 规则+LLM 提取，未知偏好不丢弃
│   │   ├── store.py                   ← PreferenceMemoryStore 端到端
│   │   ├── features.py                ← 偏好特征画像（scene>global 解析）
│   │   ├── version_index.py           ← 本地版本索引持久化
│   │   └── llm_client.py              ← SiliconFlow/OpenAI/LM Studio 统一客户端
│   ├── knowledge/
│   │   ├── memory_service/            ← ★ cb0aeabe：麒麟向量引擎 CRUD
│   │   │   ├── create_memory.py
│   │   │   ├── query_memory.py
│   │   │   ├── update_memory.py
│   │   │   ├── delete_memory.py
│   │   │   └── memory_schema.py       ← 五类 memory_type 定义
│   │   └── store.py                   ← 知识业务层（调 memory_service）
│   ├── security/                      ← 2abac5a6
│   │   ├── service.py                 ← 正则脱敏 + NL 遗忘 + HASH 占位符
│   │   ├── forget_parser.py           ← 通用 NL 遗忘目标解析（普通记忆/偏好）
│   │   ├── knowledge_forget.py        ← 知识库 NL 遗忘解析（定向召回 + 批量删除计划）
│   │   ├── semantic_detector.py       ← LLM 语义级敏感识别（LM Studio / Ollama，可选）
│   │   └── index_crypto.py            ← 索引原文 AES-GCM 加密
│   ├── embedding/                     ← efca9634
│   └── adapters/                      ← 参考 stub（可选）
├── api/server.py
├── frontend/
├── tests/
├── benchmarks/
│   ├── run_benchmarks.py              ← 一键跑 4 项硬指标，输出 report.json
│   ├── report_template.md             ← 效果验证报告模板
│   └── datasets/                      ← 评测数据集（preference/knowledge/conflict 样例）
```

---

## 六、cb0aeabe的记忆分类（全队统一 Schema）

入库 JSON 格式见 `src/knowledge/memory_service/memory_schema.py`：

| memory_type | 含义 | 示例 |
|-------------|------|------|
| `preference` | 用户偏好 | 喜欢中文回答、要可运行代码 |
| `tool_call` | 工具调用经验 | pip 安装超时 → 用国内镜像 |
| `document_chunk` | 文档片段 | 比赛要求摘要 |
| `project_context` | 项目上下文 | RAG 模块在 src/rag/memory.ts |
| `feedback` | 用户反馈 | 「刚才只是测试不要保存」 |

附加字段：`task_type`（coding / paper_writing / os_operation…）、`source`、`confidence`、`metadata`。

**Embedding 只取 `content` 字段。**

---

## 七、数据流（完整链路）

```
1. 3fba60e1  collector → cleaner → normalizer → quality
           输出 NormalizedRecord（含 quality_score、dedup_fingerprint/兼容 fingerprint）
           normalizer 按来源（tool_result/user_behavior/manual_config/conversation）
           分别构造稳定正文，只传白名单 metadata
           process() 结束前写入 SQLite staging 审计与 TTL 去重
           staging 的 content/metadata/raw 默认先脱敏；raw 可关闭或仅 dev 明文

2. efca9634  Orchestrator.process_input → 敏感过滤 → 分类 → 写入短期记忆
           classify → 偏好 or 知识 → layer_manager.write_short()

3. 26a9835a    目标链路：从 NormalizedRecord 提取偏好 → MemoryEntry
           规则+LLM 提取已合入主线；产出 MemoryEntry 经 store_memory_entry()
           完成版本管理、冲突消解和 is_latest 标记

4. efca9634  consolidate: short → mid（heat 达标）→ long（quality 达标）
           resolve_and_store → entry_adapter → create_memory(向量库)
           持久化成功后在 staging 额外绑定 milvus_memory_id，保留原本地 memory_id

5. cb0aeabe  memory_policy 评分 → embed → memory_service（create/query/update/delete）
           开发/CI 为进程内 mock + 零向量本地 fallback；真机验收走麒麟向量引擎（768 维、严格模式）

6. 2abac5a6  写入短期记忆前 filter_sensitive（正则 + 可选 LLM 语义层，默认 LM Studio）；
           脱敏正文写回 <HASH_...> 占位符，索引原文 AES-GCM 加密落盘
           forget() 解析 NL 指令并返回目标 ID（脱敏占位符 + 普通记忆/偏好 memory_id + 知识库候选），
           由 Orchestrator 统一删除索引并级联记忆层；偏好删除后同步清除本地版本索引；
           知识库遗忘支持定向召回与按 filters（向量库）批量删除
           staging 使用不可恢复的固定标签独立脱敏，不写敏感索引；目标 ID 定位成功后
           同步擦除 content、metadata、raw 和 TTL 指纹

7. 3fba60e1  POST /staging/purge 手动执行 7d raw / 90d audit 清理
           自动定时 purge 待部署方式确定后接入

8. 查询    recall(query) → query_memory → 长期记忆返回
            list_preferences(preference_key) → 版本历史
```

---

## 八、性能指标（初评硬性要求）

| 指标 | 目标 | 负责人 |
|------|------|--------|
| 偏好提取准确率 | ≥ 85% | da0f41a1 |
| 知识检索召回率 | ≥ 85% | da0f41a1 |
| 检索响应时间 | ≤ 500ms | cb0aeabe + efca9634 |
| 冲突处理正确率 | ≥ 88% | cb0aeabe + efca9634 |

> 组件级基线见 `tests/test_metrics.py` 与 `benchmarks/run_benchmarks.py`；真机/麒麟后端跑分待补，方法见 `benchmarks/report_template.md`。

---

## 九、快速开始

```bash
cd MOKOA  # 或进入实际仓库根目录
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / 麒麟
source .venv/bin/activate

pip install -r requirements.txt
python -m pytest tests/ -v
uvicorn api.server:app --reload --port 8000
```

Windows 日常开发（Python 3.12，memory + hash，不下载 BGE，也不改麒麟脚本）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1
```

说明见 [docs/WINDOWS.md](docs/WINDOWS.md)。麒麟真机继续使用上面的 `requirements.txt` 与 `scripts/*.sh`。

###  smoke test（验证环境）

```bash
python -c "from src.core.factory import create_orchestrator; o=create_orchestrator(); print('OK')"
```

### 量化评测（一键跑分）

```bash
# 组件级基线（无需真机，输出 benchmarks/results/report_<ts>.json + summary）
python benchmarks/run_benchmarks.py

# 真机/麒麟后端跑分：先按 docs/测试结果.md 第 9 节导出环境变量，再同一条命令
```

#### 偏好抽取 HF 数据集评测（`scripts/eval_preference_hf_dataset.py`）

针对 `blackhao0426/user-preference-564k` 的结构化评测，对比 `rule` / `llm` / `fusion` 三种抽取模式：

```bash
# 开放域 + 封闭集（默认 rule 模式，纯规则，无需 LLM）
python scripts/eval_preference_hf_dataset.py --mode rule --n 100 --seed 42

# 融合模式 + 语义对齐（需可用的 LLM；无参 SiliconFlowClient 优先走本地 LM Studio）
python scripts/eval_preference_hf_dataset.py --mode fusion --semantic --n 100 --seed 42

# 重新抽样并指定输出目录
python scripts/eval_preference_hf_dataset.py --mode fusion --semantic --refresh-sample --out benchmarks/results/pref_fusion
```

- 开放域：`sample_ok` = 空 gold 时不抽取，否则 gold-action 词面覆盖召回 ≥ 0.5；`--semantic` 用 LLM-judge 判系统抽取是否语义覆盖 gold 自由文本偏好（弥补中英词面 token 重叠恒为 0 的结构性盲区）。
- 封闭集：精确匹配 `preference_key=value`（动态键兜底后 `habit.custom_*` 等同样计入）。
- 报告健壮性：`git` 缺失 / LLM 调用失败不再导致报告丢失或指标误导；`semantic_errors` 显式暴露 LLM 调用失败条数，`summary.llm_available` 仅表示已配置 key、不代表服务可达。
- 参数：`--seed / --n / --n-pos / --n-neg / --mode / --hf-source / --cache / --out / --semantic / --refresh-sample`。

---

## 十、待团队会议确认的事项

- [x] **中转库**：使用3fba60e1负责的 SQLite staging；JSONL 由中转库按需导出（最终库为麒麟向量引擎）
- [x] **存储后端**：验收形态确认为麒麟向量引擎（非 Milvus）；Milvus 仅兼容档
- [x] **演示场景**：办公助手 + 编程 Agent + 系统操作偏好（星河笔记/课程助手虚构项目）
- [x] **LLM 来源**：对话用云端硅基流动（DeepSeek-V3.2，密钥在 `.env`）；Embedding 用麒麟端侧 SDK / 本地 BGE-M3
- [x] **`feature/knowledge` / `feature/preference`** 分支是否合并？（核心成果已合入 master）

---

## 十一、开发约定

1. 只改自己负责的目录  
2. **改动后先写测试再提交**：每次完成代码改动，先编写对应测试（优先纯函数 / 规则路径 / `use_llm=False` 端到端，避免不必要的远程 LLM 调用）确认通过，再 `git commit` → `git pull --no-rebase origin master`（merge 模式）→ `git push origin master`  
3. 配置修改走 `configs/default.yaml`  
4. 有问题先查 `docs/ARCHITECTURE.md` 和 `memory_service/README.md`  
5. **提交规范**：遵循 Conventional Commits 格式（如 `feat:`, `fix:`, `docs:` 等），以便自动生成 CHANGELOG
6. **更新日志**：项目使用 `git-cliff` 自动生成 `CHANGELOG.md`，请勿手动维护

## 十二、版本发布与 CHANGELOG 生成

本项目使用 [git-cliff](https://github.com/orhun/git-cliff) 自动从 Git 提交历史生成 `CHANGELOG.md`。

### 前置条件
- 安装 `git-cliff`：`winget install git-cliff` 或 `pip install git-cliff`

### 使用方法
```bash
# 生成最新的 CHANGELOG
git cliff

# 更新到指定版本
git cliff --tag v0.3.0
```

### Commit 规范
所有成员需遵循 Conventional Commits 格式：
- `feat:` 新功能 → 归类到 `Added`
- `fix:` Bug 修复 → 归类到 `Fixed`
- `docs:` 文档更新 → 归类到 `Added`
- `refactor:` 重构 → 归类到 `Changed`
- `chore/CI/风格/测试` → 默认忽略，不进入 CHANGELOG

---

## License

Apache 2.0
