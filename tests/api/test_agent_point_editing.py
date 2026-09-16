"""领域 API：地点的「读得到 id、改得动单个字段、插得进指定位次」。

补的是一个闭环断口：以前 get_memory / export_trip / get_stats 都不返回 point_id，
而 update_point / delete_point / upload_media 又都要 point_id —— 想改一个已有地点的
日期只能删掉整条行程重建，连带丢掉已上传的照片。
"""
import pytest

pytestmark = pytest.mark.api


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
def agent_key(auth_client):
    r = auth_client.post("/api/console/keys",
                         data={"name": "point-edit-test", "scope": "read,write"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


@pytest.fixture
def trip_with_points(auth_client, agent_key, client):
    """一条 3 天 3 个点的行程，通过领域 API 建（顺带覆盖 add_point）。"""
    r = client.post("/api/ai/trips", headers=_bearer(agent_key),
                    data={"name": "滇西北进藏", "start_date": "2026-10-01"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    pids = []
    for nm, lat, lng, d in [("香格里拉", 27.8231, 99.7065, "2026-10-01"),
                            ("德钦", 28.4863, 98.9111, "2026-10-02"),
                            ("芒康", 29.6795, 98.5936, "2026-10-03")]:
        rp = client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key),
                         data={"name": nm, "lat": lat, "lng": lng, "start_date": d})
        assert rp.status_code == 200, rp.text
        pids.append(rp.json()["id"])
    return {"trip_id": tid, "point_ids": pids}


# ---------------------------------------------------------------------------
# list_points：读接口要能吐出 point_id
# ---------------------------------------------------------------------------
def test_list_points_returns_ids_and_full_fields(client, agent_key, trip_with_points):
    tid = trip_with_points["trip_id"]
    r = client.get(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == tid and body["count"] == 3
    assert body["coord_sys"] == "wgs84"
    assert [p["point_id"] for p in body["points"]] == trip_with_points["point_ids"]
    assert [p["index"] for p in body["points"]] == [0, 1, 2]
    p0 = body["points"][0]
    for field in ("name", "lat", "lng", "start_date", "end_date", "description",
                  "route_mode", "is_waypoint", "sort_order", "media_n"):
        assert field in p0, f"少了字段 {field}"
    # 坐标按对外约定转回 WGS-84，和建点时传进去的一致
    assert abs(p0["lat"] - 27.8231) < 0.01 and abs(p0["lng"] - 99.7065) < 0.01


def test_list_points_counts_media(client, agent_key, trip_with_points, jpeg_bytes):
    import base64
    pid = trip_with_points["point_ids"][0]
    r = client.post(f"/api/ai/points/{pid}/media", headers=_bearer(agent_key),
                    data={"data_base64": base64.b64encode(jpeg_bytes()).decode(),
                          "filename": "a.jpg", "content_type": "image/jpeg"})
    assert r.status_code == 200, r.text
    pts = client.get(f"/api/ai/trips/{trip_with_points['trip_id']}/points",
                     headers=_bearer(agent_key)).json()["points"]
    by_id = {p["point_id"]: p for p in pts}
    assert by_id[pid]["media_n"] == 1 and by_id[pid]["photo_n"] == 1
    assert by_id[trip_with_points["point_ids"][1]]["media_n"] == 0


def test_list_points_rejects_other_users_trip(client, agent_key, trip_with_points,
                                              other_client):
    r = other_client.post("/api/console/keys",
                          data={"name": "intruder", "scope": "read,write"})
    intruder = r.json()["key"]
    r = client.get(f"/api/ai/trips/{trip_with_points['trip_id']}/points",
                   headers=_bearer(intruder))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# update_point：局部更新不该被坐标绑架
# ---------------------------------------------------------------------------
def test_update_point_without_coords_keeps_them(client, agent_key, trip_with_points):
    """只改日期/描述，不传坐标 —— 以前这里会去 geocode，地名偏一点就 400。"""
    pid = trip_with_points["point_ids"][1]
    r = client.put(f"/api/ai/points/{pid}", headers=_bearer(agent_key),
                   data={"start_date": "2026-10-05", "description": "飞来寺看日照金山"})
    assert r.status_code == 200, r.text
    assert r.json()["coord_source"] == "unchanged"
    pts = {p["point_id"]: p for p in client.get(
        f"/api/ai/trips/{trip_with_points['trip_id']}/points",
        headers=_bearer(agent_key)).json()["points"]}
    p = pts[pid]
    assert p["start_date"] == "2026-10-05"
    assert p["description"] == "飞来寺看日照金山"
    assert p["name"] == "德钦"                           # 名字没动
    assert abs(p["lat"] - 28.4863) < 0.01                # 坐标原样保留
    assert abs(p["lng"] - 98.9111) < 0.01


def test_update_point_name_only_does_not_move_it(client, agent_key, trip_with_points):
    """改名也不该顺手挪坐标 —— 出站网络在测试里是关的，真去 geocode 会炸。"""
    pid = trip_with_points["point_ids"][2]
    r = client.put(f"/api/ai/points/{pid}", headers=_bearer(agent_key),
                   data={"name": "芒康县城"})
    assert r.status_code == 200, r.text
    pts = {p["point_id"]: p for p in client.get(
        f"/api/ai/trips/{trip_with_points['trip_id']}/points",
        headers=_bearer(agent_key)).json()["points"]}
    assert pts[pid]["name"] == "芒康县城"
    assert abs(pts[pid]["lat"] - 29.6795) < 0.01


def test_update_point_with_coords_still_moves_it(client, agent_key, trip_with_points):
    pid = trip_with_points["point_ids"][0]
    r = client.put(f"/api/ai/points/{pid}", headers=_bearer(agent_key),
                   data={"lat": 28.0000, "lng": 99.5000})
    assert r.status_code == 200, r.text
    assert r.json()["coord_source"] == "manual"
    pts = {p["point_id"]: p for p in client.get(
        f"/api/ai/trips/{trip_with_points['trip_id']}/points",
        headers=_bearer(agent_key)).json()["points"]}
    assert abs(pts[pid]["lat"] - 28.0) < 0.01


def test_update_point_empty_name_rejected(client, agent_key, trip_with_points):
    r = client.put(f"/api/ai/points/{trip_with_points['point_ids'][0]}",
                   headers=_bearer(agent_key), data={"name": "   "})
    assert r.status_code == 400


def test_update_point_can_clear_date(client, agent_key, trip_with_points):
    pid = trip_with_points["point_ids"][1]
    r = client.put(f"/api/ai/points/{pid}", headers=_bearer(agent_key),
                   data={"end_date": "null"})
    assert r.status_code == 200, r.text
    pts = {p["point_id"]: p for p in client.get(
        f"/api/ai/trips/{trip_with_points['trip_id']}/points",
        headers=_bearer(agent_key)).json()["points"]}
    assert pts[pid]["end_date"] is None


# ---------------------------------------------------------------------------
# sort_order：同一天内重排 / 插队
# ---------------------------------------------------------------------------
def test_add_point_appends_within_same_day(client, agent_key, trip_with_points):
    tid = trip_with_points["trip_id"]
    for nm in ("松赞林寺", "独克宗古城"):
        client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key),
                    data={"name": nm, "lat": 27.9, "lng": 99.7,
                          "start_date": "2026-10-01"})
    day1 = [p for p in client.get(f"/api/ai/trips/{tid}/points",
                                  headers=_bearer(agent_key)).json()["points"]
            if p["start_date"] == "2026-10-01"]
    assert [p["name"] for p in day1] == ["香格里拉", "松赞林寺", "独克宗古城"]
    assert [p["sort_order"] for p in day1] == [0, 1, 2]


def test_add_point_inserts_at_sort_order(client, agent_key, trip_with_points):
    """插队：给了 sort_order，原先站在那个位次及之后的点整体顺延。"""
    tid = trip_with_points["trip_id"]
    for nm in ("松赞林寺", "独克宗古城"):
        client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key),
                    data={"name": nm, "lat": 27.9, "lng": 99.7,
                          "start_date": "2026-10-01"})
    r = client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key),
                    data={"name": "纳帕海", "lat": 27.9, "lng": 99.6,
                          "start_date": "2026-10-01", "sort_order": "1"})
    assert r.status_code == 200, r.text
    day1 = [p for p in client.get(f"/api/ai/trips/{tid}/points",
                                  headers=_bearer(agent_key)).json()["points"]
            if p["start_date"] == "2026-10-01"]
    assert [p["name"] for p in day1] == ["香格里拉", "纳帕海", "松赞林寺", "独克宗古城"]
    assert [p["sort_order"] for p in day1] == [0, 1, 2, 3]


