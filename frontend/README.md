# 前端演示指南

> **负责人：6714f379** | 对应比赛 **效果演示视频** + **应用案例展示** | 后端接口：`api/server.py`

本文档说明：**做什么页面、调哪些 API、找谁对接、答辩 Demo 怎么设计**。

> **当前状态（2026-09-08）：** 已实现 Vue 3 双栏对话与实时记忆面板，并接入3fba60e1
> `cacd8b5` 的文件摄取接口。下文原有比赛规划保留作背景；实际使用以本节为准。

### 运行与多源接入

在项目根目录准备后端环境并运行（Windows 可使用 `.venv/Scripts/python`）：

```bash
python -m pip install -r requirements.txt
python -m uvicorn api.server:app --host 127.0.0.1 --port 18080
```

另一个终端进入 `frontend`，运行 `npm install`、`npm run dev`，打开
`http://localhost:5173/`。不要直接双击 `index.html`；Vue 模块与 API 代理需要 Vite。
若后端端口不是 18080，在 `frontend/.env.local` 设置 `VITE_API_TARGET` 后重启 Vite。
若后端开启了 API 鉴权（根目录 `.env` 里的 `API_AUTH_TOKEN`），在同一文件设置 `VITE_API_TOKEN`，
前端会把令牌以 `Authorization: Bearer` 加到全部请求（含 SSE 流式对话与 multipart 上传）；
不设置该变量时保持本地开发兼容，不携带该请求头。

- 顶栏填用户 ID、连接 API。点击输入框旁「＋ 多源接入」或右栏「接入」。
- **文件 / 图片**：拖放或多选 PDF、DOCX、PNG/JPG/JPEG/WebP/BMP/GIF，单个 ≤8MB，队列最多 10 个，逐个处理。每个文件可填写独立说明。选择文件不会立即上传，点击「上传并写入记忆」才发送。
- **工具结果**：填写工具名、执行状态、关键输出；事件编号选填，同一事件重投应保持相同编号。
- **用户行为**：填写行为、操作对象和真实出现次数；单次低价值行为可能被质量门控拒绝。
- **手动配置**：填写配置键、值及作用范围；场景级配置须填场景。
- **文本对话**：直接录入希望记住的文字，原左栏对话功能继续可用。

文件通过 `/api/ingest/file` 发送 multipart；其余四类通过 `/api/ingest` 发送 JSON。
所有请求携带当前用户与可选场景，文件不会再次作为 JSON 录入。以 `accepted` 判断
接收结果，HTTP 200 下的重复/低质量等拒绝也显示真实原因。成功的知识触发
`/consolidate/{user_id}`；整理失败保留「已接收」结果并提示长期同步失败。
处理完成刷新统计与偏好，对话和总览同步显示活动。

接入页展示处理后的正文、质量评分、耗时、记忆编号和图片提取方式。
图片视觉识别、OCR 依赖后端实际能力；扫描件或无法提取的图片可补内容说明。
演示模式不会上传或伪造成功。180 秒超时/网络异常不会自动重试，请先同步确认。
切换用户或模式后，文件队列和本次处理记录清空；刷新页面不会保留上传队列。

### 验证

```bash
# frontend 目录
node --test tests/ingestion.test.js
npm run build
```

完整浏览器验收使用项目真实 FastAPI、隔离 SQLite/偏好目录、内存存储与 hash 测试
Embedding，不代表真实视觉模型、BGE-M3 或麒麟 SDK 的效果/性能验收。

1. 项目根目录运行 `python frontend/tests/serve_api.py`，记录输出的 `FIXTURE_DIR`。
2. `frontend` 目录设置 `VITE_API_TARGET=http://127.0.0.1:18086`，运行
   `npm run dev -- --host 127.0.0.1 --port 5186 --strictPort`。
3. 安装/提供 Playwright（可用 `PLAYWRIGHT_MODULE` 指向现有包路径），从项目根目录执行
   `node frontend/tests/ingestion-browser.cjs <FIXTURE_DIR>`。脚本使用本机 Edge 无头浏览器，
   产出桌面/手机截图及报告；异常分支单独注入 503/断网以检查前端行为。

测试样本、截图、报告与隔离状态位于忽略的 `tmp/frontend-ingestion-*`，不进入提交。

---

## 一、你的核心任务

比赛答题要求里和前端直接相关的有：

