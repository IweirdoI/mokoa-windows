#!/usr/bin/env bash
# Host-only: start demo stack if needed, then drive the real Vue UI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# 主机护栏：仓库不硬编码个人主机名；如需锁定具体机器，运行前导出 MOKOA_KYLIN_HOST=<hostname>。
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run on the Galaxy Kylin V11 host (Linux)" >&2
  exit 2
fi
if [[ -n "${MOKOA_KYLIN_HOST:-}" && "$(hostname)" != "${MOKOA_KYLIN_HOST}" ]]; then
  echo "Expected host ${MOKOA_KYLIN_HOST}, got $(hostname)" >&2
  exit 2
fi
# Localhost must not go through corporate proxies (Chromium/Playwright otherwise hang).
export NO_PROXY="127.0.0.1,localhost,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

install_playwright_pkg() {
  # Host pip often hits SSL EOF on pypi.org; try CN mirrors first.
  local indexes=(
    "https://pypi.tuna.tsinghua.edu.cn/simple|pypi.tuna.tsinghua.edu.cn"
    "https://mirrors.huaweicloud.com/repository/pypi/simple|mirrors.huaweicloud.com"
    "https://mirrors.aliyun.com/pypi/simple|mirrors.aliyun.com"
    "https://pypi.org/simple|pypi.org"
  )
  local entry idx host
  for entry in "${indexes[@]}"; do
    idx="${entry%%|*}"
    host="${entry##*|}"
    echo "pip install playwright via $idx"
    if pip install 'playwright>=1.40' -i "$idx" \
      --trusted-host "$host" \
      --trusted-host files.pythonhosted.org; then
      return 0
    fi
  done
  return 1
}

if ! python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"; then
  install_playwright_pkg \
    || { echo "playwright 安装失败：请确认宿主可访问清华/华为 PyPI 镜像，或手动 pip install playwright" >&2; exit 1; }
fi
# Chromium binaries: npmmirror avoids playwright.azureedge.net timeouts in CN.
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"
python -m playwright install chromium

# Reload Vite after browser deps are ready so the current tree's data-testid attributes are served.
if curl -sf --max-time 2 http://127.0.0.1:5173 >/dev/null; then
  if [[ -f "$ROOT/data/reports/kylin-demo-live/vite-5173.pid" ]]; then
    kill "$(cat "$ROOT/data/reports/kylin-demo-live/vite-5173.pid")" 2>/dev/null || true
    sleep 1
  fi
  pkill -f 'vite.js .* --port 5173' 2>/dev/null || true
  sleep 1
fi
bash "$ROOT/scripts/kylin_v11_demo_start.sh"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/data/reports/kylin-browser-$STAMP"
mkdir -p "$OUT"
exec python "$ROOT/scripts/kylin_v11_browser_acceptance.py" "$OUT"
