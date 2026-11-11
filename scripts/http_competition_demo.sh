#!/usr/bin/env bash
# Competition HTTP demo against a running FastAPI (default 127.0.0.1:18080).
set -euo pipefail
BASE="${1:-http://127.0.0.1:18080}"
USER_ID="kylin_run_0831"
OUT="${2:-data/evidence/10-http-demo.txt}"
mkdir -p "$(dirname "$OUT")"
{
  echo "BASE=$BASE"
  echo "===== health ====="
  curl -sS -i "$BASE/health"
  echo
  echo "===== ingest tool_result ====="
  curl -sS -X POST "$BASE/ingest" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"source\": \"tool_result\",
    \"payload\": {\"tool\": \"pip\", \"content\": \"pip 安装超时后改用清华镜像成功\", \"exit_code\": 0}
  }"
  echo
  echo "===== ingest user_behavior ====="
  curl -sS -X POST "$BASE/ingest" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"source\": \"user_behavior\",
    \"payload\": {\"content\": \"用户经常打开 VSCode\", \"action\": \"经常打开\", \"target\": \"VSCode\", \"occurrence_count\": 5, \"confidence\": 0.85}
  }"
  echo
  echo "===== ingest manual_config ====="
  curl -sS -X POST "$BASE/ingest" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"source\": \"manual_config\",
    \"payload\": {\"key\": \"reply_language\", \"value\": \"中文\", \"confidence\": 0.95}
  }"
  echo
  echo "===== ingest PII ====="
  curl -sS -X POST "$BASE/ingest" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"source\": \"conversation\",
    \"payload\": {\"content\": \"我的手机号是 13800138000，请记住\"}
  }"
  echo
  echo "===== extract v1 download D ====="
  curl -sS -X POST "$BASE/preferences/extract" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"text\": \"下载默认放 D 盘\"
  }"
  echo
  echo "===== extract v2 download E ====="
  curl -sS -X POST "$BASE/preferences/extract" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"text\": \"以后下载改到 E 盘\"
  }"
  echo
  echo "===== profile ====="
  curl -sS "$BASE/preferences/$USER_ID/profile"
  echo
  echo "===== history habit.download_path ====="
  curl -sS "$BASE/preferences/$USER_ID/history?preference_key=habit.download_path"
  echo
  echo "===== list preferences latest_only=false ====="
  curl -sS "$BASE/preferences/$USER_ID?latest_only=false"
  echo
  echo "===== recall download ====="
  curl -sS -X POST "$BASE/recall" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"query\": \"下载路径偏好\",
    \"top_k\": 5
  }"
  echo
  echo "===== stats ====="
  curl -sS "$BASE/stats/$USER_ID"
  echo
  echo "===== short-term ====="
  curl -sS "$BASE/short-term/$USER_ID?limit=10"
  echo
  echo "===== consolidate ====="
  curl -sS -X POST "$BASE/consolidate/$USER_ID"
  echo
  echo "===== forget preference NL ====="
  curl -sS -X POST "$BASE/forget" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"instruction\": \"忘掉下载路径偏好\"
  }"
  echo
  echo "===== forget phone NL ====="
  curl -sS -X POST "$BASE/forget" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"instruction\": \"忘记我的手机号\"
  }"
  echo
  echo "===== recall after forget ====="
  curl -sS -X POST "$BASE/recall" -H 'Content-Type: application/json' -d "{
    \"user_id\": \"$USER_ID\",
    \"query\": \"下载路径偏好\",
    \"top_k\": 5
  }"
  echo
  echo "===== staging purge ====="
  curl -sS -X POST "$BASE/staging/purge" -H 'Content-Type: application/json' -d "{}"
  echo
} | tee "$OUT"
