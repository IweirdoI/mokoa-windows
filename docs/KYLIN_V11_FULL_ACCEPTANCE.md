# V11 完整验收入口

## 交付前演示检查（当前优先级）

本轮以真实系统 SDK 下正常启动、写入、召回和前端操作为优先。性能或准确率未达标仍如实记录，不因此扩大修改范围，也不把“演示跑通”写成“全部赛题指标通过”。HTTP 功能错误仍需查看对应日志。

自动验收使用 18085 端口，并在结束时关闭它启动的 API；前端默认连接 18080。浏览器演示请另外在 V11 项目根目录的终端启动 API：

```bash
source .venv/bin/activate
export OS_AGENT_KYLIN_ACCEPTANCE=1
export OS_AGENT_MEMORY_BACKEND=kylin
export OS_AGENT_EMBEDDING_BACKEND=kylin
export EMBEDDING_BACKEND=kylin
export OS_AGENT_KYLIN_STRICT=1
export OS_AGENT_KYLIN_VECTOR_LIBRARY="$PWD/build/kylin-vector/libmokoa_kylin_vector_bridge.so"
export OS_AGENT_KYLIN_VECTOR_ENDPOINT=unix:/tmp/kylin-ai-vector-engine-1000.sock
python -m uvicorn api.server:app --host 127.0.0.1 --port 18080
```

另开终端在项目根目录执行：

```bash
curl --fail http://127.0.0.1:18080/health
cd frontend
VITE_API_TARGET=http://127.0.0.1:18080 npm run dev
```

打开 `http://127.0.0.1:5173`，确认顶栏为「实时 API 已连接」，用户不要用页面自带的 `pref-language-v2` / `tool-024` 示例数据当证据。也可在宿主一键跑浏览器验收（会启动 18080/5173、用独立用户走对话→偏好召回→知识召回→遗忘，并保存截图）：

```bash
bash scripts/kylin_v11_browser_acceptance.sh
```

证据目录：`data/reports/kylin-browser-<时间>/`（`browser.json` + PNG）。通过条件：实时 API、写入 E 盘下载偏好并能召回、写入清华镜像知识并能召回、右侧「接入」页提交工具结果/用户行为/手动配置与文本文档并在对话中召回、遗忘后生效偏好中不再出现 E 盘路径。已通过样例已归档到 [`docs/evidence/kylin-browser-20260908-002851/`](evidence/kylin-browser-20260908-002851/)（`passed: true`）。

## 自动分项验收

已同步远程 `a7e03d3` 后补强验收。截图中的 12 文档 / 15 查询、Recall@5=1、Hit@1=1、P95=43.6ms 是此前实机诊断记录，不当作本次实跑或大规模成绩。

在银河麒麟 V11 宿主终端、项目目录执行：

```bash
bash scripts/kylin_v11_full_acceptance.sh
```

脚本默认只校验运行平台为 Linux；如需锁定具体主机，运行前导出
`MOKOA_KYLIN_HOST=<hostname>`（仓库不硬编码任何主机名）。

需要已有 `.venv`、系统 Embedding SDK、已编译的 C++ 桥接库、可访问的系统向量服务，以及 httpx/uvicorn/pytest 等项目依赖。若使用此前实机确认的 socket，可设置：

```bash
export OS_AGENT_KYLIN_VECTOR_ENDPOINT=unix:/tmp/kylin-ai-vector-engine-1000.sock
```

入口检查主机、系统版本、glibc、桥接库与端口。正式模式为 `OS_AGENT_KYLIN_ACCEPTANCE=1`，数据库与 Embedding 强制 kylin，strict=1；显式 memory/milvus/local/BGE/hash 配置直接失败。系统 SDK 不可用时不得改用替代后端重跑后称通过。Windows 单元测试不属于正式验收。

`/health` 在正式模式实际调用 CoreAI 和系统向量检索，校验 768 维、无降级、SDK 标识、运行 ID 和 API PID。正常开发模式保持原行为。主机名和 glibc 检查是运行环境检查，不是防伪证明，请仍在真实宿主终端执行。

每次生成 `data/reports/kylin-v11-full-<时间和随机ID>/`，包含 summary.json、health.json、uvicorn.log、各阶段日志与 JSON。失败保留日志，业务断言失败后继续收集独立项目；后端健康失败则停止。使用唯一向量集合和独立 SQLite/偏好/安全索引目录，测试数据保留供复查，不对正常集合执行清空。

自动阶段：

- 偏好 rule 和有环境密钥时的 fusion：样本完全匹配率 ≥85%；同时保留 precision/recall/F1。LLM 请求失败单独记账，不以规则回退掩盖失败。
- `test_metrics.py` 中两个纯冲突规则组件用例：≥88%，不执行其中的模拟检索用例。
- 重新运行真实两套 SDK 的小型联合诊断，单独保存 joint.json。
- HTTP 知识旧新覆盖、偏好旧新覆盖、下载偏好遗忘、手机号遗忘及无关记忆保留：保存全部请求、响应、耗时与逐例判定。知识覆盖失败作为业务问题交负责人处理，不在验收脚本中偷偷删除旧记录。
- 100/1000 条合成数据只测 HTTP 延迟（每组20请求、全部≤500ms），报告记录实际 health，不产生召回率。
- 独立千条标注召回：`kylin_scale_recall_1000.json`（1000 文档 / 35 查询），真实 Embedding + 系统向量库，Recall@5≥85% 且全部查询端到端≤500ms。这与近重复延迟语料不是同一份数据。单独补测可在宿主执行 `bash scripts/eval_kylin_scale_recall.sh`。

仍须人工或脚本完成浏览器对话页和记忆页。推荐宿主执行 `bash scripts/kylin_v11_browser_acceptance.sh`，保留 `data/reports/kylin-browser-*`。手机号若在写入时已拦截/脱敏，报告会注明不能证明“已存手机号删除”。浏览器未补齐时整体仍为 incomplete。

退出码：1=存在失败，2=自动阶段通过但验收仍未完整，0=完整通过（当前两个待办未补齐时不会出现）。不要把退出2当作 SDK 不可用而切换后端。请把整个报告目录及浏览器证据交给负责对应分支的同学。

本次 Windows 代码验证：SDK 接线、防降级与联合评测单元测试 31 项通过，偏好提取与 API 参数回归 24 项通过；Python 编译和 diff 检查通过。正式入口在 Windows 上按预期拒绝执行。另行尝试的本地模型 Embedding 测试长时间无输出后中止，不计入通过数。以上均不替代 V11 实机验收。
