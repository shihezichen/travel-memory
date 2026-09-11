"""媒体下载（另存为）的 HTTP 面：文件名编码、quality 白名单、权限边界。

这一层最要紧的一条是**权限不比「能不能看」更宽**：
  - /api/media/{id}/download            只有本人/管理员
  - /api/share/{token}/media/{id}/download  匿名可下，但要 token 有效 + 密码对
    + 媒体确实属于这条分享的行程

所以下面越权相关的用例（别人的私有媒体、串行程的 media_id、错密码）是回归护栏，
删掉任何一条都等于把暴露面悄悄放大一档。
"""
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pytest

import app as app_mod
import db

pytestmark = pytest.mark.api


def _anon_client():
    """全新的未登录 client（不能拿 auth_client 冒充匿名，它已经登录过了）。"""
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app)


def _make_video(point_id, name="v.mp4"):
    src = Path(app_mod.UPLOAD_DIR) / name
    src.write_bytes(b"fake original video")
    return db.add_media(point_id, "video", name, None)


def _make_variant(media_id, label, stem="v", status="done", body=b"fake 720p bytes"):
    vdir = Path(app_mod.UPLOAD_DIR) / "variants"
    vdir.mkdir(parents=True, exist_ok=True)
    fname = f"{stem}_{label}.mp4"
    if status == "done":
        (vdir / fname).write_bytes(body)
    db.upsert_variant(media_id, label, filename=fname, status=status)
    return fname


def _disposition(resp):
    """把 Content-Disposition 拆成 (ascii_name, utf8_name)。"""
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;"), cd
    ascii_name = re.search(r'filename="([^"]*)"', cd).group(1)
    utf8_name = unquote(re.search(r"filename\*=UTF-8''([^;]+)", cd).group(1))
    return ascii_name, utf8_name


def _today():
    return datetime.now().strftime("%Y%m%d")


