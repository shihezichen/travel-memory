"""鉴权：中间件白名单、注册/登录/登出、会话、越权。"""
import pytest

import app as app_mod
import db

pytestmark = pytest.mark.api


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] is True


def test_protected_route_requires_login(client):
    r = client.get("/api/trips")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_index_is_public_and_uncached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")
    assert "__AMAP_KEY__" not in r.text          # 占位符已被替换


def test_register_then_me(client):
    r = client.post("/api/register", data={"username": "newbie", "password": "pw123456",
                                           "display_name": "新人"})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "newbie"
    assert app_mod.COOKIE_NAME in r.cookies or app_mod.COOKIE_NAME in client.cookies
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "新人"
    assert me.json()["is_admin"] is False


@pytest.mark.parametrize("data,code,msg", [
    ({"username": "", "password": "pw123456"}, 400, "用户名不能为空"),
    ({"username": "x" * 33, "password": "pw123456"}, 400, "用户名过长"),
    ({"username": "ok", "password": "123"}, 400, "密码至少 6 位"),
    ({"username": "ok", "password": ""}, 400, "密码至少 6 位"),
])
def test_register_validation(client, data, code, msg):
    r = client.post("/api/register", data=data)
    assert r.status_code == code
    assert msg in r.json()["error"]


def test_register_duplicate_username(client):
    client.post("/api/register", data={"username": "dup", "password": "pw123456"})
    r = client.post("/api/register", data={"username": "dup", "password": "pw123456"})
    assert r.status_code == 409


def test_login_success_and_failure(client, make_user):
    u = make_user()
    r = client.post("/api/login", data={"username": u["username"], "password": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/login", data={"username": u["username"],
                                        "password": u["_password"]})
    assert r.status_code == 200
    assert client.get("/api/me").json()["username"] == u["username"]


def test_login_missing_fields(client):
    assert client.post("/api/login", data={}).status_code == 401


def test_admin_token_login(client):
    r = client.post("/api/login", data={"token": "test-admin-token-xyz"})
    assert r.status_code == 200
    me = client.get("/api/me").json()
    assert me["username"] == "admin" and me["is_admin"] is True


def test_wrong_admin_token_is_not_a_backdoor(client):
    assert client.post("/api/login", data={"token": "wrong"}).status_code == 401


def test_logout_invalidates_session(auth_client):
    assert auth_client.get("/api/me").status_code == 200
    assert auth_client.post("/logout").status_code == 200
    assert auth_client.get("/api/me").status_code == 401


def test_expired_session_is_rejected_by_middleware(auth_client):
    conn = db.get_conn()
    conn.execute("UPDATE sessions SET expires_at=datetime('now','localtime','-1 hours')")
    conn.commit()
    conn.close()
    assert auth_client.get("/api/trips").status_code == 401


def test_session_cookie_is_httponly(client, make_user):
    u = make_user()
    r = client.post("/api/login", data={"username": u["username"],
                                        "password": u["_password"]})
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()


def test_last_trip_roundtrip(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    assert auth_client.post("/api/me/last-trip", data={"trip_id": tid}).status_code == 200
    assert auth_client.get("/api/me").json()["last_trip_id"] == tid


def test_last_trip_rejects_other_users_trip(auth_client, other_client, seeded_trip):
    r = other_client.post("/api/me/last-trip", data={"trip_id": seeded_trip["trip_id"]})
    assert r.status_code == 404


# ---------------- 访问口令门禁（方案 A） ----------------
def test_gate_disabled_by_default(client):
    assert client.get("/api/gate/status").json() == {"enabled": False}


def test_gate_blocks_then_lets_through(client, monkeypatch, make_user):
    monkeypatch.setattr(app_mod, "GATE_HASH", app_mod.gate_hash_of("openme"))
    u = make_user()
    client.post("/api/login", data={"username": u["username"], "password": u["_password"]})

    r = client.get("/api/trips")
    assert r.status_code == 401 and r.json() == {"error": "gate_required"}
    assert client.get("/api/gate/status").status_code == 401

    assert client.post("/api/gate", data={"password": "nope"}).status_code == 403
    assert client.post("/api/gate", data={"password": "openme"}).status_code == 200
    assert client.get("/api/trips").status_code == 200
    assert client.get("/api/gate/status").json() == {"enabled": True, "ok": True}
