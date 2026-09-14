#!/bin/bash
# uploads/ -> 阿里云 OSS，让媒体流量绕开 frp 隧道。
#
# 为什么要有这个脚本：公网入口 139.129.24.132 的 frp 隧道实测只有 4Mbps
# （单条 TCP，3.8% 重传，cwnd 已塌）。一路 720p 就能把它占满，视频一放
# 整站的 API 都跟着排队。媒体挪到 OSS 之后，隧道只剩 HTML + JSON。
#
# 幂等，可以反复跑。默认**不删**云端多余对象 —— 本地 uploads/ 万一被误清空，
# --delete 会把线上媒体一起带走。要清理传 --prune，且必须自己确认过。
#
# 缓存策略跟 app.py 的 UploadStaticFiles 对齐：
#   variants/  一年 immutable —— 转码档位文件名带档位，内容永不改变
#   其余       1 天           —— 原片会被 transcode.ensure_faststart 原地覆盖
set -euo pipefail

# 仓库根目录 = 本脚本所在目录的上一级。写死绝对路径会让这个脚本只在造它的
# 那台机器上成立 —— 它现在进了 git，热备机和别人的 clone 都要能直接跑。
BASE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC="$BASE/uploads"
# 桶名可以用环境变量盖掉，方便换 bucket 或者拿测试桶试跑。
BUCKET=${TRAVEL_OSS_BUCKET:-oss://travel-memory-media}/uploads
OSSUTIL="$HOME/bin/ossutil"
LOG="$BASE/logs/sync-oss.log"
WATERMARK="$BASE/logs/oss-sync-watermark"

IMMUTABLE="Cache-Control:public, max-age=31536000, immutable"
ONE_DAY="Cache-Control:public, max-age=86400"

DRY=""
PRUNE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY="yes" ;;
    --prune)   PRUNE="yes" ;;
    *) echo "未知参数: $arg (支持 --dry-run / --prune)" >&2; exit 2 ;;
  esac
done

mkdir -p "$BASE/logs"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

[ -x "$OSSUTIL" ] || { log "FAIL ossutil 不在 $OSSUTIL"; exit 1; }
[ -f "$HOME/.ossutilconfig" ] || { log "FAIL 缺少 ~/.ossutilconfig，先配 AK"; exit 1; }
[ -d "$SRC" ] || { log "FAIL 找不到 $SRC"; exit 1; }

# 本地空目录 + --prune = 清空线上。宁可不同步也不能这么干。
if [ -n "$PRUNE" ] && [ -z "$(ls -A "$SRC" 2>/dev/null)" ]; then
  log "FAIL uploads/ 是空的，拒绝带 --prune 运行"
  exit 1
fi

log "开始同步 $SRC -> $BUCKET ${DRY:+(dry-run)}"

# 水位取「本趟开始的时刻」，同步全部成功之后才写进去。config.media_url() 读它来
# 判断某个文件是不是已经在 OSS 上了 —— mtime 早于水位的就是。
# 取开始而不是结束：同步途中新落盘的文件不保证被这一趟扫到，用结束时刻会把它们
# 误判成「已同步」，于是页面指向 OSS 拿 404。
T0=$(date +%s)
# ossutil 靠 mtime+size 判断要不要传，而 sync 全程可能跨好几秒；退 2 秒当安全垫，
# 避免「刚好卡在扫描那一瞬间落盘」的文件漏网。
T0=$((T0 - 2))

# ossutil 1.7.19 没有 --dry-run，所以这里自己实现：印出将要执行的命令和
# 本地待传清单，一个字节都不上传。别把 $DRY 传给 ossutil，它会报 Bad flag。
run() {
  if [ -n "$DRY" ]; then
    echo "  [dry-run] $*" | tee -a "$LOG"
  else
    "$@" 2>&1 | tail -3 | tee -a "$LOG"
  fi
}

