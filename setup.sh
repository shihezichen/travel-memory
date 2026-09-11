#!/usr/bin/env bash
# 兼容垫片：装和跑已经合并进 ./start.sh，这个文件只是把老习惯转接过去。
# （留着是因为老文档、老笔记、别人的部署脚本里可能还写着 ./setup.sh。）
cd "$(dirname "$0")"
printf '\033[1;33m[!]\033[0m setup.sh 已并入 start.sh —— 直接跑 ./start.sh 就行，首次会自动装环境。\n' >&2
exec ./start.sh --setup-only "$@"