def test_add_point_rejects_bogus_sort_order(client, agent_key, trip_with_points):
    r = client.post(f"/api/ai/trips/{trip_with_points['trip_id']}/points",
                    headers=_bearer(agent_key),
                    data={"name": "X", "lat": 27.0, "lng": 99.0,
                          "start_date": "2026-10-01", "sort_order": "第一个"})
    assert r.status_code == 400
    r = client.post(f"/api/ai/trips/{trip_with_points['trip_id']}/points",
                    headers=_bearer(agent_key),
                    data={"name": "X", "lat": 27.0, "lng": 99.0,
                          "start_date": "2026-10-01", "sort_order": "-1"})
    assert r.status_code == 400


def test_update_point_sort_order_reorders_same_day(client, agent_key, trip_with_points):
    tid = trip_with_points["trip_id"]
    r = client.post(f"/api/ai/trips/{tid}/points", headers=_bearer(agent_key),
                   data={"name": "松赞林寺", "lat": 27.9, "lng": 99.7,
                         "start_date": "2026-10-01"})
    second = r.json()["id"]
    # 把后加的这个提到当天第一个，原第一个退到 1
    client.put(f"/api/ai/points/{second}", headers=_bearer(agent_key),
               data={"sort_order": "0"})
    client.put(f"/api/ai/points/{trip_with_points['point_ids'][0]}",
               headers=_bearer(agent_key), data={"sort_order": "1"})
    day1 = [p for p in client.get(f"/api/ai/trips/{tid}/points",
                                  headers=_bearer(agent_key)).json()["points"]
            if p["start_date"] == "2026-10-01"]
    assert [p["name"] for p in day1] == ["松赞林寺", "香格里拉"]


