#!/usr/bin/env bash
# 旅行记忆系统 —— 测试入口
#
#   tests/run.sh            快跑：单元 + API 集成（默认，不开浏览器、不联网）
#   tests/run.sh unit       只跑单元层
#   tests/run.sh api        只跑 API 集成层
#   tests/run.sh e2e        只跑端到端（拉起独立 uvicorn + 系统 Chrome）
#   tests/run.sh all        全部（含 E2E）
#   tests/run.sh -k 关键字  透传任意 pytest 参数给默认（快）套件
#
# 安全保证：全程使用临时 SQLite 与临时 uploads 目录，
#          既不读写 data.db / uploads/，也不碰 systemd 服务。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "找不到 $PY —— 请先建好 .venv"; exit 1; }

"$PY" - <<'PYCHECK' || { echo "缺少测试依赖，请先跑： .venv/bin/pip install -r requirements-dev.txt"; exit 1; }
import importlib.util, sys
missing = [m for m in ("pytest", "httpx", "fastapi") if importlib.util.find_spec(m) is None]
sys.exit(1 if missing else 0)
PYCHECK

MODE="${1:-fast}"
case "$MODE" in
  unit) shift; exec "$PY" -m pytest -m unit "$@" ;;
  api)  shift; exec "$PY" -m pytest -m api  "$@" ;;
  e2e)
    shift
    "$PY" -c "import playwright" 2>/dev/null || {
      echo "E2E 需要 playwright： .venv/bin/pip install playwright"; exit 1; }
    command -v google-chrome >/dev/null || {
      echo "E2E 需要系统 Chrome（/usr/bin/google-chrome）"; exit 1; }
    exec "$PY" -m pytest -m e2e "$@"
    ;;
  all)  shift; exec "$PY" -m pytest -m "" "$@" ;;
  fast) exec "$PY" -m pytest ;;
  *)    exec "$PY" -m pytest "$@" ;;      # 透传（如 -k xxx、路径）
esac
