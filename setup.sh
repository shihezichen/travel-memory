#!/usr/bin/env bash
# Travel Memory — 一键安装。做完这一步就能 ./start.sh 了。
#
#   ./setup.sh              装依赖 + 建库 +（首次安装时）造一份演示数据
#   ./setup.sh --demo       强制重造演示数据（等于把演示账号恢复出厂状态）
#   ./setup.sh --no-demo    只装环境，不碰演示数据
#
# 演示数据默认只在**演示账号还不存在**时造一次：重复跑 setup.sh（比如为了更新
# 依赖）不会把演示账号已有的内容洗掉。要洗请显式 --demo。
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

DEMO=auto
for arg in "$@"; do
  case "$arg" in
    --demo)    DEMO=force ;;
    --no-demo) DEMO=skip ;;
    -h|--help)
      sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "未知参数：$arg（可用：--demo / --no-demo）"; exit 1 ;;
  esac
done

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

# ---- 6. 演示数据 ----
# 以前这一步要用户自己去 README 里抄一行命令跑。可是「装完打开是一片空白」
# 恰恰是最劝退的一刻 —— 得先自己录一趟旅行，才能看出这东西长什么样。
# 所以把它并进安装流程：首次安装自动造，重复安装不动已有数据。
# 账号名写死成 test：scripts/seed_test_user.py 造的就是这个账号
# （DEMO_USER 那个变量只管每日重置 timer 盯谁，不影响这里）。
if [ "$DEMO" = skip ]; then
  say "按 --no-demo 跳过演示数据"
else
  HAS_DEMO=$(./.venv/bin/python -c "
import db
print('1' if db.get_user_by_username('test') else '0')
")
  if [ "$DEMO" = force ] || [ "$HAS_DEMO" = "0" ]; then
    say "生成演示数据（3 个行程 / 12 个地点 / 28 张照片 / 1 段视频）"
    if ./.venv/bin/python scripts/seed_test_user.py >/dev/null; then
      say "   演示账号 test / test1234 —— 登录页有一键进入按钮"
    else
      warn "演示数据没造成功（不影响使用）。手动重试： ./setup.sh --demo"
    fi
  else
    say "演示账号 test 已存在，保留现有数据（要重造： ./setup.sh --demo）"
  fi
fi

echo
say "装好了。启动： ./start.sh    然后打开 http://localhost:8084"
say "正式一点的跑法（TLS + 进程托管，开发环境也建议）见 README 的「运行方式」一节"
