"""只读分享：创建/撤销、匿名访问、密码、过期、二维码、缩略图离线降级。"""
import pytest

import db

pytestmark = pytest.mark.api


def test_create_share_and_anonymous_read(auth_client, client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    auth_client.post(f"/api/points/{seeded_trip['point_ids'][0]}/media",
                     files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")})
    r = auth_client.post(f"/api/trips/{tid}/share")
    assert r.status_code == 200 and r.json()["has_password"] is False
    token = r.json()["token"]

    # client 是全新的、未登录的会话
    body = client.get(f"/api/share/{token}").json()
    assert body["trip"]["name"] == "测试行程"
    assert [p["name"] for p in body["points"]] == ["西湖", "灵隐寺", "外滩"]
    assert len(body["points"][0]["media"]) == 1
    # 分享数据不外泄行程归属
    assert "user_id" not in body["trip"]


def test_share_page_html_renders(auth_client, client, seeded_trip):
    token = auth_client.post(
        f"/api/trips/{seeded_trip['trip_id']}/share").json()["token"]
    r = client.get(f"/share/{token}")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "__AMAP_KEY__" not in r.text


def test_share_page_unknown_token_is_404(client):
    assert client.get("/share/nope").status_code == 404
    assert client.get("/api/share/nope").status_code == 404


def test_share_is_single_link(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    t1 = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    t2 = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    assert t1 == t2
    assert auth_client.get(f"/api/trips/{tid}/share-count").json() == {"count": 1}


def test_share_password_flow(auth_client, client, seeded_trip):
    tid = seeded_trip["trip_id"]
    r = auth_client.post(f"/api/trips/{tid}/share", data={"password": "letmein"})
    assert r.json()["has_password"] is True
    token = r.json()["token"]

    assert client.get(f"/api/share/{token}").status_code == 403
    assert client.get(f"/api/share/{token}",
                      params={"password": "nope"}).status_code == 403
    ok = client.get(f"/api/share/{token}", params={"password": "letmein"})
    assert ok.status_code == 200 and ok.json()["trip"]["name"] == "测试行程"


def test_revoke_share(auth_client, client, seeded_trip):
    tid = seeded_trip["trip_id"]
    token = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    assert auth_client.delete(f"/api/trips/{tid}/share").status_code == 200
    assert client.get(f"/api/share/{token}").status_code == 404
    assert auth_client.get(f"/api/trips/{tid}/share-count").json() == {"count": 0}


def test_expired_share_is_not_readable(auth_client, client, seeded_trip):
    tid = seeded_trip["trip_id"]
    token = auth_client.post(f"/api/trips/{tid}/share",
                             data={"expire_days": 1}).json()["token"]
    conn = db.get_conn()
    conn.execute("UPDATE shares SET expires_at=datetime('now','localtime','-1 days')")
    conn.commit()
    conn.close()
    assert client.get(f"/api/share/{token}").status_code == 404


def test_share_of_missing_trip(auth_client):
    # 行程不存在 → require_owner 先判越权，返回 404
    assert auth_client.post("/api/trips/999999/share").status_code == 404


def test_share_qrcode_is_png(auth_client, client, seeded_trip):
    token = auth_client.post(
        f"/api/trips/{seeded_trip['trip_id']}/share").json()["token"]
    r = client.get(f"/api/share/{token}/qrcode")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_share_qrcode_unknown_token(client):
    assert client.get("/api/share/nope/qrcode").status_code == 404


def test_share_thumbnail_renders_offline(auth_client, client, seeded_trip):
    """地图瓦片 + OSRM 都不可达时（测试全程断网），缩略图必须仍然出图，
    退化成纯色底 + 直线连线，而不是 5xx。"""
    token = auth_client.post(
        f"/api/trips/{seeded_trip['trip_id']}/share").json()["token"]
    r = client.get(f"/api/share/{token}/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_share_thumbnail_requires_password(auth_client, client, seeded_trip):
    token = auth_client.post(f"/api/trips/{seeded_trip['trip_id']}/share",
                             data={"password": "pw"}).json()["token"]
    assert client.get(f"/api/share/{token}/thumbnail").status_code == 403
    assert client.get(f"/api/share/{token}/thumbnail",
                      params={"password": "pw"}).status_code == 200


def test_thumbnail_of_empty_trip(auth_client, client):
    tid = auth_client.post("/api/trips", data={"name": "空行程"}).json()["id"]
    token = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    r = client.get(f"/api/share/{token}/thumbnail")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
