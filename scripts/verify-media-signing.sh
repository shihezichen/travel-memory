#!/usr/bin/env bash
# 媒体签名链路体检。
#
# 为什么需要这个脚本：Bucket 改成私有之后，「签名没签上」这个故障**不会报错**
# —— config.media_url 签不出来会静默回落到裸链，前端拿到的是一串看着正常的
# OSS 地址，然后在用户浏览器里 403。服务端日志干净、接口 200、只有图是裂的。
# 2026-09-16 加固时正是靠手工实测才发现「前端还有一处自己拼裸链」。
#
# 用法：在项目根目录跑
#   scripts/verify-media-signing.sh              # 验本机 8443
#   scripts/verify-media-signing.sh https://top001.xin   # 验公网
#
# 需要一个能登录的 admin token 才能取到媒体列表；没有就只跑第 1、2 项。
set -uo pipefail
BASE="${1:-https://127.0.0.1:8443}"
CURL="curl -sk"
FAIL=0

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=1; }

# 从 media_base.txt 拿 OSS 域名（脚本不写死，跟应用同一个来源）
OSS_BASE=$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' media_base.txt 2>/dev/null | head -1 | tr -d '[:space:]')
OSS_HOST=$(printf '%s' "$OSS_BASE" | sed -e 's#https\?://##' -e 's#/.*##')

echo "== 媒体签名链路体检 =="
echo "应用地址: $BASE"
echo "OSS 域名: ${OSS_HOST:-<未配置 media_base.txt>}"
echo

# ---- 1) 裸链必须已经失效（Bucket 私有）-------------------------------
# 这是加固的目标状态。如果这条是 200，说明 Bucket 又变回公共读了。
if [ -n "$OSS_HOST" ]; then
  code=$($CURL -o /dev/null -w '%{http_code}' -m 15 \
         "$OSS_BASE/uploads/paris_1.jpg" 2>/dev/null || echo 000)
  case "$code" in
    403) ok "裸链已被拒（403）—— Bucket 是私有的" ;;
    200) bad "裸链仍可匿名下载（200）—— Bucket 又变回公共读了！" ;;
    404) say "裸链 404（找不到这个测试文件，可能本地数据不同；不算失败）" ;;
    *)   bad "裸链返回 $code，预期 403（Bucket 应是私有的）" ;;
  esac
fi

# ---- 2) 匿名列举必须被拒 --------------------------------------------
if [ -n "$OSS_HOST" ]; then
  body=$($CURL -m 15 "$OSS_BASE/?max-keys=1" 2>/dev/null || true)
  if printf '%s' "$body" | grep -q 'AccessDenied'; then
    ok "匿名列举被拒 —— 攻击者无法枚举对象"
  elif printf '%s' "$body" | grep -q '<ListBucketResult'; then
    bad "匿名可以列举 Bucket！任何人都能看到全部文件名"
  else
    say "列举结果无法判定（可能网络异常），原始响应前 80 字："
    printf '%s\n' "$body" | head -c 80
  fi
fi

# ---- 3) 应用返回的媒体 URL 必须带签名 --------------------------------
# 没有 admin token 就跳过 —— 不能为了体检去猜/造凭据。
TOKEN=$(cat token.txt 2>/dev/null | tr -d '\n' || true)
if [ -z "$TOKEN" ]; then
  echo
  say "跳过第 3、4 项：读不到 token.txt，无法取媒体列表"
  say "（要跑完整体检，请在项目根目录、且 token.txt 存在时执行）"
else
  jar=$(mktemp)
  trap 'rm -f "$jar"' EXIT
  $CURL -m 10 -X POST "$BASE/api/login" -d "token=$TOKEN" -c "$jar" -o /dev/null 2>/dev/null

  # 找一个真有媒体的点：挨个试，别假设 id=1 有东西
  json=""
  for pid in 1 2 3 4 5 6 7 8 9 10; do
    body=$($CURL -m 10 -b "$jar" "$BASE/api/points/$pid/media" 2>/dev/null || true)
    if printf '%s' "$body" | grep -q '"url"'; then json="$body"; break; fi
  done

  if [ -z "$json" ]; then
    say "没找到带媒体的点，跳过第 3、4 项"
  else
    urls=$(printf '%s' "$json" | .venv/bin/python -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
items = d if isinstance(d, list) else d.get("media", [])
for m in items[:5]:
    print(m.get("url") or "")
    print(m.get("thumb_url") or "")
' 2>/dev/null || true)

    total=0; signed=0; bad_bare=""
    while IFS= read -r u; do
      [ -z "$u" ] && continue
      total=$((total + 1))
      case "$u" in
        *Signature=*) signed=$((signed + 1)) ;;
        "$OSS_BASE"*) bad_bare="$u" ;;
      esac
    done <<< "$urls"

    if [ "$total" -eq 0 ]; then
      say "该点没有媒体 URL，跳过"
    elif [ "$signed" -eq "$total" ]; then
      ok "应用返回的 $total 条媒体 URL 全部带签名"
    else
      bad "$total 条媒体 URL 里有 $((total - signed)) 条没签名"
      [ -n "$bad_bare" ] && say "  裸链样例：$bad_bare"
      say "  -> Bucket 私有时这些链接会 403，页面裂图"
    fi

    # ---- 4) 签名 URL 必须真的能用 ------------------------------------
    first=$(printf '%s\n' "$urls" | grep 'Signature=' | head -1 || true)
    if [ -n "$first" ]; then
      code=$($CURL -o /dev/null -w '%{http_code}' -m 20 "$first" 2>/dev/null || echo 000)
      if [ "$code" = "200" ]; then
        ok "签名 URL 实测可下载（200）"
      else
        bad "签名 URL 实测返回 $code，预期 200 —— 签名算法或时钟可能有问题"
      fi
      # 篡改签名必须被拒，否则说明 Bucket 根本没在校验
      t=$(printf '%s' "$first" | sed 's/Signature=[^&]*/Signature=AAAAAAAAAAAAAAAAAAAAAAAAAAA%3D/')
      code=$($CURL -o /dev/null -w '%{http_code}' -m 20 "$t" 2>/dev/null || echo 000)
      case "$code" in
        403) ok "篡改签名被拒（403）—— 服务端确实在校验" ;;
        200) bad "篡改签名竟然通过了（200）！签名形同虚设" ;;
        *)   say "篡改签名返回 $code（预期 403，非 200 即可接受）" ;;
      esac
    fi
  fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "结论：体检通过。"
else
  echo "结论：**有项目未通过**，见上面的 ✗。"
  echo "回滚办法：把 Bucket 改回公共读可立刻恢复（签名和裸链并存，不冲突）。"
fi
exit "$FAIL"
