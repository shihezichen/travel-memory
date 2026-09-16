"""视频清晰度档位的 HTTP 面：查档位 / 重试 / 列表挂 qualities / 删除清盘。

注意：这一层**不会真跑 ffmpeg**。tests/conftest.py 的 isolated_env 默认把
transcode.enqueue 换成了记录器，所以「上传触发了转码」是靠断言那个记录器有没有
被调到来验证的；档位数据一律直接写库造出来。
"""
from pathlib import Path

import pytest

import app as app_mod
import db
import transcode

pytestmark = pytest.mark.api


def _anon_client():
    """全新的未登录 client。

    注意别用 conftest 里的 `client` fixture 来测「未登录」——
    auth_client 就是在同一个 client 对象上登录的，拿它当匿名用户会静默失真。
    """
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app)


def _variant_dir():
    return Path(app_mod.UPLOAD_DIR) / "variants"


def _make_video(point_id, name="v.mp4"):
    """造一条视频 media，连同盘上的原片文件。"""
    src = Path(app_mod.UPLOAD_DIR) / name
    src.write_bytes(b"fake video bytes")
    return db.add_media(point_id, "video", name, None)


def _make_variant(media_id, label, stem="v", status="done", **kw):
    """造一个档位：写库 + 在盘上放一个同名文件。"""
    fname = f"{stem}_{label}.mp4"
    if status == "done":
        _variant_dir().mkdir(parents=True, exist_ok=True)
        (_variant_dir() / fname).write_bytes(b"fake variant bytes")
    db.upsert_variant(media_id, label, filename=fname, status=status, **kw)
    return fname


# ---------------------------------------------------------------- 上传触发转码
def test_upload_video_enqueues_transcode(auth_client, seeded_trip):
    """上传视频要把转码丢后台，且**不阻塞**这次请求。"""
    pid = seeded_trip["point_ids"][0]
    r = auth_client.post(f"/api/points/{pid}/media",
                         files={"file": ("clip.mp4", b"\x00" * 64, "video/mp4")})
    assert r.status_code == 200
    calls = transcode._TEST_CALLS
    assert len(calls) == 1
    assert calls[0]["media_id"] == r.json()["id"]
    assert calls[0]["path"].endswith(r.json()["filename"])