| 交付物 | 要求 |
|--------|------|
| **效果演示视频** | 展示偏好捕捉、知识整合、检索复用等核心功能 |
| **至少 1 个真实场景应用案例** | 可交互 Demo，证明方案能落地 |
| **用户手册 / 操作说明**（协助） | 界面截图 + 操作步骤，da0f41a1可协助文档整合 |

**你不负责**实现记忆算法、Milvus、Embedding，只负责 **把后端能力可视化**，让评委「看得见、点得动、看得懂」。

---

## 二、建议技术栈

| 选项 | 说明 |
|------|------|
| **React + Vite**（推荐） | 生态成熟，组件多，答辩 Demo 够用 |
| **Vue 3 + Vite** | 团队熟悉即可 |
| 纯 HTML + 少量 JS | 时间极紧时的保底方案 |

**原则：**

- 前端代码 **只放在 `frontend/`**，不要改 `src/` 业务逻辑
- 通过 **HTTP 调 `api/server.py`**，不直接 import Python 模块
- UI 风格简洁专业即可，参考 [MemoryOS Playground](https://github.com/BAI-LAB/MemoryOS)、Cognee UI

**本地联调：**

```bash
# 终端 1：后端
cd MOKOA  # 或进入实际仓库根目录
uvicorn api.server:app --reload --port 8000

# 终端 2：前端（示例 React）
cd frontend
npm install
npm run dev
```

在 `vite.config.ts` 里配置代理：

```ts
server: {
  proxy: {
    '/api': {
      target: 'http://localhost:8000',
      rewrite: path => path.replace(/^\/api/, ''),
    },
  },
}
```

---

## 三、建议目录结构

```
frontend/
├── README.md              ← 本文件
├── package.json
├── src/
│   ├── pages/
│   │   ├── Dashboard.tsx       # 总览：记忆统计、快捷入口
│   │   ├── Ingest.tsx          # 写入记忆（多源模拟）
│   │   ├── MemoryList.tsx      # 记忆列表 + 分类筛选
│   │   ├── Search.tsx          # RAG 检索
│   │   ├── Preference.tsx      # 偏好画像展示
│   │   └── Forget.tsx          # 自然语言遗忘
│   ├── components/
│   │   ├── MemoryCard.tsx      # 单条记忆卡片
│   │   ├── TypeBadge.tsx         # memory_type 标签
│   │   └── LatencyBadge.tsx    # 检索耗时（≤500ms 展示）
│   ├── api/
│   │   └── client.ts           # 封装 fetch 调后端
│   └── App.tsx
└── public/
```

---

## 四、必须实现的 5 个演示能力（对应比赛功能）

### 4.1 多源数据写入 — 展示题目 (1)

**页面：** `Ingest.tsx`

**演示内容：** 模拟四类数据写入，展示清洗和标准化结果。

| Tab | 模拟场景 | 对应 source |
|-----|----------|-------------|
| 对话输入 | 用户说「以后用中文回答」 | `conversation` |
| 工具调用 | JSON：`run_shell` 失败日志 | `tool_result` |
| 用户行为 | 多次打开 VSCode | `user_behavior` |
| 手动配置 | 用户设置「下载默认 D 盘」 | `manual_config` |

**调用 API：**

```http
POST /ingest
{
  "user_id": "demo_user",
  "source": "conversation",
  "payload": { "content": "以后请用中文回答" }
}
```

**对接：3fba60e1**

- 要几条「清洗前 → 清洗后」真实样例，放进 Demo 默认文案
- 若后端返回清洗结果，页面上展示 **前后对比**（加分项）

---

### 4.2 偏好记忆展示 — 展示题目 (2)

**页面：** `Preference.tsx`

**演示内容：**

- 用户偏好列表（中文回答、编程风格、下载路径等）
- **版本历史**：通过 `/preferences/{user_id}` 展示同一偏好 v1 → v2
- **跨场景标签**：`task_type` = coding / os_operation

**数据来源（版本历史使用该接口，不要用 `/recall` 代替）：**

```http
GET /preferences/demo_user?latest_only=false&top_k=10
```

**对接：26a9835a**

- 偏好字段含义、展示哪些 metadata
- 至少 3 条可演示的偏好样例

---

### 4.3 知识检索 / RAG — 展示题目 (3)

**页面：** `Search.tsx`（答辩 **重点页**）

**演示内容：**

- 搜索框输入自然语言 query
- 展示召回的记忆列表（当前可用字段：content + memory_type + layer + metadata）
- 相似度分数暂未由 `/recall` 透传；如需展示，后端需先保留 Milvus 命中的 `score`
- **显示接口返回的检索耗时**（ms）；真实 Milvus/麒麟环境验收前不要标注“已达到 ≤500ms”
- 可选：高亮命中关键词

**调用 API：**

```http
POST /recall
{
  "user_id": "demo_user",
  "query": "pip 安装超时怎么办",
  "top_k": 5
}
```

**对接：cb0aeabe**

- `query_memory` 返回字段格式（items、latency_ms）
- 答辩前预置 5～10 条种子记忆（工具类、文档类各几条）

**对接：da0f41a1**

- Demo 用的 query **必须与测试数据集一致**，报告数字和演示可复现

---

### 4.4 记忆冲突 / 更新 — 展示题目 (3) 创新点

**页面：** 可在 `MemoryList.tsx` 或单独 `ConflictDemo.tsx`

**演示脚本（建议固定）：**

1. 先写入：「用户喜欢 Java」
2. 再写入：「用户偏好 Python，以后都用 Python」
3. 界面展示：**旧记忆被更新/替换**，带 version 或时间线

**对接：efca9634**

- `/ingest` 已返回 `updated_existing`、`memory_id` 和 `evicted_memory_id`
- 前端可用 `updated_existing` 展示本次写入是否更新了已有记忆

---

### 4.5 自然语言遗忘 — 展示题目 (5)

**页面：** `Forget.tsx`

**演示内容：**

- 输入：「忘掉关于 Java 的偏好」或「刚才只是测试，删掉」
- 展示：删除的记忆 ID 列表 + 列表刷新
- NL 目标解析已实现（forget_parser，P0 验收通过）；知识库按 NL 条件批量定位遗忘已实现（knowledge_forget.py）；遗忘接口 `POST /forget` 返回真实删除的 memory_id，无需伪造结果

**调用 API：**

```http
POST /forget
{
  "user_id": "demo_user",
  "instruction": "忘掉刚才的测试内容"
}
```

**对接：2abac5a6**

- NL 解析后的删除范围说明（删几条、按什么条件）
- 敏感信息演示：输入含手机号的内容，展示 **被过滤** 提示

---

## 五、后端 API 现状与待联调

当前 `api/server.py` 接口（**efca9634负责联调 Orchestrator**）：

| 方法 | 路径 | 状态 | 你需要 |
|------|------|------|--------|
| GET | `/health` | ✅ 可用 | 启动检查 |
| POST | `/ingest` | ✅ 可用 | 写入并返回清洗/分类结果 |
| GET | `/short-term/{user_id}` | ✅ 可用 | 短期记忆列表和统计 |
| GET | `/mid-term/{user_id}` | ✅ 可用 | 中期记忆列表和统计 |
| GET | `/preferences/{user_id}` | ✅ 已接线 | 偏好及版本查询 |
| POST | `/recall` | ⚠️ mock 已接线 | 长期记忆列表和耗时；真实向量排序待实现 |
| POST | `/consolidate/{user_id}/mid` | ✅ 可用 | 手动触发短期到中期晋升 |
| POST | `/consolidate/{user_id}` | ✅ 已接线 | 触发完整生命周期整合 |
| POST | `/forget` | ⚠️ 部分完成 | HTTP 和删除链路已接线，NL 目标解析仍为 TODO |
| POST | `/staging/purge` | ✅ 可用 | 手动执行默认 7d raw / 90d audit 清理（运维接口） |

**仍缺少或可选增加的接口（和efca9634确认）：**

| 路径 | 用途 |
|------|------|
| `GET /memories?user_id=&memory_type=` | 跨层统一记忆列表页；当前分别使用 short/mid/preferences 接口 |
| `GET /stats/{user_id}` | ✅ 可用 | Dashboard 数字（记忆总数、各类型占比） |

**原则：** 缺接口就提需求给 **efca9634**，不要在 frontend 里写 Python 业务逻辑。

---

## 六、推荐 Demo 场景（答辩用 1 个讲透）

**场景：麒麟 OS 上的编程助手 Agent**

| 步骤 | 操作 | 展示能力 |
|------|------|----------|
| 1 | 用户说「以后回答用中文，代码要能直接跑」 | 偏好写入 |
| 2 | Agent 执行 `pip install` 失败 | 工具调用记忆 |
| 3 | 上传比赛 PDF 摘要 | 文档类知识 |
| 4 | 搜索「安装 Python 包失败怎么办」 | RAG 检索 + 延迟 |
| 5 | 用户说「忘掉刚才的测试」 | NL 遗忘（后端解析完成后再纳入正式演示） |
| 6 | 打开偏好页，展示更新后的画像 | 偏好版本/整合 |

此场景与 **da0f41a1测试数据集** 对齐，演示和报告数字一致。

---

## 七、对接关系总表（速查）

| 你要展示的功能 | 找谁 | 要什么 |
|----------------|------|--------|
| 写入 / 清洗展示 | **3fba60e1** | 四类数据源样例、清洗前后 JSON |
| 偏好列表 / 版本 | **26a9835a** | 偏好字段、展示样例 |
| 检索结果 / 耗时 | **cb0aeabe** | query 返回格式、种子数据 |
| 冲突更新效果 | **efca9634** | 冲突 API 或 ingest 返回 |
| 遗忘 / 敏感过滤 | **2abac5a6** | forget 行为、过滤提示文案 |
| API 联调 / 新接口 | **efca9634** | server.py 接入 Orchestrator |
| Demo 与报告一致 | **da0f41a1** | 测试 query 列表 |
| 麒麟环境部署 | **efca9634** | 答辩机器上前后端启动方式 |

**建议：每周和efca9634对一次 API，后端就绪一个接口，前端接一个页面。**

---

## 八、页面与比赛要求对照

| 比赛要求 | 建议页面 | 答辩是否必演示 |
|----------|----------|----------------|
| (1) 多源接入 + 清洗 | Ingest | ✅ |
| (2) 偏好记忆 | Preference | ✅ |
| (3) 知识检索 + 冲突 | Search + ConflictDemo | ✅ |
| (4) 端侧 / 延迟 | Search 上显示 latency | ✅ |
| (5) 敏感 + 遗忘 | Forget | ✅ |
| (6) 短中长期流转 | Dashboard 或 Timeline（可选） | 建议有 |
| (7) 评测 | 不需前端实现 | da0f41a1负责 |

---

## 九、交付物清单

| 交付物 | 说明 | 截止参考 |
|--------|------|----------|
| 可运行前端工程 | `frontend/` 可 `npm run dev` | 8 月下旬 |
| 与后端联调通过 | 5 大演示能力均可点击 | 9 月上旬 |
| **演示视频** | 3～5 分钟，按「推荐 Demo 场景」录制 | 9 月 15 日前 |
| 界面截图 | 供da0f41a1写入用户手册 | 9 月上旬 |
| 答辩 Live Demo | 现场可离线/在线演示 | 11 月擂台 |

**录视频建议：**

- 1920×1080，字幕标注「偏好捕捉」「RAG 检索 320ms」等
- 每个功能点 30～60 秒，总时长不超过 5 分钟

---

## 十、推荐工作顺序（按周）

| 阶段 | 任务 |
|------|------|
| **第 1 周** | 选型 + 搭脚手架 + `api/client.ts` + 调通 `/health` |
| **第 2 周** | Ingest + Search 两页（mock 数据也可先画 UI） |
| **第 3 周** | 后端接口就绪后联调；Preference + Forget |
| **第 4 周** | Dashboard、冲突演示、latency 展示、UI 打磨 |
| **答辩前** | 录演示视频；与da0f41a1对齐 query；麒麟机部署试跑 |

---

## 十一、注意事项

1. **不要改 `src/`**，只改 `frontend/` 和必要时提 API 需求  
2. **不要用假数据糊弄答辩**：最终 Demo 必须调真实后端（mock Milvus 也算后端真实链路）  
3. **memory_type 五类** 用不同颜色标签区分（见 `memory_schema.py`）  
4. **检索耗时** 务必展示，对应比赛 500ms 指标  
5. CORS：若前后端分离部署，让efca9634在 FastAPI 加 `CORSMiddleware`  

---

## 十二、相关文件

| 文件 | 说明 |
|------|------|
| `api/server.py` | 后端 REST 入口 |
| `src/knowledge/memory_service/memory_schema.py` | memory_type 枚举 |
| `benchmarks/README.md` | da0f41a1测试场景（Demo 应对齐） |
| 项目根 `README.md` | 全队架构与分工 |

有问题先 @ **efca9634**（API / 联调），业务细节 @ 对应模块同学。
