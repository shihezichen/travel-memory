#!/usr/bin/env bash
# Travel Memory — 一键安装。做完这一步就能 ./start.sh 了。
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

# ---- 1. Python ----
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "找不到 python3，请先安装 Python 3.10+"; exit 1; }
VER=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
say "Python $VER"
"$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || { echo "需要 Python 3.10 及以上，当前 $VER"; exit 1; }

# ---- 2. 虚拟环境 + 依赖 ----
if [ ! -d .venv ]; then
  say "创建虚拟环境 .venv"
  "$PY" -m venv .venv
fi
say "安装依赖（requirements.txt）"
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
say "依赖就绪"

# ---- 3. ffmpeg（只影响视频缩略图，没有也能跑）----
if command -v ffmpeg >/dev/null; then
  say "ffmpeg 已安装"
else
  warn "没有 ffmpeg —— 照片一切正常，只是上传视频时抽不出封面。"
  warn "    Debian/Ubuntu: sudo apt install ffmpeg    macOS: brew install ffmpeg"
fi

# ---- 4. 配置文件占位 ----
for f in amap_key amap_security token; do
  [ -f "$f.txt" ] || { : > "$f.txt"; }
done
if [ ! -s amap_key.txt ]; then
  warn "还没填高德地图 Key，地图会是空白（其余功能可用）。"
  warn "    申请：https://console.amap.com/dev/key/app → 选「Web端(JS API)」"
  warn "    然后把 Key 写进 amap_key.txt、配套的安全密钥写进 amap_security.txt"
fi

# ---- 5. 初始化数据库（首次启动也会自动建，这里提前建好便于播种）----
say "初始化数据库 data.db"
./.venv/bin/python -c "import db; db.init_db(); print('   表结构就绪')"

echo
say "装好了。启动： ./start.sh    然后打开 http://localhost:8084"
say "想先看看效果： ./.venv/bin/python scripts/seed_test_user.py   （演示账号 test / test1234）"