if [ -n "$DRY" ]; then
  log "本地待同步文件（已排除 .chunks/）："
  find "$SRC" -type f -not -path "*/.chunks/*" -printf '  %s\t%P\n' | sort -k2 | tee -a "$LOG"
  log "共 $(find "$SRC" -type f -not -path "*/.chunks/*" | wc -l) 个文件，$(du -sh --exclude=.chunks "$SRC" | cut -f1)"
fi

# ossutil 的 --include/--exclude **只匹配文件名，不匹配路径**，所以
# `--exclude "*.chunks*"` 挡不住 .chunks/<uuid>/0 这种文件名叫 "0" 的对象
# （实测把 57MB 残留分片全传上去了）。改成显式按目录逐个同步：
#   - 根目录下的散文件用 --only-current-dir，不递归
#   - 子目录逐个点名，.chunks 永远不点它
# tiles/ 是 routers/share.py 渲染分享缩略图时缓存的高德底图瓦片，服务端自己读
# 本地磁盘（_TILE_DIR = UPLOAD_DIR / "tiles"），从来不经浏览器、也不经 OSS。
# 传上去只是白占空间和请求数，取不回来任何好处。
SKIP_DIRS=(".chunks" "tiles")

# 1) 根目录散文件（madrid_1.jpg 之类），1 天缓存，不递归。
run $OSSUTIL sync "$SRC/" "$BUCKET/" --only-current-dir --meta "$ONE_DAY" -u -f

# 2) 逐个子目录。variants/ 单独给一年 immutable —— 档位文件名带档位，
#    内容永不改变；其余（thumbs/ tiles/ generated/）跟原片一样给 1 天。
for dir in "$SRC"/*/; do
  name=$(basename "$dir")
  skip=""
  for sd in "${SKIP_DIRS[@]}"; do [ "$name" = "$sd" ] && skip=yes; done
  [ -n "$skip" ] && { log "跳过 $name/"; continue; }

  if [ "$name" = "variants" ]; then
    meta="$IMMUTABLE"
  else
    meta="$ONE_DAY"
  fi
  log "同步 $name/"
  run $OSSUTIL sync "$dir" "$BUCKET/$name/" --meta "$meta" -u -f
done

# 3) 清理云端孤儿对象（仅 --prune）。
#    **不能**用 `ossutil sync --delete` 干这件事：那条命令在删除的同时也会上传，
#    而 --exclude 挡不住目录（见上），实测会把 .chunks/ 的 57MB 残片一起推上去。
#
#    判据是「**数据库里没有任何行引用它**」，不是「本地没有这个文件」。
#    这两者曾经等价，现在不是了：演示账号的媒体只存在 OSS 上，本地一个文件
#    都没有（见 scripts/publish_demo_assets.py）。按本地存在与否来删，
#    第一趟就会把 39 个 demo 对象全部清空，演示账号直接变一片破图。
if [ -n "$PRUNE" ]; then
  log "prune: 从库里取全部被引用的文件名，再跟云端比对"
  REFS=$(mktemp); trap 'rm -f "$REFS"' EXIT
  if ! "$BASE/.venv/bin/python" "$BASE/ops/oss_referenced_names.py" > "$REFS"; then
    log "FAIL 取不到引用清单，拒绝 prune（宁可留孤儿，也不能瞎删）"
    exit 1
  fi
  n_ref=$(wc -l < "$REFS")
  # 库是空的 / 查询出了岔子 = 全云端都成了「孤儿」。这种时候绝不能动手。
  if [ "$n_ref" -lt 1 ]; then
    log "FAIL 引用清单是空的，拒绝 prune"
    exit 1
  fi
  log "prune: 库里引用着 $n_ref 个文件名"

  extra=0
  while read -r key; do
    [ -z "$key" ] && continue
    rel=${key#*/uploads/}
    case "$rel" in
      .chunks|.chunks/*) ;;                  # 残留分片，永远不该在云上
      */) continue ;;                        # 目录占位：0 字节，留着无害
      *)
        base=${rel##*/}                      # 去掉 thumbs/ variants/ 前缀
        grep -qxF "$base" "$REFS" && continue
        ;;
    esac
    extra=$((extra + 1))
    if [ -n "$DRY" ]; then
      echo "  [dry-run] 会删除 $rel" | tee -a "$LOG"
    else
      $OSSUTIL rm "$BUCKET/$rel" -f >/dev/null 2>&1 && log "  删除 $rel"
    fi
  done < <($OSSUTIL ls "$BUCKET/" 2>/dev/null | awk '{print $NF}' | grep "^oss://")
  log "prune: 处理了 $extra 个孤儿对象"
fi

# 只有真跑过（非 dry-run）且一路 set -e 没中断，才推进水位。
# 中途失败就保持旧水位：宁可让几个已经传上去的文件多走一会儿本机，
# 也不能把没传上去的标成「已同步」——那是实打实的 404。
if [ -z "$DRY" ]; then
  echo "$T0" > "$WATERMARK"
  log "水位推进到 $T0 ($(date -d "@$T0" '+%F %T'))"
fi

log "同步完成"
