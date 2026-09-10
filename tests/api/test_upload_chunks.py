"""分片上传 / 断点续传接口。

覆盖三件事，缺一不可：
  1. 正确性 —— 乱序上传 + 断点续传合出来的文件必须**字节级**等于原文件（比 sha256）。
  2. 不开口子 —— 体积上限、类型判定、归属校验在分片路径上和单请求路径上一样严；
     尤其归属校验必须**每个分片请求**都做。
  3. 收尾一致 —— 合并完成后要和单请求上传走同一段收尾（缩略图 + 写库 + 转码入队）。
"""
import hashlib
from pathlib import Path

import pytest

import app as app_mod
import transcode as transcode_mod

pytestmark = pytest.mark.api

CHUNK = app_mod.CHUNK_SIZE_MIN          # 256KB，服务端允许的最小分片


def _blob(n_chunks=3, tail=1234, seed=b"tm"):
    """造一个 n_chunks-1 个满片 + 一个零头的文件内容。"""
    body = hashlib.sha256(seed).digest() * ((CHUNK * (n_chunks - 1) + tail) // 32 + 1)
    return body[:CHUNK * (n_chunks - 1) + tail]


def _create(client, pid, data, name="big.mp4", fp="fp-1", chunk_size=CHUNK,
            filesize=None):
    return client.post(f"/api/points/{pid}/upload-sessions", data={
        "fingerprint": fp, "filename": name,
        "filesize": len(data) if filesize is None else filesize,
        "chunk_size": chunk_size})


def _put(client, sid, idx, data, chunk_size=CHUNK):
    part = data[idx * chunk_size:(idx + 1) * chunk_size]
    return client.put(f"/api/upload-sessions/{sid}/chunks/{idx}",
                      files={"file": (f"{idx}.part", part,
                                      "application/octet-stream")})


def _complete(client, sid):
    return client.post(f"/api/upload-sessions/{sid}/complete")


# --------------------------------------------------------------- 正常路径
def test_create_session_reports_plan(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    r = _create(auth_client, pid, data)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["total_chunks"] == 3 and s["chunk_size"] == CHUNK
    assert s["type"] == "video" and s["status"] == "uploading"
    assert s["received"] == [] and s["resumed"] is False
    assert len(s["session_id"]) == 32


def test_chunks_out_of_order_merge_byte_exact(auth_client, seeded_trip):
    """乱序上传三片，合并结果必须和原文件 sha256 一致。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    for idx in (2, 0, 1):
        assert _put(auth_client, sid, idx, data).status_code == 200

    r = _complete(auth_client, sid)
    assert r.status_code == 200, r.text
    out = Path(app_mod.UPLOAD_DIR) / r.json()["filename"]
    assert out.exists()
    assert hashlib.sha256(out.read_bytes()).hexdigest() == \
        hashlib.sha256(data).hexdigest()
    assert out.stat().st_size == len(data)

    lst = auth_client.get(f"/api/points/{pid}/media").json()
    assert [m["id"] for m in lst] == [r.json()["id"]]
    assert lst[0]["type"] == "video" and lst[0]["thumb_filename"]


def test_merge_enqueues_transcode(auth_client, seeded_trip):
    """合并完成也要调 transcode.on_media_uploaded —— 漏了它，分片传上来的视频
    没有 faststart、没有低码率档位，播放体验直接倒退。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob(n_chunks=2, tail=10)
    sid = _create(auth_client, pid, data).json()["session_id"]
    for i in range(2):
        _put(auth_client, sid, i, data)
    mid = _complete(auth_client, sid).json()["id"]
    assert [c["media_id"] for c in transcode_mod._TEST_CALLS] == [mid]


def test_photo_goes_through_exif_and_thumb(auth_client, seeded_trip, jpeg_bytes):
    """照片走分片路径时，缩略图/EXIF 这段收尾不能被跳过。"""
    pid = seeded_trip["point_ids"][0]
    data = jpeg_bytes(size=(1200, 900))
    sid = _create(auth_client, pid, data, name="p.jpg",
                  chunk_size=CHUNK).json()["session_id"]
    _put(auth_client, sid, 0, data)
    r = _complete(auth_client, sid)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "photo" and r.json()["thumb"]


# --------------------------------------------------------------- 断点续传
def test_resume_returns_received_chunks(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    _put(auth_client, sid, 2, data)

    again = _create(auth_client, pid, data).json()          # 同指纹 → 复用
    assert again["session_id"] == sid and again["resumed"] is True
    assert again["received"] == [0, 2]
    assert again["received_bytes"] == CHUNK + (len(data) - 2 * CHUNK)

    _put(auth_client, sid, 1, data)
    r = _complete(auth_client, sid)
    assert hashlib.sha256((Path(app_mod.UPLOAD_DIR) / r.json()["filename"])
                          .read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()


def test_point_sessions_listing_powers_the_placeholder(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    lst = auth_client.get(f"/api/points/{pid}/upload-sessions").json()
    assert [s["session_id"] for s in lst] == [sid]
    assert lst[0]["filename"] == "big.mp4" and lst[0]["received"] == [0]

    # 传完之后就不该再出现在「未完成」列表里
    _put(auth_client, sid, 1, data)
    _put(auth_client, sid, 2, data)
    _complete(auth_client, sid)
    assert auth_client.get(f"/api/points/{pid}/upload-sessions").json() == []


def test_abort_removes_session_and_chunk_files(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    d = Path(app_mod.UPLOAD_DIR) / ".chunks" / sid
    assert (d / "0").exists()

    assert auth_client.delete(f"/api/upload-sessions/{sid}").status_code == 200
    assert not d.exists()
    assert auth_client.get(f"/api/points/{pid}/upload-sessions").json() == []
    # 作废之后不能再往里塞分片
    assert _put(auth_client, sid, 1, data).status_code == 409


def test_changing_chunk_size_restarts_the_session(auth_client, seeded_trip):
    """同指纹但分片大小变了：必须换一个新会话，不能拿新片长去拼老偏移。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    s2 = _create(auth_client, pid, data, chunk_size=CHUNK * 2).json()
    assert s2["session_id"] != sid and s2["received"] == []
    assert not (Path(app_mod.UPLOAD_DIR) / ".chunks" / sid).exists()


def test_complete_is_idempotent(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob(n_chunks=1, tail=999)
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    a = _complete(auth_client, sid).json()
    b = _complete(auth_client, sid)
    assert b.status_code == 200 and b.json()["id"] == a["id"]
    assert b.json().get("duplicate") is True
    assert len(auth_client.get(f"/api/points/{pid}/media").json()) == 1


# --------------------------------------------------------------- 不能开的口子
def test_declared_size_over_limit_is_rejected_at_creation(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = _create(auth_client, pid, b"x", filesize=app_mod.MAX_UPLOAD_VIDEO + 1,
                chunk_size=8 * 1024 * 1024)
    assert r.status_code == 413
    r2 = _create(auth_client, pid, b"x", name="p.jpg",
                 filesize=app_mod.MAX_UPLOAD_PHOTO + 1, chunk_size=8 * 1024 * 1024)
    assert r2.status_code == 413


def test_unsupported_extension_is_rejected_at_creation(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = _create(auth_client, pid, b"x" * 100, name="evil.exe", filesize=100)
    assert r.status_code == 400 and "不支持的文件类型" in r.json()["detail"]


def test_chunk_must_have_the_exact_expected_size(auth_client, seeded_trip):
    """分片长度必须精确 —— 这条保证「所有片都在」等价于「总字节 == filesize」，
    谎报 filesize 之后猛塞数据这条路就不存在了。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    r = auth_client.put(f"/api/upload-sessions/{sid}/chunks/0",
                        files={"file": ("0", b"short", "application/octet-stream")})
    assert r.status_code == 400 and "分片大小不符" in r.json()["detail"]
    big = auth_client.put(f"/api/upload-sessions/{sid}/chunks/0",
                          files={"file": ("0", b"y" * (CHUNK + 1),
                                          "application/octet-stream")})
    assert big.status_code == 400


def test_chunk_index_out_of_range(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    assert _put(auth_client, sid, 99, data).status_code == 400


def test_complete_with_missing_chunk_is_409(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    r = _complete(auth_client, sid)
    assert r.status_code == 409
    assert r.json()["detail"]["missing"] == [1, 2]
    assert auth_client.get(f"/api/points/{pid}/media").json() == []


def test_other_user_is_blocked_on_every_chunk_request(auth_client, other_client,
                                                      seeded_trip):
    """归属校验不能只在建会话时做一次。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)

    assert other_client.get(f"/api/upload-sessions/{sid}").status_code == 404
    assert _put(other_client, sid, 1, data).status_code == 404
    assert _complete(other_client, sid).status_code == 404
    assert other_client.delete(f"/api/upload-sessions/{sid}").status_code == 404
    assert other_client.get(f"/api/points/{pid}/upload-sessions").status_code == 404
    assert _create(other_client, pid, data).status_code == 404
    # 别人捣乱之后，本人的会话依旧完好
    assert auth_client.get(f"/api/upload-sessions/{sid}").json()["received"] == [0]


def test_anonymous_cannot_touch_sessions(auth_client, seeded_trip):
    from fastapi.testclient import TestClient
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    # auth_client 和 client fixture 是同一个对象（已登录），这里要一个干净的匿名客户端
    with TestClient(app_mod.app) as anon:
        assert anon.get(f"/api/upload-sessions/{sid}").status_code == 401
        assert anon.post(f"/api/upload-sessions/{sid}/complete").status_code == 401
        assert anon.post(f"/api/points/{pid}/upload-sessions",
                         data={"fingerprint": "x", "filename": "a.mp4",
                               "filesize": 100}).status_code == 401


def test_session_id_is_not_a_path(auth_client, seeded_trip):
    """会话 id 会被拼进文件路径，形状不对一律当不存在。"""
    for bad in ("../../etc", "..", "a" * 32, "/abs/path"):
        r = auth_client.get(f"/api/upload-sessions/{bad}")
        assert r.status_code == 404, bad


def test_chunk_files_are_not_downloadable(auth_client, seeded_trip):
    """半成品分片就落在 uploads/ 下面，绝不能被 /uploads/ 静态路由读出去。"""
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    assert auth_client.get(f"/uploads/.chunks/{sid}/0").status_code == 404


# --------------------------------------------------------------- 过期清理
def test_prune_removes_stale_sessions_and_files(auth_client, seeded_trip):
    import db
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    d = Path(app_mod.UPLOAD_DIR) / ".chunks" / sid

    conn = db.get_conn()
    conn.execute("UPDATE upload_sessions SET updated_at=datetime('now','localtime',"
                 "'-30 hours') WHERE session_id=?", (sid,))
    conn.commit()
    conn.close()

    stat = app_mod.prune_upload_sessions(hours=24)
    assert stat["sessions"] == 1
    assert not d.exists()
    assert db.get_upload_session(sid) is None


def test_prune_keeps_fresh_sessions(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    data = _blob()
    sid = _create(auth_client, pid, data).json()["session_id"]
    _put(auth_client, sid, 0, data)
    assert app_mod.prune_upload_sessions(hours=24)["sessions"] == 0
    assert auth_client.get(f"/api/upload-sessions/{sid}").json()["received"] == [0]


def test_single_request_upload_still_works(auth_client, seeded_trip, jpeg_bytes):
    """老接口必须继续可用 —— 分享页、旧客户端、现有测试都在用它。"""
    pid = seeded_trip["point_ids"][0]
    r = auth_client.post(f"/api/points/{pid}/media",
                         files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200 and r.json()["thumb"]
