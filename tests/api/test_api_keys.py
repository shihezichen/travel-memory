"""开发者控制台 + /api/ai/* 领域 API：Key 生命周期、scope 分级、限流、幂等、审计。"""
import hashlib
import itertools

import pytest

import db
from domain import rate_limit

pytestmark = pytest.mark.api


@pytest.fixture
def key_factory(auth_client):
    """用登录态自助创建 API Key，返回 raw key。

    默认名字带自增后缀：同名的有效 Key 现在会被接口挡掉（见 test_duplicate_*），
    夹具再用一个固定名字，一个用例里建第二把就炸了。
    """
    seq = itertools.count(1)

    def _make(scope="read", name=None, expire_days=0):
        r = auth_client.post("/api/console/keys",
                             data={"name": name or f"test-key-{next(seq)}",
                                   "scope": scope, "expire_days": expire_days})
        assert r.status_code == 200, r.text
        return r.json()
    return _make


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


# ---------------- 控制台 ----------------
def test_console_requires_login(client):
    assert client.get("/api/console/keys").status_code == 401
    assert client.get("/console").status_code == 401


def test_create_key_returns_raw_once_and_never_again(auth_client, key_factory):
    made = key_factory()
    assert made["key"].startswith(made["prefix"])
    listed = auth_client.get("/api/console/keys").json()["keys"]
    assert len(listed) == 1
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["key_prefix"] == made["prefix"]


def test_non_admin_cannot_grant_admin_scope(auth_client):
    r = auth_client.post("/api/console/keys", data={"name": "x", "scope": "admin"})
    assert r.status_code == 403


def test_scope_is_normalised_on_create(key_factory):
    assert key_factory(scope="write,read,write")["scope"] == "write,read"
    assert key_factory(scope="bogus")["scope"] == "read"


def test_delete_key(auth_client, key_factory):
    kid = key_factory()["id"]
    assert auth_client.delete(f"/api/console/keys/{kid}").status_code == 200
    assert auth_client.get("/api/console/keys").json()["keys"] == []
    assert auth_client.delete(f"/api/console/keys/{kid}").status_code == 404


def test_cannot_delete_someone_elses_key(auth_client, other_client, key_factory):
    kid = key_factory()["id"]
    assert other_client.delete(f"/api/console/keys/{kid}").status_code == 404


def test_console_audit_and_usage(auth_client, key_factory):
    key_factory(name="审计用")
    audit = auth_client.get("/api/console/audit").json()["audit"]
    assert any(a["action"] == "key.create" for a in audit)
    assert auth_client.get("/api/console/usage").status_code == 200


# ---------------- /api/ai/* 鉴权 ----------------
def test_ai_endpoint_requires_bearer(client):
    r = client.get("/api/ai/trips")
    assert r.status_code == 401
    assert "API Key 无效" in r.json()["detail"]


@pytest.mark.parametrize("hdr", [{"Authorization": "Bearer bogus"},
                                 {"Authorization": "Basic abc"},
                                 {"Authorization": "bogus"}])
def test_ai_endpoint_rejects_bad_auth(client, hdr):
    assert client.get("/api/ai/trips", headers=hdr).status_code == 401


def test_ai_list_trips_with_read_key(auth_client, client, key_factory, seeded_trip):
    raw = key_factory(scope="read")["key"]
    r = client.get("/api/ai/trips", headers=_bearer(raw))
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["trips"]]
    assert names == ["测试行程"]


def test_read_key_cannot_write(client, key_factory):
    raw = key_factory(scope="read")["key"]
    r = client.post("/api/ai/trips", headers=_bearer(raw), data={"name": "偷偷建的"})
    assert r.status_code == 401
    assert "缺少 write 权限" in r.json()["detail"]


