"""MCP 开放平台：34 个工具的可用性回归。

为什么要有这一层
----------------
`/mcp` 是给远程 Agent 用的唯一入口，而它到领域 API 之间隔着一张手写的路由表
（mcp_http.TOOL_ROUTES）。这张表和 `routers/agent_api.py` 里的真实路由之间
**没有任何编译期约束**：改个端点路径、换个 Form 字段名、调整一次 scope，
工具就哑了，而 Web UI 一点事都没有（前端从不调 /api/ai/*）。
所以这里做三件事：

1. 目录一致性 —— 工具清单（companion.TOOLS）和路由表两边不许有孤儿；
2. 路由表指向的每个 (method, path) 必须真的注册在 FastAPI 上；
3. 34 个工具**逐个真跑一遍** tools/call，走完整链路：鉴权 → scope → 参数拼装
   → 领域 API → 结果。跑不通的当场就红。

第 3 步把 mcp_http._call_domain_api 换成「打进同一个 TestClient」的实现：
生产实现是 HTTP 自调用（见 mcp_http.BASE_URL），测试里没有监听端口，
但除了那一跳传输，路径拼装 / 参数 / scope / 处理函数全都是真的。
自调用地址本身另有一条回归（test_base_url_is_configurable）。
"""
import base64
import importlib
import io

import pytest

import companion
import mcp_http

pytestmark = pytest.mark.api

EXPECTED_TOOL_COUNT = 34


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


# --------------------------------------------------------------------------
# 1) 目录一致性
# --------------------------------------------------------------------------
def test_tool_catalog_matches_route_table():
    names = [t["function"]["name"] for t in companion.TOOLS]
    assert len(names) == len(set(names)), "companion.TOOLS 里有重名工具"
    assert set(names) == set(mcp_http.TOOL_ROUTES), (
        f"清单与路由表不一致：只在清单里={sorted(set(names) - set(mcp_http.TOOL_ROUTES))} "
        f"只在路由表里={sorted(set(mcp_http.TOOL_ROUTES) - set(names))}")
    assert len(names) == EXPECTED_TOOL_COUNT, (
        f"工具数变成了 {len(names)}（README / MCP-ACCESS.md 里写的是 "
        f"{EXPECTED_TOOL_COUNT} 个，改了工具就一起改文档）")


def test_every_tool_has_usable_schema():
    for t in mcp_http.to_mcp_tools():
        assert t["name"], t
        assert t["description"].strip(), f"{t['name']} 没有描述，Agent 无从判断该不该用它"
        schema = t["inputSchema"]
        assert schema.get("type") == "object", f"{t['name']} 的 inputSchema 不是 object"
        # 路由表里声明要用的参数，schema 里必须列出来，否则 Agent 根本不会传
        _m, _p, _scope, path_params, query_params, form_params = \
            mcp_http.TOOL_ROUTES[t["name"]]
        props = set((schema.get("properties") or {}).keys())
        declared = set(path_params) | set(query_params) | set(form_params)
        # bundle 走 JSON body，不在路由表的三组参数里
        if t["name"] == "import_memory":
            declared.add("bundle")
        missing = declared - props
        assert not missing, f"{t['name']} 的 inputSchema 少了参数 {sorted(missing)}"


def test_every_tool_field_is_documented():
    """每个参数都得有一句人话说明。

    Agent 只能看见 schema —— 字段光有个 `{"type": "integer"}`，它就只能靠字段名
    猜：id 从哪来？日期什么格式？不传会怎样？线上那次「芒康」建点，日期丢了正是
    因为没人告诉它「不传 start_date 这个点就排不进时间线」。
    """
    undocumented = []
    for t in mcp_http.to_mcp_tools():
        for field, spec in ((t["inputSchema"].get("properties") or {})).items():
            if not (spec.get("description") or "").strip():
                undocumented.append(f"{t['name']}.{field}")
    assert not undocumented, "这些参数没有说明，Agent 只能靠猜：" + ", ".join(undocumented)


def test_update_tools_document_partial_semantics():
    """局部更新的语义必须写在描述里，否则 agent 会以为没传的字段会被清掉
    （或者反过来 —— 以为传了就是全量覆盖）。"""
    by_name = {t["name"]: t for t in mcp_http.to_mcp_tools()}
    desc = by_name["update_point"]["description"]
    assert "只改你传了的字段" in desc and "null" in desc


def test_every_tool_scope_is_known():
    for name, scope in mcp_http.TOOL_SCOPE.items():
        assert scope in ("read", "write", "share", "admin"), (name, scope)


