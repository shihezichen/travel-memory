"""OSS 回源存档：把直传上去的对象拉回本机，并把上传链路欠下的收尾补齐。

这条链路上「慢」是设计，但**不能丢**：失败要留痕、重启要接着干、拉回来的文件
要能被同步脚本和 media_url 正确识别成「已经在云上了」。

oss 模块整个换成假的（不打网络），但假下载会真的往盘上写一张真 JPEG ——
缩略图和 EXIF 那两步必须走真代码，否则这个测试就只是在测 mock。
"""
import io
import os
import time
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest
from PIL import Image

import db
import oss_backfill


def _jpeg_bytes(size=(400, 300)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 60)).save(buf, "JPEG")
    return buf.getvalue()


class FakeOSS:
    class OSSError(RuntimeError):
        pass

    def __init__(self, payload=None, last_modified=None):
        self.payload = payload if payload is not None else _jpeg_bytes()
        self.last_modified = last_modified or format_datetime(
            datetime(2026, 9, 11, 2, 0, 0, tzinfo=timezone.utc), usegmt=True)
        self.downloads = []
        self.fail_with = None

    def head_object(self, key):
        if self.fail_with:
            raise self.OSSError(self.fail_with)
        return {"size": len(self.payload), "etag": "e",
                "last_modified": self.last_modified, "content_type": "image/jpeg"}

    def download_to(self, key, dest, expect_size=None):
        if self.fail_with:
            raise self.OSSError(self.fail_with)
        if expect_size is not None and expect_size != len(self.payload):
            raise self.OSSError(f"回源大小不符：期望 {expect_size}，实际 {len(self.payload)}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        self.downloads.append(key)
        return len(self.payload)


@pytest.fixture
def fake_oss(monkeypatch):
    f = FakeOSS()
    monkeypatch.setattr(oss_backfill, "oss", f)
    return f


@pytest.fixture
def media_row(tmp_path):
    """一条「文件只在 OSS 上、本机什么都没有」的 media —— 直传刚 complete 的样子。"""
    tid = db.create_trip({"id": None}, "t", None, None)
    pid = db.create_point(tid, "p", 1.0, 2.0, None, None)
    return db.add_media(pid, "photo", "abc123.jpg", None, None, None, None)


def test_回源把文件拉回本机并补上缩略图(media_row, fake_oss):
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    assert db.take_oss_backfill(media_row) is True
    out = oss_backfill.process_one(media_row)

    assert out["ok"] is True
    dest = oss_backfill.UPLOAD_DIR / "abc123.jpg"
    assert dest.is_file() and dest.read_bytes() == fake_oss.payload
    m = db.get_media(media_row)
    assert m["thumb_filename"] == "abc123.jpg".replace(".jpg", "") + ".jpg"
    assert db.get_oss_backfill(media_row)["status"] == "done"


def test_本地文件的_mtime_对齐到云端时间(media_row, fake_oss, monkeypatch):
    """否则 media_url 会把这份文件当成「还没同步」，链接指回本机，流量又挤回隧道。"""
    monkeypatch.setattr(oss_backfill, "_sync_watermark", lambda: None)
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    db.take_oss_backfill(media_row)
    oss_backfill.process_one(media_row)

    dest = oss_backfill.UPLOAD_DIR / "abc123.jpg"
    want = datetime(2026, 9, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(dest.stat().st_mtime - want) < 2
    assert dest.stat().st_mtime < time.time()      # 明显早于「刚刚下载完」


def test_回源的文件一律算作已同步到_oss(media_row, fake_oss, monkeypatch):
    """直传对象的 Last-Modified 就是几秒前，仍然晚于上一趟同步的水位 ——
    不压到水位以下的话，刚传完的媒体链接会指回本机，白白挤 4Mbps 的隧道。
    而「已经在云上」是 complete 时 HEAD 核实过的事实。"""
    from config import media_url
    wm = time.time() - 30                      # 30 秒前跑过一趟同步
    monkeypatch.setattr(oss_backfill, "_sync_watermark", lambda: wm)
    # 云端对象是「刚刚」才有的，比水位还新
    fake_oss.last_modified = format_datetime(
        datetime.now(timezone.utc), usegmt=True)

    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    db.take_oss_backfill(media_row)
    oss_backfill.process_one(media_row)

    dest = oss_backfill.UPLOAD_DIR / "abc123.jpg"
    assert dest.stat().st_mtime < wm, "mtime 必须压到水位以下，否则会被判成「还没同步」"

    # 真的走一遍 media_url：开了 OSS 前缀时必须指向 OSS，而不是本机 /uploads/
    monkeypatch.setenv("TRAVEL_MEDIA_BASE_URL", "https://b.oss-cn-qingdao.aliyuncs.com")
    import config
    monkeypatch.setattr(config, "MEDIA_BASE_URL", "https://b.oss-cn-qingdao.aliyuncs.com")
    monkeypatch.setattr(config, "UPLOAD_ROOT", oss_backfill.UPLOAD_DIR)
    monkeypatch.setattr(config, "_wm_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr(config, "_oss_watermark", lambda: wm)
    # 这条用例关心的是「水位判断对不对」，不是「链接怎么签」。把签名挡住，
    # 否则装了 ossutil 配置的机器会拿到带 Signature= 的 URL，跟基准串对不上。
    # 签名本身在 test_media_url.py 里单独钉。
    #
    # 注意不能 monkeypatch config.media_url —— 上面这行是
    # `from config import media_url`，早在 patch 之前就把原函数对象绑进本模块的
    # 全局了，改 config 上的属性对它没有影响（第一次就是这么写错的）。
    # 打桩 oss.conf 才是釜底抽薪：不管 media_url 从哪儿拿到，签名分支都进不去。
    import sys
    import types
    _fake = types.ModuleType("oss")

    class _Conf:
        ok = False

    _fake.conf = lambda: _Conf()
    monkeypatch.setitem(sys.modules, "oss", _fake)
    assert media_url("/uploads/abc123.jpg") == \
        "https://b.oss-cn-qingdao.aliyuncs.com/uploads/abc123.jpg"


def test_回源之后视频会进转码队列(tmp_path, fake_oss, monkeypatch):
    import transcode
    tid = db.create_trip({"id": None}, "t", None, None)
    pid = db.create_point(tid, "p", 1.0, 2.0, None, None)
    mid = db.add_media(pid, "video", "vid.mp4", None, None, None, None)
    # 视频缩略图要 ffmpeg，这里只关心「有没有排进转码」
    monkeypatch.setattr(oss_backfill.media_mod, "make_thumb",
                        lambda *a, **kw: "vid.jpg")
    db.enqueue_oss_backfill(mid, "uploads/vid.mp4", len(fake_oss.payload), "video")
    db.take_oss_backfill(mid)
    oss_backfill.process_one(mid)

    assert [c["media_id"] for c in transcode._TEST_CALLS] == [mid]


def test_不覆盖用户已经改过的位置和时间(media_row, fake_oss, monkeypatch):
    """回源是后来才发生的，不能拿 EXIF 把人家手工调好的值顶掉。"""
    conn = db.get_conn()
    conn.execute("UPDATE media SET exif_lat=31.5, exif_lng=121.5, "
                 "exif_time='2020-01-01T00:00' WHERE id=?", (media_row,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(oss_backfill.media_mod, "read_exif",
                        lambda p: ("2099-12-31T23:59", 0.0, 0.0))

    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    db.take_oss_backfill(media_row)
    oss_backfill.process_one(media_row)

    m = db.get_media(media_row)
    assert (m["exif_lat"], m["exif_lng"]) == (31.5, 121.5)
    assert m["exif_time"] == "2020-01-01T00:00"


def test_下载失败会留痕而不是静默丢掉(media_row, fake_oss):
    fake_oss.fail_with = "NoSuchKey"
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg", 999, "photo")
    db.take_oss_backfill(media_row)
    out = oss_backfill.process_one(media_row)

    assert out["ok"] is False
    row = db.get_oss_backfill(media_row)
    assert row["status"] == "error"
    assert "NoSuchKey" in row["error"]
    assert not (oss_backfill.UPLOAD_DIR / "abc123.jpg").exists()


def test_大小对不上就当失败不要半个文件(media_row, fake_oss):
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg", 12345, "photo")
    db.take_oss_backfill(media_row)
    out = oss_backfill.process_one(media_row)
    assert out["ok"] is False
    assert db.get_oss_backfill(media_row)["status"] == "error"


def test_媒体被删掉时队列里那条跟着走并且不会再下载(media_row, fake_oss):
    """外键是 ON DELETE CASCADE，删媒体会把排队的那条一起带走。

    这里连带守住「就算它还在，也不能去下一个没人要的文件」——
    process_one 对找不到的条目直接返回，不碰网络。
    """
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    db.take_oss_backfill(media_row)
    db.delete_media(media_row)
    assert db.get_oss_backfill(media_row) is None

    out = oss_backfill.process_one(media_row)
    assert out["ok"] is False and fake_oss.downloads == []
    assert not (oss_backfill.UPLOAD_DIR / "abc123.jpg").exists()


def test_本地已经有同样大小的文件就不重复下载(media_row, fake_oss):
    dest = oss_backfill.UPLOAD_DIR / "abc123.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(fake_oss.payload)
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg",
                            len(fake_oss.payload), "photo")
    db.take_oss_backfill(media_row)
    out = oss_backfill.process_one(media_row)
    assert out["ok"] is True
    assert fake_oss.downloads == []          # 一个字节都没白下


def test_重试次数用尽之后不再尝试(media_row, fake_oss):
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg", 999, "photo")
    conn = db.get_conn()
    conn.execute("UPDATE oss_backfill SET attempts=? WHERE media_id=?",
                 (oss_backfill.MAX_ATTEMPTS + 1, media_row))
    conn.commit()
    conn.close()
    out = oss_backfill.process_one(media_row)
    assert out["reason"] == "重试次数用尽"
    assert fake_oss.downloads == []


def test_同一条只会被一个_worker_抢到(media_row):
    db.enqueue_oss_backfill(media_row, "uploads/abc123.jpg", 1, "photo")
    assert db.take_oss_backfill(media_row) is True
    assert db.take_oss_backfill(media_row) is False    # 已经 running 了


def test_重复入队是幂等的并把失败状态复位(media_row):
    db.enqueue_oss_backfill(media_row, "uploads/a.jpg", 1, "photo")
    db.finish_oss_backfill(media_row, ok=False, error="boom")
    assert db.get_oss_backfill(media_row)["status"] == "error"

    db.enqueue_oss_backfill(media_row, "uploads/a.jpg", 1, "photo")
    row = db.get_oss_backfill(media_row)
    assert row["status"] == "pending" and row["error"] is None
    assert len(db.list_oss_backfill()) == 1


def test_崩在_running_的会在启动时复位(media_row, monkeypatch):
    db.enqueue_oss_backfill(media_row, "uploads/a.jpg", 1, "photo")
    db.take_oss_backfill(media_row)
    assert db.get_oss_backfill(media_row)["status"] == "running"

    # 只验复位和重新排队，不真跑 worker
    submitted = []
    monkeypatch.setattr(oss_backfill, "_submit",
                        lambda mid: submitted.append(mid) or True)
    assert oss_backfill.resume_pending() == 1
    assert submitted == [media_row]
    assert db.get_oss_backfill(media_row)["status"] == "pending"


def test_队列摘要(media_row):
    db.enqueue_oss_backfill(media_row, "uploads/a.jpg", 1, "photo")
    assert oss_backfill.queue_status()["pending"] == 1
    db.finish_oss_backfill(media_row, ok=True)
    s = oss_backfill.queue_status()
    assert s["done"] == 1 and s["pending"] == 0
