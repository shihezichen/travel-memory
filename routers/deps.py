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
