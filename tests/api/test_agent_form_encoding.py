"""/api/ai/* 写入链路的两个静默数据损坏：表单编码 和 日期格式。

背景（2026-09-15 线上）：外部 agent 通过 API 往「滇西北」里加了个点「芒康」，
HTTP 200，但网页上打开名字和描述全是乱码，而且没有日期。

两个独立的坑：
  1. Starlette 解 x-www-form-urlencoded 是按 latin-1 解字节的，body 里放
     UTF-8 裸字节（`curl -d "name=芒康"`）就会被存成「è\x8a\x92åº·」。
     修在 formfix.Utf8FormMiddleware。
  2. 日期只要不是 YYYY-MM-DD，原来会被原样写进库（甚至空着），
     点就排不进时间线。修在 agent_api._normalize_date。
"""
import pytest

import db
import formfix

pytestmark = pytest.mark.api

FORM_CT = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def ai(auth_client):
    """带 write scope 的 API Key + 一条空行程，返回调用助手。"""
    made = auth_client.post("/api/console/keys",
                            data={"name": "enc", "scope": "read,write"}).json()
    headers = {"Authorization": f"Bearer {made['key']}"}
    trip_id = auth_client.post("/api/ai/trips", headers=headers,
                               data={"name": "编码测试"}).json()["id"]

    class _Helper:
        def __init__(self):
            self.headers = headers
            self.trip_id = trip_id

        def post_raw(self, path, body: str):
            """body 里直接放 UTF-8 裸字节 —— 就是 `curl -d` 干的事。"""
            return auth_client.post(path, headers={**headers, **FORM_CT},
                                    content=body.encode("utf-8"))

        def put_form(self, path, data):
            return auth_client.put(path, headers=headers, data=data)

        def put_raw(self, path, body: str):
            return auth_client.put(path, headers={**headers, **FORM_CT},
                                   content=body.encode("utf-8"))

        def post_form(self, path, data):
            """requests 的正常路径：百分号编码。"""
            return auth_client.post(path, headers=headers, data=data)

    return _Helper()


# ---------------- 1) 表单编码 ----------------
def test_normalize_form_body_encodes_bare_utf8():
    assert formfix.normalize_form_body("name=芒康".encode()) == b"name=%E8%8A%92%E5%BA%B7"


def test_normalize_form_body_is_idempotent():
    once = formfix.normalize_form_body("name=芒康&d=测试".encode())
    assert formfix.normalize_form_body(once) == once


def test_normalize_form_body_leaves_non_utf8_alone():
    """不是 UTF-8 就别猜 —— 猜错比不动更糟。"""
    latin = "name=caf\xe9".encode("latin-1")
    assert formfix.normalize_form_body(latin) == latin


def test_create_point_with_bare_utf8_body_keeps_chinese(ai):
    """回归：这一条曾经把「芒康」存成「è\x8a\x92åº·」。"""
    r = ai.post_raw(f"/api/ai/trips/{ai.trip_id}/points",
                    "name=芒康&lat=29.68&lng=98.59&description=滇藏线上的一站")
    assert r.status_code == 200, r.text
    p = db.get_point(r.json()["id"])
    assert p["name"] == "芒康"
    assert p["description"] == "滇藏线上的一站"


def test_update_point_with_bare_utf8_body_keeps_chinese(ai):
    """线上那个坏点就是先建对、再被一次 update 写成乱码的。"""
    pid = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points",
                       {"name": "旧名", "lat": 29.68, "lng": 98.59}).json()["id"]
    r = ai.put_raw(f"/api/ai/points/{pid}",
                   "name=芒康&lat=29.68&lng=98.59&description=改过的描述")
    assert r.status_code == 200, r.text
    p = db.get_point(pid)
    assert p["name"] == "芒康" and p["description"] == "改过的描述"


def test_percent_encoded_body_still_works(ai):
    """正常客户端（requests/urllib）走的是这条路，不能被中间件改坏。"""
    r = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points",
                     {"name": "香格里拉", "lat": 27.8, "lng": 99.7})
    assert db.get_point(r.json()["id"])["name"] == "香格里拉"


def test_multipart_body_still_works(ai, auth_client):
    r = auth_client.post(f"/api/ai/trips/{ai.trip_id}/points", headers=ai.headers,
                         files={"name": (None, "德钦"), "lat": (None, "28.49"),
                                "lng": (None, "98.91")})
    assert r.status_code == 200, r.text
    assert db.get_point(r.json()["id"])["name"] == "德钦"


