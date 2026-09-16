"""db.py —— 媒体的增删、排序、caption、检索、计数。"""
import pytest

import db

pytestmark = pytest.mark.unit


@pytest.fixture
def point(make_user):
    u = make_user()
    tid = db.create_trip(u, "T", None, None)
    pid = db.create_point(tid, "P", 30.0, 120.0, "2026-05-01", None)
    return {"user": u, "trip_id": tid, "point_id": pid}


def test_add_and_get_media(point):
    mid = db.add_media(point["point_id"], "photo", "a.jpg", "a_t.jpg",
                       "2026-05-01T10:00:00", 30.1, 120.1)
    m = db.get_media(mid)
    assert m["type"] == "photo" and m["filename"] == "a.jpg"
    assert m["thumb_filename"] == "a_t.jpg"
    assert m["exif_lat"] == 30.1
    assert m["sort_order"] == 0          # 0 = 还没手工排过
    assert db.get_media(999999) is None


def test_list_media_default_order_is_by_shot_time(point):
    late = db.add_media(point["point_id"], "photo", "late.jpg", "t1.jpg", "2026-05-01T18:00:00")
    early = db.add_media(point["point_id"], "photo", "early.jpg", "t2.jpg", "2026-05-01T08:00:00")
    assert [m["id"] for m in db.list_media(point["point_id"])] == [early, late]


def test_set_media_order_puts_manual_order_first(point):
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    b = db.add_media(point["point_id"], "photo", "b.jpg", "t.jpg", "2026-05-01T09:00:00")
    c = db.add_media(point["point_id"], "photo", "c.jpg", "t.jpg", "2026-05-01T10:00:00")
    assert db.set_media_order(point["point_id"], [c, a, b]) == 3
    assert [m["id"] for m in db.list_media(point["point_id"])] == [c, a, b]


def test_set_media_order_ignores_foreign_and_garbage_ids(point, make_user):
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg")
    other_pid = db.create_point(db.create_trip(make_user(), "X", None, None),
                                "OP", 1.0, 1.0, None, None)
    foreign = db.add_media(other_pid, "photo", "f.jpg", "t.jpg")
    n = db.set_media_order(point["point_id"], [a, foreign, "abc", None, 999999])
    assert n == 1
    assert db.get_media(foreign)["sort_order"] == 0   # 别人的没被动过


def test_newly_uploaded_media_sinks_below_manually_ordered(point):
    """已排序的排前面，之后新传的（sort_order=0）应沉底，而不是插进中间。"""
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    b = db.add_media(point["point_id"], "photo", "b.jpg", "t.jpg", "2026-05-01T09:00:00")
    db.set_media_order(point["point_id"], [b, a])
    fresh = db.add_media(point["point_id"], "photo", "new.jpg", "t.jpg", "2026-05-01T01:00:00")
    assert [m["id"] for m in db.list_media(point["point_id"])] == [b, a, fresh]


def test_set_media_caption_empty_becomes_null(point):
    mid = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg")
    db.set_media_caption(mid, "海很蓝")
    assert db.get_media(mid)["caption"] == "海很蓝"
    db.set_media_caption(mid, "")
    assert db.get_media(mid)["caption"] is None


def test_delete_media(point):
    mid = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg")
    db.delete_media(mid)
    assert db.get_media(mid) is None


def test_get_point_media_counts(point):
    db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg")
    db.add_media(point["point_id"], "photo", "b.jpg", "t.jpg")
    db.add_media(point["point_id"], "video", "c.mp4", "t.jpg")
    counts = db.get_point_media_counts(point["trip_id"])
    assert counts[point["point_id"]] == {"photo": 2, "video": 1}


def test_get_point_media_files_and_cascade_delete(point):
    db.add_media(point["point_id"], "photo", "a.jpg", "a_t.jpg")
    assert db.get_point_media_files(point["point_id"]) == [("a.jpg", "a_t.jpg")]
    assert db.get_trip_media_files(point["trip_id"]) == [("a.jpg", "a_t.jpg")]
    db.delete_point(point["point_id"])
    assert db.list_media(point["point_id"]) == []


