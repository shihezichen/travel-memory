"""开发者控制台 + /api/ai/* 领域 API：Key 生命周期、scope 分级、限流、幂等、审计。"""
import pytest

import db
from domain import rate_limit

pytestmark = pytest.mark.api


@pytest.fixture
def key_factory(auth_client):
    """用登录态自助创建 API Key，返回 raw key。"""
    def _make(scope="read", name="test-key", expire_days=0):
        r = auth_client.post("/api/console/keys",
                             data={"name": name, "scope": scope,
                                   "expire_days": expire_days})
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
