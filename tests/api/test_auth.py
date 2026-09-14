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


def test_root_serves_landing_when_anonymous(client):
    """没登录时根路径给宣传页，不是应用壳。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "把一趟旅行" in r.text          # 宣传页 h1
    assert "login-overlay" not in r.text   # 不是应用页


def test_root_serves_app_when_logged_in(client):
    client.post("/api/register", data={"username": "lander", "password": "pw123456"})
    r = client.get("/")
    assert r.status_code == 200
    assert "login-overlay" in r.text
    assert "__AMAP_KEY__" not in r.text


def test_app_and_welcome_are_public(client):
    """/app 无论登没登录都给应用壳；/welcome 始终给宣传页。"""
    r = client.get("/app")
    assert r.status_code == 200 and "login-overlay" in r.text
    assert "no-store" in r.headers.get("cache-control", "")

    r = client.get("/welcome")
    assert r.status_code == 200 and "把一趟旅行" in r.text


def test_landing_has_no_unreplaced_placeholder(client):
    assert "__ICP_FOOTER__" not in client.get("/welcome").text


def test_verify_txt_is_public(client, tmp_path, monkeypatch):
    """站长验证文件：不登录就能取到，内容原样返回。"""
    import app as app_mod
    d = app_mod.VERIFY_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / "tm_unit_probe.txt"
    f.write_text("probe-token-123", encoding="utf-8")
    try:
        r = client.get("/tm_unit_probe.txt")
        assert r.status_code == 200
        assert r.text == "probe-token-123"
    finally:
        f.unlink(missing_ok=True)


def test_verify_txt_missing_is_404(client):
    assert client.get("/definitely_not_here.txt").status_code == 404


def test_verify_txt_rejects_traversal(client):
    """文件名白名单：带路径分隔符或点的一律不给。"""
    import app as app_mod
    assert app_mod.verify_file_path("../icp") is None
    assert app_mod.verify_file_path("a/b") is None
    assert app_mod.verify_file_path("") is None
    assert app_mod.verify_file_path("x" * 81) is None
