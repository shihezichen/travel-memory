#!/usr/bin/env python3
"""MCP server — 把旅行记忆领域 API 暴露为 MCP 标准工具（本机 stdio 模式）

让其他 agent（爱马仕总控 / Claude Desktop / Cursor / 任意 MCP 客户端）
通过 MCP 标准协议消费「旅程搭子」的读写能力，无需知道内部 HTTP API。

两种接入模式：
  ① 远程（推荐，多用户开放平台）：POST /mcp，Streamable HTTP transport，
     每请求用调用者自己的 API Key 认证 → 见 mcp_http.py。
  ② 本机（本文件）：MCP over stdio，单用户（环境变量里一个 Key）。

协议：MCP over stdio，JSON-RPC 2.0，newline-delimited JSON。
工具定义与能力直接复用 companion.py（领域 API 客户端 + function-calling schema）。

用法：
  python3 mcp_server.py              # 由 MCP 客户端拉起（stdio 交互）

环境变量：
  COMPANION_API_KEY  API Key（写操作需 write scope，读操作 read scope 即可）
  TRAVEL_BASE_URL    领域 API 地址（默认 http://127.0.0.1:8084）
"""
from __future__ import annotations

import json
import sys

import companion  # 复用领域 API 客户端 + TOOLS / TOOL_FNS

SERVER_INFO = {"name": "travel-memory", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _to_mcp_tools() -> list:
    """把 companion.py 的 OpenAI function-calling 格式转成 MCP 标准 inputSchema 格式。"""
    tools = []
    for t in companion.TOOLS:
        fn = t.get("function", {})
        tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return tools


def _handle(method: str, params: dict, msg_id) -> dict | None:
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _to_mcp_tools()}}
    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        fn = companion.TOOL_FNS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": _text_result(f"未知工具：{name}", True)}
        try:
            result = fn(**args)
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": _text_result(json.dumps(result, ensure_ascii=False))}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": _text_result(f"工具执行失败：{e}", True)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method and method.startswith("notifications/"):
        return None  # 通知无需响应
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"未知方法：{method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg.get("method"), msg.get("params", {}), msg.get("id"))
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
