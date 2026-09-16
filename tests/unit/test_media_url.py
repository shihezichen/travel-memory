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
    c = importlib.reload(config)
    # 这个文件测的是「前缀怎么拼」这条老逻辑，所以要把签名那条分支按下去。
    # 不管的话，跑测试的机器只要装了 ossutil 配置（oss.conf().ok 为真），
    # media_url 就会去问 OSS 要签名 —— 于是断言里的期望值变成一串带
    # Signature= 的 URL，跟基准前缀对不上（实测 2026-09-16 挂了三条）。
    #
    # 用替换 **_synced_to_oss** 之外最干净的办法：直接把 oss.conf 打桩成不可用。
    # 这个文件不 import oss，所以在 sys.modules 里放一个假的即可。
    import sys
    import types
    fake = types.ModuleType("oss")

    class _Conf:
        ok = False          # <-- 关键：让 media_url 走裸链分支

    fake.conf = lambda: _Conf()
    monkeypatch.setitem(sys.modules, "oss", fake)
    return c


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


# ---------- 签名模式：Bucket 关掉公共读之后的唯一通路 ----------
# 背景：Bucket 原本是 public-read，谁拿到 URL 谁能永久下载，且无法吊销。
# 2026-09-16 起改成私有 + 全走签名 URL。这一组钉住「签名可用时一定走签名」。

def _with_signing(monkeypatch, tmp_path, base, can_sign, ttl=None):
    """和 _with_watermark 一样搭好环境，但把 oss.conf().ok 按 can_sign 打桩。

    media_url 走的是 `import oss; oss.conf().ok`，所以在 sys.modules 里塞假模块
    即可 —— 比 monkeypatch config.media_url 可靠（那个会被 `from config import`
    抢跑，实测踩过）。
    """
    c = _with_watermark(monkeypatch, tmp_path, base, wm=3000,
                        files={"old.jpg": 2000})
    if ttl is not None:
        monkeypatch.setattr(c, "MEDIA_URL_TTL", ttl)

    import sys
    import types
    fake = types.ModuleType("oss")

    class _Conf:
        ok = can_sign

    fake.conf = lambda: _Conf()
    # 签名函数回一个能一眼认出来的串，免得测试去解析真实签名格式
    def _sign(method, key, params=None, content_type="", expires_in=0):
        return f"{base}/{key}?Expires={expires_in}&Signature=FAKE"
    fake.sign_url = _sign
    monkeypatch.setitem(sys.modules, "oss", fake)
    return c


def test_signed_when_oss_available(monkeypatch, tmp_path):
    """签名可用 -> 返回签名 URL，而不是裸链。"""
    c = _with_signing(monkeypatch, tmp_path, "https://cdn.example.com", can_sign=True)
    out = c.media_url("/uploads/old.jpg")
    assert "Signature=FAKE" in out, out
    assert out.startswith("https://cdn.example.com/uploads/old.jpg?")


def test_signed_url_uses_configured_ttl(monkeypatch, tmp_path):
    """TTL 走 TRAVEL_MEDIA_URL_TTL，默认 2 小时（网页场景够用且泄露窗口小）。"""
    c = _with_signing(monkeypatch, tmp_path, "https://cdn.example.com",
                      can_sign=True, ttl=1234)
    assert "Expires=1234" in c.media_url("/uploads/old.jpg")


def test_falls_back_to_bare_link_when_signing_unavailable(monkeypatch, tmp_path):
    """签不出来就回裸链 —— 没关公共读的部署 / 本地开发不能被这条改动搞挂。"""
    c = _with_signing(monkeypatch, tmp_path, "https://cdn.example.com", can_sign=False)
    assert c.media_url("/uploads/old.jpg") == \
        "https://cdn.example.com/uploads/old.jpg"


def test_unsynced_file_never_signed(monkeypatch, tmp_path):
    """还没同步到 OSS 的文件签了也是 404，必须先回本机路径。"""
    c = _with_watermark(monkeypatch, tmp_path, "https://cdn.example.com",
                        wm=1000, files={"new.jpg": 2000})
    import sys
    import types
    fake = types.ModuleType("oss")

    class _Conf:
        ok = True
    fake.conf = lambda: _Conf()
    fake.sign_url = lambda *a, **k: "SHOULD-NOT-BE-CALLED"
    monkeypatch.setitem(sys.modules, "oss", fake)
    assert c.media_url("/uploads/new.jpg") == "/uploads/new.jpg"


def test_signing_exception_degrades_not_crashes(monkeypatch, tmp_path):
    """签名抛异常时降级回裸链，不能把整个媒体链打挂。"""
    c = _with_signing(monkeypatch, tmp_path, "https://cdn.example.com", can_sign=True)
    import sys
    fake = sys.modules["oss"]

    def _boom(*a, **k):
        raise RuntimeError("oss 挂了")
    fake.sign_url = _boom
    assert c.media_url("/uploads/old.jpg") == \
        "https://cdn.example.com/uploads/old.jpg"


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
