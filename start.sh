#!/usr/bin/env bash
# Travel Memory — 唯一的入口。第一次跑装环境，以后跑直接启动。
#
#   ./start.sh                启动（首次自动装环境、建库、造演示数据）
#   ./start.sh --port 9000    换端口（默认 8084，也可用环境变量 PORT）
#   ./start.sh --setup-only   只把环境准备好，不启动（装机 / CI 用）
#   ./start.sh --demo         重造演示数据，等于把演示账号恢复出厂状态
#   ./start.sh --no-demo      装环境时不造演示数据
#   ./start.sh --reinstall    强制重装依赖（依赖装坏了的时候用）
#
# 为什么合成一个脚本：装和跑之间没有真正的决策点 —— 环境缺就补，补好就跑。
# 分成两个脚本的唯一后果，是每个新用户都要先读一遍 README 才知道要跑两次。
#
# 重复启动不会重复干活：依赖靠 requirements.txt 的哈希判断要不要重装，
# 演示数据只在演示账号还不存在时造一次。所以 ./start.sh 可以无脑重复跑。
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

PORT=${PORT:-8084}
HOST=${HOST:-0.0.0.0}
DEMO=auto
SETUP_ONLY=0
REINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port)       PORT="${2:?--port 后面要跟端口号}"; shift 2 ;;
    --port=*)     PORT="${1#*=}"; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    --demo)       DEMO=force; shift ;;
    --no-demo)    DEMO=skip; shift ;;
    --reinstall)  REINSTALL=1; shift ;;
    -h|--help)    sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）"; exit 1 ;;
  esac
done

# ---- 1. Python ----
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "找不到 python3，请先安装 Python 3.10+"; exit 1; }
"$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' || {
  echo "需要 Python 3.10 及以上，当前 $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  exit 1
}

# ---- 2. 虚拟环境 + 依赖 ----
# 依赖只在 requirements.txt 变过（或 .venv 是新建的）时才装：pip 即使全部命中缓存
# 也要好几秒，每次启动都等一遍毫无必要。哈希存在 .venv 里，删掉 .venv 就等于重装。
FRESH_VENV=0
if [ ! -x .venv/bin/python ]; then
  say "创建虚拟环境 .venv"
  "$PY" -m venv .venv
  FRESH_VENV=1
fi

REQ_STAMP=.venv/.tm-requirements-sha
REQ_HASH=$("$PY" - <<'PYHASH'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())
PYHASH
)
if [ "$REINSTALL" = 1 ] || [ "$FRESH_VENV" = 1 ] || [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" != "$REQ_HASH" ]; then
  say "安装依赖（requirements.txt）"
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
  printf '%s' "$REQ_HASH" > "$REQ_STAMP"
  say "依赖就绪"
fi

# ---- 3. ffmpeg（只影响视频封面和转码，没有也能跑）----
if ! command -v ffmpeg >/dev/null; then
  warn "没有 ffmpeg —— 照片一切正常，只是视频抽不出封面、没有清晰度分层。"
  warn "    Debian/Ubuntu: sudo apt install ffmpeg    macOS: brew install ffmpeg"
fi

# ---- 4. 配置文件占位 ----
for f in amap_key amap_security token; do
  [ -f "$f.txt" ] || : > "$f.txt"
done
if [ ! -s amap_key.txt ]; then
  warn "还没填高德地图 Key，地图会是空白（其余功能都能用）。"
  warn "    申请：https://console.amap.com/dev/key/app → 选「Web端(JS API)」"
  warn "    然后把 Key 写进 amap_key.txt、配套的安全密钥写进 amap_security.txt"
fi

# ---- 5. 数据库 ----
# 首次请求时应用也会自动建表，这里提前建好是为了下一步能直接播种。
./.venv/bin/python -c "import db; db.init_db()"

# ---- 6. 演示数据 ----
# 「装完打开是一片空白」是最劝退的一刻 —— 得先自己录一趟旅行，才能看出这东西
# 长什么样。所以首次启动自动造一份，之后重复启动不动已有数据。
# 账号名写死成 test：scripts/seed_test_user.py 造的就是这个账号
# （DEMO_USER 那个变量只管每日重置 timer 盯谁，不影响这里）。
if [ "$DEMO" != skip ]; then
  HAS_DEMO=$(./.venv/bin/python -c "import db; print('1' if db.get_user_by_username('test') else '0')")
  if [ "$DEMO" = force ] || [ "$HAS_DEMO" = "0" ]; then
    say "生成演示数据（3 个行程 / 12 个地点 / 28 张照片 / 1 段视频）"
    if ./.venv/bin/python scripts/seed_test_user.py >/dev/null; then
      say "   演示账号 test / test1234 —— 登录页有一键进入的按钮"
    else
      warn "演示数据没造成功（不影响使用）。想重试： ./start.sh --demo"
    fi
  fi
fi

if [ "$SETUP_ONLY" = 1 ]; then
  say "环境就绪。启动： ./start.sh"
  exit 0
fi

# ---- 7. 启动 ----
# 注意用 .venv 里的 python，不是系统 python：依赖全装在 .venv 里。
echo
say "http://localhost:$PORT    （Ctrl-C 停止）"
say "长期运行请用 systemd + Caddy，见 README「让它一直跑着」一节"
exec ./.venv/bin/python -m uvicorn app:app --host "$HOST" --port "$PORT"
