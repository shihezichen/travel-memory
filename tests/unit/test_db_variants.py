"""db.py —— 视频清晰度档位表（media_variants）的读写、级联、批量查询。"""
import pytest

import db

pytestmark = pytest.mark.unit


@pytest.fixture
def vid(make_user):
    """一个带视频的点。返回 trip/point/media 三个 id。"""
    u = make_user()
    tid = db.create_trip(u, "T", None, None)
    pid = db.create_point(tid, "P", 30.0, 120.0, None, None)
    mid = db.add_media(pid, "video", "v.mp4", "v.jpg")
    return {"user": u, "trip_id": tid, "point_id": pid, "media_id": mid}


def test_upsert_creates_then_updates_in_place(vid):
    """同一个 (media, label) 反复写只有一行 —— 回填脚本能反复跑的前提。"""
    mid = vid["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", width=720, height=958,
                      status="pending")
    db.upsert_variant(mid, "720p", status="done", bitrate=931, filesize=699987)

    rows = db.list_variants(mid)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "done" and r["bitrate"] == 931 and r["filesize"] == 699987
    # 没重新传的字段保持原值，不会被清成 NULL
    assert r["filename"] == "v_720p.mp4" and r["width"] == 720 and r["height"] == 958


def test_upsert_ignores_unknown_fields(vid):
    """只认白名单字段，防止调用方拼错列名拼出个 SQL 错误。"""
    db.upsert_variant(vid["media_id"], "720p", filename="v.mp4", nonsense="x", id=999)
    rows = db.list_variants(vid["media_id"])
    assert len(rows) == 1 and rows[0]["filename"] == "v.mp4"
    assert rows[0]["id"] != 999


def test_upsert_with_no_fields_just_creates_row(vid):
    db.upsert_variant(vid["media_id"], "480p")
    rows = db.list_variants(vid["media_id"])
    assert len(rows) == 1 and rows[0]["status"] == "pending"   # 默认值


def test_get_variant(vid):
    db.upsert_variant(vid["media_id"], "720p", filename="v_720p.mp4", status="done")
    got = db.get_variant(vid["media_id"], "720p")
    assert got["label"] == "720p" and got["status"] == "done"
    assert db.get_variant(vid["media_id"], "1080p") is None
    assert db.get_variant(999999, "720p") is None


def test_set_variant_status_clears_error_on_success(vid):
    mid = vid["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4")
    db.set_variant_status(mid, "720p", "failed", error="ffmpeg 炸了")
    assert db.get_variant(mid, "720p")["error"] == "ffmpeg 炸了"

    db.set_variant_status(mid, "720p", "done")
    r = db.get_variant(mid, "720p")
    assert r["status"] == "done" and r["error"] is None


def test_updated_at_moves_forward(vid):
    db.upsert_variant(vid["media_id"], "720p", status="pending")
    first = db.get_variant(vid["media_id"], "720p")["updated_at"]
    assert first is not None
    db.set_variant_status(vid["media_id"], "720p", "done")
    assert db.get_variant(vid["media_id"], "720p")["updated_at"] >= first


def test_list_variants_scoped_to_one_media(vid):
    other = db.add_media(vid["point_id"], "video", "o.mp4", None)
    db.upsert_variant(vid["media_id"], "720p", filename="v_720p.mp4")
    db.upsert_variant(other, "480p", filename="o_480p.mp4")
    assert [r["label"] for r in db.list_variants(vid["media_id"])] == ["720p"]
    assert [r["label"] for r in db.list_variants(other)] == ["480p"]
    assert db.list_variants(999999) == []


def test_get_variants_map_batches(vid):
    mid = vid["media_id"]
    other = db.add_media(vid["point_id"], "video", "o.mp4", None)
    bare = db.add_media(vid["point_id"], "video", "b.mp4", None)
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="done")
    db.upsert_variant(mid, "480p", filename="v_480p.mp4", status="pending")
    db.upsert_variant(other, "720p", filename="o_720p.mp4", status="done")

    m = db.get_variants_map([mid, other, bare])
    assert sorted(x["label"] for x in m[mid]) == ["480p", "720p"]
    assert [x["label"] for x in m[other]] == ["720p"]
    assert bare not in m                       # 没档位的就不在 map 里


