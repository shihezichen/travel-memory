#!/bin/bash
# 旅行记忆系统 — 启动脚本
cd "$(dirname "$0")"
PORT=8084
exec python3 -m uvicorn app:app --host 0.0.0.0 --port $PORT
