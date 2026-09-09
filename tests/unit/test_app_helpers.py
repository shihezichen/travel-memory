"""app.py 里不依赖请求上下文的纯函数（路由白名单、归一化、相册聚类、瓦片换算）。"""
import math

import pytest
from fastapi import HTTPException

import app as app_mod

pytestmark = pytest.mark.unit


# ---------------- 免鉴权路径白名单 ----------------
@pytest.mark.parametrize("path", [
    "/", "/health", "/api/login", "/api/register", "/logout",
    "/api/route", "/api/gate", "/api/gate/status",
    "/static/app.js", "/uploads/x.jpg", "/uploads/thumbs/x.jpg",
    "/md/README.md", "/share/abc", "/api/share/abc", "/api/ai/trips", "/mcp",
])
def test_public_paths(path):
    assert app_mod.is_public_path(path) is True


@pytest.mark.parametrize("path", [
    "/api/trips", "/api/me", "/api/points/1/media", "/console",
    "/api/console/keys", "/api/footprint", "/api/photo-layer",
    "/api/trips/1/share",
])
def test_protected_paths(path):
    assert app_mod.is_public_path(path) is False


def test_share_prefix_does_not_leak_write_routes():
    """/api/trips/{id}/share 是写操作，必须走鉴权；只有 /api/share/* 是公开读。"""
    assert app_mod.is_public_path("/api/trips/9/share") is False
    assert app_mod.is_public_path("/api/share/tok") is True


# ---------------- 归一化 ----------------
@pytest.mark.parametrize("raw,expect", [
    ("", "read"),
    (None, "read"),
    ("read", "read"),
    ("write", "write"),
    ("read,write", "read,write"),
    ("write,read,write", "write,read"),        # 去重，保留首次出现顺序
    ("read bogus admin", "read,admin"),        # 空格分隔 + 丢弃非法 token
    ("bogus", "read"),                          # 全非法 → 回落 read
])
def test_normalize_scope(raw, expect):
    assert app_mod._normalize_scope(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("planning", "planning"), ("traveling", "traveling"),
    ("completed", "completed"), ("archived", "archived"),
    ("bogus", "planning"), ("", "planning"), (None, "planning"),
])
def test_normalize_trip_status(raw, expect):
    assert app_mod._normalize_trip_status(raw) == expect


# ---------------- 门禁口令哈希 ----------------
def test_gate_hash_is_plain_sha256():
    import hashlib
    assert app_mod.gate_hash_of("abc") == hashlib.sha256(b"abc").hexdigest()


# ---------------- 归属校验 ----------------
def test_require_owner():
    user = {"id": 7, "is_admin": 0}
    admin = {"id": 1, "is_admin": 1}
    app_mod.require_owner(user, 7, "行程")             # 本人 → 放行
    app_mod.require_owner(admin, 999, "行程")          # 管理员 → 放行
    for bad in (8, None):
        with pytest.raises(HTTPException) as e:
            app_mod.require_owner(user, bad, "行程")
        assert e.value.status_code == 404              # 越权按 404 处理，不泄露存在性


# ---------------- 路线缓存键 ----------------
def test_route_cache_key_is_stable_and_precise():
    k1 = app_mod._route_cache_key(120.1, 30.2, 121.4, 31.2)
    k2 = app_mod._route_cache_key(120.1000000, 30.2000000, 121.4, 31.2)
    assert k1 == k2 == "drive|120.100000,30.200000|121.400000,31.200000"
    # 起终点互换 = 不同的 key
    assert app_mod._route_cache_key(121.4, 31.2, 120.1, 30.2) != k1


# ---------------- 媒体扩展名推断 ----------------
@pytest.mark.parametrize("fname,ctype,expect", [
    ("a.JPG", "", ".jpg"),
    ("a.heic", "", ".heic"),
    ("noext", "image/jpeg", ".jpg"),
    ("noext", "image/png;charset=x", ".png"),
    ("noext", "video/quicktime", ".mov"),
    ("noext", "application/octet-stream", ""),
    ("", "", ""),
    (None, None, ""),
])
def test_media_ext(fname, ctype, expect):
    assert app_mod._media_ext(fname, ctype) == expect


# ---------------- 相册聚类 ----------------
def test_gap_hours():
    assert app_mod._gap_hours("2026-05-01T08:00:00", "2026-05-01T12:00:00") == 4
    assert app_mod._gap_hours("2026-05-01T12:00:00", "2026-05-01T08:00:00") == 4  # 绝对值
    assert app_mod._gap_hours("garbage", "2026-05-01T08:00:00") == 0              # 解析失败不切分
    assert app_mod._gap_hours("", "") == 0


def _item(mid, t, mtype="photo", thumb="t.jpg"):
    return {"id": mid, "type": mtype, "filename": f"{mid}.jpg", "thumb_filename": thumb,
            "exif_time": t, "point_id": 5, "point_name": "西湖", "caption": ""}


def test_make_album_shape_and_counts():
    a = app_mod._make_album(1, 5, [_item(1, "2026-05-01T08:00:00"),
                                   _item(2, "2026-05-01T09:00:00", "video")])
    assert a["id"] == 1 and a["point_id"] == 5
    assert a["count"] == 2 and a["photo_count"] == 1 and a["video_count"] == 1
    assert a["date_range"] == "2026-05-01"
    assert a["title"] == "西湖 · 2026-05-01"
    assert a["cover_url"] == "/uploads/thumbs/t.jpg"
    assert a["media"][0]["url"] == "/uploads/1.jpg"


def test_make_album_multi_day_range():
    a = app_mod._make_album(1, 5, [_item(1, "2026-05-01T08:00:00"),
                                   _item(2, "2026-05-03T09:00:00")])
    assert a["date_range"] == "2026-05-01 ~ 2026-05-03"


def test_make_album_without_thumbnail_has_no_cover():
    a = app_mod._make_album(1, 5, [_item(1, "2026-05-01T08:00:00", thumb=None)])
    assert a["cover_url"] is None
    assert a["media"][0]["thumb_url"] is None


# ---------------- 瓦片坐标 ----------------
def test_deg2num_matches_web_mercator():
    x, y = app_mod._deg2num(0.0, 0.0, 1)
    assert math.isclose(x, 1.0) and math.isclose(y, 1.0)     # 赤道本初子午线 = 瓦片网格中心
    x, y = app_mod._deg2num(0.0, -180.0, 2)
    assert math.isclose(x, 0.0)
    # 纬度越高 y 越小（越靠北）
    assert app_mod._deg2num(60.0, 0.0, 5)[1] < app_mod._deg2num(0.0, 0.0, 5)[1]


# ---------------- ICP 页脚 ----------------
def test_icp_footer(monkeypatch):
    monkeypatch.setattr(app_mod, "ICP_BEIAN", "")
    assert app_mod.icp_footer_html() == ""
    monkeypatch.setattr(app_mod, "ICP_BEIAN", "京ICP备<x>号")
    html = app_mod.icp_footer_html()
    assert "beian.miit.gov.cn" in html
    assert "&lt;x&gt;" in html          # HTML 转义，防注入