def test_list_trip_media_carries_point_name(point):
    db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    items = db.list_trip_media(point["trip_id"])
    assert len(items) == 1
    assert items[0]["point_name"] == "P"


def test_search_trip_media_filters(point):
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    b = db.add_media(point["point_id"], "video", "b.mp4", "t.jpg", "2026-05-05T08:00:00")

    assert [m["id"] for m in db.search_trip_media(point["trip_id"], mtype="video")] == [b]
    # q 命中文件名 / 地点名（当前实现的检索域）
    assert [m["id"] for m in db.search_trip_media(point["trip_id"], q="a.jpg")] == [a]
    assert {m["id"] for m in db.search_trip_media(point["trip_id"], q="P")} == {a, b}
    assert db.search_trip_media(point["trip_id"], q="不存在的词") == []
    got = db.search_trip_media(point["trip_id"], date_from="2026-05-03")
    assert [m["id"] for m in got] == [b]
    got = db.search_trip_media(point["trip_id"], date_to="2026-05-02")
    assert [m["id"] for m in got] == [a]


def test_search_trip_media_should_match_caption(point):
    """caption 是用户自己写的文字，必须能被关键字搜到（曾为已知缺陷，2026-09-10 修复）。"""
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    db.set_media_caption(a, "日出很好看")
    assert [m["id"] for m in db.search_trip_media(point["trip_id"], q="日出")] == [a]


def test_search_trip_media_date_to_same_day_should_include(point):
    """date_to 给到某天时，当天的照片必须包含在内（曾为已知缺陷，2026-09-10 修复）。"""
    a = db.add_media(point["point_id"], "photo", "a.jpg", "t.jpg", "2026-05-01T08:00:00")
    assert [m["id"] for m in db.search_trip_media(point["trip_id"],
                                                  date_to="2026-05-01")] == [a]


def test_list_photo_points_scoping_and_shape(point, make_user):
    db.add_media(point["point_id"], "photo", "a.jpg", "a_t.jpg")
    # 视频用首帧缩略图一起进图层，带 type 让前端打 ▶（2026-09-11 改）：
    # 之前只取 photo，手机连拍的视频在照片堆里凭空消失，看着像上传丢了。
    db.add_media(point["point_id"], "video", "v.mp4", "v_t.jpg")
    out = db.list_photo_points(point["user"], point["trip_id"])
    assert len(out) == 1
    assert out[0]["point_id"] == point["point_id"]
    assert [(p["thumb"], p["type"]) for p in out[0]["photos"]] == [
        ("a_t.jpg", "photo"), ("v_t.jpg", "video")]
    # 别人看不到
    assert db.list_photo_points(make_user(), point["trip_id"]) == []


def test_list_photo_points_skips_media_without_thumbnail(point):
    db.add_media(point["point_id"], "photo", "a.jpg", None)
    assert db.list_photo_points(point["user"], point["trip_id"]) == []


def test_list_photo_points_gives_thumb_url(point):
    """每条 photo 必须带 thumb_url（服务端算好的对外地址）。

    回归用例：原来只给文件名 `thumb`，前端只好自己拼 /uploads/thumbs/xxx。
    Bucket 转私有之后那条裸链是 403 —— 地图上照片堆整片空白，而接口本身
    200、日志干净，只有图是白的（2026-09-16 实测到的 bug）。
    """
    db.add_media(point["point_id"], "photo", "a.jpg", "a_t.jpg")
    out = db.list_photo_points(point["user"], point["trip_id"])
    ph = out[0]["photos"][0]
    assert ph["thumb"] == "a_t.jpg"
    assert ph["thumb_url"], "thumb_url 不能为空 —— 前端没有它就只能拼裸链"
    # 地址必须指向缩略图本身，不能是原图（缩略图小，图层里几十张直接用原图会拖垮加载）
    assert "a_t.jpg" in ph["thumb_url"]
