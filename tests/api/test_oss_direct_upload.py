"""OSS 直传的服务端那一半。

真的打阿里云的只有 tests/unit/test_oss_sign.py 里的签名构造（纯计算）。这里把
oss 模块整个换成一个**内存里的假 OSS**：分片存在 dict 里，ListParts / Complete /
HeadObject 都按真实语义实现。这样能覆盖到真打云端时很难构造的分支 ——
分片不全、大小不符、超限、重复 complete、过期 uploadId。

守的重点：
  · 对象名（key）永远是服务端生成的，客户端报什么都进不来
  · complete **不信客户端报的分片**，一律以 ListParts 为准
  · 归属校验每个请求都做，不是只在建会话时查一次
  · 完成之后 media 行立刻在，缩略图留空等回源补
"""
import io

import pytest
from PIL import Image

import app as app_mod
import db
import oss_backfill


class FakeOSS:
    """够用的假 OSS。只实现直传链路真正会调的那几个方法。"""

    class OSSError(RuntimeError):
        pass

    PART_SIZE = 8 * 1024 * 1024
    PART_SIZE_MIN = 100 * 1024
    MAX_PARTS = 10000

    def __init__(self):
        self.uploads = {}        # upload_id -> {key, parts: {n: (etag, size)}}
        self.objects = {}        # key -> bytes 大小
        self.aborted = []
        self.seq = 0

    # --- 配置 ---
    def direct_upload_enabled(self):
        return True

    def object_url(self, key):
        return "https://fake.oss/" + key

    def plan_parts(self, filesize, part_size=None):
        ps = max(self.PART_SIZE_MIN, int(part_size or self.PART_SIZE))
        total = (max(1, int(filesize)) + ps - 1) // ps
        return ps, total

    # --- 分片 ---
    def init_multipart(self, key, filename="", cache_control=None):
        self.seq += 1
        uid = f"UP{self.seq}"
        self.uploads[uid] = {"key": key, "parts": {}}
        return uid

    def sign_part_url(self, key, upload_id, n, expires_in=3600):
        return f"https://fake.oss/{key}?partNumber={n}&uploadId={upload_id}&Signature=x"

    def list_parts(self, key, upload_id):
        u = self.uploads.get(upload_id)
        if not u:
            raise self.OSSError("NoSuchUpload")
        return [{"number": n, "etag": e, "size": s}
                for n, (e, s) in sorted(u["parts"].items())]

    def complete_multipart(self, key, upload_id, parts):
        u = self.uploads.pop(upload_id)
        self.objects[key] = sum(s for _, s in u["parts"].values())
        return {"etag": "final", "url": self.object_url(key)}

    def abort_multipart(self, key, upload_id):
        self.aborted.append((key, upload_id))
        self.uploads.pop(upload_id, None)

    def head_object(self, key):
        if key not in self.objects:
            raise self.OSSError("NoSuchKey")
        return {"size": self.objects[key], "etag": "final",
                "last_modified": "Thu, 11 Sep 2026 02:00:00 GMT",
                "content_type": "video/mp4"}

    # --- 测试辅助：模拟浏览器把一片传上去 ---
    def put_part(self, upload_id, n, size):
        self.uploads[upload_id]["parts"][n] = (f"ETAG{n}", size)


@pytest.fixture
def fake_oss(monkeypatch):
    f = FakeOSS()
    monkeypatch.setattr(app_mod, "oss", f)
    # 回源在这些用例里不该真跑（它会去下载），换成只记账
    calls = []
    monkeypatch.setattr(app_mod.oss_backfill, "enqueue",
                        lambda mid, key, size=None, mtype=None: (
                            calls.append({"media_id": mid, "key": key,
                                          "filesize": size, "mtype": mtype}) or True))
    f.backfill_calls = calls
    return f


@pytest.fixture
def point_id(auth_client, seeded_trip):
    return seeded_trip["point_ids"][0]


def _create(client, point_id, name="a.mp4", size=10 * 1024 * 1024, fp="fp-1"):
    return client.post(f"/api/points/{point_id}/oss-uploads",
                       data={"fingerprint": fp, "filename": name,
                             "filesize": str(size)})