def test_upload_photo_does_not_enqueue_transcode(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    r = auth_client.post(f"/api/points/{pid}/media",
                         files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200
    assert transcode._TEST_CALLS == []


# ---------------------------------------------------------------- GET variants
def test_variants_endpoint_returns_ladder(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p", width=720, height=958, bitrate=931, filesize=699987)
    _make_variant(mid, "480p", width=480, height=638, bitrate=496, filesize=373328)

    r = auth_client.get(f"/api/media/{mid}/variants")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "video"
    assert body["transcoding"] is False
    q = body["qualities"]
    assert [x["label"] for x in q] == ["original", "720p", "480p"]
    assert q[0]["url"] == "/uploads/v.mp4"          # 不带 ?v=1：会被签进 OSS key
    assert q[1]["url"] == "/uploads/variants/v_720p.mp4"
    assert q[1]["width"] == 720 and q[1]["height"] == 958 and q[1]["bitrate"] == 931


def test_variants_endpoint_reports_transcoding_in_progress(auth_client, seeded_trip):
    """转码中：档位列出来但没有 url，transcoding=true 让前端继续轮询。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p", status="running")
    _make_variant(mid, "480p", status="pending")

    body = auth_client.get(f"/api/media/{mid}/variants").json()
    assert body["transcoding"] is True
    byl = {x["label"]: x for x in body["qualities"]}
    assert byl["720p"]["status"] == "running" and byl["720p"]["url"] is None
    assert byl["480p"]["status"] == "pending" and byl["480p"]["url"] is None
    # 原画永远可播 —— 转码没跟上也不会让人看不了视频
    assert byl["original"]["status"] == "done" and byl["original"]["url"]


def test_variants_endpoint_failed_tier_still_leaves_original(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p", status="failed")

    body = auth_client.get(f"/api/media/{mid}/variants").json()
    assert body["transcoding"] is False          # 失败不算「还在转」，别无限轮询
    byl = {x["label"]: x for x in body["qualities"]}
    assert byl["720p"]["status"] == "failed" and byl["720p"]["url"] is None
    assert byl["original"]["url"]


def test_variants_endpoint_video_without_variants(auth_client, seeded_trip):
    """源本来就小 → 只有原画一档，这是正常状态不是错误。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    body = auth_client.get(f"/api/media/{mid}/variants").json()
    assert [x["label"] for x in body["qualities"]] == ["original"]
    assert body["transcoding"] is False


def test_variants_endpoint_photo_returns_empty(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = db.add_media(pid, "photo", "a.jpg", "t.jpg")
    body = auth_client.get(f"/api/media/{mid}/variants").json()
    assert body["type"] == "photo" and body["qualities"] == []


def test_variants_endpoint_404_for_missing_media(auth_client):
    assert auth_client.get("/api/media/999999/variants").status_code == 404


def test_variants_endpoint_is_anonymous(auth_client, seeded_trip):
    """分享页是匿名的，它必须能查转码进度，否则档位永远停在「转码中」。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p")

    r = _anon_client().get(f"/api/media/{mid}/variants")
    assert r.status_code == 200
    assert any(x["label"] == "720p" for x in r.json()["qualities"])


def test_variants_path_whitelist_does_not_leak_other_media_apis(auth_client, seeded_trip):
    """放行的只有 /api/media/{id}/variants 这一条，别的媒体接口仍要登录。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    anon = _anon_client()
    assert anon.delete(f"/api/media/{mid}").status_code == 401
    # 多一段路径就落不进白名单分支，写操作仍旧要登录
    assert anon.post(f"/api/media/{mid}/variants/retry").status_code == 401
    # 路径长得像但 id 不是数字的，也不放行
    assert anon.get("/api/media/abc/variants").status_code == 401


# ---------------------------------------------------------------- 重试
def test_retry_resets_failed_and_requeues(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p", status="failed", error="boom")
    _make_variant(mid, "480p", status="done")

    r = auth_client.post(f"/api/media/{mid}/variants/retry")
    assert r.status_code == 200 and r.json()["queued"] is True
    rows = {v["label"]: v for v in db.list_variants(mid)}
    assert rows["720p"]["status"] == "pending" and rows["720p"]["error"] is None
    assert rows["480p"]["status"] == "done"
    assert transcode._TEST_CALLS and transcode._TEST_CALLS[-1]["media_id"] == mid


def test_retry_rejects_photo(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = db.add_media(pid, "photo", "a.jpg", "t.jpg")
    assert auth_client.post(f"/api/media/{mid}/variants/retry").status_code == 400


def test_retry_rejects_other_users_media(other_client, auth_client, seeded_trip):
    """别人的视频不能重试。返回 404 而不是 403 是 require_owner 的既定约定：
    对无权访问的资源一律说「不存在」，不泄露它是否存在。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    assert other_client.post(f"/api/media/{mid}/variants/retry").status_code == 404
    assert db.get_media(mid) is not None       # 但东西还在，只是不给他看


def test_retry_missing_media_404(auth_client):
    assert auth_client.post("/api/media/999999/variants/retry").status_code == 404


# ---------------------------------------------------------------- 列表接口挂 qualities
def test_point_media_list_carries_qualities(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p", width=720, height=958)
    db.add_media(pid, "photo", "a.jpg", "t.jpg")

    rows = auth_client.get(f"/api/points/{pid}/media").json()
    byid = {r["id"]: r for r in rows}
    assert [q["label"] for q in byid[mid]["qualities"]] == ["original", "720p"]
    photo = [r for r in rows if r["type"] == "photo"][0]
    assert "qualities" not in photo              # 照片没有这个键


def test_trip_album_carries_qualities(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "480p", width=480, height=638)

    items = auth_client.get(f"/api/trips/{seeded_trip['trip_id']}/media").json()["items"]
    vid_item = [i for i in items if i["id"] == mid][0]
    assert [q["label"] for q in vid_item["qualities"]] == ["original", "480p"]
    # 原有字段一个没丢
    for key in ("id", "type", "point_id", "filename", "url", "thumb_url", "taken_at"):
        assert key in vid_item


def test_share_view_carries_qualities(auth_client, seeded_trip):
    """匿名分享页也要能选清晰度。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    _make_variant(mid, "720p")
    tok = auth_client.post(f"/api/trips/{seeded_trip['trip_id']}/share").json()["token"]

    body = _anon_client().get(f"/api/share/{tok}").json()
    vids = [m for p in body["points"] for m in p["media"] if m["type"] == "video"]
    assert vids and [q["label"] for q in vids[0]["qualities"]] == ["original", "720p"]


# ---------------------------------------------------------------- 删除清盘
def test_delete_media_removes_variant_files_and_rows(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    f720 = _make_variant(mid, "720p")
    f480 = _make_variant(mid, "480p")
    assert (_variant_dir() / f720).exists()

    assert auth_client.delete(f"/api/media/{mid}").status_code == 200
    assert not (_variant_dir() / f720).exists()
    assert not (_variant_dir() / f480).exists()
    assert not (Path(app_mod.UPLOAD_DIR) / "v.mp4").exists()
    assert db.list_variants(mid) == []


def test_delete_media_survives_missing_variant_file(auth_client, seeded_trip):
    """盘上文件早就没了也不能让删除接口 500。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    fname = _make_variant(mid, "720p")
    (_variant_dir() / fname).unlink()
    assert auth_client.delete(f"/api/media/{mid}").status_code == 200


def test_delete_point_removes_variant_files(auth_client, seeded_trip):
    """删点是级联删 media 的，档位文件不跟着删就成了永远没人认领的孤儿。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    fname = _make_variant(mid, "720p")
    assert (_variant_dir() / fname).exists()

    assert auth_client.delete(f"/api/points/{pid}").status_code == 200
    assert not (_variant_dir() / fname).exists()
    assert db.list_variants(mid) == []


def test_delete_trip_removes_variant_files(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    fname = _make_variant(mid, "720p")

    assert auth_client.delete(f"/api/trips/{tid}").status_code == 200
    assert not (_variant_dir() / fname).exists()


# ---------------------------------------------------------------- 静态资源缓存
def _static_client():
    """把 UploadStaticFiles 挂到**临时** uploads 上的一次性 app。

    不能直接打主 app 的 /uploads：那个 mount 在 import app 时就把目录绑死在
    生产 uploads/ 上了，isolated_env 改 app.UPLOAD_DIR 也改不动它（改得动才危险）。
    这里要验的是这个 StaticFiles 子类的缓存策略，单独挂一份最干净。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    a = FastAPI()
    a.mount("/uploads", app_mod.UploadStaticFiles(directory=app_mod.UPLOAD_DIR))
    return TestClient(a)


def test_variant_files_get_immutable_cache(auth_client, seeded_trip):
    """档位文件内容永不改变，值得一年 immutable：重播不该再回源。"""
    pid = seeded_trip["point_ids"][0]
    mid = _make_video(pid)
    fname = _make_variant(mid, "720p")

    r = _static_client().get(f"/uploads/variants/{fname}")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]
    assert "max-age=31536000" in r.headers["cache-control"]


def test_original_video_is_not_immutable(auth_client, seeded_trip):
    """原片会被 faststart 重封装**原地覆盖**，给了 immutable 客户端就会
    一直留着重封装之前那份卡顿版。"""
    pid = seeded_trip["point_ids"][0]
    _make_video(pid)
    r = _static_client().get("/uploads/v.mp4")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "immutable" not in cc and "max-age=86400" in cc


def test_versioned_url_is_immutable(auth_client, seeded_trip):
    """前端主动打了 ?v= 版本号的，走父类的一年 immutable。"""
    pid = seeded_trip["point_ids"][0]
    _make_video(pid)
    r = _static_client().get("/uploads/v.mp4?v=1")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]
