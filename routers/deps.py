"""路由间共享的请求级辅助函数。

从 app.py 原样搬过来的（函数体一字未改）。存在的理由很简单：拆分后的
routers/* 需要这几个函数，但它们不能 `import app` —— app.py 自己要 import
routers.*，会绕成循环 import。所以把这批「谁都要用、又不依赖 app 全局状态」的
纯函数放在这里，app.py 和各 router 都从这里拿。

除了请求级辅助函数，这里也放几个 app.py 和 routers/* 都要用的小工具
（高德 JS API 版本判定、未配置 Key 的提示条、路线缓存键）。

注意这里**只放无状态的纯函数**。真正的全局设施 —— 鉴权中间件、PUBLIC_PATHS /
is_public_path、异常处理器、静态挂载 —— 全部留在 app.py，不要搬进来。
current_user 读的 request.state.user 就是 app.py 那个中间件塞进去的。
"""
from fastapi import HTTPException, Request

from config import AMAP_KEY


def current_user(request: Request):
    """从请求上下文取当前登录用户；未登录抛 401"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def require_owner(user, owner_id: int, what: str):
    """校验资源归属：管理员放行，否则 owner 必须等于当前用户"""
    if user["is_admin"]:
        return
    if owner_id is None or owner_id != user["id"]:
        raise HTTPException(404, f"{what}不存在")


def _public_user(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u.get("display_name") or u["username"],
        "is_admin": bool(u["is_admin"]),
        "last_trip_id": u.get("last_trip_id"),
    }


def amap_version(request: Request) -> str:
    """微信内置浏览器 WebGL 渲染不完整、矢量瓦片地名标注缺失，
    降级到 JS API 1.4（栅格瓦片，地名烘焙在图片里）；其余浏览器用 2.0。"""
    ua = request.headers.get("user-agent", "") or ""
    return "1.4.15" if ("MicroMessenger" in ua or "wxwork" in ua) else "2.0"


def amap_key_notice_html() -> str:
    """没配高德 Key 时，在页面顶部挂一条醒目提示。

    没有这个提示的话，新部署的人看到的是一整片空白地图，既不报错也没有线索，
    只能自己去翻源码——这是「零配置」最容易卡住人的一步，所以宁可啰嗦。
    """
    if AMAP_KEY:
        return ""
    return (
        '<div style="position:fixed;left:0;right:0;top:0;z-index:9999;'
        'background:#b45309;color:#fff;padding:10px 14px;font:14px/1.6 '
        '-apple-system,\'PingFang SC\',sans-serif;text-align:center">'
        '尚未配置高德地图 Key，地图无法显示。'
        '把 Key 写进项目根目录的 <code>amap_key.txt</code>、'
        '安全密钥写进 <code>amap_security.txt</code>，然后重启即可。'
        '（申请：<a href="https://console.amap.com/dev/key/app" target="_blank" '
        'rel="noopener" style="color:#fde68a">高德开放平台</a> → Web端(JS API)）'
        '</div>')


def _route_cache_key(from_lng, from_lat, to_lng, to_lat):
    """路线缓存键：同一对端点（坐标 6 位小数 ≈ 0.1m 精度）的驾车路线共享缓存。"""
    return f"drive|{from_lng:.6f},{from_lat:.6f}|{to_lng:.6f},{to_lat:.6f}"


# ---------------------------------------------------------------- 静态资源版本号
# 为什么要自动算：index.html 里那串 `?v=20260911v6` 原来是手写的，而 /static 对带
# `v=` 的请求回 `max-age=31536000, immutable`。2026-09-11 就真的踩了一次：
# 08:08 的提交把版本号从 v4 改到 v6，10:50 的「直传 OSS」又改了 app.js + common.js
# 却**忘了再改版本号** —— 于是 08:08 之后打开过页面的浏览器把 v6 那份 JS 锁死缓存
# 一年，直传代码永远到不了它手上。库里留着证据：11:18 和 11:21 两次上传的
# upload_sessions 行 oss_key 是 NULL（走的还是隧道分片那条老路）。
#
# 所以版本号改成从文件本身算出来：内容一变，URL 必然变；不变就继续吃缓存。
# 忘记手动 bump 这个失败模式从此不存在。
import hashlib
import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
# 参与版本号计算的文件。只放**会被 ?v= 引用**的那几个，别把整个 static/ 扫一遍
# （里面有图标、places-zh-en.json 这些几百 KB 的东西，每次请求都 stat 一遍不值当）。
_VERSIONED_ASSETS = ("app.js", "common.js", "share.js", "style.css")
# 只认 /static/ 自己的资源。早先这里写的是裸 `(?<=[?&])v=...`，把高德 SDK 的
# `https://webapi.amap.com/maps?v=2.0` 也一起改掉了 —— SDK 当场加载失败，
# window.AMap 没了，initMap() 一抛异常整条启动链断掉，页面永远停在「载入中」。
_ASSET_V_RE = re.compile(r"(?<![\w./-])(/static/[\w./-]+\?)v=[0-9A-Za-z._-]+")
_asset_v_cache = {"at": 0.0, "value": ""}


def asset_version() -> str:
    """静态资源版本号 = 几个文件的 (名字, mtime_ns, 大小) 的短哈希。带 5 秒缓存。

    用 mtime+size 而不是读文件内容算 sha：这几个文件加起来 400KB+，每次 GET /
    都读一遍没必要；mtime_ns 的精度足够区分任何一次真实改动。
    """
    import time
    now = time.monotonic()
    if now - _asset_v_cache["at"] < 5.0 and _asset_v_cache["value"]:
        return _asset_v_cache["value"]
    h = hashlib.sha256()
    for name in _VERSIONED_ASSETS:
        p = _STATIC_DIR / name
        try:
            st = p.stat()
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size};".encode())
        except OSError:
            h.update(f"{name}:missing;".encode())   # 文件不在也要参与，别悄悄同版
    v = h.hexdigest()[:12]
    _asset_v_cache.update({"at": now, "value": v})
    return v


def stamp_assets(html: str) -> str:
    """把 HTML 里 `/static/xxx?v=...` 的版本号换成当前的资源版本号。

    直接正则替换而不是留一个 `__ASSET_V__` 占位符：占位符同样要靠人记得写，
    而模板里本来就已经有 `?v=` 了 —— 认这个既有写法，漏不掉。

    只动 `/static/` 开头的 URL：第三方 SDK 的 `?v=` 是它自己的 API 版本号，
    改了就加载不出来（见 _ASSET_V_RE 上面那段）。
    """
    return _ASSET_V_RE.sub(r"\1v=" + asset_version(), html)