def test_write_key_can_create_trip_and_point(client, key_factory):
    raw = key_factory(scope="read,write")["key"]
    r = client.post("/api/ai/trips", headers=_bearer(raw),
                    data={"name": "Agent 建的行程", "start_date": "2026-07-01"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    rp = client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(raw),
                     data={"name": "某地", "lat": "30.0", "lng": "120.0"})
    assert rp.status_code == 200, rp.text
    got = client.get("/api/ai/trips", headers=_bearer(raw)).json()["trips"]
    assert any(t["id"] == tid for t in got)


@pytest.mark.parametrize("path_tpl", [
    "/api/ai/trips/{tid}/export",
    "/api/ai/trips/{tid}/stats",
    "/api/ai/trips/{tid}/generations",
    "/api/ai/trips/{tid}/meta",
    "/api/ai/trips/{tid}/share-count",
    "/api/ai/trips/{tid}/reinterpret",   # 越权检查在 domain/reinterpret.py 里
])
def test_api_key_cannot_read_other_users_trip(client, other_client, key_factory,
                                              seeded_trip, path_tpl):
    """A 的 Key 不能读 B 的行程。"""
    raw = key_factory(scope="read,write")["key"]
    tid_other = other_client.post("/api/trips", data={"name": "别人的"}).json()["id"]
    other_client.post(f"/api/trips/{tid_other}/points",
                      data={"name": "秘密地点", "lat": 1.0, "lng": 2.0})
    r = client.get(path_tpl.format(tid=tid_other), headers=_bearer(raw))
    assert r.status_code == 404, r.text


def test_api_key_must_not_read_other_users_trip_memory(client, other_client,
                                                       key_factory):
    """2026-09-10 修复：ai_get_memory 漏了 _key_is_owner，可按 trip_id 越权读全文。

    状态码接受 403/404 两种：仓库里另外 19 个同类路由一律返回 403「无权访问该行程」，
    这里跟随既有约定；404（连存在性都不透露）也是可接受的更严做法。
    真正要守住的是下面那条——别人的内容一个字都不能出现在响应里。
    """
    raw = key_factory(scope="read")["key"]
    tid_other = other_client.post("/api/trips", data={"name": "别人的"}).json()["id"]
    other_client.post(f"/api/trips/{tid_other}/points",
                      data={"name": "秘密地点", "lat": 1.0, "lng": 2.0})
    r = client.get(f"/api/ai/trips/{tid_other}/memory", headers=_bearer(raw))
    assert r.status_code in (403, 404), r.text
    assert "秘密地点" not in r.text


def test_key_scope_share(client, key_factory, seeded_trip):
    tid = seeded_trip["trip_id"]
    read_only = key_factory(scope="read")["key"]
    assert client.post(f"/api/ai/trips/{tid}/share",
                       headers=_bearer(read_only)).status_code == 401
    sharer = key_factory(scope="read,share")["key"]
    r = client.post(f"/api/ai/trips/{tid}/share", headers=_bearer(sharer))
    assert r.status_code == 200 and r.json()["token"]
    assert client.get(f"/api/ai/trips/{tid}/share-count",
                      headers=_bearer(sharer)).json()["count"] == 1


def test_revoked_key_is_rejected(auth_client, client, key_factory):
    made = key_factory(scope="read")
    assert client.get("/api/ai/trips", headers=_bearer(made["key"])).status_code == 200
    auth_client.delete(f"/api/console/keys/{made['id']}")
    assert client.get("/api/ai/trips", headers=_bearer(made["key"])).status_code == 401


def test_expired_key_is_rejected(client, key_factory):
    made = key_factory(scope="read", expire_days=1)
    conn = db.get_conn()
    conn.execute("UPDATE api_keys SET expires_at=datetime('now','localtime','-1 days')")
    conn.commit()
    conn.close()
    assert client.get("/api/ai/trips", headers=_bearer(made["key"])).status_code == 401


# ---------------- 限流 ----------------
def test_rate_limit_returns_429_with_retry_after(client, key_factory, monkeypatch):
    monkeypatch.setattr(rate_limit, "READ_LIMIT", 3)
    raw = key_factory(scope="read")["key"]
    codes = [client.get("/api/ai/trips", headers=_bearer(raw)).status_code
             for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    r = client.get("/api/ai/trips", headers=_bearer(raw))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# ---------------- 写幂等 ----------------
def test_write_is_idempotent_by_client_request_id(client, key_factory):
    raw = key_factory(scope="read,write")["key"]
    payload = {"name": "只应存在一次", "client_request_id": "req-001"}
    r1 = client.post("/api/ai/trips", headers=_bearer(raw), data=payload)
    r2 = client.post("/api/ai/trips", headers=_bearer(raw), data=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    names = [t["name"] for t in client.get("/api/ai/trips",
                                           headers=_bearer(raw)).json()["trips"]]
    assert names.count("只应存在一次") == 1


# ---------------- 审计 ----------------
def test_ai_calls_are_audited(auth_client, client, key_factory):
    raw = key_factory(scope="read")["key"]
    client.get("/api/ai/trips", headers=_bearer(raw))
    actions = [a["action"] for a in auth_client.get("/api/console/audit").json()["audit"]]
    assert "trips.list" in actions or any(a.startswith("trip") for a in actions), actions


# ---------------- 地理编码离线降级 ----------------
def test_ai_geocode_degrades_offline(client, key_factory):
    raw = key_factory(scope="read")["key"]
    r = client.get("/api/ai/geocode", headers=_bearer(raw),
                   params={"address": "杭州西湖"})
    assert r.status_code in (200, 404, 502), r.text
    if r.status_code == 200:
        assert r.json().get("result") in (None, {}) or r.json().get("found") is False


# ---------------- 控制台：手机端改版带来的接口行为 ----------------
def test_write_scope_implies_read(key_factory):
    """只有 write 的 Key 写得进读不出 —— 建的人自己都查不了自己写的数据。"""
    assert "read" in key_factory(scope="write")["scope"].split(",")
    assert "read" in key_factory(scope="write,share")["scope"].split(",")


def test_audit_can_be_filtered_by_key(auth_client, key_factory):
    """日志页要能按 Key 分别看，所以接口得支持 key_id 过滤。"""
    a = key_factory(name="key-a", scope="read")
    b = key_factory(name="key-b", scope="read")
    auth_client.get("/api/ai/trips", headers=_bearer(a["key"]))
    auth_client.get("/api/ai/trips", headers=_bearer(b["key"]))
    auth_client.get("/api/ai/trips", headers=_bearer(b["key"]))

    only_b = auth_client.get(f"/api/console/audit?key_id={b['id']}").json()["audit"]
    assert len(only_b) == 2
    assert {r["api_key_id"] for r in only_b} == {b["id"]}
    # 日志卡片上要显示「这条是哪个 Key 调的」，所以结果里得带 Key 名字
    assert {r["key_name"] for r in only_b} == {"key-b"}

    all_rows = auth_client.get("/api/console/audit").json()["audit"]
    assert len([r for r in all_rows if r["api_key_id"] in (a["id"], b["id"])]) == 3


def test_audit_filter_rejects_other_users_key(auth_client, other_client):
    mine = other_client.post("/api/console/keys",
                             data={"name": "别人的", "scope": "read"}).json()
    assert auth_client.get(f"/api/console/audit?key_id={mine['id']}").status_code == 404


def test_console_audit_of_key_less_actions_still_listed(auth_client, key_factory):
    """建 Key 这种控制台操作没有 api_key_id，不能因为加了 JOIN 就从「全部」里漏掉。"""
    key_factory(name="k")
    rows = auth_client.get("/api/console/audit").json()["audit"]
    created = [r for r in rows if r["action"] == "key.create"]
    assert created and created[0]["api_key_id"] is None
    assert created[0]["key_name"] is None


# ---------------- 审计日志：请求来自哪里 ----------------
def test_audit_records_client_ip_from_forwarded_header(auth_client, key_factory):
    """链路是 公网 → frps → frpc → uvicorn，传输层地址永远是隧道本地地址，
    所以真实来源只能从 X-Forwarded-For 拿。"""
    k = key_factory(scope="read")
    auth_client.get("/api/ai/trips",
                    headers={**_bearer(k["key"]), "X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    row = auth_client.get(f"/api/console/audit?key_id={k['id']}").json()["audit"][0]
    assert row["client_ip"] == "203.0.113.7"        # 取最左一段 = 最初的客户端


def test_audit_falls_back_to_real_ip_then_peer(auth_client, key_factory):
    k = key_factory(scope="read")
    auth_client.get("/api/ai/trips",
                    headers={**_bearer(k["key"]), "X-Real-IP": "198.51.100.9"})
    rows = auth_client.get(f"/api/console/audit?key_id={k['id']}").json()["audit"]
    assert rows[0]["client_ip"] == "198.51.100.9"

    auth_client.get("/api/ai/trips", headers=_bearer(k["key"]))
    rows = auth_client.get(f"/api/console/audit?key_id={k['id']}").json()["audit"]
    assert rows[0]["client_ip"]                      # 没有头就退回传输层地址，不能是空


def test_console_key_actions_also_record_ip(auth_client):
    auth_client.post("/api/console/keys", data={"name": "ip-test", "scope": "read"},
                     headers={"X-Forwarded-For": "203.0.113.20"})
    rows = auth_client.get("/api/console/audit").json()["audit"]
    created = [r for r in rows if r["action"] == "key.create"][0]
    assert created["client_ip"] == "203.0.113.20"


def test_mcp_call_forwards_caller_ip_not_localhost(client, auth_client, monkeypatch):
    """MCP 走的是自调用（127.0.0.1 → 自己）。不透传的话每条 MCP 记录都是本机地址，
    等于没记 —— 而 MCP 恰恰是远程 agent 的主入口。"""
    import mcp_http
    made = auth_client.post("/api/console/keys",
                            data={"name": "mcp-ip", "scope": "read"}).json()

    def _in_process(method, path, api_key, form=None, json_body=None, client_ip=None):
        headers = {"Authorization": f"Bearer {api_key}"}
        if client_ip:
            headers["X-Forwarded-For"] = client_ip
        r = client.request(method, path, headers=headers, data=form or None)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})

    monkeypatch.setattr(mcp_http, "_call_domain_api", _in_process)
    client.post("/mcp", headers={**_bearer(made["key"]), "X-Forwarded-For": "203.0.113.55"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "list_trips", "arguments": {}}})
    row = auth_client.get(f"/api/console/audit?key_id={made['id']}").json()["audit"][0]
    assert row["client_ip"] == "203.0.113.55"


# ---------------- Key 重名 ----------------
def test_duplicate_active_key_name_is_rejected(auth_client, key_factory):
    key_factory(name="Cursor 接入")
    r = auth_client.post("/api/console/keys",
                         data={"name": "Cursor 接入", "scope": "read"})
    assert r.status_code == 400
    assert "换个名字" in r.json()["detail"]
    assert len(auth_client.get("/api/console/keys").json()["keys"]) == 1


@pytest.mark.parametrize("dupe", ["cursor 接入", "  Cursor 接入  ", "CURSOR 接入"])
def test_duplicate_check_ignores_case_and_padding(auth_client, key_factory, dupe):
    """「Cursor」和「cursor 」看着一样，就该算一个。"""
    key_factory(name="Cursor 接入")
    assert auth_client.post("/api/console/keys",
                            data={"name": dupe, "scope": "read"}).status_code == 400


def test_name_is_free_again_after_the_key_is_deleted(auth_client, key_factory):
    made = key_factory(name="临时调试")
    auth_client.delete(f"/api/console/keys/{made['id']}")
    assert auth_client.post("/api/console/keys",
                            data={"name": "临时调试", "scope": "read"}).status_code == 200


def test_expired_key_does_not_hold_its_name(auth_client, key_factory):
    """过期的 Key 还留在列表里（历史要能查），但名字该让出来。"""
    made = key_factory(name="过期了的")
    conn = db.get_conn()
    conn.execute("UPDATE api_keys SET expires_at=datetime('now','localtime','-1 day') WHERE id=?",
                 (made["id"],))
    conn.commit()
    conn.close()
    assert auth_client.post("/api/console/keys",
                            data={"name": "过期了的", "scope": "read"}).status_code == 200


def test_revoked_key_does_not_hold_its_name(auth_client, key_factory):
    made = key_factory(name="吊销了的")
    db.revoke_api_key(made["id"], auth_client.user["id"])
    assert auth_client.post("/api/console/keys",
                            data={"name": "吊销了的", "scope": "read"}).status_code == 200


def test_duplicate_name_is_scoped_per_user(auth_client, other_client):
    """重名只在自己名下算数 —— 别人叫什么跟我没关系。"""
    auth_client.post("/api/console/keys", data={"name": "同名", "scope": "read"})
    assert other_client.post("/api/console/keys",
                             data={"name": "同名", "scope": "read"}).status_code == 200


def test_unnamed_keys_also_collide(auth_client):
    """不填名字会落成「未命名」，第二把同样会被挡下来 —— 正好逼着起个名。"""
    assert auth_client.post("/api/console/keys", data={"scope": "read"}).status_code == 200
    r = auth_client.post("/api/console/keys", data={"scope": "read"})
    assert r.status_code == 400 and "未命名" in r.json()["detail"]


# ---------------- Key 明文不留在服务器上 ----------------
def test_server_keeps_no_recoverable_copy_of_the_key(auth_client, key_factory):
    """只存 sha256。2026-09-16 一度加过一列加密存档（为了「验密码后取回」），
    已经撤掉 —— 服务器上不该有任何能还原出 Key 明文的东西。"""
    made = key_factory()
    conn = db.get_conn()
    row = dict(conn.execute("SELECT * FROM api_keys WHERE id=?", (made["id"],)).fetchone())
    conn.close()
    blob = " ".join(str(v) for v in row.values())
    assert made["key"] not in blob
    assert row["key_hash"] == hashlib.sha256(made["key"].encode()).hexdigest()
    assert not row.get("key_enc")          # 列可能还在（sqlite 不好删），但必须是空的


def test_reveal_endpoint_is_gone(auth_client, key_factory):
    """取回接口已经撤掉：留着它就等于承认服务器手里有明文。"""
    made = key_factory()
    r = auth_client.post(f"/api/console/keys/{made['id']}/reveal",
                         data={"password": auth_client.user["_password"]})
    assert r.status_code == 404


def test_console_page_carries_a_version_stamp(auth_client):
    """页脚的版本标识：改了东西却还看到老样子时，用它分辨是没生效还是浏览器拿着旧页面。"""
    html = auth_client.get("/console").text
    assert "__CONSOLE_VER__" not in html          # 占位符必须被替换掉
    import re
    assert re.search(r'console-ver">版本 \d\d-\d\d \d\d:\d\d<', html), html[-400:]
