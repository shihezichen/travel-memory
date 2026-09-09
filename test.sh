#!/usr/bin/env bash
# 旅行记忆系统 —— 一个显而易见的测试入口。
#
#   ./test.sh                 快速套件：单元 + API（约 10 秒，不开浏览器、不联网）
#   ./test.sh unit            只跑单元层
#   ./test.sh api             只跑 API 集成层
#   ./test.sh e2e             只跑端到端（拉起独立 uvicorn + 系统 Chrome）
#   ./test.sh all             全量（含 E2E，约 20 秒）—— 提 PR / 收工前跑这个
#   ./test.sh secrets         扫一遍有没有硬编码密钥
#   ./test.sh install-hooks   装上提交前检查（新 clone 后跑一次）
#   ./test.sh -k 关键字 -vv   其余参数原样透传给 pytest
#
# 数据安全：测试全程用临时 SQLite + 临时 uploads，且 tests/conftest.py 劫持了
# sqlite3.connect —— 任何测试进程试图打开生产 data.db 都会直接抛异常。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ -t 1 ]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else R=''; G=''; Y=''; B=''; N=''; fi

case "${1:-}" in
  install-hooks)
    git config core.hooksPath .githooks
    chmod +x .githooks/* 2>/dev/null
    echo "${G}✓ 已启用 .githooks —— 之后每次 git commit 会自动：${N}"
    echo "    1) scripts/scan-secrets.sh --staged   扫暂存区的密钥"
    echo "    2) ./tests/run.sh                     跑快速套件"
    echo "  急事绕过： git commit --no-verify（提交后会有明显提示）"
    exit 0 ;;
  secrets)
    shift; exec ./scripts/scan-secrets.sh "$@" ;;
  -h|--help|help)
    sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
esac

./tests/run.sh "$@"
RC=$?
echo ""
if [ "$RC" -eq 0 ]; then
  echo "${G}${B}✓ 通过${N}   （已知缺陷 xfail 是预期状态，不算失败）"
else
  echo "${R}${B}✗ 没过（退出码 $RC）${N}"
  echo "${Y}  上面 'short test summary info' 里 FAILED/ERROR 开头的就是挂掉的用例。${N}"
  echo "${Y}  看某一条的详细堆栈：  ./test.sh -k <用例名> -vv --tb=long${N}"
fi
exit $RC