# --------------------------------------------------------------------------
# 2) 路由表指向的端点真的存在
# --------------------------------------------------------------------------
def test_every_tool_maps_to_a_registered_route():
    import app as app_mod
    paths = app_mod.app.openapi()["paths"]
    broken = []
    for name, (method, path, *_rest) in mcp_http.TOOL_ROUTES.items():
        methods = {m.lower() for m in paths.get(path, {})}
        if method.lower() not in methods:
            broken.append((name, method, path))
    assert not broken, f"路由表指向了不存在的端点：{broken}"


# --------------------------------------------------------------------------
# 3) 逐个真跑
# --------------------------------------------------------------------------
@pytest.fixture
def mcp_call(client, monkeypatch):
    """把 MCP 的领域 API 自调用接进同一个 TestClient，然后返回一个 tools/call 调用器。"""
    def _in_process(method, path, api_key, form=None, json_body=None, client_ip=None):
        headers = {"Authorization": f"Bearer {api_key}"}
        if client_ip:
            headers["X-Forwarded-For"] = client_ip
        if json_body is not None:
            r = client.request(method, path, headers=headers, json=json_body)
        else:
            r = client.request(method, path, headers=headers, data=form or None)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text}

    monkeypatch.setattr(mcp_http, "_call_domain_api", _in_process)

    def _call(raw_key, tool, args=None, msg_id=1):
        r = client.post("/mcp", headers=_bearer(raw_key), json={
            "jsonrpc": "2.0", "id": msg_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        })
        return r
    return _call