# --------------------------------------------------------------- 建会话

def test_建会话返回分片计划和第一批签名(auth_client, point_id, fake_oss):
    r = _create(auth_client, point_id, size=20 * 1024 * 1024)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "oss"
    assert d["total_parts"] == 3          # 20MB / 8MB 向上取整
    assert d["part_size"] == 8 * 1024 * 1024
    assert d["received"] == []
    assert sorted(d["urls"].keys()) == ["1", "2", "3"]
    assert d["mtype"] == "video"


def test_对象名由服务端生成客户端插不上手(auth_client, point_id, fake_oss):
    # 文件名里带路径也没用：服务端只取扩展名，名字是新 uuid
    _create(auth_client, point_id, name="../../etc/passwd.mp4")
    key = next(iter(fake_oss.uploads.values()))["key"]
    assert key.startswith("uploads/")
    assert ".." not in key
    assert key.endswith(".mp4")
    assert len(key.split("/")) == 2


def test_不支持的扩展名直接拒(auth_client, point_id, fake_oss):
    r = _create(auth_client, point_id, name="x.exe")
    assert r.status_code == 400


def test_超过上限的文件在建会话时就拒(auth_client, point_id, fake_oss, monkeypatch):
    monkeypatch.setattr(app_mod, "MAX_UPLOAD_VIDEO", 5 * 1024 * 1024)
    r = _create(auth_client, point_id, size=10 * 1024 * 1024)
    assert r.status_code == 413
    assert fake_oss.uploads == {}          # 没白开一个 multipart


def test_别人的点传不进来(other_client, auth_client, point_id, fake_oss):
    r = _create(other_client, point_id)
    assert r.status_code in (403, 404)


def test_没登录传不进来(auth_client, point_id, fake_oss):
    # client fixture 和 auth_client 是同一个对象（已登录），匿名要另起一个
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as anon:
        assert _create(anon, point_id).status_code == 401


def test_同一个文件再建会话是续传不是重开(auth_client, point_id, fake_oss):
    d1 = _create(auth_client, point_id, size=20 * 1024 * 1024).json()
    uid = next(iter(fake_oss.uploads))
    fake_oss.put_part(uid, 1, 8 * 1024 * 1024)

    d2 = _create(auth_client, point_id, size=20 * 1024 * 1024).json()
    assert d2["resumed"] is True
    assert d2["session_id"] == d1["session_id"]
    assert [p["number"] for p in d2["received"]] == [1]
    # 只签还没传的那两片
    assert sorted(d2["urls"].keys()) == ["2", "3"]


def test_指纹撞车但大小变了就作废重来(auth_client, point_id, fake_oss):
    d1 = _create(auth_client, point_id, size=20 * 1024 * 1024).json()
    d2 = _create(auth_client, point_id, size=30 * 1024 * 1024).json()
    assert d2["session_id"] != d1["session_id"]
    assert fake_oss.aborted, "老会话在 OSS 那边的碎片必须撤掉"


def test_uploadid_在云上没了就重开一个(auth_client, point_id, fake_oss):
    d1 = _create(auth_client, point_id).json()
    fake_oss.uploads.clear()               # 模拟云端过期清理
    d2 = _create(auth_client, point_id).json()
    assert d2["session_id"] != d1["session_id"]
    assert d2["resumed"] is False


def test_直传没启用时回_503(auth_client, point_id, fake_oss, monkeypatch):
    monkeypatch.setattr(fake_oss, "direct_upload_enabled", lambda: False)
    r = _create(auth_client, point_id)
    assert r.status_code == 503


# --------------------------------------------------------------- 补签 / 查询

def test_补签分片_url(auth_client, point_id, fake_oss):
    sid = _create(auth_client, point_id, size=30 * 1024 * 1024).json()["session_id"]
    r = auth_client.post(f"/api/oss-uploads/{sid}/sign", data={"parts": "2,4"})
    assert r.status_code == 200
    assert sorted(r.json()["urls"].keys()) == ["2", "4"]


