# Windows 开发

麒麟真机验收不走本文。真机仍用 `requirements.txt`、`scripts/*.sh` 和麒麟 Embedding SDK。本文只覆盖 Windows 上的安装、接线和本地 API。

原版代码在 [mokoa-kylin](https://github.com/IweirdoI/mokoa-kylin)，对应上游阿里云 Codeup `git@codeup.aliyun.com:6a60620d8c4f3693e22016a1/MOKOA.git` 的提交 `220bdd1`。本仓与上游的关系见 [发布说明.md](发布说明.md)。

## 能证明什么

| 环境 | 能做 | 不能写成比赛验收 |
|------|------|------------------|
| Windows + hash | 安装、API、规则抽取、单测 | 召回质量、500ms、麒麟 SDK |
| Windows + BGE-M3（`local`） | 语义检索和前后端联调 | 麒麟 SDK；维度是 1024，不是真机 768 |
| 本机 Milvus Lite 或 Docker 里的 Milvus | 重启后数据还在 | Embedding SDK |

Hash 向量没有语义。报告里如果写了 Windows BGE 的数字，必须注明「非麒麟 SDK」。

## 日常

需要 Python 3.12。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1
```

第一条会创建 `.venv`，安装 `requirements-windows.txt`，必要时复制 `.env`，并用 memory + hash 跑 `scripts/smoke_e2e.py`。第二条在 http://127.0.0.1:8000 启动 API。

同一终端里要手动带上环境时：

```powershell
. .\scripts\windows_env.ps1
.\.venv\Scripts\python.exe -m pytest tests/test_preference_extract.py tests/test_preference_orchestrator_wire.py tests/test_api.py::test_health -q
```

`windows_env.ps1` 只改当前窗口：UTF-8、`PYTHONPATH`、记忆后端 `memory`、向量后端 `hash`。

## 语义检索

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Profile full
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1 -Embedding local
```

`full` 安装的是完整 `requirements.txt`，包含 `sentence-transformers`。模型体积大，需要能访问 HuggingFace 或镜像 `HF_ENDPOINT=https://hf-mirror.com`。验收脚本仍是 `scripts/windows_bge_acceptance.py`，隔离端口 18085。

前端另开终端：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 http://localhost:5173 。默认代理见 `docs/LANGGRAPH_AGENT.md`，开发文档里的 API 端口是 18080：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1 -Port 18080
```

## 不要在 Windows 上跑的入口

这些脚本依赖麒麟宿主 Python、`.so` 或 Unix socket，Windows 上会直接说明并退出，或只应在真机执行：

- `scripts/run_host_py.sh`
- `scripts/kylin_preflight.py`
- `scripts/smoke_kylin_embedding.py`
- `scripts/run_flow_persistence_check.py`
- `scripts/kylin_v11_*.sh`

不要把 Windows 的 `.venv` 拷到麒麟上。两边各自创建虚拟环境。
