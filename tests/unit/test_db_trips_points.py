"""db.py —— 行程 / 轨迹点 / 归属 / 排序。"""
import pytest

import db

pytestmark = pytest.mark.unit


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture
def admin(make_user):
    return make_user(is_admin=1)


def test_create_and_get_trip(user):
    tid = db.create_trip(user, "环岛", "2026-01-01", "2026-01-05", "描述", "planning")
    t = db.get_trip(tid)
    assert t["name"] == "环岛" and t["status"] == "planning"
    assert t["user_id"] == user["id"]
    assert db.get_trip(999999) is None


def test_list_trips_only_shows_own(user, make_user):
    other = make_user()
    db.create_trip(user, "我的", None, None)
    db.create_trip(other, "别人的", None, None)
    names = [t["name"] for t in db.list_trips(user)]
    assert names == ["我的"]


def test_list_trips_admin_sees_all(user, admin):
    db.create_trip(user, "我的", None, None)
    db.create_trip(admin, "管理员的", None, None)
    assert len(db.list_trips(admin)) == 2


def test_update_trip_partial_fields_only(user):
    tid = db.create_trip(user, "原名", "2026-01-01", "2026-01-02", "原描述", "planning")
    db.update_trip(tid, status="traveling")          # 只改 status
    t = db.get_trip(tid)
    assert t["status"] == "traveling"
    assert t["name"] == "原名" and t["description"] == "原描述"
    db.update_trip(tid)                               # 全 None = 空操作
    assert db.get_trip(tid)["name"] == "原名"


def test_delete_trip_cascades_points(user):
    tid = db.create_trip(user, "T", None, None)
    db.create_point(tid, "P", 30.0, 120.0, None, None)
    db.delete_trip(tid)
    assert db.get_trip(tid) is None
    assert db.list_points(tid) == []


def test_owner_lookup_helpers(user):
    tid = db.create_trip(user, "T", None, None)
    pid = db.create_point(tid, "P", 30.0, 120.0, None, None)
    mid = db.add_media(pid, "photo", "x.jpg", "x.jpg")
    assert db.get_trip_owner_id(tid) == user["id"]
    assert db.get_point_owner_id(pid) == user["id"]
    assert db.get_media_owner_id(mid) == user["id"]
    assert db.get_trip_owner_id(999999) is None
    assert db.get_point_owner_id(999999) is None
    assert db.get_media_owner_id(999999) is None


def test_create_point_defaults(user):
    tid = db.create_trip(user, "T", None, None)
    pid = db.create_point(tid, "西湖", 30.259, 120.13, "2026-05-01", None)
    p = db.get_point(pid)
    assert p["route_mode"] == "drive"
    assert p["is_waypoint"] == 0
    assert p["elevation"] is None
    assert p["coord_sys"] == "gcj02"


def test_update_point_and_route_mode(user):
    tid = db.create_trip(user, "T", None, None)
    pid = db.create_point(tid, "A", 1.0, 2.0, None, None)
    db.update_point(pid, "B", 3.0, 4.0, "2026-02-01", "2026-02-02", "desc", "line", 1)
    p = db.get_point(pid)
    assert (p["name"], p["lat"], p["lng"], p["route_mode"], p["is_waypoint"]) == \
        ("B", 3.0, 4.0, "line", 1)
    db.set_point_route_mode(pid, "drive")
    assert db.get_point(pid)["route_mode"] == "drive"


def test_list_points_ordered_by_date_then_sort_order(user):
    tid = db.create_trip(user, "T", None, None)
    p3 = db.create_point(tid, "第三天", 3.0, 3.0, "2026-05-03", None)
    p1 = db.create_point(tid, "第一天", 1.0, 1.0, "2026-05-01", None)
    p2 = db.create_point(tid, "第二天", 2.0, 2.0, "2026-05-02", None)
    assert [p["id"] for p in db.list_points(tid)] == [p1, p2, p3]


def test_reorder_points_within_same_day(user):
    tid = db.create_trip(user, "T", None, None)
    a = db.create_point(tid, "A", 1.0, 1.0, "2026-05-01", None)
    b = db.create_point(tid, "B", 2.0, 2.0, "2026-05-01", None)
    c = db.create_point(tid, "C", 3.0, 3.0, "2026-05-01", None)
    assert db.reorder_points(tid, [c, a, b]) is True
    assert [p["name"] for p in db.list_points(tid)] == ["C", "A", "B"]


def test_reorder_points_does_not_reorder_across_days(user):
    """跨天顺序由 start_date 决定 —— 就算把第二天的点排在最前面也不该越到第一天前。"""
    tid = db.create_trip(user, "T", None, None)
    d1 = db.create_point(tid, "D1", 1.0, 1.0, "2026-05-01", None)
    d2 = db.create_point(tid, "D2", 2.0, 2.0, "2026-05-02", None)
    assert db.reorder_points(tid, [d2, d1]) is True
    assert [p["name"] for p in db.list_points(tid)] == ["D1", "D2"]


def test_reorder_points_rejects_bad_input(user, make_user):
    tid = db.create_trip(user, "T", None, None)
    a = db.create_point(tid, "A", 1.0, 1.0, "2026-05-01", None)
    other_tid = db.create_trip(make_user(), "别人的", None, None)
    foreign = db.create_point(other_tid, "X", 9.0, 9.0, "2026-05-01", None)

    assert db.reorder_points(tid, []) is False              # 空
    assert db.reorder_points(tid, [a, foreign]) is False    # 混入别的行程的点
    assert db.reorder_points(tid, [999999]) is False        # 不存在


def test_reorder_points_should_reject_duplicate_ids(user):
    """重复 id 必须被拒绝（曾为已知缺陷，2026-09-10 修复）。"""
    tid = db.create_trip(user, "T", None, None)
    a = db.create_point(tid, "A", 1.0, 1.0, "2026-05-01", None)
    assert db.reorder_points(tid, [a, a]) is False


def test_list_all_points_scoping(user, make_user, admin):
    other = make_user()
    t1 = db.create_trip(user, "我的", None, None)
    db.create_point(t1, "我的点", 30.0, 120.0, None, None)
    t2 = db.create_trip(other, "别人的", None, None)
    db.create_point(t2, "别人的点", 31.0, 121.0, None, None)
    assert [p["name"] for p in db.list_all_points(user)] == ["我的点"]
    assert len(db.list_all_points(admin)) == 2


def test_get_trip_stats(user):
    tid = db.create_trip(user, "T", None, None)
    p = db.create_point(tid, "A", 1.0, 1.0, "2026-05-01", "2026-05-03")
    db.add_media(p, "photo", "a.jpg", "a.jpg")
    db.add_media(p, "video", "b.mp4", "b.jpg")
    s = db.get_trip_stats(tid)
    assert s["point_count"] == 1
    assert s["photo_count"] == 1
    assert s["video_count"] == 1
    assert db.get_trip_stats(999999) is None


def test_elevation_backfill_helpers(user):
    tid = db.create_trip(user, "T", None, None)
    p = db.create_point(tid, "A", 30.0, 120.0, None, None)
    assert [r["id"] for r in db.list_points_without_elevation(tid)] == [p]
    db.set_point_elevation(p, 1234.5)
    assert db.list_points_without_elevation(tid) == []
    assert db.get_point(p)["elevation"] == 1234.5
