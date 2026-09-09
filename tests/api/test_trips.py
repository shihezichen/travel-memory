"""行程 CRUD / 导入导出 / 统计。"""
import json

import pytest

pytestmark = pytest.mark.api


def test_create_list_update_delete_trip(auth_client):
    r = auth_client.post("/api/trips", data={"name": "冰岛环岛",
                                             "start_date": "2026-06-01",
                                             "end_date": "2026-06-10",
                                             "description": "极光"})
    assert r.status_code == 200
    tid = r.json()["id"]

    trips = auth_client.get("/api/trips").json()
    assert [t["name"] for t in trips] == ["冰岛环岛"]
    assert trips[0]["status"] == "planning"

    assert auth_client.put(f"/api/trips/{tid}",
                           data={"name": "冰岛环岛 2.0",
                                 "status": "traveling"}).status_code == 200
    t = auth_client.get("/api/trips").json()[0]
    assert t["name"] == "冰岛环岛 2.0" and t["status"] == "traveling"

    assert auth_client.delete(f"/api/trips/{tid}").status_code == 200
    assert auth_client.get("/api/trips").json() == []


def test_trip_list_is_per_user(auth_client, other_client):
    auth_client.post("/api/trips", data={"name": "我的"})
    other_client.post("/api/trips", data={"name": "别人的"})
    assert [t["name"] for t in auth_client.get("/api/trips").json()] == ["我的"]
    assert [t["name"] for t in other_client.get("/api/trips").json()] == ["别人的"]


@pytest.mark.parametrize("method,path_tpl", [
    ("put", "/api/trips/{tid}"),
    ("delete", "/api/trips/{tid}"),
    ("get", "/api/trips/{tid}/points"),
    ("get", "/api/trips/{tid}/stats"),
    ("get", "/api/trips/{tid}/export"),
    ("get", "/api/trips/{tid}/albums"),
    ("get", "/api/trips/{tid}/media"),
    ("get", "/api/trips/{tid}/share-count"),
    ("post", "/api/trips/{tid}/share"),
    ("delete", "/api/trips/{tid}/share"),
])
def test_other_user_cannot_touch_trip(other_client, seeded_trip, method, path_tpl):
    path = path_tpl.format(tid=seeded_trip["trip_id"])
    r = getattr(other_client, method)(path, **({"data": {"name": "x"}}
                                               if method in ("post", "put") else {}))
    assert r.status_code == 404, f"{method.upper()} {path} -> {r.status_code}"


def test_trip_stats(auth_client, seeded_trip):
    s = auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/stats").json()
    assert s["point_count"] == 3
    assert s["photo_count"] == 0 and s["video_count"] == 0


def test_stats_of_missing_trip_is_404(auth_client):
    assert auth_client.get("/api/trips/999999/stats").status_code == 404


def test_export_shape_and_headers(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    r = auth_client.get(f"/api/trips/{tid}/export")
    assert r.status_code == 200
    assert f'filename="travel-{tid}.json"' in r.headers["content-disposition"]
    payload = json.loads(r.text)
    assert payload["format"] == "travel-memory" and payload["version"] == 1
    assert payload["trip"]["name"] == "测试行程"
    assert [p["name"] for p in payload["points"]] == ["西湖", "灵隐寺", "外滩"]
    # 导出不含媒体与内部 id
    assert "id" not in payload["points"][0]


def test_export_import_roundtrip(auth_client, seeded_trip):
    exported = auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/export").json()
    r = auth_client.post("/api/trips/import", json=exported)
    assert r.status_code == 200
    assert r.json()["point_count"] == 3
    new_tid = r.json()["id"]
    pts = auth_client.get(f"/api/trips/{new_tid}/points").json()
    assert [p["name"] for p in pts] == ["西湖", "灵隐寺", "外滩"]


@pytest.mark.parametrize("body,msg", [
    ({}, "缺少行程名称"),
    ({"trip": {"name": "  "}}, "缺少行程名称"),
    ({"trip": {"name": "A"}, "points": "notalist"}, "points 必须是数组"),
    ([1, 2, 3], "JSON 顶层必须是对象"),
])
def test_import_validation(auth_client, body, msg):
    r = auth_client.post("/api/trips/import", json=body)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_import_skips_malformed_points_but_keeps_good_ones(auth_client):
    r = auth_client.post("/api/trips/import", json={
        "trip": {"name": "混杂导入", "status": "not-a-status"},
        "points": [
            {"name": "好点", "lat": 30.0, "lng": 120.0},
            {"name": "没坐标"},
            {"name": "坏坐标", "lat": "abc", "lng": "def"},
            "不是对象",
            {"name": "", "lat": 1, "lng": 1},
        ],
    })
    assert r.status_code == 200
    assert r.json()["point_count"] == 1
    tid = r.json()["id"]
    assert [t["status"] for t in auth_client.get("/api/trips").json()
            if t["id"] == tid] == ["planning"]      # 非法 status 回落


def test_import_bad_json_body(auth_client):
    r = auth_client.post("/api/trips/import", content=b"not json",
                         headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_generations_history_is_empty_but_reachable(auth_client, seeded_trip):
    r = auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/generations")
    assert r.status_code == 200 and r.json() == []
