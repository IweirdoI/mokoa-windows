#!/usr/bin/env bash
# Start the V11 browser-demo stack on 18080 + 5173. Does not touch 18085.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${MOKOA_DEMO_LOG_DIR:-$ROOT/data/reports/kylin-demo-live}"
mkdir -p "$LOG_DIR" /tmp/mokoa-ai

export OS_AGENT_KYLIN_ACCEPTANCE=1
export OS_AGENT_MEMORY_BACKEND=kylin
export OS_AGENT_EMBEDDING_BACKEND=kylin
export EMBEDDING_BACKEND=kylin
export OS_AGENT_KYLIN_STRICT=1
export OS_AGENT_KYLIN_EMBEDDING_MODEL="${OS_AGENT_KYLIN_EMBEDDING_MODEL:-ensemble-embd_gte-base_uint8-text}"
export OS_AGENT_KYLIN_VECTOR_LIBRARY="${OS_AGENT_KYLIN_VECTOR_LIBRARY:-$ROOT/build/kylin-vector/libmokoa_kylin_vector_bridge.so}"
export OS_AGENT_KYLIN_VECTOR_ENDPOINT="${OS_AGENT_KYLIN_VECTOR_ENDPOINT:-unix:/tmp/kylin-ai-vector-engine-1000.sock}"
unset OS_AGENT_MILVUS_COLLECTION || true

resolve_node() {
  if command -v node >/dev/null 2>&1; then
    command -v node
    return
  fi
  local cursor_node="/usr/share/cursor/resources/app/resources/helpers/node"
  if [[ -x "$cursor_node" ]]; then
    echo "$cursor_node"
    return
  fi
  echo "node 不在 PATH。请先安装 Node，或用 Cursor 自带 node。" >&2
  exit 1
}

wait_http() {
  local url="$1" tries="${2:-40}" timeout="${3:-5}"
  for _ in $(seq 1 "$tries"); do
    if curl -sf --max-time "$timeout" "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

echo "1) GTE models (kytensor explicit mode)"
for model in tokenizer_gte-base_uint8-text embd_gte-base_uint8-text ensemble-embd_gte-base_uint8-text; do
  code="$(curl -sS -o /tmp/mokoa-ai/load-"$model".txt -w '%{http_code}' \
    -X POST "http://127.0.0.1:8000/v2/repository/models/${model}/load" || true)"
  echo "  load $model -> HTTP $code"
done
curl -sf --max-time 5 "http://127.0.0.1:8000/v2/models/ensemble-embd_gte-base_uint8-text/ready" >/dev/null \
  || { echo "GTE ensemble 未就绪，检查 kytensor.service"; exit 1; }

echo "2) kylin-ai-runtime"
RUNTIME_BIN="$(command -v kylin-ai-runtime || true)"
if [[ -z "$RUNTIME_BIN" && -x /usr/bin/kylin-ai-runtime ]]; then
  RUNTIME_BIN=/usr/bin/kylin-ai-runtime
fi
if [[ -z "$RUNTIME_BIN" ]]; then
  echo "找不到 kylin-ai-runtime。请在银河麒麟宿主终端执行本脚本，不要在 Cursor/KARE 里启动。" >&2
  exit 1
fi
if ! pgrep -f '[k]ylin-ai-runtime' >/dev/null; then
  rm -f /tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock
  mkdir -p /tmp/.kylin-ai-runtime-unix/1000
  nohup "$RUNTIME_BIN" >>/tmp/mokoa-ai/runtime.log 2>&1 &
  echo $! >/tmp/mokoa-ai/runtime.pid
fi
for _ in $(seq 1 20); do
  [[ -S /tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock ]] && break
  sleep 0.5
done
[[ -S /tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock ]] \
  || { echo "core-textembedding.sock 未出现"; exit 1; }
[[ -S /tmp/kylin-ai-vector-engine-1000.sock ]] \
  || { echo "向量引擎 socket 不在"; exit 1; }

echo "3) API 18080"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
if ! python -c "import multipart" >/dev/null 2>&1; then
  echo "缺少 python-multipart（/ingest/file 需要），正在安装…"
  pip install 'python-multipart>=0.0.9' \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    || pip install 'python-multipart>=0.0.9'
fi
if ! curl -sf --max-time 2 http://127.0.0.1:18080/health >/dev/null; then
  nohup python -m uvicorn api.server:app --host 127.0.0.1 --port 18080 \
    >"$LOG_DIR/uvicorn-18080.log" 2>&1 &
  echo $! >"$LOG_DIR/uvicorn-18080.pid"
fi
wait_http http://127.0.0.1:18080/health 60 20 \
  || { echo "API /health 失败，见 $LOG_DIR/uvicorn-18080.log"; exit 1; }
curl -sS http://127.0.0.1:18080/health | tee "$LOG_DIR/health.json"
echo

echo "4) frontend 5173"
NODE_BIN="$(resolve_node)"
if ! curl -sf --max-time 2 http://127.0.0.1:5173 >/dev/null; then
  (
    cd "$ROOT/frontend"
    nohup env VITE_API_TARGET=http://127.0.0.1:18080 \
      "$NODE_BIN" "$ROOT/frontend/node_modules/vite/bin/vite.js" \
      --configLoader runner --host 127.0.0.1 --port 5173 \
      >"$LOG_DIR/vite-5173.log" 2>&1 &
    echo $! >"$LOG_DIR/vite-5173.pid"
  )
fi
wait_http http://127.0.0.1:5173 30 \
  || { echo "前端未起来，见 $LOG_DIR/vite-5173.log"; exit 1; }
# Frontend talks to API through Vite /api proxy; verify that path too.
wait_http http://127.0.0.1:5173/api/health 30 5 \
  || { echo "前端 /api/health 代理失败，检查 VITE_API_TARGET 与 API 18080"; exit 1; }

echo
echo "演示栈已就绪："
echo "  API     http://127.0.0.1:18080/health"
echo "  前端    http://127.0.0.1:5173"
echo "  日志    $LOG_DIR"
echo "打开页面后确认顶栏为「实时 API 已连接」，用户用 demo_user，不要用示例数据当证据。"
