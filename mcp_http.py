#!/usr/bin/env python3
"""MCP Streamable HTTP 远程端点 — 开放平台「远程 Agent 接入」通道

让任意远程 Agent（爱马仕总控 / Claude Desktop / Cursor / 任意 MCP 客户端）
通过标准 MCP 协议（Streamable HTTP transport）接入旅行记忆系统。

与 stdio 版（mcp_server.py）的关键区别：
- 每个 HTTP 请求用调用者**自己的 API Key**（Authorization: Bearer tm_xxx）认证
- tools/call 翻译成对领域 API（/api/ai/*）的调用，**携带该请求者的 key**
  → 数据隔离由领域 API 层的 _key_is_owner 保证：用户 A 的 agent 永远只能
    读写用户 A 自己的行程，天然多租户安全。
- 无状态：每个请求独立鉴权处理，不维护 session（stateless server，规范允许）

端点：POST /mcp
协议：JSON-RPC 2.0 over HTTP（MCP Streamable HTTP transport, 2025-03-26）
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import companion  # 复用 TOOLS 定义（工具清单与描述）

SERVER_INFO = {"name": "travel-memory", "version": "1.1.0"}
PROTOCOL_VERSION = "2025-03-26"

# ---------- 工具 → 领域 API 路由表 ----------
# 每项: (method, path_template, required_scope, path_params, query_params, form_params)
# path_params 从调用参数取值并替换进 path 的 {xxx} 占位符；
# query_params 作为 URL query；form_params 作为表单字段（仅 POST/PUT/DELETE）。
TOOL_ROUTES = {
    "list_trips":       ("GET",    "/api/ai/trips",                    "read",  [], [], []),
    "get_memory":       ("GET",    "/api/ai/trips/{trip_id}/memory",   "read",  ["trip_id"], [], []),
    "get_memory_overview": ("GET", "/api/ai/memory",                   "read",  [], [], []),
    "export_memory":     ("GET",    "/api/ai/memory/export",           "read",  [], [], []),
    "import_memory":     ("POST",   "/api/ai/memory/import",           "write", [], [], []),
    "discover_patterns": ("GET",    "/api/ai/patterns",                "read",  [], [], []),
    "find_unfinished_journeys": ("GET", "/api/ai/unfinished",          "read",  [], [], []),
    "annual_travel_report": ("GET",    "/api/ai/wrapped",              "read",  [], ["year"], []),
    "shared_memory":     ("GET",    "/api/ai/shared",                  "read",  [], ["with_username"], []),
    "reinterpret_trip":  ("GET",    "/api/ai/trips/{trip_id}/reinterpret", "read", ["trip_id"], ["lens"], []),
    "travel_biography":   ("GET",    "/api/ai/biography",                   "read",  [], [], []),
    "time_travel":        ("GET",    "/api/ai/timetravel",                  "read",  [], ["mode", "date", "trip_id"], []),
    "recommend_destinations": ("POST", "/api/ai/recommend",             "read",  [], [], ["count", "style"]),
    "recall_memory":    ("GET",    "/api/ai/recall",                   "read",  [], ["lookahead_days"], []),
    "get_associations": ("GET",    "/api/ai/associations",             "read",  [], ["trip_id", "limit"], []),
    "get_preferences":  ("GET",    "/api/ai/preferences",              "read",  [], [], []),
    "search_memory":    ("POST",   "/api/ai/search",                   "read",  [], [], ["query", "limit"]),
    "ask_memory":       ("POST",   "/api/ai/ask",                      "read",  [], [], ["question", "limit"]),
    "generate":         ("POST",   "/api/ai/generate",                 "read",  [], [], ["kind", "trip_id", "style", "language"]),
    "create_trip":      ("POST",   "/api/ai/trips",                    "write", [], [], ["name", "start_date", "end_date", "description", "status", "client_request_id"]),
    "update_trip_status": ("PUT",  "/api/ai/trips/{trip_id}",          "write", ["trip_id"], [], ["status"]),
    "geocode":          ("GET",    "/api/ai/geocode",                  "read",  [], ["address"], []),
    "add_point":        ("POST",   "/api/ai/trips/{trip_id}/points",   "write", ["trip_id"], [], ["name", "lat", "lng", "start_date", "end_date", "description", "route_mode", "is_waypoint", "client_request_id"]),
    "update_point":     ("PUT",    "/api/ai/points/{point_id}",        "write", ["point_id"], [], ["name", "lat", "lng", "start_date", "end_date", "description", "route_mode", "is_waypoint"]),
    "delete_point":     ("DELETE", "/api/ai/points/{point_id}",        "write", ["point_id"], [], []),
    "delete_trip":      ("DELETE", "/api/ai/trips/{trip_id}",          "write", ["trip_id"], [], []),
    "export_trip":      ("GET",    "/api/ai/trips/{trip_id}/export",   "read",  ["trip_id"], [], []),
    "get_stats":        ("GET",    "/api/ai/trips/{trip_id}/stats",    "read",  ["trip_id"], [], []),
    "list_media":       ("GET",    "/api/ai/points/{point_id}/media",  "read",  ["point_id"], [], []),
    "upload_media":     ("POST",   "/api/ai/points/{point_id}/media",  "write", ["point_id"], [], ["data_base64", "filename", "content_type"]),
    "delete_media":     ("DELETE", "/api/ai/media/{media_id}",         "write", ["media_id"], [], []),
    "create_share":     ("POST",   "/api/ai/trips/{trip_id}/share",    "share", ["trip_id"], [], ["expire_days", "password"]),
    "delete_share":     ("DELETE", "/api/ai/trips/{trip_id}/share",    "share", ["trip_id"], [], []),
    "share_count":      ("GET",    "/api/ai/trips/{trip_id}/share-count", "read", ["trip_id"], [], []),
}

# 工具名 → 所需 scope（供 tools/call 鉴权；与路由表一致，便于快速查询）
TOOL_SCOPE = {name: spec[2] for name, spec in TOOL_ROUTES.items()}

# 写操作默认值（对齐 companion.py 的调用习惯）
_DEFAULTS = {
    "generate": {"style": "旅行者视角", "language": "zh"},
    "create_trip": {"status": "planning"},
    "update_trip_status": {"status": "planning"},
    "add_point": {"route_mode": "drive", "is_waypoint": "0"},
    "update_point": {"route_mode": "drive", "is_waypoint": "0"},
}


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def to_mcp_tools() -> list:
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


def _call_domain_api(method: str, path: str, api_key: str, form: dict | None = None,
                     json_body=None):
    """用调用者自己的 API Key 调领域 API。返回 (status, body_dict)。"""
    url = "http://127.0.0.1:8084" + path
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    elif form:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:300]
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw}


def call_tool(name: str, args: dict, api_key: str) -> dict:
    """执行 MCP 工具调用：翻译成领域 API 请求并携带调用者 key。"""
    route = TOOL_ROUTES.get(name)
    if route is None:
        return _text_result(f"未知工具：{name}", True)
    method, path_tpl, _scope, path_params, query_params, form_params = route

    # 1) path 参数替换
    path = path_tpl
    for p in path_params:
        if p not in args or args[p] in (None, ""):
            return _text_result(f"缺少参数：{p}", True)
        path = path.replace("{" + p + "}", str(args[p]))

    # 2) query 参数
    query = []
    for q in query_params:
        if q in args and args[q] not in (None, ""):
            query.append(f"{q}={urllib.parse.quote(str(args[q]))}")
    if query:
        path += "?" + "&".join(query)

    # 3) form 参数（合并默认值）
    merged = dict(_DEFAULTS.get(name, {}))
    for f in form_params:
        if f in args and args[f] is not None:
            merged[f] = args[f]

    # 4) import_memory 用 JSON body 传完整 bundle（避免 form 转义大对象）
    json_body = None
    if name == "import_memory":
        bundle = args.get("bundle")
        if not isinstance(bundle, dict):
            return _text_result("缺少参数：bundle（需为完整导出对象）", True)
        json_body = bundle
        status, body = _call_domain_api(method, path, api_key, json_body=json_body)
    else:
        status, body = _call_domain_api(method, path, api_key, merged or None)

    if status >= 400:
        detail = body.get("detail") or body.get("error") or json.dumps(body, ensure_ascii=False)
        return _text_result(f"领域 API {status}：{detail}", True)
    return _text_result(json.dumps(body, ensure_ascii=False))


def handle_jsonrpc(msg: dict, api_key: str) -> dict | None:
    """处理单条 JSON-RPC 消息。返回响应 dict；通知返回 None。"""
    method = msg.get("method")
    params = msg.get("params") or {}
    msg_id = msg.get("id")

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
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": to_mcp_tools()}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(name, args, api_key)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method and method.startswith("notifications/"):
        return None  # 通知无需响应
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"未知方法：{method}"}}


def auth_error_response(msg_id) -> dict:
    """鉴权失败统一错误（-32001：未认证 / -32002：scope 不足）。"""
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32001, "message": "invalid_api_key"}}


def scope_error_response(msg_id, tool: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32002,
                      "message": f"工具 {tool} 需要 {TOOL_SCOPE.get(tool, '?')} scope，当前 Key 权限不足"}}
