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
    body = other_client.get("/api/footprint").json()
    assert body["count"] == 0 and body["data"] == []


# ---------------- 足迹：服务端网格聚合 ----------------
def test_footprint_returns_exact_points_when_there_are_few(auth_client, seeded_trip):
    """点少的时候不聚合——几百个点的响应本来就小，聚合只会让位置变糊。"""
    body = auth_client.get("/api/footprint", params={"zoom": 4}).json()
    assert body["aggregated"] is False
    assert body["points"] == 3
    assert {round(d["lng"], 4) for d in body["data"]} == {120.13, 120.101, 121.49}


def test_footprint_switches_to_grid_aggregation_past_the_threshold(
        auth_client, seeded_trip, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(app_mod, "FOOTPRINT_AGG_THRESHOLD", 2)

    # zoom 7 的格子约 0.44 度：杭州两个点（相距 ~0.03 度）并成一格，上海另成一格
    body = auth_client.get("/api/footprint", params={"zoom": 7}).json()
    assert body["aggregated"] is True
    assert body["points"] == 3                       # 原始点数照实报
    assert body["count"] == 2                        # 但只发 2 个格子出去
    assert sorted(d["count"] for d in body["data"]) == [1, 2]

    # 放大到街道级，格子细到 0.0001 度量级，两个点就该分开
    zoomed = auth_client.get("/api/footprint", params={"zoom": 16}).json()
    assert zoomed["count"] == 3
    assert zoomed["cell_deg"] < body["cell_deg"]


HANGZHOU = "120.05,30.20,120.20,30.30"          # 只框住西湖 + 灵隐寺，上海在外


def test_footprint_bbox_scopes_the_point_count(auth_client, seeded_trip):
    """视野只有杭州时，上海的点和「要不要聚合」这件事无关。"""
    boxed = auth_client.get("/api/footprint",
                            params={"zoom": 13, "bbox": HANGZHOU}).json()
    assert boxed["points"] == 2                   # 全局 3 个点，视野里只有 2 个
    assert boxed["aggregated"] is False           # 2 个点还不到阈值，不用聚合
    assert {round(d["lng"], 3) for d in boxed["data"]} == {120.13, 120.101}


def test_footprint_bbox_aggregates_within_the_viewport(auth_client,
                                                       seeded_trip, monkeypatch):
    """框住视野之后照常聚合，且用的是按缩放级别算出来的细格子。"""
    import app as app_mod
    monkeypatch.setattr(app_mod, "FOOTPRINT_AGG_THRESHOLD", 1)

    boxed = auth_client.get("/api/footprint",
                            params={"zoom": 13, "bbox": HANGZHOU}).json()
    assert boxed["aggregated"] is True
    assert boxed["points"] == 2
    # 关键：格子就是 zoom 13 该有的细度，没有被上限逼着加粗
    assert boxed["cell_deg"] == app_mod.footprint_cell_deg(13)


def test_footprint_without_bbox_still_aggregates_globally(auth_client,
                                                          seeded_trip, monkeypatch):
    """老客户端不带 bbox 也能用——回落到全局聚合。"""
    import app as app_mod
    monkeypatch.setattr(app_mod, "FOOTPRINT_AGG_THRESHOLD", 1)
    body = auth_client.get("/api/footprint", params={"zoom": 13}).json()
    assert body["aggregated"] is True
    assert body["points"] == 3


def test_footprint_ignores_a_malformed_bbox(auth_client):
    """bbox 写错了不该 500，回落到全局就行。"""
    for bad in ("", "1,2,3", "a,b,c,d", "10,20,5,25", "999,0,1000,1"):
        r = auth_client.get("/api/footprint", params={"zoom": 6, "bbox": bad})
        assert r.status_code == 200, (bad, r.text)
        assert r.json()["count"] >= 0


def test_parse_bbox_validates_ranges():
    import app as app_mod
    assert app_mod.parse_bbox("120.1,30.2,120.2,30.3") == (120.1, 30.2, 120.2, 30.3)
    assert app_mod.parse_bbox("120.2,30.2,120.1,30.3") is None   # min > max
    assert app_mod.parse_bbox(None) is None
    assert app_mod.parse_bbox("") is None


def test_footprint_grid_caps_the_number_of_cells(auth_client, seeded_trip,
                                                 monkeypatch):
    """低缩放下不该往前端灌几万个圈：超了就把格子调粗再聚一次。"""
    import app as app_mod
    monkeypatch.setattr(app_mod, "FOOTPRINT_AGG_THRESHOLD", 2)
    monkeypatch.setattr(app_mod, "FOOTPRINT_MAX_CELLS", 1)

    body = auth_client.get("/api/footprint", params={"zoom": 16}).json()
    assert body["count"] == 1                        # 一路加粗到全部并成一格
    assert sum(d["count"] for d in body["data"]) == 3   # 一个到访都没丢


def test_footprint_grid_does_not_merge_across_the_equator_or_meridian(
        auth_client, seeded_trip):
    """sqlite 的 CAST 是向零截断不是向下取整，负坐标最容易被错并进同一格。"""
    import db
    tid = seeded_trip["trip_id"]
    for nm, lat, lng in [("西经北纬", 10.0, -0.5), ("东经北纬", 10.0, 0.5),
                         ("东经南纬", -0.5, 20.0), ("东经北纬2", 0.5, 20.0)]:
        auth_client.post(f"/api/trips/{tid}/points",
                         data={"name": nm, "lat": lat, "lng": lng})

    user = db.get_user_by_username(auth_client.user["username"])
    cells = db.aggregate_footprint(user, 1.0)        # 1 度的格子
    got = {(round(c["lng"], 3), round(c["lat"], 3)): c["count"] for c in cells}
    assert got[(-0.5, 10.0)] == 1 and got[(0.5, 10.0)] == 1     # 本初子午线两侧
    assert got[(20.0, -0.5)] == 1 and got[(20.0, 0.5)] == 1     # 赤道两侧


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
                                         "distance": 100.0, "duration": 60.0,
                                         "steps": []}]})
    r = client.get("/api/route", params={"from_lng": 120.1, "from_lat": 30.2,
                                         "to_lng": 121.4, "to_lat": 31.2})
    assert r.status_code == 200
    assert r.json()["routes"][0]["distance"] == 100.0