def test_get_variants_map_empty_and_none_ids(vid):
    assert db.get_variants_map([]) == {}
    assert db.get_variants_map([None, None]) == {}


def test_get_variants_map_over_sqlite_variable_limit(vid):
    """SQLite 默认最多 999 个绑定变量，超过要分批 —— 相册大了必踩。"""
    ids = []
    for i in range(1200):
        m = db.add_media(vid["point_id"], "video", f"v{i}.mp4", None)
        ids.append(m)
    db.upsert_variant(ids[0], "720p", filename="a.mp4", status="done")
    db.upsert_variant(ids[-1], "480p", filename="z.mp4", status="done")

    m = db.get_variants_map(ids)
    assert [x["label"] for x in m[ids[0]]] == ["720p"]
    assert [x["label"] for x in m[ids[-1]]] == ["480p"]


def test_delete_variants_returns_filenames_and_clears_rows(vid):
    mid = vid["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="done")
    db.upsert_variant(mid, "480p", filename="v_480p.mp4", status="done")
    db.upsert_variant(mid, "1080p", status="failed")       # 没 filename，不该混进来

    names = db.delete_variants(mid)
    assert sorted(names) == ["v_480p.mp4", "v_720p.mp4"]
    assert db.list_variants(mid) == []


def test_delete_variants_on_media_without_variants(vid):
    assert db.delete_variants(vid["media_id"]) == []


def test_deleting_media_cascades_variant_rows(vid):
    """删 media 时档位行必须跟着走，否则档位表会越积越多的孤儿行。"""
    mid = vid["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="done")
    db.delete_media(mid)
    assert db.list_variants(mid) == []


def test_deleting_point_cascades_variant_rows(vid):
    db.upsert_variant(vid["media_id"], "720p", filename="v_720p.mp4", status="done")
    db.delete_point(vid["point_id"])
    assert db.list_variants(vid["media_id"]) == []


def test_get_point_variant_files(vid):
    """删点之前要先把档位文件名捞出来 —— 行会级联消失，盘上的文件不会。"""
    mid = vid["media_id"]
    other = db.add_media(vid["point_id"], "video", "o.mp4", None)
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="done")
    db.upsert_variant(other, "480p", filename="o_480p.mp4", status="done")
    db.upsert_variant(other, "720p", status="failed")           # 没文件名，跳过

    assert sorted(db.get_point_variant_files(vid["point_id"])) == \
        ["o_480p.mp4", "v_720p.mp4"]
    assert db.get_point_variant_files(999999) == []


def test_get_trip_variant_files(vid, make_user):
    mid = vid["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="done")
    # 第二个点，同一行程
    pid2 = db.create_point(vid["trip_id"], "P2", 31.0, 121.0, None, None)
    m2 = db.add_media(pid2, "video", "w.mp4", None)
    db.upsert_variant(m2, "480p", filename="w_480p.mp4", status="done")
    # 另一个行程的不能混进来
    u2 = make_user()
    tid2 = db.create_trip(u2, "T2", None, None)
    pid3 = db.create_point(tid2, "P3", 32.0, 122.0, None, None)
    m3 = db.add_media(pid3, "video", "x.mp4", None)
    db.upsert_variant(m3, "720p", filename="x_720p.mp4", status="done")

    got = sorted(db.get_trip_variant_files(vid["trip_id"]))
    assert got == ["v_720p.mp4", "w_480p.mp4"]
    assert db.get_trip_variant_files(999999) == []


def test_list_all_videos_only_videos_in_id_order(vid):
    """回填脚本靠它取活；按 id 升序才能「中断后从上次那条继续」。"""
    db.add_media(vid["point_id"], "photo", "p.jpg", None)
    b = db.add_media(vid["point_id"], "video", "b.mp4", None)
    a = db.add_media(vid["point_id"], "video", "a.mp4", None)

    rows = db.list_all_videos()
    assert [r["id"] for r in rows] == sorted([vid["media_id"], b, a])
    assert {r["filename"] for r in rows} == {"v.mp4", "b.mp4", "a.mp4"}
    assert all("filename" in r for r in rows)


def test_list_all_videos_empty_db(make_user):
    assert db.list_all_videos() == []
