"""media_url 开关：空 = 走本机 /uploads/，填了 = 指向 OSS/CDN。

这个开关是媒体流量绕开 frp 隧道（4Mbps 硬顶）的总闸，
误伤 /static/ 会让前端 JS 和后端版本脱节，所以单独钉住。
"""
import importlib

import config


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
