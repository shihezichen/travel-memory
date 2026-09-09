"""地图图层类接口：足迹热力、照片图层、相册聚类，以及外部服务离线时的降级。"""
import pytest

pytestmark = pytest.mark.api


def _upload(client, pid, name="a.jpg", content=b""):
    return client.post(f"/api/points/{pid}/media",
                       files={"file": (name, content, "image/jpeg")})


# ---------------- 足迹 ----------------
def test_footprint_aggregates_duplicate_coordinates(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    # 再加一个和「西湖」完全同坐标的点（环线重复经过）
    auth_client.post(f"/api/trips/{tid}/points",
                     data={"name": "西湖（再来）", "lat": 30.2590, "lng": 120.1300})
    body = auth_client.get("/api/footprint").json()
    assert body["count"] == 3                       # 4 个点聚成 3 个坐标
    hot = [d for d in body["data"] if d["count"] == 2]
    assert len(hot) == 1
    assert hot[0]["lat"] == 30.259 and hot[0]["lng"] == 120.13


def test_footprint_is_scoped_to_user(auth_client, other_client, seeded_trip):
    assert other_client.get("/api/footprint").json() == {"count": 0, "data": []}


def test_footprint_requires_login(client):
    assert client.get("/api/footprint").status_code == 401


# ---------------- 照片图层 ----------------
def test_photo_layer_returns_points_with_thumbs(auth_client, seeded_trip, jpeg_bytes):
    tid, pid = seeded_trip["trip_id"], seeded_trip["point_ids"][0]
    _upload(auth_client, pid, content=jpeg_bytes())
    _upload(auth_client, pid, name="b.jpg", content=jpeg_bytes(color=(0, 200, 0)))
    body = auth_client.get("/api/photo-layer", params={"trip_id": tid}).json()
    assert body["count"] == 1
    entry = body["data"][0]
    assert entry["point_id"] == pid and entry["name"] == "西湖"
    assert len(entry["photos"]) == 2
    assert all(p["thumb"].endswith(".jpg") for p in entry["photos"])


def test_photo_layer_is_scoped_to_the_requested_trip(auth_client, seeded_trip,
                                                     jpeg_bytes):
    """正在看哪个行程就只该看到哪个行程的照片。"""
    _upload(auth_client, seeded_trip["point_ids"][0], content=jpeg_bytes())
    other_tid = auth_client.post("/api/trips", data={"name": "另一个行程"}).json()["id"]
    assert auth_client.get("/api/photo-layer",
                           params={"trip_id": other_tid}).json()["count"] == 0
    # 不带 trip_id = 该用户全部
    assert auth_client.get("/api/photo-layer").json()["count"] == 1


def test_photo_layer_rejects_other_users_trip(other_client, seeded_trip):
    r = other_client.get("/api/photo-layer",
                         params={"trip_id": seeded_trip["trip_id"]})
    assert r.status_code == 404


# ---------------- 相册聚类 ----------------
def test_albums_group_by_point(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    for pid in seeded_trip["point_ids"][:2]:
        _upload(auth_client, pid, content=jpeg_bytes())
    body = auth_client.get(f"/api/trips/{tid}/albums").json()
    assert body["count"] == 2
    assert {a["point_name"] for a in body["albums"]} == {"西湖", "灵隐寺"}
    assert all(a["cover_url"].startswith("/uploads/thumbs/") for a in body["albums"])


def test_albums_split_by_time_gap(auth_client, seeded_trip, jpeg_bytes):
    """同一地点内，拍摄间隔 > 4 小时切成两本相册。"""
    import db
    tid, pid = seeded_trip["trip_id"], seeded_trip["point_ids"][0]
    m1 = _upload(auth_client, pid, content=jpeg_bytes()).json()["id"]
    m2 = _upload(auth_client, pid, name="b.jpg",
                 content=jpeg_bytes(color=(0, 0, 200))).json()["id"]
    conn = db.get_conn()
    conn.execute("UPDATE media SET exif_time='2026-05-01T08:00:00' WHERE id=?", (m1,))
    conn.execute("UPDATE media SET exif_time='2026-05-01T20:00:00' WHERE id=?", (m2,))
    conn.commit()
    conn.close()
    body = auth_client.get(f"/api/trips/{tid}/albums").json()
    assert body["count"] == 2
    assert all(a["point_id"] == pid for a in body["albums"])


def test_albums_keep_close_shots_together(auth_client, seeded_trip, jpeg_bytes):
    import db
    tid, pid = seeded_trip["trip_id"], seeded_trip["point_ids"][0]
    m1 = _upload(auth_client, pid, content=jpeg_bytes()).json()["id"]
    m2 = _upload(auth_client, pid, name="b.jpg",
                 content=jpeg_bytes(color=(0, 0, 200))).json()["id"]
    conn = db.get_conn()
    conn.execute("UPDATE media SET exif_time='2026-05-01T08:00:00' WHERE id=?", (m1,))
    conn.execute("UPDATE media SET exif_time='2026-05-01T09:00:00' WHERE id=?", (m2,))
    conn.commit()
    conn.close()
    body = auth_client.get(f"/api/trips/{tid}/albums").json()
    assert body["count"] == 1 and body["albums"][0]["count"] == 2


def test_albums_empty_trip(auth_client, seeded_trip):
    assert auth_client.get(
        f"/api/trips/{seeded_trip['trip_id']}/albums").json() == {"albums": []}


# ---------------- 外部服务离线降级 ----------------
def test_route_api_returns_empty_when_osrm_unreachable(client):
    """/api/route 是免登录公开接口；OSRM 不可达时返回空 routes 而不是报错。"""
    r = client.get("/api/route", params={"from_lng": 120.1, "from_lat": 30.2,
                                         "to_lng": 121.4, "to_lat": 31.2})
    assert r.status_code == 200 and r.json() == {"routes": []}


def test_route_api_serves_from_cache_without_network(client):
    import db
    key = "drive|120.100000,30.200000|121.400000,31.200000"
    db.set_route_cache(key, {"routes": [{"path": [[120.1, 30.2], [121.4, 31.2]],
                                         "distance": 100.0, "duration": 60.0}]})
    r = client.get("/api/route", params={"from_lng": 120.1, "from_lat": 30.2,
                                         "to_lng": 121.4, "to_lat": 31.2})
    assert r.status_code == 200
    assert r.json()["routes"][0]["distance"] == 100.0


def test_route_refresh_bypasses_cache_and_degrades(client):
    import db
    key = "drive|120.100000,30.200000|121.400000,31.200000"
    db.set_route_cache(key, {"routes": [{"path": [[1, 2], [3, 4]],
                                         "distance": 1, "duration": 1}]})
    r = client.get("/api/route", params={"from_lng": 120.1, "from_lat": 30.2,
                                         "to_lng": 121.4, "to_lat": 31.2,
                                         "refresh": 1})
    assert r.json() == {"routes": []}       # 强制刷新 + 断网 → 空



def test_clear_route_cache_requires_login(client):
    assert client.post("/api/route/clear-cache").status_code == 401


def test_clear_route_cache(auth_client):
    import db
    db.set_route_cache("k", {"routes": [1]})
    assert auth_client.post("/api/route/clear-cache").status_code == 200
    assert db.get_route_cache("k") is None
