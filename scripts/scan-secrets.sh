#!/usr/bin/env bash
# 扫描仓库里有没有把密钥写进代码。
#
# 为什么需要它：2026-09-09 发现 companion.py 和两个 verify 脚本里各硬编码了一把
# 真实可用的 API Key，并且已经推到了 GitHub。代码审查没拦住，.gitignore 也拦不住
# ——因为这些是**源码文件**，本来就该入库，问题出在文件内容上。
#
# 用法：
#   scripts/scan-secrets.sh                 # 扫当前工作区（已跟踪的文件）
#   scripts/scan-secrets.sh --staged        # 只扫已 git add 的内容（给 pre-commit 用）
#   scripts/scan-secrets.sh --history       # 扫所有历史提交（慢，发布前跑一次）
#
# 命中返回 1，干净返回 0。
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

# 每条模式后面跟一句人话说明，命中时直接告诉人这是什么
PATTERNS=(
  'tm_[A-Za-z0-9_-]{25,}|本项目的 API Key'
  'sk-[A-Za-z0-9]{20,}|OpenAI/DeepSeek 一类的 API Key'
  'ghp_[A-Za-z0-9]{30,}|GitHub personal access token'
  'github_pat_[A-Za-z0-9_]{30,}|GitHub fine-grained token'
  'xox[baprs]-[A-Za-z0-9-]{10,}|Slack token'
  'AKIA[0-9A-Z]{16}|AWS access key id'
  'open-apis/bot/v2/hook/[A-Za-z0-9-]{20,}|飞书群机器人 webhook'
  'cli_[a-z0-9]{16,}|飞书自建应用 APP_ID'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----|私钥文件内容'
)
# 例外：模板和文档里出现的占位符/示例不算
EXCLUDE_RE='\.example$|\.md:.*(示例|example|placeholder|YOUR_|<your)|scan-secrets\.sh'

mode="${1:-worktree}"
hits=0
scan_one() {   # $1=正则 $2=说明
  local out
  case "$mode" in
    --staged)  out=$(git diff --cached -U0 --diff-filter=ACM | grep -nE "^\+.*($1)" 2>/dev/null) ;;
    --history) out=$(for c in $(git rev-list --all); do
                       git grep -nEI "$1" "$c" 2>/dev/null
                     done) ;;
    *)         out=$(git grep -nEI "$1" -- . 2>/dev/null) ;;
  esac
  out=$(echo "$out" | grep -vE "$EXCLUDE_RE" | grep -v '^$')
  if [ -n "$out" ]; then
    hits=1
    echo "❌ 疑似泄露：$2"
    echo "$out" | sed 's/^/     /' | head -8
    echo
  fi
}

for p in "${PATTERNS[@]}"; do
  scan_one "${p%%|*}" "${p##*|}"
done

if [ "$hits" -ne 0 ]; then
  cat <<'TIP'
———
密钥不该出现在仓库里的任何文件中，哪怕这个文件被 .gitignore 排除、
哪怕仓库现在是私有的。正确做法：
  · 代码里从环境变量或仓库**外**的配置文件读取
  · 仓库里只放 xxx.example 模板，写清楚要填什么
  · 已经提交过的密钥一律视为已泄露，必须吊销并换发（改代码不够）
确认是误报可以加白名单，或用 git commit --no-verify 跳过（但请先想清楚）。
TIP
  exit 1
fi
echo "✅ 未发现硬编码密钥（模式 ${#PATTERNS[@]} 条，范围：$mode）"
exit 0