# ---------------------------------------------------------------- 基本下载
def test_download_photo_is_attachment(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    raw = jpeg_bytes()
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", raw, "image/jpeg")}).json()["id"]
    r = auth_client.get(f"/api/media/{mid}/download")
    assert r.status_code == 200
    assert r.content == raw                      # 下到的确实是原图字节
    assert r.headers["content-type"].startswith("image/jpeg")
    ascii_name, utf8_name = _disposition(r)
    # 「旅行记忆-<日期>-<地点>-<序号>.jpg」
    assert utf8_name == f"旅行记忆-{_today()}-西湖-01.jpg"
    assert ascii_name == f"travel-memory-{_today()}-01.jpg"


def test_download_filename_is_sequenced_within_point(auth_client, seeded_trip, jpeg_bytes):
    """同一个点里的第 N 张，名字里就是 -0N —— 和灯箱里看到的顺序一致。"""
    pid = seeded_trip["point_ids"][1]
    ids = [auth_client.post(f"/api/points/{pid}/media",
                            files={"file": (f"{i}.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
           for i in range(3)]
    names = [_disposition(auth_client.get(f"/api/media/{i}/download"))[1] for i in ids]
    assert [n.rsplit("-", 1)[1] for n in names] == ["01.jpg", "02.jpg", "03.jpg"]
    assert all("灵隐寺" in n for n in names)


def test_ascii_fallback_is_pure_ascii(auth_client, seeded_trip, jpeg_bytes):
    """老浏览器只认 filename=，那一份必须能被 latin-1 编码，否则整个头都发不出去。"""
    pid = seeded_trip["point_ids"][0]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    r = auth_client.get(f"/api/media/{mid}/download")
    ascii_name, _ = _disposition(r)
    ascii_name.encode("ascii")                   # 不抛就算过
    assert '"' not in ascii_name and "/" not in ascii_name


def test_download_name_survives_hostile_point_name(auth_client, seeded_trip, jpeg_bytes):
    """地点名是用户输入。带 ../ 和引号的名字不能漏进 Content-Disposition。"""
    tid = seeded_trip["trip_id"]
    pid = auth_client.post(f"/api/trips/{tid}/points", data={
        "name": '../../etc "x"/passwd', "lat": 30.0, "lng": 120.0,
        "start_date": "2026-05-01", "end_date": "2026-05-01"}).json()["id"]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    r = auth_client.get(f"/api/media/{mid}/download")
    assert r.status_code == 200
    ascii_name, utf8_name = _disposition(r)
    for name in (ascii_name, utf8_name):
        assert "/" not in name and "\\" not in name and '"' not in name
        assert ".." not in name


# ---------------------------------------------------------------- 清晰度档位
def test_download_video_original_by_default(auth_client, seeded_trip):
    mid = _make_video(seeded_trip["point_ids"][0])
    r = auth_client.get(f"/api/media/{mid}/download")
    assert r.status_code == 200 and r.content == b"fake original video"
    _, utf8_name = _disposition(r)
    assert utf8_name.endswith(".mp4") and "720p" not in utf8_name


def test_download_video_specific_quality(auth_client, seeded_trip):
    mid = _make_video(seeded_trip["point_ids"][0])
    _make_variant(mid, "720p")
    r = auth_client.get(f"/api/media/{mid}/download", params={"quality": "720p"})
    assert r.status_code == 200 and r.content == b"fake 720p bytes"
    _, utf8_name = _disposition(r)
    # 档位后缀不能省：不然 720p 和原画下下来同名，第二个会覆盖第一个
    assert utf8_name == f"旅行记忆-{_today()}-西湖-01-720p.mp4"


def test_download_is_not_gzipped(auth_client, seeded_trip):
    """视频/图片不能进 gzip 中间件。

    真视频动辄几百 MB，压它既省不下带宽（本来就压过了）又会把 CPU 和响应
    延迟顶上去。starlette 默认把 image/* 和 video/* 排除在外，这条用例是
    防止哪天有人调 GZipMiddleware 的参数时把这层默认保护顺手掀掉。
    """
    mid = _make_video(seeded_trip["point_ids"][0], name="big.mp4")
    (Path(app_mod.UPLOAD_DIR) / "big.mp4").write_bytes(b"\x00" * 200_000)
    r = auth_client.get(f"/api/media/{mid}/download",
                        headers={"Accept-Encoding": "gzip, deflate"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None, r.headers
    assert r.headers["content-length"] == "200000"


def test_download_supports_range_requests(auth_client, seeded_trip):
    """断点续传：手机上下几百 MB 的视频，断了要能接着下。"""
    mid = _make_video(seeded_trip["point_ids"][0])
    r = auth_client.get(f"/api/media/{mid}/download", headers={"Range": "bytes=5-9"})
    assert r.status_code == 206
    assert r.content == b"origi"                 # "fake original video"[5:10]
    assert r.headers["content-range"] == "bytes 5-9/19"


def test_download_quality_not_ready_is_404(auth_client, seeded_trip):
    mid = _make_video(seeded_trip["point_ids"][0])
    _make_variant(mid, "720p", status="running")
    assert auth_client.get(f"/api/media/{mid}/download",
                           params={"quality": "720p"}).status_code == 404


def test_download_quality_never_queued_is_404(auth_client, seeded_trip):
    mid = _make_video(seeded_trip["point_ids"][0])
    assert auth_client.get(f"/api/media/{mid}/download",
                           params={"quality": "480p"}).status_code == 404


@pytest.mark.parametrize("bad", [
    "../../data.db", "..", "../uploads", "720P", "1080", "orig", "",
    "variants/x.mp4", "%2e%2e%2f", "original/../../data.db",
])
def test_download_quality_whitelist(auth_client, seeded_trip, bad):
    """quality 参与选文件，所以只认字面量白名单。空串按 original 处理。"""
    mid = _make_video(seeded_trip["point_ids"][0])
    r = auth_client.get(f"/api/media/{mid}/download", params={"quality": bad})
    if bad == "":
        assert r.status_code == 200          # 空串 = 没传 = 原画
    else:
        assert r.status_code == 400, f"{bad!r} 竟然没被挡住"


def test_photo_with_video_quality_is_404(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    assert auth_client.get(f"/api/media/{mid}/download",
                           params={"quality": "720p"}).status_code == 404


def test_download_missing_file_on_disk_is_404(auth_client, seeded_trip):
    """库里有行、盘上没文件（历史数据）→ 404，不要 500。"""
    mid = db.add_media(seeded_trip["point_ids"][0], "photo", "ghost.jpg", None)
    assert auth_client.get(f"/api/media/{mid}/download").status_code == 404


def test_download_unknown_media_is_404(auth_client):
    assert auth_client.get("/api/media/999999/download").status_code == 404


# ---------------------------------------------------------------- 权限边界
def test_download_requires_login(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    assert _anon_client().get(f"/api/media/{mid}/download").status_code == 401


def test_download_other_users_private_media_is_404(auth_client, other_client,
                                                   seeded_trip, jpeg_bytes):
    """**核心护栏**：别人的私有媒体必须下不到（含指定档位那条路）。"""
    pid = seeded_trip["point_ids"][0]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    vid = _make_video(pid)
    _make_variant(vid, "720p")
    assert other_client.get(f"/api/media/{mid}/download").status_code == 404
    assert other_client.get(f"/api/media/{vid}/download").status_code == 404
    assert other_client.get(f"/api/media/{vid}/download",
                            params={"quality": "720p"}).status_code == 404


def test_admin_can_download_others_media(auth_client, make_user, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = auth_client.post(f"/api/points/{pid}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}).json()["id"]
    from fastapi.testclient import TestClient
    admin = make_user(is_admin=1)
    c = TestClient(app_mod.app)
    c.post("/api/login", data={"username": admin["username"],
                               "password": admin["_password"]})
    assert c.get(f"/api/media/{mid}/download").status_code == 200


# ---------------------------------------------------------------- 分享页匿名下载
def test_share_anonymous_download(auth_client, seeded_trip, jpeg_bytes):
    """分享页能看的，匿名就能下 —— 这条不算新增暴露面（/uploads/ 本来就公开）。"""
    tid = seeded_trip["trip_id"]
    raw = jpeg_bytes()
    mid = auth_client.post(f"/api/points/{seeded_trip['point_ids'][0]}/media",
                           files={"file": ("a.jpg", raw, "image/jpeg")}).json()["id"]
    token = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    r = _anon_client().get(f"/api/share/{token}/media/{mid}/download")
    assert r.status_code == 200 and r.content == raw
    assert r.headers["content-disposition"].startswith("attachment;")


def test_share_anonymous_download_quality(auth_client, seeded_trip):
    tid = seeded_trip["trip_id"]
    vid = _make_video(seeded_trip["point_ids"][0])
    _make_variant(vid, "480p", body=b"fake 480p bytes")
    token = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    r = _anon_client().get(f"/api/share/{token}/media/{vid}/download",
                           params={"quality": "480p"})
    assert r.status_code == 200 and r.content == b"fake 480p bytes"


def test_share_download_rejects_foreign_media(auth_client, other_client, seeded_trip,
                                              jpeg_bytes):
    """**核心护栏**：拿自己的 token 顺着 media_id 猜，不能下到别人行程的媒体。"""
    victim_tid = other_client.post("/api/trips", data={
        "name": "别人的行程", "start_date": "2026-06-01",
        "end_date": "2026-06-02"}).json()["id"]
    victim_pid = other_client.post(f"/api/trips/{victim_tid}/points", data={
        "name": "秘密", "lat": 1.0, "lng": 1.0,
        "start_date": "2026-06-01", "end_date": "2026-06-01"}).json()["id"]
    victim_mid = other_client.post(f"/api/points/{victim_pid}/media",
                                   files={"file": ("s.jpg", jpeg_bytes(), "image/jpeg")}
                                   ).json()["id"]

    my_token = auth_client.post(
        f"/api/trips/{seeded_trip['trip_id']}/share").json()["token"]
    r = _anon_client().get(f"/api/share/{my_token}/media/{victim_mid}/download")
    assert r.status_code == 404


def test_share_download_unknown_token_is_404(auth_client, seeded_trip, jpeg_bytes):
    mid = auth_client.post(f"/api/points/{seeded_trip['point_ids'][0]}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}
                           ).json()["id"]
    assert _anon_client().get(f"/api/share/nope/media/{mid}/download").status_code == 404


def test_share_download_honours_password(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    mid = auth_client.post(f"/api/points/{seeded_trip['point_ids'][0]}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}
                           ).json()["id"]
    token = auth_client.post(f"/api/trips/{tid}/share",
                             data={"password": "letmein"}).json()["token"]
    anon = _anon_client()
    assert anon.get(f"/api/share/{token}/media/{mid}/download").status_code == 403
    assert anon.get(f"/api/share/{token}/media/{mid}/download",
                    params={"password": "nope"}).status_code == 403
    assert anon.get(f"/api/share/{token}/media/{mid}/download",
                    params={"password": "letmein"}).status_code == 200


def test_share_download_expired_share_is_404(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    mid = auth_client.post(f"/api/points/{seeded_trip['point_ids'][0]}/media",
                           files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")}
                           ).json()["id"]
    token = auth_client.post(f"/api/trips/{tid}/share").json()["token"]
    conn = db.get_conn()
    conn.execute("UPDATE shares SET expires_at=datetime('now','localtime','-1 day') "
                 "WHERE token=?", (token,))
    conn.commit()
    conn.close()
    assert _anon_client().get(
        f"/api/share/{token}/media/{mid}/download").status_code == 404


# ---------------------------------------------------------------- 走 OSS 还是走本机
# 下载是这条链路上字节数最大的一段。原来只有「本地文件不存在」时才重定向到 OSS，
# 于是回源存档一跑完，一段 130MB 的视频就改由本机 FileResponse 吐出去，整整
# 130MB 全挤在 4Mbps 的 frp 隧道里 —— 而同一份字节在 OSS 上躺着没人用。
# 这组用例钉住新规则：**云上有就走云，只有云上没有才由本机吐**。

def _with_oss(monkeypatch, base="https://bkt.oss-cn-qingdao.aliyuncs.com", wm=None):
    """打开 MEDIA_BASE_URL + 一把假 AK，并把同步水位设成 wm。

    AK 也要给：下载链接必须签名（OSS 不许匿名请求覆盖响应头），没 AK 就签不出来，
    `_oss_redirect` 会老实返回 None。给一把假的就能走完真实的签名代码路径 ——
    签名本身是纯计算，不打网络。
    """
    import config
    monkeypatch.setattr(config, "MEDIA_BASE_URL", base)
    monkeypatch.setenv("TRAVEL_MEDIA_BASE_URL", base)
    monkeypatch.setenv("TRAVEL_OSS_AK", "TESTAK")
    monkeypatch.setenv("TRAVEL_OSS_SK", "TESTSK")
    monkeypatch.setattr(config, "_oss_watermark", lambda: wm)
    return base


def _set_mtime(path, ts):
    import os
    os.utime(path, (ts, ts))


def test_本地有但云上也有时重定向到oss(auth_client, seeded_trip, monkeypatch):
    mid = _make_video(seeded_trip["point_ids"][0], name="oss1.mp4")
    src = Path(app_mod.UPLOAD_DIR) / "oss1.mp4"
    _set_mtime(src, 1000)                       # 早于水位 = 已经同步上去了
    _with_oss(monkeypatch, wm=2000)
    r = auth_client.get(f"/api/media/{mid}/download", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://bkt.oss-cn-qingdao.aliyuncs.com/uploads/oss1.mp4")
    # 必须是签过的 URL，否则 OSS 回 400（匿名不许覆盖响应头）
    assert "Signature=" in loc and "OSSAccessKeyId=" in loc
    # 重定向过去也必须是「另存为」，否则浏览器会内联播放
    assert "response-content-disposition=" in loc
    assert "attachment" in unquote(loc)


def test_本地比水位新时老实走本机(auth_client, seeded_trip, monkeypatch):
    """刚重封装/刚转好还没推上去的那一小段时间：云上那份是旧的，不能指过去。"""
    mid = _make_video(seeded_trip["point_ids"][0], name="oss2.mp4")
    src = Path(app_mod.UPLOAD_DIR) / "oss2.mp4"
    _set_mtime(src, 3000)                       # 晚于水位 = 本地这份更新
    _with_oss(monkeypatch, wm=2000)
    r = auth_client.get(f"/api/media/{mid}/download", follow_redirects=False)
    assert r.status_code == 200 and r.content == b"fake original video"


def test_转码档位也走oss(auth_client, seeded_trip, monkeypatch):
    """ops/sync-oss.sh 一直在同步 variants/，档位文件云上是有的。"""
    mid = _make_video(seeded_trip["point_ids"][0], name="oss3.mp4")
    fname = _make_variant(mid, "720p", stem="oss3")
    _set_mtime(Path(app_mod.UPLOAD_DIR) / "variants" / fname, 1000)
    _with_oss(monkeypatch, wm=2000)
    r = auth_client.get(f"/api/media/{mid}/download", params={"quality": "720p"},
                        follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith(
        "https://bkt.oss-cn-qingdao.aliyuncs.com/uploads/variants/" + fname)
    assert "Signature=" in r.headers["location"]


def test_本地没有文件时仍然重定向到oss(auth_client, seeded_trip, monkeypatch):
    """直传刚完成、回源还没跑：本机一个字节都没有，只能指云上。"""
    mid = db.add_media(seeded_trip["point_ids"][0], "video", "gone.mp4", None)
    _with_oss(monkeypatch, wm=2000)
    r = auth_client.get(f"/api/media/{mid}/download", follow_redirects=False)
    assert r.status_code == 307
    assert "/uploads/gone.mp4" in r.headers["location"]


def test_没配oss时一律走本机(auth_client, seeded_trip, monkeypatch):
    mid = _make_video(seeded_trip["point_ids"][0], name="oss4.mp4")
    _set_mtime(Path(app_mod.UPLOAD_DIR) / "oss4.mp4", 1000)
    _with_oss(monkeypatch, base="", wm=2000)
    r = auth_client.get(f"/api/media/{mid}/download", follow_redirects=False)
    assert r.status_code == 200