def test_web_ui_form_also_survives_middleware(auth_client):
    """中间件是全局的，Web UI 那批路由也得照样过。"""
    tid = auth_client.post("/api/trips", data={"name": "网页行程"}).json()["id"]
    r = auth_client.post(f"/api/trips/{tid}/points",
                         headers=FORM_CT,
                         content="name=虎跳峡&lat=27.19&lng=100.11".encode("utf-8"))
    assert r.status_code == 200, r.text
    assert db.get_point(r.json()["id"])["name"] == "虎跳峡"


# ---------------- 2) 日期 ----------------
@pytest.mark.parametrize("raw,expect", [
    ("2026-10-08", "2026-10-08"),
    ("2026/10/8", "2026-10-08"),
    ("2026.10.08", "2026-10-08"),
    ("2026年10月8日", "2026-10-08"),
])
def test_point_date_formats_are_normalized(ai, raw, expect):
    r = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points",
                     {"name": "芒康", "lat": 29.68, "lng": 98.59, "start_date": raw})
    assert r.status_code == 200, r.text
    assert db.get_point(r.json()["id"])["start_date"] == expect


@pytest.mark.parametrize("raw", ["10月8日", "下周二", "2026-13-01", "明天", "8/10"])
def test_unparseable_date_is_rejected_not_swallowed(ai, raw):
    """原来这些会被原样写进库，点就排不进时间线；现在直接 400 让 agent 重试。"""
    r = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points",
                     {"name": "芒康", "lat": 29.68, "lng": 98.59, "start_date": raw})
    assert r.status_code == 400
    assert "start_date" in r.json()["detail"]


def test_empty_date_still_allowed(ai):
    """没日期是合法的（还没排期的点），不能因为校验把它拦掉。"""
    r = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points",
                     {"name": "待定", "lat": 29.68, "lng": 98.59, "start_date": ""})
    assert r.status_code == 200
    assert db.get_point(r.json()["id"])["start_date"] is None


def test_trip_date_formats_are_normalized(ai, auth_client):
    r = auth_client.post("/api/ai/trips", headers=ai.headers,
                         data={"name": "日期行程", "start_date": "2026/10/1",
                               "end_date": "2026年10月8日"})
    t = db.get_trip(r.json()["id"])
    assert (t["start_date"], t["end_date"]) == ("2026-10-01", "2026-10-08")


# ---------------- 3) 局部更新：没传的字段不许被抹掉 ----------------
def test_update_point_keeps_untouched_fields(ai):
    """回归：agent 只想改名字，结果日期被抹成 NULL、描述被抹成空串。

    线上「芒康」那个点丢日期就是这么来的 —— 不是 agent 忘了传，是 update
    把它清了。
    """
    pid = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points", {
        "name": "德钦", "lat": 28.49, "lng": 98.91,
        "start_date": "2026-10-05", "end_date": "2026-10-05",
        "description": "飞来寺看日照金山", "route_mode": "line", "is_waypoint": "1",
    }).json()["id"]

    # agent：「把这个点改名叫芒康」—— 只传名字和坐标
    assert ai.put_form(f"/api/ai/points/{pid}",
                       {"name": "芒康", "lat": 29.68, "lng": 98.59}).status_code == 200

    p = db.get_point(pid)
    assert p["name"] == "芒康"
    assert p["start_date"] == "2026-10-05"
    assert p["end_date"] == "2026-10-05"
    assert p["description"] == "飞来寺看日照金山"
    assert p["route_mode"] == "line"
    assert p["is_waypoint"] == 1


def test_update_point_can_still_clear_a_field_explicitly(ai):
    """「不传 = 不改」不能把「清空」这条路堵死：传字面量 null 仍然要能清掉。

    为什么不是空串：FastAPI 的 Form 里「传了空串」和「根本没传」拿到的都是
    默认值，分不开 —— 所以清空得有个说得出口的写法。
    """
    pid = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points", {
        "name": "德钦", "lat": 28.49, "lng": 98.91,
        "start_date": "2026-10-05", "description": "先写着",
    }).json()["id"]
    ai.put_form(f"/api/ai/points/{pid}",
                {"name": "德钦", "lat": 28.49, "lng": 98.91,
                 "start_date": "null", "description": "null"})
    p = db.get_point(pid)
    assert p["start_date"] is None and p["description"] == ""


def test_update_point_changes_what_was_passed(ai):
    pid = ai.post_form(f"/api/ai/trips/{ai.trip_id}/points", {
        "name": "德钦", "lat": 28.49, "lng": 98.91, "start_date": "2026-10-05",
    }).json()["id"]
    ai.put_form(f"/api/ai/points/{pid}",
                {"name": "德钦", "lat": 28.49, "lng": 98.91, "start_date": "2026/10/6"})
    assert db.get_point(pid)["start_date"] == "2026-10-06"
