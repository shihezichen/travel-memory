"""media_url 开关：空 = 走本机 /uploads/，填了 = 指向 OSS/CDN。

这个开关是媒体流量绕开 frp 隧道（4Mbps 硬顶）的总闸，
误伤 /static/ 会让前端 JS 和后端版本脱节，所以单独钉住。
"""
import importlib
import os

import pytest

import config


@pytest.fixture(autouse=True)
def _no_cdn_leak():
    """每条用例跑完都把 config 复位。

    这个文件里到处 reload(config)，而 transcode 那边是 `from config import media_url`
    —— 拿到的函数对象 __globals__ 就是 config 的模块字典，reload 是就地更新它，
    于是 CDN 前缀会顺着泄漏到后面的测试文件去（实测挂掉 test_transcode 三条）。
    原来靠文件末尾一条 test_module_left_clean 收尾，只要有人往后面追加用例就失效。
    """
    yield
    os.environ["TRAVEL_MEDIA_BASE_URL"] = ""
    importlib.reload(config)


def _reload(monkeypatch, value):
    # 故意不用 delenv：删掉环境变量后 config.py 会回落去读仓库里的
    # media_base.txt（生产配置，指向 OSS），测试就不再自洽了。
    monkeypatch.setenv("TRAVEL_MEDIA_BASE_URL", value)
    return importlib.reload(config)


def test_empty_base_keeps_local_path(monkeypatch):
    c = _reload(monkeypatch, "")
    assert c.media_url("/uploads/a.jpg") == "/uploads/a.jpg"


def test_base_prefixes_uploads(monkeypatch):
    c = _reload(monkeypatch, "https://cdn.example.com")
    assert c.media_url("/uploads/a.jpg") == "https://cdn.example.com/uploads/a.jpg"
    assert c.media_url("/uploads/variants/x_720p.mp4?v=1") == \
        "https://cdn.example.com/uploads/variants/x_720p.mp4?v=1"


def test_trailing_slash_does_not_double(monkeypatch):
    c = _reload(monkeypatch, "https://cdn.example.com/")
    assert c.media_url("/uploads/a.jpg") == "https://cdn.example.com/uploads/a.jpg"


def test_static_is_never_rewritten(monkeypatch):
    """/static/ 是应用自己的 JS/CSS，必须跟代码版本走，不能进对象存储。"""
    c = _reload(monkeypatch, "https://cdn.example.com")
    assert c.media_url("/static/app.js") == "/static/app.js"
    assert c.media_url("/api/media") == "/api/media"


def test_module_left_clean(monkeypatch):
    """别把 CDN 前缀泄漏给后面的测试。"""
    c = _reload(monkeypatch, "")
    assert c.MEDIA_BASE_URL == ""


# ---------- 同步水位：刚上传、还没推到 OSS 的文件必须走本机 ----------
# 媒体是「先落本地磁盘 → ops/sync-oss.sh 每 2 分钟增量推上去」，中间有最长
# 两分多钟的空窗。原来 media_url 无条件拼 OSS 前缀，于是刚传完的照片、
# 转码中视频的封面全是 404 裂图。

def _with_watermark(monkeypatch, tmp_path, base, wm, files):
    """把 config 的 uploads 根和水位文件都指到 tmp_path 上。

    files: {相对路径: mtime}，mtime 早于水位 = 已同步。
    """
    c = _reload(monkeypatch, base)
    up = tmp_path / "uploads"
    up.mkdir(exist_ok=True)
    for rel, mtime in files.items():
        f = up / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        import os
        os.utime(f, (mtime, mtime))
    wmf = tmp_path / "oss-sync-watermark"
    if wm is not None:
        wmf.write_text(str(wm), encoding="utf-8")
    monkeypatch.setattr(c, "UPLOAD_ROOT", up)
    monkeypatch.setattr(c, "OSS_WATERMARK_FILE", wmf)
    c._wm_cache["at"] = 0.0          # 水位有 2 秒缓存，测试里必须清掉
    return c


def test_not_yet_synced_file_stays_local(monkeypatch, tmp_path):
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com",
                        wm=1000, files={"new.jpg": 2000})
    assert c.media_url("/uploads/new.jpg") == "/uploads/new.jpg"


def test_already_synced_file_goes_to_oss(monkeypatch, tmp_path):
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com",
                        wm=3000, files={"old.jpg": 2000})
    assert c.media_url("/uploads/old.jpg") == "https://cdn.example.com/uploads/old.jpg"


def test_thumbs_follow_the_same_rule(monkeypatch, tmp_path):
    """视频转码中那块裂图就是这个：原片同步过了，新生成的封面还没。"""
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com", wm=3000,
                        files={"v.mp4": 2000, "thumbs/v.jpg": 4000})
    assert c.media_url("/uploads/v.mp4").startswith("https://cdn.example.com")
    assert c.media_url("/uploads/thumbs/v.jpg") == "/uploads/thumbs/v.jpg"


def test_no_watermark_file_keeps_old_behaviour(monkeypatch, tmp_path):
    """没在跑同步（或刚部署）时不做判断 —— 不能因为缺个文件就把媒体全导回隧道。"""
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com",
                        wm=None, files={"new.jpg": 9_999_999_999})
    assert c.media_url("/uploads/new.jpg") == "https://cdn.example.com/uploads/new.jpg"


def test_missing_local_file_still_goes_to_oss(monkeypatch, tmp_path):
    """本地没有 = 历史数据或本地清理过，云端反而可能有；指本机只会拿 404。"""
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com",
                        wm=3000, files={})
    assert c.media_url("/uploads/gone.jpg") == "https://cdn.example.com/uploads/gone.jpg"


def test_query_string_is_stripped_before_stat(monkeypatch, tmp_path):
    """档位 URL 带 ?v=1，不能拿它去 stat 文件。"""
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com", wm=1000,
                        files={"variants/x_720p.mp4": 2000})
    assert c.media_url("/uploads/variants/x_720p.mp4?v=1") == \
        "/uploads/variants/x_720p.mp4?v=1"
