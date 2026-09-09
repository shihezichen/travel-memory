"""轨迹点：CRUD、连线方式、排序、海拔回填（离线降级）。"""
import pytest

pytestmark = pytest.mark.api


def test_list_points_includes_media_counts(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    auth_client.post(f"/api/points/{pid}/media",
                     files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")})
    pts = auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/points").json()
    by_id = {p["id"]: p for p in pts}
    assert by_id[pid]["photo_count"] == 1 and by_id[pid]["video_count"] == 0
    assert by_id[seeded_trip["point_ids"][1]]["photo_count"] == 0


def test_create_point_defaults_and_waypoint_flag(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    r = auth_client.post(f"/api/trips/{tid}/points", data={
        "name": "途经点", "lat": 30.0, "lng": 120.0, "is_waypoint": "true",
        "route_mode": "line"})
    assert r.status_code == 200
    p = [x for x in auth_client.get(f"/api/trips/{tid}/points").json()
         if x["id"] == r.json()["id"]][0]
    assert p["is_waypoint"] == 1 and p["route_mode"] == "line"


def test_create_point_rejects_bogus_route_mode(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    r = auth_client.post(f"/api/trips/{tid}/points", data={
        "name": "X", "lat": 1, "lng": 2, "route_mode": "teleport"})
    p = [x for x in auth_client.get(f"/api/trips/{tid}/points").json()
         if x["id"] == r.json()["id"]][0]
    assert p["route_mode"] == "drive"        # 非法值回落 drive


def test_create_point_requires_coordinates(auth_client, seeded_trip):
    r = auth_client.post(f"/api/trips/{seeded_trip['trip_id']}/points",
                         data={"name": "缺坐标"})
    assert r.status_code == 422              # FastAPI 表单校验


def test_update_point(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = auth_client.put(f"/api/points/{pid}", data={
        "name": "西湖（改）", "lat": 30.3, "lng": 120.2,
        "start_date": "2026-05-01", "end_date": "2026-05-01",
        "description": "改过了"})
    assert r.status_code == 200
    p = [x for x in auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/points").json()
         if x["id"] == pid][0]
    assert p["name"] == "西湖（改）" and p["lat"] == 30.3


def test_set_route_mode_endpoint(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = auth_client.put(f"/api/points/{pid}/route-mode", data={"route_mode": "line"})
    assert r.json() == {"ok": True, "route_mode": "line"}
    r = auth_client.put(f"/api/points/{pid}/route-mode", data={"route_mode": "bogus"})
    assert r.json()["route_mode"] == "drive"


def test_reorder_points(auth_client, seeded_trip):
    """三个点分属三天 —— 同一天内才排序，所以先把它们改成同一天。"""
    tid, pids = seeded_trip["trip_id"], seeded_trip["point_ids"]
    for pid in pids:
        p = [x for x in auth_client.get(f"/api/trips/{tid}/points").json()
             if x["id"] == pid][0]
        auth_client.put(f"/api/points/{pid}", data={
            "name": p["name"], "lat": p["lat"], "lng": p["lng"],
            "start_date": "2026-05-01", "end_date": "2026-05-01"})
    new_order = [pids[2], pids[0], pids[1]]
    r = auth_client.post(f"/api/trips/{tid}/points/reorder",
                         data={"ordered_ids": ",".join(map(str, new_order))})
    assert r.status_code == 200
    got = [p["id"] for p in auth_client.get(f"/api/trips/{tid}/points").json()]
    assert got == new_order


def test_reorder_rejects_invalid_payload(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    assert auth_client.post(f"/api/trips/{tid}/points/reorder",
                            data={"ordered_ids": "abc,def"}).status_code == 400
    assert auth_client.post(f"/api/trips/{tid}/points/reorder",
                            data={"ordered_ids": "999999"}).status_code == 400


def test_delete_point(auth_client, seeded_trip):
    tid, pid = seeded_trip["trip_id"], seeded_trip["point_ids"][0]
    assert auth_client.delete(f"/api/points/{pid}").status_code == 200
    assert pid not in [p["id"] for p in
                       auth_client.get(f"/api/trips/{tid}/points").json()]


@pytest.mark.parametrize("method,path_tpl,data", [
    ("put", "/api/points/{pid}", {"name": "x", "lat": 1, "lng": 2}),
    ("put", "/api/points/{pid}/route-mode", {"route_mode": "line"}),
    ("delete", "/api/points/{pid}", None),
    ("get", "/api/points/{pid}/media", None),
    ("put", "/api/points/{pid}/media/order", {"ids": "1"}),
])
def test_other_user_cannot_touch_point(other_client, seeded_trip, method, path_tpl, data):
    path = path_tpl.format(pid=seeded_trip["point_ids"][0])
    r = getattr(other_client, method)(path, **({"data": data} if data else {}))
    assert r.status_code == 404


def test_elevation_backfill_degrades_gracefully_offline(auth_client, seeded_trip):
    """外部高程服务不可达（本套测试全程断网）时必须返回 filled=0 而不是 5xx。"""
    r = auth_client.post(f"/api/trips/{seeded_trip['trip_id']}/elevation")
    assert r.status_code == 200
    assert r.json() == {"filled": 0, "elevations": {}}


def test_elevation_on_trip_without_points(auth_client):
    tid = auth_client.post("/api/trips", data={"name": "空行程"}).json()["id"]
    assert auth_client.post(f"/api/trips/{tid}/elevation").json() == \
        {"filled": 0, "elevations": {}}