def test_补签时越界的序号被丢掉(auth_client, point_id, fake_oss):
    sid = _create(auth_client, point_id, size=10 * 1024 * 1024).json()["session_id"]
    r = auth_client.post(f"/api/oss-uploads/{sid}/sign", data={"parts": "0,1,99"})
    assert sorted(r.json()["urls"].keys()) == ["1"]


def test_别人拿不到我的会话(auth_client, other_client, point_id, fake_oss):
    sid = _create(auth_client, point_id).json()["session_id"]
    assert other_client.get(f"/api/oss-uploads/{sid}").status_code in (403, 404)
    assert other_client.post(f"/api/oss-uploads/{sid}/sign",
                             data={"parts": "1"}).status_code in (403, 404)
    assert other_client.delete(f"/api/oss-uploads/{sid}").status_code in (403, 404)


def test_隧道会话不能走直传接口(auth_client, point_id, fake_oss):
    """两套会话不能互相串门 —— 串了就会拿着 None 的 uploadId 去调 OSS。"""
    sid = auth_client.post(
        f"/api/points/{point_id}/upload-sessions",
        data={"fingerprint": "fp-x", "filename": "a.mp4",
              "filesize": "1000", "chunk_size": str(1 << 20)}).json()["session_id"]
    assert auth_client.get(f"/api/oss-uploads/{sid}").status_code == 400


# --------------------------------------------------------------- complete

def _upload_all(fake_oss, filesize, part_size):
    uid = next(iter(fake_oss.uploads))
    total = (filesize + part_size - 1) // part_size
    for n in range(1, total + 1):
        size = part_size if n < total else filesize - part_size * (total - 1)
        fake_oss.put_part(uid, n, size)
    return uid


def test_传完之后媒体立刻可见缩略图留空等回源(auth_client, point_id, fake_oss):
    size = 20 * 1024 * 1024
    d = _create(auth_client, point_id, size=size).json()
    _upload_all(fake_oss, size, d["part_size"])

    r = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["type"] == "video"
    assert out["thumb"] is None
    assert out["pending_backfill"] is True
    assert out["filename"].endswith(".mp4")

    m = db.get_media(out["id"])
    assert m and m["filename"] == out["filename"]
    assert m["thumb_filename"] is None

    # 回源排进队列了，且带着正确的 key 和大小
    assert fake_oss.backfill_calls == [
        {"media_id": out["id"], "key": "uploads/" + out["filename"],
         "filesize": size, "mtype": "video"}]


def test_分片不全时拒绝合并并告诉少了哪几片(auth_client, point_id, fake_oss):
    size = 20 * 1024 * 1024
    d = _create(auth_client, point_id, size=size).json()
    uid = next(iter(fake_oss.uploads))
    fake_oss.put_part(uid, 1, 8 * 1024 * 1024)

    r = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete")
    assert r.status_code == 409
    assert r.json()["detail"]["missing"] == [2, 3]


def test_分片总大小和声明不符时拒绝(auth_client, point_id, fake_oss):
    size = 20 * 1024 * 1024
    d = _create(auth_client, point_id, size=size).json()
    uid = next(iter(fake_oss.uploads))
    for n in (1, 2, 3):
        fake_oss.put_part(uid, n, 1024)          # 明显对不上
    r = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete")
    assert r.status_code == 400


def test_实际传上来的超了上限就撤掉不留文件(auth_client, point_id, fake_oss, monkeypatch):
    size = 20 * 1024 * 1024
    d = _create(auth_client, point_id, size=size).json()
    _upload_all(fake_oss, size, d["part_size"])
    # 建会话时上限还宽，合并这一刻收紧 —— 模拟「客户端谎报大小」
    monkeypatch.setattr(app_mod, "MAX_UPLOAD_VIDEO", 1024)
    r = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete")
    assert r.status_code == 413
    assert fake_oss.aborted
    assert fake_oss.objects == {}


