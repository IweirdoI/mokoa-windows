#!/usr/bin/env bash
# Contest git-dataset sampling eval on commit 17af2b3 (parallel preference + kylin paths).
# Sample size n=150 = 1.5× reporting floor 100 = 3.0× script default 50 (band 1.5×–5×).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAMP="${CONTEST_EVAL_STAMP:-$(date +%Y%m%d-%H%M)}"
OUT="${CONTEST_EVAL_OUT:-docs/evidence/dataset-eval-${STAMP:0:8}}"
LOG="$OUT/run_${STAMP}.log"
N="${CONTEST_EVAL_N:-150}"
N_POS="${CONTEST_EVAL_N_POS:-120}"
N_NEG="${CONTEST_EVAL_N_NEG:-30}"
NQ="${CONTEST_EVAL_NQ:-150}"
RC="${CONTEST_EVAL_RC:-2000}"
WORKERS="${CONTEST_EVAL_WORKERS:-8}"
SEED="${CONTEST_EVAL_SEED:-42}"
PREF_CACHE="$ROOT/git/user-preference-564k/sampled_seed${SEED}_n${N}.jsonl"

mkdir -p "$OUT/raw_io" "$OUT/prompts"

export OS_AGENT_EMBEDDING_BACKEND="${OS_AGENT_EMBEDDING_BACKEND:-kylin}"
export OS_AGENT_MEMORY_BACKEND="${OS_AGENT_MEMORY_BACKEND:-memory}"
export OS_AGENT_KYLIN_STRICT="${OS_AGENT_KYLIN_STRICT:-1}"
export OS_AGENT_KYLIN_VECTOR_LIBRARY="${OS_AGENT_KYLIN_VECTOR_LIBRARY:-$ROOT/build/kylin-vector/libmokoa_kylin_vector_bridge.so}"
export OS_AGENT_KYLIN_VECTOR_ENDPOINT="${OS_AGENT_KYLIN_VECTOR_ENDPOINT:-unix:/tmp/kylin-ai-vector-engine-1000.sock}"
# Align with 17af2b3 fusion+semantic verification (DeepSeek-V4-Flash), even if .env still has V3.2.
export SILICONFLOW_LLM_MODEL="${SILICONFLOW_LLM_MODEL:-deepseek-ai/DeepSeek-V4-Flash}"

