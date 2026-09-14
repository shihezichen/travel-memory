"""删媒体时必须把 OSS 上的对象一起清掉，否则云端只增不减。

这里同时钉住六个删除入口（app.py 的删行程/点位/媒体 + agent_api 的同三个）——
它们原来各写各的 unlink，结果三处漏删 variants、一处（ai_delete_point）
干脆什么文件都不删。收敛到 media_purge 之后，用参数化把六条路一起测。
"""
import pytest

import app as app_mod
import db
import media as media_mod
import media_purge

pytestmark = pytest.mark.api


@pytest.fixture
def oss_spy(monkeypatch):
    """记录所有被删的 OSS key，并让后台线程池同步执行（测试要可断言）。"""
    deleted = []

    monkeypatch.setattr(media_purge, "_oss_ready", lambda: True)
    monkeypatch.setattr(media_purge.oss, "delete_object", lambda key: deleted.append(key))

    class _Inline:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)

    monkeypatch.setattr(media_purge, "_pool", lambda: _Inline())
    return deleted


# ---------------------------------------------------------------- 单元


def test_oss_keys_use_the_same_prefixes_as_sync(tmp_path):
    keys = media_purge.oss_keys(["a.jpg"], ["a.jpg"], ["a_720p.mp4"])
    assert keys == ["uploads/a.jpg", "uploads/thumbs/a.jpg", "uploads/variants/a_720p.mp4"]


def test_purge_removes_local_files(tmp_path, oss_spy):
    up, th = tmp_path / "up", tmp_path / "th"
    (up / "variants").mkdir(parents=True)
    th.mkdir()
    (up / "x.mp4").write_bytes(b"x")
    (th / "x.jpg").write_bytes(b"t")
    (up / "variants" / "x_720p.mp4").write_bytes(b"v")

    media_purge.purge(up, th, ["x.mp4"], ["x.jpg"], ["x_720p.mp4"])

    assert not (up / "x.mp4").exists()
    assert not (th / "x.jpg").exists()
    assert not (up / "variants" / "x_720p.mp4").exists()
    assert set(oss_spy) == {"uploads/x.mp4", "uploads/thumbs/x.jpg",
                            "uploads/variants/x_720p.mp4"}


def test_purge_deletes_from_oss_even_when_local_is_absent(tmp_path, oss_spy):
    """演示账号的媒体只在 OSS 上，本地一个文件都没有 —— 照样要能删掉。"""
    up, th = tmp_path / "up", tmp_path / "th"
    (up / "variants").mkdir(parents=True)
    th.mkdir()

    media_purge.purge(up, th, ["test_1_1_1.jpg"], ["test_1_1_1.jpg"])

    assert set(oss_spy) == {"uploads/test_1_1_1.jpg", "uploads/thumbs/test_1_1_1.jpg"}


def test_purge_skips_oss_when_unconfigured(tmp_path, monkeypatch):
    """本机开发没配 AK：只删本地，静默跳过，不该炸也不该刷日志。"""
    called = []
    monkeypatch.setattr(media_purge, "_oss_ready", lambda: False)
    monkeypatch.setattr(media_purge.oss, "delete_object", lambda k: called.append(k))
    up, th = tmp_path / "up", tmp_path / "th"
    (up / "variants").mkdir(parents=True)
    th.mkdir()
    (up / "y.jpg").write_bytes(b"y")

    assert media_purge.purge(up, th, ["y.jpg"]) == 0
    assert not (up / "y.jpg").exists()
    assert called == []


def test_purge_ignores_empty_names(tmp_path, oss_spy):
    """thumb_filename 可以是 NULL（直传后还没回源补缩略图），别拼出 uploads/thumbs/ 这种 key。"""
    up, th = tmp_path / "up", tmp_path / "th"
    (up / "variants").mkdir(parents=True)
    th.mkdir()

    media_purge.purge(up, th, ["z.mp4"], [None], [])
    assert oss_spy == ["uploads/z.mp4"]


# ---------------------------------------------------------------- 六个入口


def _make_media(auth_client, jpeg_bytes, seeded_trip):
    """往第一个点上传一张照片，返回 (point_id, media_id, filename, thumb)。"""
    pid = seeded_trip["point_ids"][0]
    r = auth_client.post(f"/api/points/{pid}/media",
                         files={"file": ("p.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    m = r.json()
    return pid, m["id"], m["filename"], m["thumb"]


def test_delete_media_endpoint_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy):
    pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)

    assert auth_client.delete(f"/api/media/{mid}").status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/thumbs/{thumb}" in oss_spy
    assert db.get_media(mid) is None


def test_delete_point_endpoint_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy):
    pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)

    assert auth_client.delete(f"/api/points/{pid}").status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/thumbs/{thumb}" in oss_spy


def test_delete_trip_endpoint_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy):
    pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)
    tid = seeded_trip["trip_id"]

    assert auth_client.delete(f"/api/trips/{tid}").status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/thumbs/{thumb}" in oss_spy


def test_video_variants_are_purged_too(auth_client, jpeg_bytes, seeded_trip, oss_spy, tmp_path):
    """删视频要连 720p/480p 一起清 —— 这三处原来全漏了。"""
    pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)
    db.upsert_variant(mid, "720p", filename=f"{mid}_720p.mp4", status="ready")

    assert auth_client.delete(f"/api/media/{mid}").status_code == 200

    assert f"uploads/variants/{mid}_720p.mp4" in oss_spy


# ---------------------------------------------------------------- AI 接口三条


@pytest.fixture
def ai_headers(auth_client):
    """write scope 的 Bearer 头。AI 那三条删除路径都要它。"""
    r = auth_client.post("/api/console/keys",
                         data={"name": "purge-test", "scope": "write", "expire_days": 0})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer %s" % r.json()["key"]}


def test_ai_delete_media_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy, ai_headers):
    _pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)
    db.upsert_variant(mid, "720p", filename=f"{mid}_720p.mp4", status="ready")

    assert auth_client.delete(f"/api/ai/media/{mid}", headers=ai_headers).status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/thumbs/{thumb}" in oss_spy
    assert f"uploads/variants/{mid}_720p.mp4" in oss_spy      # 原来漏了


def test_ai_delete_point_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy, ai_headers):
    """这条原来什么文件都不删 —— 删完点位，磁盘和 OSS 上全是查无主的孤儿。"""
    pid, _mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)

    assert auth_client.delete(f"/api/ai/points/{pid}", headers=ai_headers).status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/thumbs/{thumb}" in oss_spy


def test_ai_delete_trip_purges_oss(auth_client, jpeg_bytes, seeded_trip, oss_spy, ai_headers):
    _pid, mid, fname, thumb = _make_media(auth_client, jpeg_bytes, seeded_trip)
    db.upsert_variant(mid, "480p", filename=f"{mid}_480p.mp4", status="ready")
    tid = seeded_trip["trip_id"]

    assert auth_client.delete(f"/api/ai/trips/{tid}", headers=ai_headers).status_code == 200

    assert f"uploads/{fname}" in oss_spy
    assert f"uploads/variants/{mid}_480p.mp4" in oss_spy       # 原来漏了