def test_重复_complete_是幂等的(auth_client, point_id, fake_oss):
    size = 8 * 1024 * 1024
    d = _create(auth_client, point_id, size=size).json()
    _upload_all(fake_oss, size, d["part_size"])
    a = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete").json()
    b = auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete").json()
    assert b["id"] == a["id"]
    assert b["duplicate"] is True
    assert len(db.list_media(point_id)) == 1


def test_放弃会话会让云端碎片一起没(auth_client, point_id, fake_oss):
    d = _create(auth_client, point_id).json()
    assert auth_client.delete(f"/api/oss-uploads/{d['session_id']}").status_code == 200
    assert fake_oss.aborted
    assert db.get_upload_session(d["session_id"])["status"] == "aborted"


def test_已结束的会话不能再补签(auth_client, point_id, fake_oss):
    d = _create(auth_client, point_id).json()
    auth_client.delete(f"/api/oss-uploads/{d['session_id']}")
    r = auth_client.post(f"/api/oss-uploads/{d['session_id']}/sign", data={"parts": "1"})
    assert r.status_code == 409


# --------------------------------------------------------------- 客户端缩略图

def _jpeg(size=(300, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _complete_one(auth_client, point_id, fake_oss, size=8 * 1024 * 1024):
    d = _create(auth_client, point_id, size=size).json()
    _upload_all(fake_oss, size, d["part_size"])
    return auth_client.post(f"/api/oss-uploads/{d['session_id']}/complete").json()


def test_浏览器抽的封面能补上去(auth_client, point_id, fake_oss):
    out = _complete_one(auth_client, point_id, fake_oss)
    r = auth_client.post(f"/api/media/{out['id']}/thumb",
                         files={"file": ("t.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    name = r.json()["thumb"]
    assert name == out["filename"].rsplit(".", 1)[0] + ".jpg"
    assert (app_mod.UPLOAD_DIR / "thumbs" / name).is_file()
    assert db.get_media(out["id"])["thumb_filename"] == name


def test_已经有缩略图时客户端顶不掉它(auth_client, point_id, fake_oss):
    out = _complete_one(auth_client, point_id, fake_oss)
    db.set_media_thumb(out["id"], "real.jpg")
    r = auth_client.post(f"/api/media/{out['id']}/thumb",
                         files={"file": ("t.jpg", _jpeg(), "image/jpeg")})
    assert r.json()["skipped"] is True
    assert db.get_media(out["id"])["thumb_filename"] == "real.jpg"


def test_不是图片的东西落不进公开的_thumbs_目录(auth_client, point_id, fake_oss):
    out = _complete_one(auth_client, point_id, fake_oss)
    r = auth_client.post(f"/api/media/{out['id']}/thumb",
                         files={"file": ("t.jpg", b"<?php evil ?>", "image/jpeg")})
    assert r.status_code == 400
    assert db.get_media(out["id"])["thumb_filename"] is None


def test_缩略图过大被拒(auth_client, point_id, fake_oss, monkeypatch):
    monkeypatch.setattr(app_mod, "CLIENT_THUMB_MAX", 100)
    out = _complete_one(auth_client, point_id, fake_oss)
    r = auth_client.post(f"/api/media/{out['id']}/thumb",
                         files={"file": ("t.jpg", _jpeg((800, 600)), "image/jpeg")})
    assert r.status_code == 413


def test_别人的媒体补不了缩略图(auth_client, other_client, point_id, fake_oss):
    out = _complete_one(auth_client, point_id, fake_oss)
    r = other_client.post(f"/api/media/{out['id']}/thumb",
                          files={"file": ("t.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code in (403, 404)


# --------------------------------------------------------------- 过期清理

def test_过期的直传会话会把云端碎片一起撤掉(auth_client, point_id, fake_oss):
    d = _create(auth_client, point_id).json()
    conn = db.get_conn()
    conn.execute("UPDATE upload_sessions SET updated_at=datetime('now','-48 hours') "
                 "WHERE session_id=?", (d["session_id"],))
    conn.commit()
    conn.close()
    out = app_mod.prune_upload_sessions(hours=24)
    assert out["oss_aborted"] == 1
    assert fake_oss.aborted