find_host_py() {
  local cand
  for cand in \
    /run/host/usr/bin/python3.12 \
    /usr/bin/python3.12 \
    /opt/kare-applications/host/usr/bin/python3.12 \
    /root/sysroot/usr/bin/python3.12
  do
    if [ -x "$cand" ]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

HOST_LD="${HOST_LD:-/run/host/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2}"
HOST_LIB="${HOST_LIB:-/run/host/usr/lib/x86_64-linux-gnu:/run/host/lib/x86_64-linux-gnu}"
HOST_PY="$(find_host_py || true)"

run_py() {
  if [ -n "${HOST_PY:-}" ] && [ -x "${HOST_LD}" ]; then
    PYTHONPATH="$ROOT:$ROOT/.venv/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}" \
      VIRTUAL_ENV="$ROOT/.venv" PATH="$ROOT/.venv/bin:$PATH" \
      "$HOST_LD" --library-path "$HOST_LIB" "$HOST_PY" "$@"
  elif [ -n "${HOST_PY:-}" ]; then
    PYTHONPATH="$ROOT:$ROOT/.venv/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}" \
      VIRTUAL_ENV="$ROOT/.venv" PATH="$ROOT/.venv/bin:$PATH" \
      "$HOST_PY" "$@"
  elif [ -x /run/host/usr/bin/python3.12 ]; then
    bash "$ROOT/scripts/run_host_py.sh" "$@"
  else
    echo "FATAL: cannot find host Python 3.12" >&2
    return 127
  fi
}

{
  echo "===== START $(date '+%F %T %z') ====="
  echo "STAMP=$STAMP OUT=$OUT N=$N NQ=$NQ RC=$RC SEED=$SEED WORKERS=$WORKERS"
  echo "HEAD=$(git rev-parse HEAD)"
  echo "HEAD_SHORT=$(git rev-parse --short HEAD)"
  echo "EVAL_CODE=17af2b3f509ecea1ed10f1b02134277e530775ed"
  echo "ORIGIN_MASTER=$(git rev-parse --short origin/master 2>/dev/null || echo unknown)"
  echo "HOST_PY=${HOST_PY:-unset}"
  echo "SILICONFLOW_LLM_MODEL=$SILICONFLOW_LLM_MODEL"
  echo "python_probe:"
  run_py -c "import sys; print(sys.version); print(sys.executable)"
  echo "===== remaining git/ ====="
  ls git/
  echo "===== env (no secrets) ====="
  echo "OS_AGENT_EMBEDDING_BACKEND=$OS_AGENT_EMBEDDING_BACKEND"
  echo "OS_AGENT_MEMORY_BACKEND=$OS_AGENT_MEMORY_BACKEND"
  echo "OS_AGENT_KYLIN_STRICT=$OS_AGENT_KYLIN_STRICT"
  echo "OS_AGENT_KYLIN_VECTOR_LIBRARY=$OS_AGENT_KYLIN_VECTOR_LIBRARY"
  echo "OS_AGENT_KYLIN_VECTOR_ENDPOINT=$OS_AGENT_KYLIN_VECTOR_ENDPOINT"
  echo "SILICONFLOW_BASE_URL=${SILICONFLOW_BASE_URL:-from-.env}"
  git -C "$ROOT" status --short | head -80
  echo "===== reproducibility snapshot + LLM probe ====="
  run_py scripts/write_contest_repro_snapshot.py --out "$OUT" --stamp "$STAMP" \
    --seed "$SEED" --n "$N" --n-pos "$N_POS" --n-neg "$N_NEG" --nq "$NQ" --rc "$RC" --workers "$WORKERS"
  echo "===== preference (parallel fusion+rule+semantic workers=$WORKERS) ====="
  run_py scripts/eval_preference_hf_dataset.py --seed "$SEED" --n "$N" --n-pos "$N_POS" --n-neg "$N_NEG" \
    --mode both --semantic --llm-endpoint remote --workers "$WORKERS" \
    --cache "$PREF_CACHE" --out "$OUT"
  echo "===== conflict ====="
  run_py scripts/eval_conflict_longmemeval.py --seed "$SEED" --n "$N" --out "$OUT"
  echo "===== dialog_tool ====="
  run_py scripts/eval_dialog_tool_datasets.py --seed "$SEED" --n "$N" --out "$OUT"
  echo "===== behavior (PersonaChat; BPMN skipped if missing) ====="
  run_py scripts/eval_behavior_knowledge_datasets.py --seed "$SEED" --n "$N" --out "$OUT"
  echo "===== dialog_quant ====="
  run_py scripts/eval_dialog_quant_datasets.py --seed "$SEED" --n "$N" --out "$OUT"
  echo "===== retrieval MS MARCO + TREC ====="
  run_py scripts/eval_retrieval_datasets.py --seed "$SEED" --nq "$NQ" --rc "$RC" --out "$OUT"
  echo "===== dump sampled raw IO ====="
  run_py scripts/dump_contest_sample_io.py --seed "$SEED" --n "$N" --nq "$NQ" --out "$OUT"
  echo "===== write report ====="
  run_py scripts/write_contest_sample_report.py --stamp "$STAMP" --out "$OUT"
  echo "===== ALL_EVALS_DONE $(date '+%F %T %z') ====="
} 2>&1 | tee "$LOG"
echo "LOG=$LOG WRAPPER_EXIT=${PIPESTATUS[0]}"
exit "${PIPESTATUS[0]}"
