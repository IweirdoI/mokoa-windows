#!/usr/bin/env bash
# Host-only: 1000-doc labeled Recall@5 on real Kylin embedding + vector SDK.
# Do not run inside Cursor/KARE (glibc too old to load V11 .so).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# 主机护栏：真机为银河麒麟 V11（glibc 2.38），Cursor/KARE（glibc 2.31）无法加载 V11 的 .so。
# 仓库不硬编码个人主机名；如需锁定具体机器，运行前导出 MOKOA_KYLIN_HOST=<hostname>。
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run on the Galaxy Kylin V11 host (Linux)" >&2
  exit 2
fi
if [[ -n "${MOKOA_KYLIN_HOST:-}" && "$(hostname)" != "${MOKOA_KYLIN_HOST}" ]]; then
  echo "Expected host ${MOKOA_KYLIN_HOST}, got $(hostname)" >&2
  exit 2
fi
export OS_AGENT_KYLIN_STRICT="${OS_AGENT_KYLIN_STRICT:-1}"
export OS_AGENT_KYLIN_VECTOR_LIBRARY="${OS_AGENT_KYLIN_VECTOR_LIBRARY:-$ROOT/build/kylin-vector/libmokoa_kylin_vector_bridge.so}"
export OS_AGENT_KYLIN_VECTOR_ENDPOINT="${OS_AGENT_KYLIN_VECTOR_ENDPOINT:-unix:/tmp/kylin-ai-vector-engine-1000.sock}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/data/reports/kylin-scale-recall-1000-$STAMP.json"
mkdir -p "$ROOT/data/reports"
exec "${ROOT}/.venv/bin/python" "$ROOT/scripts/eval_kylin_joint.py" \
  --dataset "$ROOT/benchmarks/datasets/kylin_scale_recall_1000.json" \
  --output "$OUT" \
  --top-k 5 \
  --repeats 3