@pytest.fixture
def admin_key(auth_client):
    """一把读写分享全给的 Key —— 34 个工具要一次跑完，权限得配齐。"""
    r = auth_client.post("/api/console/keys",
                         data={"name": "mcp-test", "scope": "read,write,share"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


@pytest.fixture
def stub_llm(monkeypatch):
    """LLM 一律给一句固定回复：测试不联网、不起 claude CLI，也不看模型脸色。"""
    from domain import llm_provider
    monkeypatch.setattr(llm_provider, "call",
                        lambda system, user, max_tokens=800: ("这是一段测试生成的文字。", 12))
    return True


def _result(resp):
    """从 JSON-RPC 响应里取出 MCP 结果；顺带把协议层错误暴露出来。"""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body["error"]
    return body["result"]


def _text(resp):
    res = _result(resp)
    return res["content"][0]["text"]


def test_initialize_and_tools_list(client, admin_key):
    r = client.post("/mcp", headers=_bearer(admin_key), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "travel-memory"

    r = client.post("/mcp", headers=_bearer(admin_key), json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = r.json()["result"]["tools"]
    assert len(tools) == EXPECTED_TOOL_COUNT


def test_all_tools_execute(mcp_call, admin_key, make_user, seeded_trip,
                           stub_llm, jpeg_bytes):
    """34 个工具全跑一遍，任何一个报 isError 都算回归。

    调用顺序是有讲究的：先读、再写、最后删 —— 删完了后面的读就没数据了。
    """
    tid = seeded_trip["trip_id"]
    pid = seeded_trip["point_ids"][0]
    called = set()

    def run(tool, args=None, allow_error=False):
        called.add(tool)
        res = _result(mcp_call(admin_key, tool, args))
        text = res["content"][0]["text"]
        if not allow_error:
            assert not res.get("isError"), f"工具 {tool} 执行失败：{text}"
        return res, text

    # ---- 只读：行程/统计/导出 ----
    run("list_trips")
    run("get_stats", {"trip_id": tid})
    run("export_trip", {"trip_id": tid})
    run("get_memory", {"trip_id": tid})
    run("get_memory_overview")
    run("discover_patterns")
    run("find_unfinished_journeys")
    run("annual_travel_report", {"year": 2026})
    # 共同记忆要有个「对方」—— 和自己比会被领域层挡掉（404）
    run("shared_memory", {"with_username": make_user()["username"]})
    run("travel_biography")
    run("time_travel", {"mode": "rewind", "date": "2026-05-01"})
    run("recall_memory", {"lookahead_days": 7})
    run("get_associations", {"trip_id": tid, "limit": 3})
    run("get_preferences")
    run("search_memory", {"query": "西湖", "limit": 5})
    run("ask_memory", {"question": "我去过哪些地方？", "limit": 5})
    run("reinterpret_trip", {"trip_id": tid, "lens": "成长"})
    run("recommend_destinations", {"count": 2, "style": "自然风光"})
    run("generate", {"kind": "summary", "trip_id": tid})

    # geocode 要打高德，测试里出站网络是关的 —— 只要求它「优雅地失败」，
    # 别把异常泄成 500
    _res, text = run("geocode", {"address": "杭州西湖"}, allow_error=True)
    assert "Traceback" not in text

    # ---- 导出 / 导入（import 用 JSON body，是唯一一条特殊路径）----
    _res, exported = run("export_memory")
    import json as _json
    bundle = _json.loads(exported)
    run("import_memory", {"bundle": bundle})

    # ---- 写：建行程 / 改状态 / 加点 / 改点 ----
    _res, created = run("create_trip", {"name": "MCP 建的行程", "start_date": "2026-06-01"})
    new_tid = _json.loads(created)["id"]
    run("update_trip_status", {"trip_id": new_tid, "status": "traveling"})
    _res, added = run("add_point", {"trip_id": new_tid, "name": "京都",
                                    "lat": 35.0116, "lng": 135.7681,
                                    "start_date": "2026-06-01"})
    new_pid = _json.loads(added)["id"]
    # 改名会触发重新地理编码，测试里出站网络是关的，所以显式带上坐标
    run("update_point", {"point_id": new_pid, "name": "京都站",
                         "lat": 34.9855, "lng": 135.7587})

    # ---- 媒体 ----
    run("list_media", {"point_id": pid})
    _res, up = run("upload_media", {
        "point_id": pid,
        "data_base64": base64.b64encode(jpeg_bytes()).decode(),
        "filename": "mcp.jpg", "content_type": "image/jpeg"})
    mid = _json.loads(up)["id"]
    run("delete_media", {"media_id": mid})

    # ---- 分享 ----
    run("create_share", {"trip_id": tid, "expire_days": 7})
    run("share_count", {"trip_id": tid})
    run("delete_share", {"trip_id": tid})

    # ---- 删（放最后）----
    run("delete_point", {"point_id": new_pid})
    run("delete_trip", {"trip_id": new_tid})

    missing = set(mcp_http.TOOL_ROUTES) - called
    assert not missing, f"这些工具没被覆盖到，请补用例：{sorted(missing)}"


def test_unknown_tool_is_reported_not_crashed(mcp_call, admin_key):
    res = _result(mcp_call(admin_key, "no_such_tool", {}))
    assert res["isError"] is True
    assert "未知工具" in res["content"][0]["text"]


def test_missing_path_param_is_reported(mcp_call, admin_key):
    res = _result(mcp_call(admin_key, "get_stats", {}))
    assert res["isError"] is True
    assert "trip_id" in res["content"][0]["text"]


def test_read_key_cannot_call_write_tools(client, auth_client):
    raw = auth_client.post("/api/console/keys",
                           data={"name": "ro", "scope": "read"}).json()["key"]
    write_tools = [n for n, s in mcp_http.TOOL_SCOPE.items() if s != "read"]
    assert write_tools
    for tool in write_tools:
        r = client.post("/mcp", headers=_bearer(raw), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}})
        assert r.status_code == 403, (tool, r.text)
        assert r.json()["error"]["code"] == -32002, (tool, r.text)


def test_mcp_requires_api_key(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == -32001


# --------------------------------------------------------------------------
# 4) 自调用地址：这里曾经是写死的 8084，导致生产上 34 个工具全哑
# --------------------------------------------------------------------------
def test_base_url_is_configurable(monkeypatch):
    monkeypatch.setenv("TRAVEL_BASE_URL", "https://127.0.0.1:8443")
    importlib.reload(mcp_http)
    try:
        assert mcp_http.BASE_URL == "https://127.0.0.1:8443"
    finally:
        monkeypatch.delenv("TRAVEL_BASE_URL")
        importlib.reload(mcp_http)     # 复原，别污染同进程里的其它用例
    assert mcp_http.BASE_URL == "http://127.0.0.1:8084"


def test_unreachable_domain_api_degrades_to_tool_error(client, admin_key):
    """自调用连不上时，远端 Agent 应当收到一条能看懂的工具错误，而不是 500。

    这个用例**不**接管 _call_domain_api，走的就是生产那条 urllib 路径；
    conftest 的 no_outbound_network 已经把 urlopen 换成抛 OSError。
    """
    r = client.post("/mcp", headers=_bearer(admin_key), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_trips", "arguments": {}}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["isError"] is True
    assert "不可达" in res["content"][0]["text"]