# ---------------------------------------------------------------------------
# update_trip：改名称/日期，别把没传的字段抹掉
# ---------------------------------------------------------------------------
def test_update_trip_partial_fields(client, agent_key, trip_with_points):
    tid = trip_with_points["trip_id"]
    r = client.put(f"/api/ai/trips/{tid}", headers=_bearer(agent_key),
                   data={"end_date": "2026-10-20"})
    assert r.status_code == 200, r.text
    trips = client.get("/api/ai/trips", headers=_bearer(agent_key)).json()
    trip = [t for t in (trips if isinstance(trips, list) else trips["trips"])
            if t["id"] == tid][0]
    assert trip["end_date"] == "2026-10-20"
    assert trip["name"] == "滇西北进藏"        # 名称没传 → 不动
    assert trip["start_date"] == "2026-10-01"  # 开始日期没传 → 不动


def test_update_trip_status_does_not_wipe_name(client, agent_key, trip_with_points):
    """只改状态的调用方习惯带一个空 name 过来 —— 照收会把行程名抹成空。"""
    tid = trip_with_points["trip_id"]
    r = client.put(f"/api/ai/trips/{tid}", headers=_bearer(agent_key),
                   data={"name": "", "status": "traveling"})
    assert r.status_code == 200, r.text
    trips = client.get("/api/ai/trips", headers=_bearer(agent_key)).json()
    trip = [t for t in (trips if isinstance(trips, list) else trips["trips"])
            if t["id"] == tid][0]
    assert trip["name"] == "滇西北进藏"