def test_route_api_refetches_cache_without_steps(client):
    """steps 是后加的，加之前存下的缓存没有公路编号。

    这种缓存要当未命中处理、重新去要一次，否则那些路段永远标不出编号
    （独库公路那段 G217 就是这么丢的）。这里断网，所以重算的结果是空 —— 
    能拿到空就说明它没有把旧缓存直接端出来。"""
    import db
    key = "drive|120.100000,30.200000|121.400000,31.200000"
    db.set_route_cache(key, {"routes": [{"path": [[120.1, 30.2], [121.4, 31.2]],
                                         "distance": 100.0, "duration": 60.0}]})
    r = client.get("/api/route", params={"from_lng": 120.1, "from_lat": 30.2,
                                         "to_lng": 121.4, "to_lat": 31.2})
    assert r.status_code == 200
    assert r.json() == {"routes": []}


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


# ---------------------------------------------------------------- 并发容量

def test_同步路由的线程池容量被提高了(client):
    """FastAPI 把 `def` 路由丢进 anyio 的默认线程池，而它默认只有 40 个槽位。

    本应用绝大多数路由是同步 def，其中几条要等外部服务（OSRM 8 秒、地理编码
    10 秒、分享页抓图 20 秒），而前端画路线时会并发请求一条行程的所有路段。
    槽位被这类慢请求占满时，连「查个列表」都要跟着排队 —— 这条断言守的就是
    启动钩子真的把容量提上去了，而不是悄悄回落到 40。
    """
    import anyio.to_thread
    import app as app_mod

    got = client.portal.call(
        lambda: anyio.to_thread.current_default_thread_limiter().total_tokens)
    assert got == app_mod.THREADPOOL_LIMIT
    assert got > 40, "提高线程池容量的启动钩子没生效"


def test_osrm_steps_prefers_ref_and_keeps_unnamed():
    """OSRM 的 steps → 前端认识的 [{road, distance, path}]。

    三件事：ref 优先于 name（高德给「独库公路」，OSRM 给 G217，要的是后者）；
    没有 ref 的 step 也得留着（segRoadRuns 靠它们判断两截同编号的路中间是不是
    只隔了个服务区，丢了会把长路劈成够不着门槛的碎段）；超长的轨迹要抽稀，
    否则一条 300 公里的路线能把缓存撑到几十 KB。
    """
    from routers.deps import osrm_steps, OSRM_STEP_MAX_PTS
    route = {"legs": [{"steps": [
        {"ref": "G217", "name": "独库公路", "distance": 293049.0,
         "geometry": {"coordinates": [[84.0 + i * 0.01, 43.0] for i in range(200)]}},
        {"ref": "", "name": "", "distance": 120.0,
         "geometry": {"coordinates": [[85.0, 42.0], [85.1, 42.0]]}},
        {"name": "友谊路", "distance": 758.0,
         "geometry": {"coordinates": [[86.0, 41.0], [86.1, 41.0]]}},
    ]}]}
    out = osrm_steps(route)
    assert [s["road"] for s in out] == ["G217", "", "友谊路"]
    assert out[0]["distance"] == 293049.0
    assert len(out[0]["path"]) == OSRM_STEP_MAX_PTS      # 抽稀过
    assert out[0]["path"][0] == [84.0, 43.0]             # 首尾必须保住
    assert out[0]["path"][-1] == [84.0 + 199 * 0.01, 43.0]
    assert len(out[1]["path"]) == 2                      # 够短的原样留着
