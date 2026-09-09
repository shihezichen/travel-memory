"""旅行记忆系统 — FastAPI 主入口（多用户版）"""
import io
import os
import base64
import json
import time
import threading
import math
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
import uvicorn
from starlette.concurrency import run_in_threadpool

import qrcode
from PIL import Image, ImageDraw, ImageFont

import db
import media as media_mod
import mcp_http  # MCP Streamable HTTP 远程端点（开放平台：远程 Agent 接入）

# 领域 API 层（记忆/联想/生成 + API Key 认证 + 审计）
from domain import memory as domain_memory
from domain import association as domain_assoc
from domain import generation as domain_gen
from domain import llm_provider
from domain import search as domain_search
from domain import recall as domain_recall
from domain import trigger as domain_trigger
from domain import meta as domain_meta
from domain import pattern as domain_pattern
from domain import recommend as domain_recommend
from domain import unfinished as domain_unfinished
from domain import wrapped as domain_wrapped
from domain import relation as domain_relation
from domain import reinterpret as domain_reinterpret
from domain import biography as domain_biography
from domain import timetravel as domain_timetravel
from domain import token_auth
from domain import schemas as domain_schemas
from domain import geocode as domain_geocode
from domain import rate_limit

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
# 全新克隆没有 uploads/（它在 .gitignore 里），而下面 app.mount 的 StaticFiles
# 在目录不存在时会直接抛异常 —— 服务连启动都启动不了。这里先建好。
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
(UPLOAD_DIR / "thumbs").mkdir(parents=True, exist_ok=True)

# 管理员令牌（从 token.txt 读取，作为管理员直登凭据；可不存在）
ADMIN_TOKEN = ""
_token_file = BASE / "token.txt"
if _token_file.exists():
    ADMIN_TOKEN = _token_file.read_text(encoding="utf-8").strip()

# 高德地图 key（从 amap_key.txt 读取，若未配置则为空）
AMAP_KEY = ""
_key_file = BASE / "amap_key.txt"
if _key_file.exists():
    AMAP_KEY = _key_file.read_text(encoding="utf-8").strip()

# 高德安全密钥（JS API 2.0 securityJsCode，从 amap_security.txt 读取）
AMAP_SECURITY = ""
_sec_file = BASE / "amap_security.txt"
if _sec_file.exists():
    AMAP_SECURITY = _sec_file.read_text(encoding="utf-8").strip()

# ICP 备案号（从 icp.txt 读取）。工信部要求：已备案网站首页底部须展示备案号
# 并链接到 beian.miit.gov.cn。文件不存在或为空则不渲染（备案下来前的状态）。
ICP_BEIAN = ""
_icp_file = BASE / "icp.txt"
if _icp_file.exists():
    ICP_BEIAN = _icp_file.read_text(encoding="utf-8").strip()


def icp_footer_html() -> str:
    """生成备案号页脚片段；未配置备案号时返回空串（页面上不留痕迹）。"""
    if not ICP_BEIAN:
        return ""
    from html import escape
    num = escape(ICP_BEIAN)
    return (
        '<div class="icp-footer">'
        f'<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">{num}</a>'
        '</div>'
    )


# 访问口令（方案 A 门禁）：gate_hash.txt 存 sha256 口令哈希；文件不存在则门禁关闭（向后兼容）
import hashlib
GATE_HASH = ""
_gate_file = BASE / "gate_hash.txt"
if _gate_file.exists():
    GATE_HASH = _gate_file.read_text(encoding="utf-8").strip()

def gate_hash_of(passwd: str) -> str:
    return hashlib.sha256(passwd.encode("utf-8")).hexdigest()

app = FastAPI(title="旅行记忆系统")

# ---------- 响应压缩 ----------
# 前端是三个大文本文件（app.js / share.js / style.css，未压缩合计 340KB+），
# 行政区划图层更是单次 3.5MB 的 JSON —— 这些都是 gzip 比极高的纯文本。
# minimum_size=1024：小响应压了反而更大，也不值那点 CPU。
# compresslevel=6：对这些文本，6 和 9 的压缩比只差 1~2%，但 6 快得多。
# starlette 1.6 的 thread_minimum_size 默认 128KB —— 超过这个大小的响应体会被丢进
# 线程池压缩，所以 3.5MB 的行政区划不会把事件循环堵住；图片/视频类 content-type
# 也在它的 exclude_content_types 默认名单里，不会做无用功。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

db.init_db()

# 登录后 cookie 名（沿用 tm_token，存 session token）
COOKIE_NAME = "tm_token"
# 访问口令 cookie（方案 A 门禁，2026-08-26 刚哥授权全权落实）
GATE_COOKIE = "tm_gate"

# 无需登录即可访问的路径（首页、登录/注册接口、静态资源、只读分享页、路线规划、门禁接口）
PUBLIC_PATHS = ("/", "/api/login", "/api/register", "/logout", "/api/route",
                "/api/gate", "/api/gate/status", "/health")

# OSRM 全球驾车路线规划服务（高德只支持国内，海外路线用此兜底）
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
# OSRM 超时。原来是 30 秒 —— 而实测这台机器打 OSRM 公共服务是 663/665/675 ms
# （巴黎→布鲁塞尔，连打 3 次）。30 秒等于给了 45 倍的余量，代价是缓存未命中时
# 每个请求最长占住一个线程池槽位 30 秒；前端 drawRoute() 还会用 Promise.all
# 对一条行程的所有路段并发发请求（库里最长的行程是 16 段）。
# 收到 8 秒：相对实测值仍有 12 倍余量，而最坏情况的槽位占用降到 3.75 分之一。
# 超时的后果是返回空 routes，前端已有直线兜底，不会白屏。
OSRM_TIMEOUT = float(os.environ.get("TRAVEL_OSRM_TIMEOUT", "8"))

# 上传大小上限。原来一个上限都没有：raw = file.read() 是把整个文件读进内存，
# 一个 200MB 的视频就是 200MB RSS 峰值，并发几个就能把进程打到 OOM。
MAX_UPLOAD_PHOTO = int(os.environ.get("TRAVEL_MAX_UPLOAD_PHOTO_MB", "30")) * 1024 * 1024
MAX_UPLOAD_VIDEO = int(os.environ.get("TRAVEL_MAX_UPLOAD_VIDEO_MB", "200")) * 1024 * 1024


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/static/"):
        return True
    # 媒体文件（图片/视频）：分享页匿名访问必需，且为只读静态资源
    if path.startswith("/uploads/"):
        return True
    # Markdown 在线渲染（只读文档）
    if path.startswith("/md/"):
        return True
    if path.startswith("/share/") or path.startswith("/api/share/"):
        return True
    # 领域 API：用 API Key 认证（Bearer），不走 session cookie
    if path.startswith("/api/ai/"):
        return True
    # MCP 远程端点：用 API Key 认证（Bearer），不走 session cookie
    if path == "/mcp":
        return True
    return False


def set_session_cookie(resp: Response, token: str):
    # cookie 仅作为会话容器长期保留；实际「3 小时无活动下线」由服务端滑动过期控制
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600)


# ---------- LLM 并发闸门的状态码映射 ----------
@app.exception_handler(llm_provider.LLMBusy)
def _llm_busy_handler(request: Request, exc: llm_provider.LLMBusy):
    """LLM 槽位满 -> 503 + Retry-After，而不是 500。

    503 表达的是「服务器暂时忙，重试会成功」，客户端/agent 才知道该退避重试；
    500 会让人以为是代码 bug。这里刻意不写审计日志：正在过载的时候再往
    audit_log 里怼写事务只会雪上加霜。
    """
    return JSONResponse({"error": str(exc)}, status_code=503,
                        headers={"Retry-After": "5"})


# ---------- 鉴权中间件 ----------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_public_path(path):
        return await call_next(request)

    # 门禁（方案 A：访问口令）。未启用（无 gate_hash.txt）时跳过。
    if GATE_HASH and request.cookies.get(GATE_COOKIE) != GATE_HASH:
        return JSONResponse({"error": "gate_required"}, status_code=401)

    token = request.cookies.get(COOKIE_NAME)
    # 中间件本身是 async 的，跑在事件循环线程上。get_session_user 是同步 SQLite
    # （建连接 + 一次读 + 可能一次写事务 + 关连接），直接在这里调等于每个需要登录的
    # 请求都拿整个进程的事件循环去做阻塞 I/O。丢进线程池，和 api_fill_elevation 一个写法。
    user = await run_in_threadpool(db.get_session_user, token)
    if user:
        request.state.user = user
        return await call_next(request)
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ---------- 健康检查（免鉴权，供 frp/外部监控探活）----------
@app.get("/health")
def health():
    """存活探针：验证进程活着且 DB 可读。不返回任何业务数据。"""
    try:
        db.get_conn().execute("select 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    body = {"status": "ok" if db_ok else "degraded", "db": db_ok,
            "llm_provider": llm_provider.resolve_provider()}
    return JSONResponse(body, status_code=200 if db_ok else 503)


# ---------- 访问口令门禁（方案 A） ----------
@app.get("/api/gate/status")
def gate_status(request: Request):
    if not GATE_HASH:
        return {"enabled": False}
    if request.cookies.get(GATE_COOKIE) == GATE_HASH:
        return {"enabled": True, "ok": True}
    return JSONResponse({"error": "gate_required"}, status_code=401)


@app.post("/api/gate")
def gate_login(request: Request, password: str = Form(...)):
    if not GATE_HASH:
        return {"ok": True}
    if gate_hash_of(password) != GATE_HASH:
        return JSONResponse({"error": "口令错误"}, status_code=403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(GATE_COOKIE, GATE_HASH, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600)
    return resp


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


# ---------- 认证 ----------
@app.post("/api/login")
def api_login(request: Request,
              username: str = Form(""),
              password: str = Form(""),
              token: str = Form("")):
    # 管理员令牌直登
    if ADMIN_TOKEN and token and token == ADMIN_TOKEN:
        admin = db.get_or_create_admin()
        sess = db.create_session(admin["id"])
        resp = JSONResponse({"ok": True, "user": _public_user(admin)})
        set_session_cookie(resp, sess)
        return resp

    if not username or not password:
        return JSONResponse({"error": "请输入用户名和密码"}, status_code=401)
    u = db.verify_user(username, password)
    if not u:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    sess = db.create_session(u["id"])
    resp = JSONResponse({"ok": True, "user": _public_user(u)})
    set_session_cookie(resp, sess)
    return resp


@app.post("/api/register")
def api_register(username: str = Form(""), password: str = Form(""),
                 display_name: str = Form("")):
    username = (username or "").strip()
    if not username:
        return JSONResponse({"error": "用户名不能为空"}, status_code=400)
    if len(username) > 32:
        return JSONResponse({"error": "用户名过长（≤32 字符）"}, status_code=400)
    if not password or len(password) < 6:
        return JSONResponse({"error": "密码至少 6 位"}, status_code=400)
    if db.get_user_by_username(username):
        return JSONResponse({"error": "用户名已被占用"}, status_code=409)

    uid = db.create_user(username, password, display_name or username)
    u = db.get_user_by_id(uid)
    sess = db.create_session(uid)
    resp = JSONResponse({"ok": True, "user": _public_user(u)})
    set_session_cookie(resp, sess)
    return resp


@app.post("/logout")
def api_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/me")
def api_me(request: Request):
    return _public_user(current_user(request))


@app.post("/api/me/last-trip")
def api_set_last_trip(request: Request, trip_id: int = Form(...)):
    """记住当前打开的行程，下次登录（含换设备/换浏览器）自动恢复"""
    user = current_user(request)
    require_owner(user, db.get_trip_owner_id(trip_id), "行程")
    db.set_last_trip(user["id"], trip_id)
    return {"ok": True}


def _public_user(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u.get("display_name") or u["username"],
        "is_admin": bool(u["is_admin"]),
        "last_trip_id": u.get("last_trip_id"),
    }


# ---------- 静态文件 ----------
# 静态资源的缓存策略要明确，否则浏览器走「启发式缓存」——没有 Cache-Control 时
# 它自己按 Last-Modified 猜一个时长，于是改了代码用户还看到老版本，说不清道不明。
# 带 ?v=xxx 的一律当成不可变（版本号每次发布都变，URL 变了自然会重新取）；
# 不带版本号的一律不缓存。
class CacheHeaderStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        qs = (scope.get("query_string") or b"").decode()
        if "v=" in qs:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


class MdRedirectStaticFiles(CacheHeaderStaticFiles):
    """.md 文件不直接下载，301 重定向到 /md/ 渲染端点（在线浏览）"""

    async def get_response(self, path: str, scope):
        if path.lower().endswith(".md"):
            from starlette.responses import RedirectResponse
            return RedirectResponse(url=f"/md/{path}", status_code=301)
        return await super().get_response(path, scope)


app.mount("/static", MdRedirectStaticFiles(directory=BASE / "static"), name="static")
app.mount("/uploads", CacheHeaderStaticFiles(directory=UPLOAD_DIR), name="uploads")


# ---------- Markdown 在线渲染（/md/<文件名>.md） ----------
import markdown as md_lib

MD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · travel-memory 文档</title>
<link rel="stylesheet" href="/static/md-style.css">
</head>
<body>
<main class="markdown-body">
{body}
</main>
</body>
</html>"""


@app.get("/md/{filename:path}")
def render_markdown(filename: str):
    """渲染 static/ 下的 .md 文件为美观 HTML 页面（防止路径穿越）。"""
    static_root = (BASE / "static").resolve()
    filepath = (static_root / filename).resolve()
    if (
        filename.endswith("/")
        or ".." in filename
        or not str(filepath).startswith(str(static_root))
        or filepath.suffix.lower() != ".md"
        or not filepath.is_file()
    ):
        raise HTTPException(status_code=404, detail="文档不存在")
    text = filepath.read_text(encoding="utf-8")
    body = md_lib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return HTMLResponse(MD_TEMPLATE.format(title=filepath.stem, body=body))


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


@app.get("/")
def index(request: Request):
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__AMAP_KEY__", AMAP_KEY)
    html = html.replace("__AMAP_SECURITY__", AMAP_SECURITY)
    html = html.replace("__AMAP_VERSION__", amap_version(request))
    html = html.replace("__ICP_FOOTER__", icp_footer_html())
    html = html.replace("<body>", "<body>" + amap_key_notice_html(), 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ---------- 只读分享 ----------
@app.get("/share/{token}")
def share_page(token: str, request: Request):
    share = db.get_share(token)
    if not share:
        return HTMLResponse(
            "<div style='font-family:sans-serif;padding:40px;text-align:center'>"
            "<h2>分享不存在或已失效</h2></div>", status_code=404)
    html = (BASE / "static" / "share.html").read_text(encoding="utf-8")
    html = html.replace("__AMAP_KEY__", AMAP_KEY)
    html = html.replace("__AMAP_SECURITY__", AMAP_SECURITY)
    html = html.replace("__AMAP_VERSION__", amap_version(request))
    html = html.replace("<body>", "<body>" + amap_key_notice_html(), 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.post("/api/trips/{trip_id}/share")
def api_create_share(request: Request, trip_id: int, expire_days: int = Form(None),
                     password: str = Form("")):
    """为行程生成只读分享链接（需本人或管理员）；expire_days=None 表示永久有效；
    password 可选，非空则访问需密码"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    token = db.create_share(trip_id, expire_days, password or None)
    return {"token": token, "has_password": bool(password)}


@app.delete("/api/trips/{trip_id}/share")
def api_delete_share(request: Request, trip_id: int):
    """撤销某行程的全部分享链接（需本人或管理员）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    db.delete_shares_for_trip(trip_id)
    return {"ok": True}


@app.get("/api/trips/{trip_id}/share-count")
def api_share_count(request: Request, trip_id: int):
    """当前行程有效分享链接数量（需本人或管理员）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    return {"count": db.count_shares_for_trip(trip_id)}


@app.get("/api/share/{token}")
def api_share_view(token: str, password: str = ""):
    """只读分享数据：行程 + 点（含媒体），任何人凭 token 可访问，不能写；
    若分享设了密码，需携带正确 password 参数"""
    share = db.get_share(token)
    if not share:
        raise HTTPException(404, "分享不存在或已失效")
    if share.get("has_password"):
        if not db.verify_share_password(token, password):
            return JSONResponse({"error": "wrong_password"}, status_code=403)
    trip = db.get_trip(share["trip_id"])
    if not trip:
        raise HTTPException(404, "行程不存在")
    points = db.list_points(share["trip_id"])
    for p in points:
        p["media"] = db.list_media(p["id"])
    return {
        "trip": {
            "id": trip["id"], "name": trip["name"],
            "start_date": trip["start_date"], "end_date": trip["end_date"],
            "description": trip["description"],
        },
        "points": points,
    }


# ---------- 分享缩略图 / 二维码 ----------
_FONT_PATH = "/usr/share/fonts/google-noto-vf/NotoSans-VF.ttf"
_TILE_DIR = UPLOAD_DIR / "tiles"


def _load_font(size: int):
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _deg2num(lat, lng, zoom):
    """经纬度 -> Web Mercator 瓦片坐标（浮点，含小数用于精确定位）。"""
    n = 2.0 ** zoom
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _fetch_tile(z, x, y):
    """拉取高德栅格底图瓦片（带磁盘缓存），返回 PIL Image 或 None。"""
    _TILE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _TILE_DIR / f"{z}_{x}_{y}.png"
    if cache.exists():
        try:
            return Image.open(cache).convert("RGBA")
        except Exception:
            cache.unlink(missing_ok=True)
    host = f"webrd0{(x + y) % 4 + 1}.is.autonavi.com"
    url = f"https://{host}/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = urllib.request.urlopen(req, timeout=12).read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.save(cache, "PNG")
        return img
    except Exception:
        return None


def _osrm_route_path(a_lng, a_lat, b_lng, b_lat):
    """OSRM 单段驾车路线（带 route_cache 缓存），返回 [[lng,lat],...] 或 None。

    供 /api/route 与缩略图共用；缩略图渲染时逐段取真实路线，避免卡片上画直线。"""
    cache_key = _route_cache_key(a_lng, a_lat, b_lng, b_lat)
    cached = db.get_route_cache(cache_key)
    if cached and cached.get("routes"):
        return cached["routes"][0]["path"]
    url = (f"{OSRM_URL}/{a_lng},{a_lat};{b_lng},{b_lat}"
           f"?overview=simplified&geometries=geojson")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "travel-memory/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    coords = data["routes"][0].get("geometry", {}).get("coordinates", []) or []
    path = [[c[0], c[1]] for c in coords]
    if len(path) >= 2:
        db.set_route_cache(cache_key, {"routes": [{
            "path": path,
            "distance": data["routes"][0].get("distance", 0),
            "duration": data["routes"][0].get("duration", 0),
        }]})
    return path if len(path) >= 2 else None


def _render_trip_thumbnail(points, size=(640, 400)):
    """路线缩略图：高德栅格底图瓦片拼接作为背景，叠加真实驾车路线（OSRM）+ 点 + 序号。

    逐段取 OSRM 真实路线（失败段退化为直线直连），与地图页面显示一致，避免卡片上"全部直线"。"""
    w, h = size
    if not points:
        img = Image.new("RGB", size, (247, 249, 252))
        d = ImageDraw.Draw(img)
        f = _load_font(22)
        d.text((w // 2, h // 2), "暂无轨迹点", font=f, fill=(150, 150, 150), anchor="mm")
        return img

    # 并行取各段真实路线（带缓存；失败段返回 None → 画直线兜底）
    seg_paths = [None] * (len(points) - 1)
    if len(points) > 1:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [
                ex.submit(_osrm_route_path, a["lng"], a["lat"], b["lng"], b["lat"])
                for a, b in zip(points[:-1], points[1:])
            ]
            for i, f in enumerate(futs):
                try:
                    seg_paths[i] = f.result()
                except Exception:
                    seg_paths[i] = None

    # 合并点 + 路线坐标计算包围盒（路线可能绕行超出点包围盒）
    all_pts = [(p["lng"], p["lat"]) for p in points]
    for sp in seg_paths:
        if sp:
            all_pts.extend((c[0], c[1]) for c in sp)
    lats = [c[1] for c in all_pts]
    lngs = [c[0] for c in all_pts]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    span_lng = (max_lng - min_lng) or 0.001
    span_lat = (max_lat - min_lat) or 0.001

    # 选 zoom：让行程跨度约占图 75%
    target_px = 0.75 * min(w, h)
    z_lng = math.log2(target_px * 360.0 / (span_lng * 256.0))
    z_lat = math.log2(target_px * 360.0 / (span_lat * 256.0))
    zoom = int(round(min(z_lng, z_lat)))
    zoom = max(2, min(zoom, 11))

    # 瓦片范围（含 padding）
    pad_lng = span_lng * 0.3
    pad_lat = span_lat * 0.3
    tx0, ty0 = _deg2num(max_lat + pad_lat, min_lng - pad_lng, zoom)
    tx1, ty1 = _deg2num(min_lat - pad_lat, max_lng + pad_lng, zoom)
    x0, x1 = int(math.floor(tx0)), int(math.floor(tx1))
    y0, y1 = int(math.floor(ty0)), int(math.floor(ty1))

    # 拼接瓦片成底图
    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    canvas = Image.new("RGB", (cols * 256, rows * 256), (247, 249, 252))
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = _fetch_tile(zoom, tx, ty)
            if tile:
                canvas.paste(tile, ((tx - x0) * 256, (ty - y0) * 256))

    def project(lng, lat):
        tx, ty = _deg2num(lat, lng, zoom)
        return (tx - x0) * 256, (ty - y0) * 256

    # 行程投影包围盒，裁剪到目标尺寸
    proj = [project(p["lng"], p["lat"]) for p in points]
    px_min = min(x for x, _ in proj)
    px_max = max(x for x, _ in proj)
    py_min = min(y for _, y in proj)
    py_max = max(y for _, y in proj)

    pad_px = 30
    # 行程包围盒中心与跨度（含 padding），按目标宽高比确定裁剪框，避免 resize 拉伸变形
    cx = (px_min + px_max) / 2
    cy = (py_min + py_max) / 2
    span_x = (px_max - px_min) + 2 * pad_px
    span_y = (py_max - py_min) + 2 * pad_px
    aspect = w / h  # 目标宽高比（640/400 = 1.6）
    # 裁剪框宽高比恒为 aspect，且能容纳行程（含 padding）
    crop_w = max(span_x, span_y * aspect)
    crop_h = crop_w / aspect
    if crop_h < span_y:
        crop_h = span_y
        crop_w = crop_h * aspect
    crop_l = cx - crop_w / 2
    crop_t = cy - crop_h / 2
    crop_r = cx + crop_w / 2
    crop_b = cy + crop_h / 2
    # 超出瓦片画布则整体平移回画布内（保持尺寸与比例不变）
    if crop_l < 0:
        crop_r -= crop_l; crop_l = 0
    if crop_t < 0:
        crop_b -= crop_t; crop_t = 0
    if crop_r > canvas.width:
        crop_l -= (crop_r - canvas.width); crop_r = canvas.width
    if crop_b > canvas.height:
        crop_t -= (crop_b - canvas.height); crop_b = canvas.height
    crop_l = max(0, int(crop_l)); crop_t = max(0, int(crop_t))
    crop_r = min(canvas.width, int(crop_r)); crop_b = min(canvas.height, int(crop_b))
    if crop_r - crop_l < 2:
        crop_r = crop_l + 2
    if crop_b - crop_t < 2:
        crop_b = crop_t + 2

    region = canvas.crop((crop_l, crop_t, crop_r, crop_b)).resize((w, h), Image.LANCZOS)

    def to_px(lng, lat):
        x, y = project(lng, lat)
        return (x - crop_l) / (crop_r - crop_l) * w, (y - crop_t) / (crop_b - crop_t) * h

    d = ImageDraw.Draw(region)
    # 真实路线逐段画（失败段直线直连兜底）
    for i, (a, b) in enumerate(zip(points[:-1], points[1:])):
        sp = seg_paths[i]
        if sp and len(sp) >= 2:
            seg_pts = [to_px(c[0], c[1]) for c in sp]
        else:
            seg_pts = [to_px(a["lng"], a["lat"]), to_px(b["lng"], b["lat"])]
        d.line(seg_pts, fill=(0, 122, 255), width=3, joint="curve")

    font = _load_font(11)
    pts = [to_px(p["lng"], p["lat"]) for p in points]
    for i, (x, y) in enumerate(pts):
        r = 10
        d.ellipse([x - r, y - r, x + r, y + r], fill=(0, 122, 255), outline=(255, 255, 255), width=2)
        d.text((x, y), str(i + 1), font=font, fill=(255, 255, 255), anchor="mm")
    return region.convert("RGB")


def _etag_of(*parts) -> str:
    """由「决定图片内容的输入」算 ETag —— 注意不是由渲染结果算。

    由结果算的话每次都得先把图渲染出来才知道能不能回 304，省下的只有传输；
    由输入算就能在渲染之前直接回 304，把 PIL 合成的 CPU 也一起省掉。"""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'


def _maybe_304(request: Request, etag: str, cache_control: str):
    """If-None-Match 命中就回 304，否则返回 None 让调用方继续渲染。"""
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": cache_control})
    return None


@app.get("/api/share/{token}/qrcode")
def api_share_qrcode(request: Request, token: str):
    """分享二维码（PNG），扫码直达只读分享页。"""
    share = db.get_share(token)
    if not share:
        raise HTTPException(404, "分享不存在或已失效")
    url = str(request.base_url).rstrip("/") + "/share/" + token
    # 二维码内容完全由 URL 决定，URL 不变图就不变，可以放心让浏览器长期缓存。
    etag = _etag_of("qrcode", url)
    cc = "public, max-age=86400"
    hit = _maybe_304(request, etag, cc)
    if hit:
        return hit
    img = qrcode.make(url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"ETag": etag, "Cache-Control": cc})


@app.get("/api/share/{token}/thumbnail")
def api_share_thumbnail(request: Request, token: str, password: str = ""):
    """分享路线缩略图（PNG）。设密码的分享需携带正确 password。"""
    share = db.get_share(token)
    if not share:
        raise HTTPException(404, "分享不存在或已失效")
    if share.get("has_password"):
        if not db.verify_share_password(token, password):
            return JSONResponse({"error": "wrong_password"}, status_code=403)
    points = db.list_points(share["trip_id"])
    # 这是个匿名端点（在 is_public_path 放行名单里），而每次请求都要拉高德底图瓦片、
    # 逐段查 OSRM、再用 PIL 合成一张 155KB 的 PNG（冷路径实测 2.2s）。原来一个缓存头
    # 都没有，等于把爬虫和微信预览的每一次抓取都变成一次全量重算。
    # ETag 由全部点算出来：改了行程图就变，没改就一直命中。
    etag = _etag_of("thumbnail", points)
    # 带密码的分享不能进共享缓存（CDN / 中间代理），只允许浏览器自己存。
    cc = "private, max-age=3600" if share.get("has_password") else "public, max-age=3600"
    hit = _maybe_304(request, etag, cc)
    if hit:
        return hit
    img = _render_trip_thumbnail(points)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"ETag": etag, "Cache-Control": cc})


# ---------- 路线规划 ----------
def _route_cache_key(from_lng, from_lat, to_lng, to_lat):
    """路线缓存键：同一对端点（坐标 6 位小数 ≈ 0.1m 精度）的驾车路线共享缓存。"""
    return f"drive|{from_lng:.6f},{from_lat:.6f}|{to_lng:.6f},{to_lat:.6f}"


@app.get("/api/route")
def api_route(from_lng: float, from_lat: float, to_lng: float, to_lat: float,
              refresh: int = 0):
    """全球驾车路线规划：高德不支持海外路线，用 OSRM 兜底。
    返回 routes 数组，每条含 polyline 坐标 path [[lng,lat],...]（与高德坐标顺序一致）、
    distance（米）、duration（秒）；无结果返回空 routes。

    结果按端点坐标缓存（route_cache 表），命中直接返回，避免每次刷新重复计算；
    仅当端点坐标变化（增删点/移动点）或 refresh=1 时才重新计算。"""
    cache_key = _route_cache_key(from_lng, from_lat, to_lng, to_lat)
    if not refresh:
        cached = db.get_route_cache(cache_key)
        if cached and cached.get("routes"):
            return cached
    url = (f"{OSRM_URL}/{from_lng},{from_lat};{to_lng},{to_lat}"
           f"?overview=simplified&geometries=geojson&alternatives=true")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "travel-memory/1.0"})
        with urllib.request.urlopen(req, timeout=OSRM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"routes": []}
    if data.get("code") != "Ok" or not data.get("routes"):
        return {"routes": []}
    routes = []
    for r in data["routes"]:
        coords = r.get("geometry", {}).get("coordinates", []) or []
        routes.append({
            "path": [[c[0], c[1]] for c in coords],
            "distance": r.get("distance", 0),
            "duration": r.get("duration", 0),
        })
    result = {"routes": routes}
    if routes:
        db.set_route_cache(cache_key, result)
    return result


@app.post("/api/route/clear-cache")
def api_clear_route_cache(request: Request):
    """清空路线缓存，强制下次加载重新计算全部曲线（需登录）。"""
    db.clear_route_cache()
    return {"ok": True}


# ---------- 行程 ----------
@app.get("/api/trips")
def api_list_trips(request: Request):
    return db.list_trips(current_user(request))


@app.post("/api/trips")
def api_create_trip(request: Request, name: str = Form(...), start_date: str = Form(""),
                    end_date: str = Form(""), description: str = Form(""),
                    status: str = Form("planning")):
    tid = db.create_trip(current_user(request), name, start_date or None,
                         end_date or None, description, status)
    return {"id": tid}


@app.put("/api/trips/{trip_id}")
def api_update_trip(request: Request, trip_id: int, name: str = Form(None),
                    start_date: str = Form(""), end_date: str = Form(""),
                    description: str = Form(""), status: str = Form(None)):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    db.update_trip(trip_id, name, start_date or None, end_date or None,
                   description or None, status or None)
    return {"ok": True}


@app.get("/api/trips/{trip_id}/generations")
def api_list_generations(request: Request, trip_id: int, kind: str = None, limit: int = 20):
    """查询 AI 生成内容历史（P1-2 落库后可按行程追溯）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    return db.list_generations(trip_id, kind, min(limit, 100))


@app.delete("/api/trips/{trip_id}")
def api_delete_trip(request: Request, trip_id: int):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    files = db.get_trip_media_files(trip_id)
    db.delete_trip(trip_id)
    for fname, thumb in files:
        (UPLOAD_DIR / fname).unlink(missing_ok=True)
        if thumb:
            (media_mod.THUMB_DIR / thumb).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/trips/{trip_id}/export")
def api_export_trip(request: Request, trip_id: int):
    """导出行程为纯文本 JSON（便于手工编辑/备份/迁移）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "行程不存在")
    points = db.list_points(trip_id)
    payload = {
        "format": "travel-memory",
        "version": 1,
        "trip": {
            "name": trip["name"],
            "start_date": trip["start_date"],
            "end_date": trip["end_date"],
            "description": trip["description"],
            "status": trip.get("status") or "planning",
        },
        "points": [
            {
                "name": p["name"],
                "lat": p["lat"],
                "lng": p["lng"],
                "start_date": p["start_date"],
                "end_date": p["end_date"],
                "description": p["description"],
                "route_mode": p["route_mode"] or "drive",
                "is_waypoint": int(p["is_waypoint"] or 0),
            }
            for p in points
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"travel-{trip_id}.json"
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/trips/import")
async def api_import_trip(request: Request):
    """从纯文本 JSON 导入行程（新建行程 + 全部点，不含媒体文件）"""
    user = current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "无法解析 JSON（需为导出格式）")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON 顶层必须是对象")
    trip = body.get("trip") or {}
    name = (trip.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "缺少行程名称")
    points = body.get("points") or []
    if not isinstance(points, list):
        raise HTTPException(400, "points 必须是数组")
    status = trip.get("status")
    if status not in ("planning", "traveling", "completed", "archived"):
        status = "planning"
    tid = db.create_trip(user, name, trip.get("start_date") or None,
                         trip.get("end_date") or None,
                         trip.get("description") or "", status)
    imported = 0
    for p in points:
        if not isinstance(p, dict):
            continue
        pname = (p.get("name") or "").strip()
        lat = p.get("lat")
        lng = p.get("lng")
        if not pname or lat is None or lng is None:
            continue
        try:
            lat = float(lat); lng = float(lng)
        except (TypeError, ValueError):
            continue
        mode = p.get("route_mode") if p.get("route_mode") in ("drive", "line") else "drive"
        wp = 1 if p.get("is_waypoint") else 0
        db.create_point(tid, pname, lat, lng,
                        p.get("start_date") or None, p.get("end_date") or None,
                        p.get("description") or "", mode, wp)
        imported += 1
    return {"id": tid, "point_count": imported}


@app.get("/api/trips/{trip_id}/stats")
def api_trip_stats(request: Request, trip_id: int):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    stats = db.get_trip_stats(trip_id)
    if not stats:
        raise HTTPException(404, "行程不存在")
    return stats


# ---------- 点 ----------
@app.get("/api/footprint")
def api_footprint(request: Request):
    """全量足迹（用户所有行程的所有点坐标），供地图足迹热力图使用。"""
    user = current_user(request)
    points = db.list_all_points(user)
    # 精简为热力图所需坐标 + 权重（同坐标聚合为更高 count，环线多次经过更亮）
    from collections import Counter
    counter = Counter()
    for p in points:
        counter[(round(p["lng"], 5), round(p["lat"], 5))] += 1
    data = [
        {"lng": lng, "lat": lat, "count": cnt}
        for (lng, lat), cnt in counter.items()
    ]
    return {"count": len(data), "data": data}


@app.get("/api/photo-layer")
def api_photo_layer(request: Request, trip_id: int = None):
    """地图照片图层：某个行程（trip_id）含照片的点 + 缩略图。

    不带 trip_id 时回落到「该用户全部行程」——保留这个行为是给足迹类用途留口子，
    但地图上的照片图层一律带 trip_id：正在看哪个行程，就只该看到哪个行程的照片。
    """
    user = current_user(request)
    if trip_id is not None:
        require_owner(user, db.get_trip_owner_id(trip_id), "行程")
    points = db.list_photo_points(user, trip_id)
    return {"count": len(points), "data": points}


@app.post("/api/diag/map")
async def api_diag_map(request: Request):
    """页面把自己的地图图层结构报回来，写进日志。

    排查「照片堆遮挡路线」时用：截图经常传不过来，而这个问题又强依赖
    浏览器实际渲染出的层级结构，只能让现场的页面自己说。
    不需要登录 —— 内容只有图层名和 z-index，没有任何用户数据。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    line = json.dumps({
        "t": time.strftime("%F %T"),
        "ua": (request.headers.get("user-agent", "") or "")[:180],
        "d": body,
    }, ensure_ascii=False)
    p = BASE / "logs" / "diag-map.log"
    p.parent.mkdir(exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return {"ok": True}


@app.get("/api/trips/{trip_id}/points")
def api_list_points(request: Request, trip_id: int):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    points = db.list_points(trip_id)
    counts = db.get_point_media_counts(trip_id)
    for p in points:
        c = counts.get(p["id"], {})
        p["photo_count"] = c.get("photo", 0)
        p["video_count"] = c.get("video", 0)
    return points


# ---------- 海拔回填 ----------
# 高德 JS API 不提供高程，所以走公开的 OpenTopoData（SRTM 90m）。
# 查到就写回 points.elevation 缓存住，同一个点只查一次；查不到保持 NULL，
# 下次打开这个行程会再试一次（不做重试上限——点数很少，且失败通常是临时的）。
_ELEV_URL = "https://api.opentopodata.org/v1/srtm90m"
_ELEV_BATCH = 100          # 服务方单次上限 100 个坐标
_ELEV_TIMEOUT = 8


def _fetch_elevations(coords):
    """coords: [(lat, lng), ...]（WGS-84）→ [elevation or None, ...]，整批失败返回全 None。"""
    if not coords:
        return []
    import urllib.parse as _up
    loc = "|".join("%.6f,%.6f" % (a, b) for a, b in coords)
    url = _ELEV_URL + "?locations=" + _up.quote(loc, safe="|,")
    try:
        with urllib.request.urlopen(url, timeout=_ELEV_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return [None] * len(coords)
    if data.get("status") != "OK":
        return [None] * len(coords)
    out = []
    for item in data.get("results", []):
        e = item.get("elevation")
        out.append(float(e) if isinstance(e, (int, float)) else None)
    # 服务端返回条数理论上一一对应，防御性补齐
    while len(out) < len(coords):
        out.append(None)
    return out[:len(coords)]


@app.post("/api/trips/{trip_id}/elevation")
async def api_fill_elevation(request: Request, trip_id: int):
    """给该行程里还没有海拔的点补上海拔。前端加载行程后调一次，失败不影响任何功能。"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    todo = db.list_points_without_elevation(trip_id)
    if not todo:
        return {"filled": 0, "elevations": {}}

    def work():
        filled = {}
        for i in range(0, len(todo), _ELEV_BATCH):
            chunk = todo[i:i + _ELEV_BATCH]
            # 库内是 GCJ-02（高德火星坐标），高程服务要 WGS-84，国内不转会偏几百米
            coords = []
            for p in chunk:
                wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
                coords.append((wlat, wlng))
            for p, e in zip(chunk, _fetch_elevations(coords)):
                if e is None:
                    continue
                db.set_point_elevation(p["id"], e)
                filled[str(p["id"])] = round(e, 1)
        return filled

    filled = await run_in_threadpool(work)
    return {"filled": len(filled), "elevations": filled}


@app.post("/api/trips/{trip_id}/points")
def api_create_point(request: Request, trip_id: int, name: str = Form(...),
                     lat: float = Form(...), lng: float = Form(...),
                     start_date: str = Form(""), end_date: str = Form(""),
                     description: str = Form(""), route_mode: str = Form("drive"),
                     is_waypoint: str = Form("0")):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    mode = route_mode if route_mode in ("drive", "line") else "drive"
    wp = 1 if is_waypoint in ("1", "true", "on", "yes") else 0
    pid = db.create_point(trip_id, name, lat, lng, start_date or None,
                          end_date or None, description, mode, wp)
    return {"id": pid}


@app.put("/api/points/{point_id}")
def api_update_point(request: Request, point_id: int, name: str = Form(...),
                     lat: float = Form(...), lng: float = Form(...),
                     start_date: str = Form(""), end_date: str = Form(""),
                     description: str = Form(""), route_mode: str = Form("drive"),
                     is_waypoint: str = Form("0")):
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    mode = route_mode if route_mode in ("drive", "line") else "drive"
    wp = 1 if is_waypoint in ("1", "true", "on", "yes") else 0
    db.update_point(point_id, name, lat, lng, start_date or None,
                    end_date or None, description, mode, wp)
    return {"ok": True}


@app.put("/api/points/{point_id}/route-mode")
def api_set_point_route_mode(request: Request, point_id: int, route_mode: str = Form("drive")):
    """单独更新某点「到下一点」的连线方式（drive/line），用于每段独立切换。"""
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    mode = route_mode if route_mode in ("drive", "line") else "drive"
    db.set_point_route_mode(point_id, mode)
    return {"ok": True, "route_mode": mode}


@app.post("/api/trips/{trip_id}/points/reorder")
def api_reorder_points(request: Request, trip_id: int, ordered_ids: str = Form(...)):
    """按前端传来的完整点序（逗号分隔的 id）重排同一天内的顺序。
    仅影响同一天内的 sort_order，跨天顺序由 start_date 决定不受影响。"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    ids = [int(x) for x in ordered_ids.split(",") if x.strip().isdigit()]
    if not db.reorder_points(trip_id, ids):
        raise HTTPException(400, "排序数据无效")
    return {"ok": True}


@app.delete("/api/points/{point_id}")
def api_delete_point(request: Request, point_id: int):
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    files = db.get_point_media_files(point_id)
    db.delete_point(point_id)
    for fname, thumb in files:
        (UPLOAD_DIR / fname).unlink(missing_ok=True)
        if thumb:
            (media_mod.THUMB_DIR / thumb).unlink(missing_ok=True)
    return {"ok": True}


# ---------- 媒体 ----------
@app.get("/api/points/{point_id}/media")
def api_list_media(request: Request, point_id: int):
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    return db.list_media(point_id)


@app.post("/api/points/{point_id}/media")
def api_upload_media(request: Request, point_id: int, file: UploadFile = File(...)):
    """上传照片/视频。

    刻意是同步 `def` 而不是 `async def`：函数体里有三段重活 —— 整文件写盘、
    PIL 解码缩放编码、视频还要 subprocess.run(ffmpeg, timeout=30)。写成 async 的话
    这些全在事件循环线程上跑，传一个视频就能让整站对所有人卡住最长 30 秒。
    改成同步 def 之后 FastAPI 会自动把整个函数丢进线程池，阻塞就只影响这一个请求。
    """
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    if not db.get_point(point_id):
        raise HTTPException(404, "点不存在")

    # 先看 Content-Length，能在把 body 读进内存之前就把超大请求挡掉。
    # 客户端可以谎报，所以下面拿到 raw 之后还会按真实大小再判一次。
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > MAX_UPLOAD_VIDEO + (2 << 20):
        raise HTTPException(413, f"文件过大，上限 {MAX_UPLOAD_VIDEO // (1024 * 1024)}MB")

    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "空文件")

    fname = file.filename or "unknown"
    ext = Path(fname).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"):
        mtype = "photo"
    elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        mtype = "video"
    else:
        raise HTTPException(400, f"不支持的文件类型: {ext}")

    limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
    if len(raw) > limit:
        raise HTTPException(
            413, f"{'照片' if mtype == 'photo' else '视频'}最大 {limit // (1024 * 1024)}MB，"
                 f"当前 {len(raw) / (1024 * 1024):.1f}MB")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    src_path = UPLOAD_DIR / stored_name
    src_path.write_bytes(raw)

    thumb_name = media_mod.make_thumb(src_path, mtype)
    exif_time = exif_lat = exif_lng = None
    if mtype == "photo":
        exif_time, exif_lat, exif_lng = media_mod.read_exif(src_path)

    mid = db.add_media(point_id, mtype, stored_name, thumb_name,
                       exif_time, exif_lat, exif_lng)
    return {
        "id": mid, "type": mtype, "filename": stored_name,
        "thumb": thumb_name, "exif_time": exif_time,
        "exif_lat": exif_lat, "exif_lng": exif_lng,
    }


@app.put("/api/media/{media_id}/caption")
def api_set_media_caption(request: Request, media_id: int, caption: str = Form("")):
    """给单张照片/视频写一段文字描述（需本人或管理员）"""
    require_owner(current_user(request), db.get_media_owner_id(media_id), "媒体")
    db.set_media_caption(media_id, (caption or "").strip()[:500])
    return {"ok": True}


@app.put("/api/points/{point_id}/media/order")
def api_set_media_order(request: Request, point_id: int, ids: str = Form(...)):
    """点详情里拖拽排序后保存新顺序。ids 是逗号分隔的 media id，按新顺序排列。"""
    require_owner(current_user(request), db.get_point_owner_id(point_id), "轨迹点")
    order = [x for x in (ids or "").split(",") if x.strip()]
    n = db.set_media_order(point_id, order)
    return {"ok": True, "count": n}


@app.delete("/api/media/{media_id}")
def api_delete_media(request: Request, media_id: int):
    require_owner(current_user(request), db.get_media_owner_id(media_id), "媒体")
    m = db.get_media(media_id)
    if m:
        (UPLOAD_DIR / m["filename"]).unlink(missing_ok=True)
        if m["thumb_filename"]:
            (media_mod.THUMB_DIR / m["thumb_filename"]).unlink(missing_ok=True)
        db.delete_media(media_id)
    return {"ok": True}


# ---------- P2-3 多媒体检索 ----------
@app.get("/api/trips/{trip_id}/media/search")
def api_search_media(request: Request, trip_id: int,
                     q: str = "", type: str = "", date_from: str = "", date_to: str = ""):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    results = db.search_trip_media(
        trip_id,
        q=q or None,
        mtype=type or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    for r in results:
        r["url"] = f"/uploads/{r['filename']}"
        r["thumb_url"] = f"/uploads/thumbs/{r['thumb_filename']}" if r.get("thumb_filename") else None
    return {"results": results, "count": len(results)}


@app.get("/api/trips/{trip_id}/media")
def api_trip_media(request: Request, trip_id: int):
    """行程内全部媒体的扁平列表，按拍摄时间升序（相册网格用，前端自行筛选/排序）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    items = db.list_trip_media(trip_id)
    return {
        "items": [{
            "id": m["id"],
            "type": m["type"],
            "point_id": m["point_id"],
            "point_name": m.get("point_name"),
            "filename": m["filename"],
            "thumb_filename": m.get("thumb_filename"),
            "url": f"/uploads/{m['filename']}",
            "thumb_url": (f"/uploads/thumbs/{m['thumb_filename']}"
                          if m.get("thumb_filename") else None),
            # 优先 EXIF 拍摄时间，缺失时退到上传时间（排序/分组都用它）
            "taken_at": m.get("exif_time") or m.get("created_at"),
            "caption": m.get("caption") or "",
        } for m in items],
        "count": len(items),
    }


# ---------- P1-4a 相册智能聚类 ----------
_GAP_HOURS = 4  # 同一地点内，拍摄间隔超过 4 小时视为新事件


@app.get("/api/trips/{trip_id}/albums")
def api_trip_albums(request: Request, trip_id: int):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    media_list = db.list_trip_media(trip_id)
    if not media_list:
        return {"albums": []}

    # 按点分组，组内按时间排序
    from collections import OrderedDict
    groups: "OrderedDict[int, list]" = OrderedDict()
    for m in media_list:
        groups.setdefault(m["point_id"], []).append(m)

    albums = []
    aid = 1
    for point_id, items in groups.items():
        items.sort(key=lambda x: (x.get("exif_time") or "", x["id"]))
        # 组内按时间间隔切分事件
        cur: list = []
        prev_time = None
        for it in items:
            t = it.get("exif_time")
            if prev_time and t and prev_time and _gap_hours(prev_time, t) > _GAP_HOURS:
                albums.append(_make_album(aid, point_id, cur))
                aid += 1
                cur = []
            cur.append(it)
            if t:
                prev_time = t
        if cur:
            albums.append(_make_album(aid, point_id, cur))
            aid += 1

    return {"albums": albums, "count": len(albums)}


def _gap_hours(t1: str, t2: str) -> float:
    """两个 ISO 时间字符串的小时间隔；解析失败返回 0（不切分）"""
    try:
        from datetime import datetime
        f1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        f2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        if f1.tzinfo and f2.tzinfo:
            return abs((f2 - f1).total_seconds()) / 3600
        return abs((f2 - f1).total_seconds()) / 3600
    except Exception:
        return 0


def _make_album(aid: int, point_id: int, items: list) -> dict:
    point_name = items[0].get("point_name") or "未命名地点"
    dates = [it.get("exif_time") or "" for it in items]
    dates = [d[:10] for d in dates if d]
    date_range = dates[0] if dates else (items[0].get("point_start") or "")
    if len(set(dates)) > 1:
        date_range = f"{dates[0]} ~ {dates[-1]}"
    cover = items[0]
    return {
        "id": aid,
        "point_id": point_id,
        "title": f"{point_name} · {date_range}" if date_range else point_name,
        "point_name": point_name,
        "date_range": date_range,
        "count": len(items),
        "photo_count": sum(1 for it in items if it["type"] == "photo"),
        "video_count": sum(1 for it in items if it["type"] == "video"),
        "cover_url": f"/uploads/thumbs/{cover['thumb_filename']}" if cover.get("thumb_filename") else None,
        "media": [{
            "id": it["id"], "type": it["type"],
            "url": f"/uploads/{it['filename']}",
            "thumb_url": f"/uploads/thumbs/{it['thumb_filename']}" if it.get("thumb_filename") else None,
            "exif_time": it.get("exif_time"),
            "point_id": it["point_id"],
            "caption": it.get("caption") or "",
        } for it in items],
    }


# ---------- P1-4b 旅程视频生成（FFmpeg 模板引擎） ----------
import video_gen

_VIDEO_TASKS: dict = {}  # trip_id -> task dict
_VIDEO_LOCK = threading.Lock()


@app.post("/api/trips/{trip_id}/video")
def api_start_video(request: Request, trip_id: int,
                    template: str = Form("slideshow"),
                    seconds_per_photo: float = Form(3.0)):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    if template not in ("slideshow", "cinematic"):
        raise HTTPException(400, "template 仅支持 slideshow / cinematic")
    if not (1 <= seconds_per_photo <= 8):
        raise HTTPException(400, "seconds_per_photo 需在 1~8 之间")

    with _VIDEO_LOCK:
        cur = _VIDEO_TASKS.get(trip_id)
        if cur and cur["status"] in ("pending", "running"):
            raise HTTPException(409, f"该行程已有视频任务在生成中（{cur['status']}）")

        task = {
            "trip_id": trip_id, "template": template,
            "seconds_per_photo": seconds_per_photo,
            "status": "pending", "progress": 0, "total": 0,
            "message": "排队中…", "output": None, "error": None,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _VIDEO_TASKS[trip_id] = task

    def _worker():
        def on_progress(done, total, msg):
            with _VIDEO_LOCK:
                task["progress"] = done
                task["total"] = total
                task["message"] = msg
        try:
            with _VIDEO_LOCK:
                task["status"] = "running"
                task["message"] = "准备素材…"
            result = video_gen.generate_trip_video(
                trip_id, template=template,
                seconds_per_photo=seconds_per_photo,
                on_progress=on_progress,
            )
            with _VIDEO_LOCK:
                task["status"] = "done"
                task["progress"] = task["total"]
                task["message"] = "生成完成"
                task["output"] = result["output"]
                task["result"] = result
        except Exception as e:
            with _VIDEO_LOCK:
                task["status"] = "failed"
                task["error"] = str(e)
                task["message"] = f"失败：{e}"

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "task": {k: task[k] for k in ("status", "template", "message")}}


@app.get("/api/trips/{trip_id}/video/status")
def api_video_status(request: Request, trip_id: int):
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    with _VIDEO_LOCK:
        task = _VIDEO_TASKS.get(trip_id)
        if not task:
            return {"status": "idle", "message": "暂无生成任务"}
        return {
            "status": task["status"], "progress": task["progress"],
            "total": task["total"], "message": task["message"],
            "output": task["output"], "error": task["error"],
            "template": task["template"],
            "started_at": task["started_at"],
            "video_url": f"/{task['output']}" if task.get("output") else None,
        }


# ============================================================
# 领域 API 层（/api/ai/*）—— 记忆 / 联想 / 生成 + API Key 认证
# ============================================================

def _api_key_auth(request: Request, required_scope: str):
    """API Key 鉴权：成功返回 api_key dict，失败抛 401。"""
    auth = request.headers.get("Authorization", "")
    key, err = token_auth.authorize(auth, required_scope)
    if not key:
        msg = {"invalid_api_key": "API Key 无效或已过期/吊销",
               "insufficient_scope": f"API Key 缺少 {required_scope} 权限"}[err]
        raise HTTPException(401, msg)
    _enforce_rate_limit(request, key, required_scope)
    return key


def _enforce_rate_limit(request: Request, key, required_scope: str):
    """P0-1 限流：Key 维度（读/写分级）+ IP 维度兜底。超限抛 429。"""
    allowed, retry = rate_limit.check_key(key["id"], required_scope)
    if not allowed:
        raise HTTPException(429, f"请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    ip = request.client.host if request.client else ""
    allowed, retry = rate_limit.check_ip(ip)
    if not allowed:
        raise HTTPException(429, f"请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})


# ---------- P0-2 写操作幂等（client_request_id 去重） ----------
def _idempotency_check(key, client_request_id: str, action: str):
    """写操作幂等守卫：client_request_id 非空时去重。

    返回 None（首次执行，继续）或 dict（幂等命中，直接返回缓存结果）。
    并发冲突（pending 未完成）抛 409。
    """
    if not client_request_id:
        return None
    if db.idempotency_claim(key["id"], client_request_id, action):
        return None  # 首次，抢占成功
    rec = db.idempotency_get(key["id"], client_request_id)
    if rec and rec.get("status") == "completed" and rec.get("result"):
        try:
            return json.loads(rec["result"])
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(409, "幂等冲突：请求正在处理中，请勿重复提交")
    raise HTTPException(409, "幂等冲突：相同 client_request_id 正在处理中，请勿重复提交")


def _idempotency_save(key, client_request_id: str, result: dict):
    """执行成功后缓存结果，供幂等命中时原样返回。"""
    if client_request_id:
        db.idempotency_complete(key["id"], client_request_id,
                                json.dumps(result, ensure_ascii=False))


def _idempotency_fail(key, client_request_id: str):
    """执行失败后丢弃 pending 记录，允许客户端修正参数后重试。"""
    if client_request_id:
        db.idempotency_discard(key["id"], client_request_id)


def _audit(api_key, action, status, detail=None, tokens_used=0, latency_ms=None):
    db.add_audit(
        api_key_id=api_key["id"] if api_key else None,
        user_id=api_key["user_id"] if api_key else None,
        action=action, method="AI", path=action, scope=api_key["scope"] if api_key else "",
        status=status, detail=detail, tokens_used=tokens_used, latency_ms=latency_ms,
    )


@app.get("/api/ai/trips")
def ai_list_trips(request: Request):
    """列出当前 API Key 所属用户的行程（简表），供 agent 枚举资源。"""
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    trips = db.list_trips(user or {"id": key["user_id"], "is_admin": 0})
    brief = [
        {"id": t["id"], "name": t["name"], "start_date": t["start_date"],
         "end_date": t["end_date"], "description": t["description"],
         "status": t.get("status") or "planning"}
        for t in trips
    ]
    _audit(key, "trips.list", "ok", detail=f"n={len(brief)}")
    return {"trips": brief}


@app.get("/api/ai/memory")
def ai_memory_overview(request: Request):
    """N-12 记忆共享协议：返回用户旅行记忆全景 bundle（标准化 schema）。

    让外部 agent（爱马仕总控等）一次性拉取「这个人去过哪里、是什么
    旅行人格」的一站式上下文，无需逐个行程调用。坐标统一 WGS-84。
    read scope；纯聚合无 LLM 调用，不受生成限额约束。
    """
    key = _api_key_auth(request, "read")
    try:
        result = domain_memory.build_memory_overview(key["user_id"])
        _audit(key, "memory.overview", "ok",
               detail=f"trips={result['overview']['n_trips']} "
                      f"points={result['overview']['n_points']}")
        return result
    except Exception as e:
        _audit(key, "memory.overview", "error", detail=str(e))
        raise HTTPException(500, f"记忆全景构建失败：{e}")


@app.get("/api/ai/patterns")
def ai_discover_patterns(request: Request):
    """N-6 跨旅程模式发现：行为聚类，产出「旅行画像」可分享卡片。

    read scope，纯规则聚类（零 token，不调 LLM）。发现用户自己都未必
    意识到的模式：重访地点、季节偏好、地点类型偏好、出行节奏。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_pattern.discover_patterns(user, db)
    _audit(key, "patterns.discover", "ok",
           detail=f"trips={result['trip_count']} points={result['point_count']}")
    return result


@app.get("/api/ai/unfinished")
def ai_unfinished(request: Request):
    """N-21 未完成旅程识别：把「没说出口的遗憾」变成前进动力。

    规则层（零 token）扫描三类信号——搁置的计划（planning > 90 天）、
    没归档的旅程（traveling 超期）、计划过没去成的地方（planning 城市
    从未在 completed 行程出现）；有信号时 DeepSeek 生成一句
    「遗憾→动力」提示语（可降级规则模板）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_unfinished.find_unfinished(user, db)
    _audit(key, "unfinished.scan", "ok",
           detail=f"stalled={len(result['stalled_plans'])} "
                  f"unclosed={len(result['unclosed_trips'])} "
                  f"unfulfilled={len(result['unfulfilled_destinations'])}")
    return result


@app.get("/api/ai/wrapped")
def ai_wrapped(request: Request, year: int | None = None):
    """N-11 年度旅行报告：把一年的足迹折叠成一张卡片。

    规则层（零 token）聚合该年行程/地点/城市/里程/媒体/情绪统计；LLM 层
    生成有温度的年度叙事（可降级规则模板）。机制先行：任何年份可回溯，
    数据不足输出轻量版；12 月后自动可出首份真实年报。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_wrapped.annual_report(user, year, db)
    _audit(key, "wrapped.annual", "ok",
           detail=f"year={result['year']} trips={result['stats']['trip_count']} "
                  f"points={result['stats']['point_count']}")
    return result


@app.get("/api/ai/shared")
def ai_shared(request: Request, with_username: str = ""):
    """N-14 共同记忆 v1：两个人的足迹叠在一起的故事。

    规则层（零 token）合并两用户足迹——共同城市、时间重叠（同日/同月）、
    各自独享目的地；LLM 层生成「共同记忆」叙事（可降级规则模板）。
    隐私边界：只输出城市级+日期级信息。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    if not with_username:
        raise HTTPException(422, "缺少参数 with_username")
    try:
        result = domain_relation.shared_memory(user, with_username, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _audit(key, "relation.shared", "ok",
           detail=f"with={with_username} common={len(result['common_cities'])}")
    return result


@app.get("/api/ai/trips/{trip_id}/reinterpret")
def ai_reinterpret(request: Request, trip_id: int, lens: str = "成长"):
    """N-16 记忆再阐释：给过去的旅程换一个视角重新讲述。

    规则层提取行程原始摘要+真实地点清单（防 LLM 编造地名）；LLM 层按
    lens（成长/遗憾/同行/幽默）重讲，可降级规则模板。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    if lens not in domain_reinterpret.LENSES:
        raise HTTPException(400, f"lens 必须是 {'/'.join(domain_reinterpret.LENSES)} 之一")
    try:
        result = domain_reinterpret.reinterpret_trip(user, trip_id, lens, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _audit(key, "reinterpret.run", "ok",
           detail=f"trip={trip_id} lens={result['lens']}")
    return result


@app.get("/api/ai/biography")
def ai_biography(request: Request):
    """N-18 旅行人生传记：把一生的足迹写成一本分章节的书。

    规则层按时间排序全部已成行行程生成章节骨架 + 全局统计；LLM 逐章
    生成纪实体散文（无地点清单的行程降级规则模板防编造）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_biography.biography(user, db)
    _audit(key, "biography.generate", "ok",
           detail=f"trips={result['stats']['trip_count']} "
                  f"chapters={len(result['chapters'])}")
    return result


@app.get("/api/ai/timetravel")
def ai_timetravel(request: Request, mode: str = "", date: str = "",
                  trip_id: int | None = None):
    """N-20 时间旅行：rewind 回溯（date=YYYY-MM-DD 当天足迹快照）/
    preview 预演（date 当天规划行程）/ counterfactual 反事实
    （trip_id 行程的「如果换一条路」推演，带【虚构推演】标注）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    try:
        result = domain_timetravel.time_travel(
            user, mode, db, date=date or None, trip_id=trip_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(key, "timetravel.run", "ok",
           detail=f"mode={result['mode']} date={date or '-'} trip={trip_id or '-'}")
    return result


@app.post("/api/ai/recommend")
def ai_recommend(request: Request, count: int = Form(5), style: str = Form("")):
    """P2-2 行程推荐：基于历史足迹推荐「还没去过、但可能喜欢」的目的地。

    每个推荐带可解释理由（「因为你上次在海边城市最开心」）。是 N-15
    （人格驱动的下一个目的地）的地基版——不依赖尚未完成的 N-5 人格库。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    count = max(1, min(int(count), 6))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"推荐过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_recommend.recommend_destinations(key["user_id"], count, style)
        _audit(key, "memory.recommend", "ok",
               detail=f"n={len(result['recommendations'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.recommend", "error", detail=str(e))
        raise HTTPException(500, f"推荐失败：{e}")


# ===== N-17 记忆可携带/数字遗产：全量导出 + 全量导入 =====
# 数据主权叙事（N-1）的技术闭环：用户数据不锁定在平台，可随时全量
# 带走（export）、迁移到另一账号（import）、传承给继承人（export+import）。
# schema: travel-memory.full-bundle.v1 —— 媒体文件二进制不打包，仅导出
# 元数据（filename/exif 等），文件本体需单独迁移（诚实告知，不假装完美）。

_FULL_BUNDLE_SCHEMA = "travel-memory.full-bundle.v1"


def _point_to_bundle(p: dict) -> dict:
    """把库内点（GCJ-02）转成可携带 bundle 点（WGS-84），带情绪元数据 + 媒体元数据。"""
    try:
        wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
    except (TypeError, ValueError):
        wlng, wlat = p["lng"], p["lat"]
    meta = db.get_point_meta(p["id"])
    out = {
        "name": p["name"], "lat": wlat, "lng": wlng,
        "start_date": p["start_date"], "end_date": p["end_date"],
        "description": p["description"], "route_mode": p["route_mode"] or "drive",
        "is_waypoint": int(p["is_waypoint"] or 0),
    }
    if meta:
        out["meta"] = {k: meta[k] for k in
                       ("mood", "weather", "scene", "time_of_day", "season", "meta_json")
                       if meta.get(k)}
    media = db.list_media(p["id"])
    if media:
        out["media"] = [{
            "type": m["type"], "filename": m["filename"],
            "thumb_filename": m["thumb_filename"], "exif_time": m["exif_time"],
            "exif_lat": m["exif_lat"], "exif_lng": m["exif_lng"],
        } for m in media]
    return out


@app.get("/api/ai/memory/export")
def ai_memory_export(request: Request):
    """N-17 全量打包：导出当前用户全部旅行记忆（行程+点+情绪元数据+媒体元数据+人格画像+联想）。

    read scope，纯聚合无 LLM。返回可导入/迁移/传承的 full-bundle.v1。
    """
    from datetime import datetime, timezone
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    trips = db.list_trips(user)

    bundle_trips = []
    for t in trips:
        bundle_trips.append({
            "name": t["name"], "start_date": t["start_date"],
            "end_date": t["end_date"], "description": t["description"],
            "status": t.get("status") or "planning",
            "points": [_point_to_bundle(p) for p in db.list_points(t["id"])],
        })

    prefs = db.list_prefs(key["user_id"])
    assoc = domain_assoc.get_associations(key["user_id"], limit=50)

    payload = {
        "schema": _FULL_BUNDLE_SCHEMA,
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "person": {"display_name": user.get("display_name", "") if user else ""},
        "note": "媒体文件二进制未打包，仅导出元数据（filename/exif）；文件本体需单独迁移。",
        "trips": bundle_trips,
        "persona": {"preferences": prefs, "associations": assoc},
    }
    n_points = sum(len(t["points"]) for t in bundle_trips)
    _audit(key, "memory.export", "ok",
           detail=f"trips={len(bundle_trips)} points={n_points}")
    return payload


@app.post("/api/ai/memory/import")
async def ai_memory_import(request: Request, client_request_id: str = None):
    """N-17 全量导入：把 full-bundle.v1 打包恢复到当前账号（迁移/传承/灾备恢复）。

    write scope。坐标 WGS-84 → 库内 GCJ-02。媒体仅导入元数据。
    幂等：client_request_id 走 query 参数（body 为 JSON bundle，避免 form/json 流冲突）。
    """
    key = _api_key_auth(request, "write")
    client_request_id = client_request_id or ""

    # 幂等编排
    if client_request_id:
        idem = _idempotency_check(key, client_request_id, "memory.import")
        if idem is not None:
            return idem

    try:
        body = await request.json()
    except Exception:
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, "无法解析 JSON（需为 full-bundle.v1 导出格式）")

    if not isinstance(body, dict) or body.get("schema") != _FULL_BUNDLE_SCHEMA:
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, f"不支持的 bundle schema（应为 {_FULL_BUNDLE_SCHEMA}）")

    trips = body.get("trips") or []
    if not isinstance(trips, list):
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, "trips 必须是数组")

    user = db.get_user_by_id(key["user_id"])
    n_trips = n_points = n_media = 0
    for t in trips:
        if not isinstance(t, dict):
            continue
        name = (t.get("name") or "").strip()
        if not name:
            continue
        status = t.get("status")
        if status not in ("planning", "traveling", "completed", "archived"):
            status = "planning"
        tid = db.create_trip(user, name, t.get("start_date"), t.get("end_date"),
                             t.get("description") or "", status)
        n_trips += 1

        for p in t.get("points") or []:
            if not isinstance(p, dict):
                continue
            pname = (p.get("name") or "").strip()
            if not pname:
                continue
            # WGS-84 → 库内 GCJ-02
            try:
                glng, glat = domain_schemas.wgs84_to_gcj02(
                    float(p["lng"]), float(p["lat"]))
            except (TypeError, ValueError, KeyError):
                glng, glat = p.get("lng"), p.get("lat")
            pid = db.create_point(tid, pname, glat, glng, p.get("start_date"),
                                  p.get("end_date"), p.get("description") or "",
                                  p.get("route_mode") or "drive",
                                  int(p.get("is_waypoint") or 0))
            n_points += 1

            meta = p.get("meta")
            if isinstance(meta, dict):
                db.upsert_point_meta(
                    pid, mood=meta.get("mood"), weather=meta.get("weather"),
                    scene=meta.get("scene"), time_of_day=meta.get("time_of_day"),
                    season=meta.get("season"), meta_json=meta.get("meta_json"),
                    source=meta.get("source") or "import")

            for m in p.get("media") or []:
                if not isinstance(m, dict):
                    continue
                db.add_media(pid, m.get("type") or "photo", m.get("filename") or "",
                             m.get("thumb_filename"), m.get("exif_time"),
                             m.get("exif_lat"), m.get("exif_lng"))
                n_media += 1

    # 人格偏好导入（strength 经 upsert 重新累加，evidence 从 1 起，可接受失真）
    for pr in (body.get("persona") or {}).get("preferences") or []:
        if not isinstance(pr, dict):
            continue
        k = pr.get("pref_key") or pr.get("key")
        if not k:
            continue
        db.upsert_pref(key["user_id"], k, pr.get("pref_value") or pr.get("value"))

    result = {"imported_trips": n_trips, "imported_points": n_points,
              "imported_media": n_media,
              "note": "媒体文件本体未迁移，仅元数据已导入；文件需另行上传。"}
    if client_request_id:
        _idempotency_save(key, client_request_id, result)
    _audit(key, "memory.import", "ok",
           detail=f"trips={n_trips} points={n_points} media={n_media}")
    return result


@app.get("/api/ai/trips/{trip_id}/generations")
def ai_list_generations(request: Request, trip_id: int, kind: str = None, limit: int = 20):
    """查询行程的 AI 生成历史（P1-2 落库后按行程追溯）。read scope。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    _audit(key, "trips.generations", "ok", detail=f"trip={trip_id}")
    return {"generations": db.list_generations(trip_id, kind, min(limit, 100))}


@app.get("/api/ai/trips/{trip_id}/memory")
def ai_get_memory(request: Request, trip_id: int):
    key = _api_key_auth(request, "read")
    # 归属校验。这一行原来漏了 —— 同目录另外 19 个按 trip_id 取数的路由都有，
    # 只有这个没有，任何 read scope 的 Key 遍历 trip_id 就能读到别人整条行程
    # （行程名、日期、每个点的名称/坐标/描述）。实测已确认可越权读取。
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    mem = domain_memory.build_memory(trip_id)
    if not mem:
        raise HTTPException(404, "行程不存在")
    _audit(key, "memory.get", "ok")
    return mem


@app.get("/api/ai/associations")
def ai_get_associations(request: Request, trip_id: int = None, limit: int = 5):
    key = _api_key_auth(request, "read")
    result = domain_assoc.get_associations(key["user_id"], trip_id=trip_id, limit=limit)
    _audit(key, "association.get", "ok", detail=f"n={len(result)}")
    return {"associations": result}


@app.get("/api/ai/preferences")
def ai_get_preferences(request: Request):
    key = _api_key_auth(request, "read")
    # 惰性沉淀：先幂等派生偏好（单行程也沉淀），再返回
    added = domain_assoc.derive_preferences(key["user_id"])
    result = domain_assoc.get_preferences(key["user_id"])
    _audit(key, "preferences.get", "ok", detail=f"n={len(result)} new={added}")
    return {"preferences": result, "newly_derived": added}


@app.post("/api/ai/trips/{trip_id}/profile")
def ai_extract_profile(request: Request, trip_id: int):
    """N-5 旅行人格画像库（LLM 档）：从行程抽取细粒度偏好，upsert 到 user_prefs。

    与规则档（theme/season，derive_preferences）互补：规则档幂等惰性沉淀，
    本端点显式触发 DeepSeek 抽取（目的地类型/活动/时间/住宿/消费/拍照/饮食）。
    偏好 key 用 `llm:{类别}` 前缀，与规则档 `theme:`/`season:` 区分。
    """
    key = _api_key_auth(request, "read")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    # LLM 抽取同样受生成限额约束（最贵操作）
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"抽取请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_gen.extract_profile(trip_id)
        # 同类别的多值合并（value 顿号连接，strength 取均值）
        merged = {}
        for p in result["prefs"]:
            cat = p["key"]
            m = merged.setdefault(cat, {"values": [], "strength": 0.0, "n": 0})
            m["values"].append(p["value"])
            m["strength"] += p["strength"]
            m["n"] += 1
        for cat, m in merged.items():
            val = "、".join(m["values"])
            s = m["strength"] / m["n"]
            db.upsert_pref(key["user_id"], f"llm:{cat}", val, strength_delta=s)
        added = len(merged)
        _audit(key, "profile.extract", "ok", detail=f"trip={trip_id} new={added}",
               tokens_used=result["tokens_used"], latency_ms=int((time.time() - t0) * 1000))
        return {"trip_id": trip_id, "added": added, "prefs": result["prefs"],
                "tokens_used": result["tokens_used"]}
    except ValueError as e:
        _audit(key, "profile.extract", "error", detail=str(e))
        raise HTTPException(404, str(e))
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "profile.extract", "error", detail=str(e))
        raise HTTPException(500, f"偏好抽取失败：{e}")


@app.post("/api/ai/points/{point_id}/meta")
def ai_extract_point_meta(request: Request, point_id: int):
    """N-7：单点情绪/场景元数据采集（LLM 抽取 + 落 point_meta）。read scope。"""
    key = _api_key_auth(request, "read")
    point = db.get_point(point_id)
    if not point:
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    trip = db.get_trip(point["trip_id"])
    pd = dict(point)
    pd["trip_name"] = trip["name"] if trip else ""
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"抽取请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        meta = domain_meta.extract_point_meta(pd)
        db.upsert_point_meta(point_id, mood=meta["mood"], weather=meta["weather"],
                             scene=meta["scene"], time_of_day=meta["time_of_day"],
                             season=meta["season"], source="llm")
        _audit(key, "meta.extract", "ok", detail=f"point={point_id}",
               tokens_used=meta["tokens_used"], latency_ms=int((time.time() - t0) * 1000))
        return {"point_id": point_id,
                "meta": {k: meta[k] for k in ("mood", "weather", "scene",
                                              "time_of_day", "season")},
                "tokens_used": meta["tokens_used"]}
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "meta.extract", "error", detail=str(e))
        raise HTTPException(500, f"元数据抽取失败：{e}")


@app.post("/api/ai/trips/{trip_id}/meta")
def ai_extract_trip_meta(request: Request, trip_id: int, max_points: int = Form(50)):
    """N-7：整行程所有点的元数据批量采集（串行 LLM，逐点占生成限额）。read scope。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    points = db.list_points(trip_id)
    if not points:
        return {"trip_id": trip_id, "total": 0, "processed": 0,
                "errors": [], "tokens_used": 0, "results": []}
    trip = db.get_trip(trip_id)
    trip_name = trip["name"] if trip else ""
    max_points = max(1, min(int(max_points or 50), 200))
    points = points[:max_points]

    results, errors = [], []
    total_tokens = 0
    t0 = time.time()
    for p in points:
        allowed, retry = rate_limit.check_generate(key["id"])
        if not allowed:
            errors.append({"point_id": p["id"], "name": p["name"],
                           "error": f"生成限额已耗尽，请 {retry}s 后重试"})
            break
        pd = dict(p)
        pd["trip_name"] = trip_name
        try:
            meta = domain_meta.extract_point_meta(pd)
            db.upsert_point_meta(p["id"], mood=meta["mood"], weather=meta["weather"],
                                 scene=meta["scene"], time_of_day=meta["time_of_day"],
                                 season=meta["season"], source="llm")
            total_tokens += meta["tokens_used"]
            results.append({"point_id": p["id"], "name": p["name"],
                            "meta": {k: meta[k] for k in ("mood", "weather", "scene",
                                                          "time_of_day", "season")}})
        except llm_provider.LLMBusy:
            # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
            raise
        except Exception as e:
            errors.append({"point_id": p["id"], "name": p["name"], "error": str(e)})
    _audit(key, "meta.extract_trip", "ok",
           detail=f"trip={trip_id} processed={len(results)} errors={len(errors)}",
           tokens_used=total_tokens, latency_ms=int((time.time() - t0) * 1000))
    return {"trip_id": trip_id, "total": len(points), "processed": len(results),
            "errors": errors, "tokens_used": total_tokens, "results": results}


@app.get("/api/ai/trips/{trip_id}/meta")
def ai_get_trip_meta(request: Request, trip_id: int):
    """N-7：查询行程下已采集的点级元数据（纯查询，不占生成限额）。read scope。

    返回坐标统一 WGS-84（库内 GCJ-02），meta_json 解析为对象。
    """
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    metas = db.list_trip_meta(trip_id)
    out = []
    for m in metas:
        m = dict(m)
        if m.get("meta_json"):
            try:
                m["meta_json"] = json.loads(m["meta_json"])
            except Exception:
                pass
        if m.get("lat") is not None and m.get("lng") is not None:
            wgs_lng, wgs_lat = domain_schemas.gcj02_to_wgs84(m["lng"], m["lat"])
            m["lng"], m["lat"] = round(wgs_lng, 6), round(wgs_lat, 6)
        out.append(m)
    _audit(key, "meta.list", "ok", detail=f"trip={trip_id} n={len(out)}")
    return {"trip_id": trip_id, "count": len(out), "meta": out}


@app.post("/api/ai/search")
def ai_search(request: Request, query: str = Form(""), limit: int = Form(5)):
    """N-8 记忆语义检索：自然语言查询旅行记忆，供外部 agent 跨 agent 调用。

    「我去过哪些海边？」「哪里拍过日落？」「去年这个时候我在哪儿？」
    两段式：规则召回（零 token）+ DeepSeek 语义精排（可降级）。
    read scope；LLM 精排受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    if not query or not query.strip():
        raise HTTPException(400, "缺少 query")
    query = query.strip()
    limit = max(1, min(int(limit), 10))
    # LLM 精排受生成限额约束
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"检索请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_search.semantic_search(key["user_id"], query, limit)
        _audit(key, "memory.search", "ok",
               detail=f"q={query[:40]} n={len(result['matches'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.search", "error", detail=str(e))
        raise HTTPException(500, f"检索失败：{e}")


@app.post("/api/ai/ask")
def ai_ask(request: Request, question: str = Form(""), limit: int = Form(5)):
    """N-9 记忆可对话：自然语言「问」自己的旅行记忆，返回一段对话式回答。

    「我上次在巴黎吃的小馆子叫什么？」「我是不是每次去海边都住海景房？」
    建立在 N-8 语义检索之上：先检索匹配记忆，再用 LLM 组织成有温度的回答。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    if not question or not question.strip():
        raise HTTPException(400, "缺少 question")
    question = question.strip()
    limit = max(1, min(int(limit), 10))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"提问过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_search.answer_question(key["user_id"], question, limit)
        _audit(key, "memory.ask", "ok",
               detail=f"q={question[:40]} n={len(result['matches'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.ask", "error", detail=str(e))
        raise HTTPException(500, f"问答失败：{e}")


@app.get("/api/ai/recall")
def ai_recall(request: Request, lookahead_days: int = 7):
    """N-2 记忆唤醒（手动触发版）：今天/未来 N 天内的周年纪念 + LLM 唤醒语。

    「去年今天你在巴黎」—— 找用户「去过」记忆里「月-日」落在 [今天,
    今天+lookahead_days] 且年份早于今年的点，用 DeepSeek 组织成有温度的唤醒语。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    lookahead_days = max(0, min(int(lookahead_days or 7), 30))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"唤醒请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_recall.compose_recall(key["user_id"], None, lookahead_days)
        _audit(key, "memory.recall", "ok",
               detail=f"n={len(result['anniversaries'])} empty={result['empty']}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.recall", "error", detail=str(e))
        raise HTTPException(500, f"唤醒失败：{e}")


@app.get("/api/ai/triggers")
def ai_triggers(request: Request, lookahead_days: int = 7):
    """N-10 主动叙事触发器清单：扫描所有触发器类型，产出「今日候选触发器」。

    化学反应 7 —— 让 agent 从「被动响应」走向「主动陪伴」。返回 anniversary /
    seasonal / revisit / location 四类候选触发器 + summary 计数。纯聚合
    （复用 recall 的周年匹配），不调 LLM、不占生成限额，read scope。
    """
    key = _api_key_auth(request, "read")
    lookahead_days = max(0, min(int(lookahead_days or 7), 30))
    t0 = time.time()
    try:
        result = domain_trigger.scan_triggers(key["user_id"], None, lookahead_days)
        _audit(key, "memory.triggers", "ok",
               detail=f"total={result['total']} {result['summary']}",
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except Exception as e:
        _audit(key, "memory.triggers", "error", detail=str(e))
        raise HTTPException(500, f"触发器扫描失败：{e}")


@app.post("/api/ai/generate")
def ai_generate(request: Request, kind: str = Form(""), trip_id: int = Form(0),
                style: str = Form("旅行者视角"), language: str = Form("zh")):
    key = _api_key_auth(request, "read")
    if not kind or not trip_id:
        raise HTTPException(400, "缺少 kind 或 trip_id")
    # P0-1：LLM 生成额外加更严限额（最贵操作，防刷 token）
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"生成请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_gen.generate(kind, trip_id, style, language)
        _audit(key, f"generate.{kind}", "ok",
               detail=f"trip={trip_id}", tokens_used=result["tokens_used"],
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except ValueError as e:
        _audit(key, f"generate.{kind}", "error", detail=str(e))
        raise HTTPException(404, str(e))
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, f"generate.{kind}", "error", detail=str(e))
        raise HTTPException(500, f"生成失败：{e}")


# ---------- 领域 API 层 — 写操作（write scope） ----------
# 让 agent（搭子/其他智能体）不止能"读"旅行记忆，还能"记"新地点/新行程。
# 坐标约定：对外接收 WGS-84（GPS 原生），入库前统一转 GCJ-02（高德前端坐标系）。

def _key_is_owner(key, owner_id) -> bool:
    """API Key 用户是否为资源 owner（admin 放行）。"""
    if owner_id is None:
        return False
    if owner_id == key["user_id"]:
        return True
    user = db.get_user_by_id(key["user_id"])
    return bool(user and user.get("is_admin"))


@app.post("/api/ai/trips")
def ai_create_trip(request: Request, name: str = Form(...), start_date: str = Form(""),
                   end_date: str = Form(""), description: str = Form(""),
                   status: str = Form("planning"), client_request_id: str = Form("")):
    """创建新行程（write scope）。返回新行程 id。

    client_request_id（可选）：幂等键，网络重试时重复提交不会重复建行程，
    直接返回首次结果。
    """
    key = _api_key_auth(request, "write")
    cached = _idempotency_check(key, client_request_id, "trips.create")
    if cached is not None:
        return cached
    try:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "行程名称不能为空")
        status = _normalize_trip_status(status)
        user = {"id": key["user_id"], "is_admin": 0}
        tid = db.create_trip(user, name, start_date or None, end_date or None,
                             description, status)
        _audit(key, "trips.create", "ok", detail=f"trip={tid} name={name}")
        result = {"id": tid}
        _idempotency_save(key, client_request_id, result)
        return result
    except HTTPException:
        _idempotency_fail(key, client_request_id)
        raise


@app.put("/api/ai/trips/{trip_id}")
def ai_update_trip(request: Request, trip_id: int, name: str = Form(None),
                   start_date: str = Form(""), end_date: str = Form(""),
                   description: str = Form(""), status: str = Form(None)):
    """更新行程（write scope）。status 可单独更新行程状态机。"""
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    db.update_trip(trip_id, name, start_date or None, end_date or None,
                   description or None,
                   _normalize_trip_status(status) if status else None)
    _audit(key, "trips.update", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


def _resolve_point_coords(name: str, lat_s: str, lng_s: str):
    """解析地点坐标 → (gcj_lng, gcj_lat, source, formatted)。

    优先用调用方提供的 WGS-84 坐标（转 GCJ-02 入库）；
    缺失时用高德地理编码按地名兜底（返回 GCJ-02 直接入库）。
    坐标越界（lat -90~90 / lng -180~180）或 geocode 失败 → 抛 HTTPException。
    formatted 为解析到的规范化地址（geocode 时返回，供核对；manual 时为空）。
    """
    if lat_s and lng_s:
        try:
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            raise HTTPException(400, "坐标格式错误")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(400, f"坐标越界：lat={lat} lng={lng}")
        gcj_lng, gcj_lat = domain_schemas.wgs84_to_gcj02(lng, lat)
        return gcj_lng, gcj_lat, "manual", ""
    geo = domain_geocode.geocode(name)
    if not geo:
        raise HTTPException(400, "未提供坐标且地名地理编码失败，请提供准确经纬度")
    return geo["lng"], geo["lat"], "geocode", geo.get("formatted", "")


@app.get("/api/ai/geocode")
def ai_geocode(request: Request, address: str = ""):
    """按地名查询坐标（read scope）。返回 WGS-84（对外约定）。供 agent 校验/补坐标。"""
    key = _api_key_auth(request, "read")
    address = (address or "").strip()
    if not address:
        raise HTTPException(400, "缺少 address 参数")
    geo = domain_geocode.geocode_wgs84(address)
    if not geo:
        raise HTTPException(404, f"未能解析地名「{address}」")
    _audit(key, "geocode.lookup", "ok", detail=f"addr={address}")
    return {"address": address, "formatted": geo["formatted"],
            "lng": geo["lng"], "lat": geo["lat"]}


@app.post("/api/ai/trips/{trip_id}/points")
def ai_create_point(request: Request, trip_id: int, name: str = Form(...),
                    lat: str = Form(""), lng: str = Form(""),
                    start_date: str = Form(""), end_date: str = Form(""),
                    description: str = Form(""), route_mode: str = Form("drive"),
                    is_waypoint: str = Form("0"), client_request_id: str = Form("")):
    """为行程添加地点（write scope）。lat/lng 为 WGS-84（可选），缺失时按地名地理编码。

    client_request_id（可选）：幂等键，网络重试不会重复建点。
    """
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    cached = _idempotency_check(key, client_request_id, "points.create")
    if cached is not None:
        return cached
    try:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "地点名称不能为空")
        mode = route_mode if route_mode in ("drive", "line") else "drive"
        wp = 1 if is_waypoint in ("1", "true", "on", "yes") else 0
        gcj_lng, gcj_lat, src, fmt = _resolve_point_coords(name, lat, lng)
        pid = db.create_point(trip_id, name, gcj_lat, gcj_lng, start_date or None,
                              end_date or None, description, mode, wp)
        _audit(key, "points.create", "ok", detail=f"trip={trip_id} name={name} coord={src}")
        result = {"id": pid, "coord_source": src, "formatted": fmt}
        _idempotency_save(key, client_request_id, result)
        return result
    except HTTPException:
        _idempotency_fail(key, client_request_id)
        raise


@app.put("/api/ai/points/{point_id}")
def ai_update_point(request: Request, point_id: int, name: str = Form(...),
                    lat: str = Form(""), lng: str = Form(""),
                    start_date: str = Form(""), end_date: str = Form(""),
                    description: str = Form(""), route_mode: str = Form("drive"),
                    is_waypoint: str = Form("0")):
    """更新地点（write scope）。lat/lng 为 WGS-84（可选），缺失时按地名地理编码。"""
    key = _api_key_auth(request, "write")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "地点名称不能为空")
    mode = route_mode if route_mode in ("drive", "line") else "drive"
    wp = 1 if is_waypoint in ("1", "true", "on", "yes") else 0
    gcj_lng, gcj_lat, src, fmt = _resolve_point_coords(name, lat, lng)
    db.update_point(point_id, name, gcj_lat, gcj_lng, start_date or None,
                    end_date or None, description, mode, wp)
    _audit(key, "points.update", "ok", detail=f"point={point_id} coord={src}")
    return {"ok": True, "coord_source": src, "formatted": fmt}


@app.delete("/api/ai/points/{point_id}")
def ai_delete_point(request: Request, point_id: int):
    """删除地点（write scope）。"""
    key = _api_key_auth(request, "write")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    db.delete_point(point_id)
    _audit(key, "points.delete", "ok", detail=f"point={point_id}")
    return {"ok": True}


@app.delete("/api/ai/trips/{trip_id}")
def ai_delete_trip(request: Request, trip_id: int):
    """删除行程（write scope），级联删除媒体文件。"""
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    files = db.get_trip_media_files(trip_id)
    db.delete_trip(trip_id)
    for fname, thumb in files:
        (UPLOAD_DIR / fname).unlink(missing_ok=True)
        if thumb:
            (media_mod.THUMB_DIR / thumb).unlink(missing_ok=True)
    _audit(key, "trips.delete", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


@app.get("/api/ai/trips/{trip_id}/export")
def ai_export_trip(request: Request, trip_id: int):
    """导出行程为 JSON（read scope）。坐标对外转 WGS-84（符合领域 API 约定），
    便于其他 agent 备份/迁移/二次加工。"""
    key = _api_key_auth(request, "read")
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    points = db.list_points(trip_id)
    payload = {
        "format": "travel-memory",
        "version": 1,
        "coord_sys": "wgs84",  # 对外统一 WGS-84
        "trip": {
            "name": trip["name"], "start_date": trip["start_date"],
            "end_date": trip["end_date"], "description": trip["description"],
        },
        "points": [],
    }
    for p in points:
        try:
            wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
        except (TypeError, ValueError):
            wlng, wlat = p["lng"], p["lat"]
        payload["points"].append({
            "name": p["name"], "lat": wlat, "lng": wlng,
            "start_date": p["start_date"], "end_date": p["end_date"],
            "description": p["description"], "route_mode": p["route_mode"] or "drive",
            "is_waypoint": int(p["is_waypoint"] or 0),
        })
    _audit(key, "trips.export", "ok", detail=f"trip={trip_id} points={len(payload['points'])}")
    return payload


@app.get("/api/ai/trips/{trip_id}/stats")
def ai_trip_stats(request: Request, trip_id: int):
    """行程统计（read scope）：点数/媒体数/起止日期。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    stats = db.get_trip_stats(trip_id)
    _audit(key, "trips.stats", "ok", detail=f"trip={trip_id}")
    return stats


@app.post("/api/ai/trips/{trip_id}/share")
def ai_create_share(request: Request, trip_id: int, expire_days: int = Form(None),
                    password: str = Form("")):
    """为行程生成只读分享链接（share scope），返回 token（供 agent 分享给他人）。"""
    key = _api_key_auth(request, "share")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    token = db.create_share(trip_id, expire_days, password or None)
    _audit(key, "trips.share", "ok", detail=f"trip={trip_id}")
    return {"token": token, "has_password": bool(password),
            "url": f"/share/{token}"}


@app.delete("/api/ai/trips/{trip_id}/share")
def ai_delete_share(request: Request, trip_id: int):
    """撤销行程的全部分享链接（share scope）。"""
    key = _api_key_auth(request, "share")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    db.delete_shares_for_trip(trip_id)
    _audit(key, "trips.share.revoke", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


@app.get("/api/ai/trips/{trip_id}/share-count")
def ai_share_count(request: Request, trip_id: int):
    """当前行程有效分享链接数量（read scope）。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    return {"count": db.count_shares_for_trip(trip_id)}


# ---------- 领域 API 层：媒体（P1-1，供 agent 上传/查询/删除照片视频） ----------
def _media_ext(fname: str, content_type: str) -> str:
    """根据文件名或 content_type 推断扩展名，返回带点后缀（可能为空）。"""
    ext = Path(fname or "").suffix.lower()
    if ext:
        return ext
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/heic": ".heic",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    }.get((content_type or "").split(";")[0].strip(), "")


@app.get("/api/ai/points/{point_id}/media")
def ai_list_media(request: Request, point_id: int):
    """列出某地点的媒体（read scope）。返回元数据（含 filename/thumb/exif）。"""
    key = _api_key_auth(request, "read")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    _audit(key, "media.list", "ok", detail=f"point={point_id}")
    return db.list_media(point_id)


@app.post("/api/ai/points/{point_id}/media")
def ai_upload_media(request: Request, point_id: int, data_base64: str = Form(...),
                    filename: str = Form(""), content_type: str = Form("")):
    """上传媒体（write scope）。data_base64 为文件内容 base64（HTTP transport 无法传 multipart）。

    返回媒体元数据（含 exif 提取的时间/坐标）。filename/content_type 用于推断类型。
    """
    key = _api_key_auth(request, "write")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    try:
        raw = base64.b64decode(data_base64)
    except Exception:
        raise HTTPException(400, "data_base64 解码失败，请传标准 base64 编码内容")
    if not raw:
        raise HTTPException(400, "空文件")

    ext = _media_ext(filename, content_type)
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"):
        mtype = "photo"
    elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        mtype = "video"
    else:
        raise HTTPException(400, f"不支持的文件类型（需在 filename 或 content_type 中指明）: {ext or '未知'}")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    src_path = UPLOAD_DIR / stored_name
    src_path.write_bytes(raw)

    thumb_name = media_mod.make_thumb(src_path, mtype)
    exif_time = exif_lat = exif_lng = None
    if mtype == "photo":
        exif_time, exif_lat, exif_lng = media_mod.read_exif(src_path)

    mid = db.add_media(point_id, mtype, stored_name, thumb_name,
                       exif_time, exif_lat, exif_lng)
    _audit(key, "media.upload", "ok", detail=f"point={point_id} type={mtype}")
    return {"id": mid, "type": mtype, "filename": stored_name, "thumb": thumb_name,
            "exif_time": exif_time, "exif_lat": exif_lat, "exif_lng": exif_lng}


@app.delete("/api/ai/media/{media_id}")
def ai_delete_media(request: Request, media_id: int):
    """删除媒体（write scope）。"""
    key = _api_key_auth(request, "write")
    if not _key_is_owner(key, db.get_media_owner_id(media_id)):
        raise HTTPException(404, "媒体不存在")
    m = db.get_media(media_id)
    if m:
        (UPLOAD_DIR / m["filename"]).unlink(missing_ok=True)
        if m["thumb_filename"]:
            (media_mod.THUMB_DIR / m["thumb_filename"]).unlink(missing_ok=True)
        db.delete_media(media_id)
    _audit(key, "media.delete", "ok", detail=f"media={media_id}")
    return {"ok": True}


# ---------- MCP Streamable HTTP 远程端点（开放平台：远程 Agent 接入） ----------
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP 端点 — 远程 Agent 通过标准 MCP 协议接入。

    认证：Authorization: Bearer tm_xxx（调用者**自己的** API Key，每个请求独立鉴权）。
    tools/call 按工具所需 scope（read/write/share）校验；
    工具执行时携带调用者 key 调领域 API → 数据隔离由领域 API 层保证。
    无状态实现（stateless server），不维护 MCP session。
    """
    _mcp_headers = {"MCP-Protocol-Version": mcp_http.PROTOCOL_VERSION}
    auth = request.headers.get("Authorization", "")
    key = token_auth.resolve_key(auth)
    raw_key = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    if not key or not raw_key:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": "invalid_api_key"}},
            status_code=401, headers=_mcp_headers)
    # P0-1 限流（Key 维度 + IP 兜底）
    allowed, retry = rate_limit.limiter.check(f"key:{key['id']}:mcp", rate_limit.READ_LIMIT)
    if not allowed:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32029, "message": f"rate limited, retry in {retry}s"}},
            status_code=429, headers=_mcp_headers)
    ip = request.client.host if request.client else ""
    allowed, retry = rate_limit.check_ip(ip)
    if not allowed:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32029, "message": f"rate limited, retry in {retry}s"}},
            status_code=429, headers=_mcp_headers)
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "parse error"}},
            status_code=400, headers=_mcp_headers)
    msg_id = body.get("id")
    # tools/call：校验该工具所需 scope（防止低权限 key 调用高权限工具）
    if body.get("method") == "tools/call":
        tool = (body.get("params") or {}).get("name")
        need = mcp_http.TOOL_SCOPE.get(tool, "read")
        if tool and not token_auth.has_scope(key, need):
            return JSONResponse(mcp_http.scope_error_response(msg_id, tool),
                                status_code=403, headers=_mcp_headers)
    # 领域 API 调用可能耗时（如 generate 调 LLM），放线程池避免阻塞事件循环
    resp = await run_in_threadpool(mcp_http.handle_jsonrpc, body, raw_key)
    if resp is None:
        return Response(status_code=202, headers=_mcp_headers)  # 通知类消息
    return JSONResponse(resp, headers=_mcp_headers)


# ============================================================
# 开放平台 / 开发者控制台（/console）—— 面向开发者，session 认证
# 与领域 API（/api/ai/*，API Key 认证）彻底分离：这是给「人」管理 Key 的面板，
# 领域 API 是给「agent」调用的能力。二者认证体系不同，路径命名空间也不同。
# ============================================================

VALID_SCOPES = ("read", "write", "share", "admin")


def _normalize_scope(scope_s: str) -> str:
    """校验并规范化 scope：去重、排序、只保留合法 token（read/write/share/admin）。"""
    tokens = []
    for t in (scope_s or "read").replace(",", " ").split():
        if t in VALID_SCOPES and t not in tokens:
            tokens.append(t)
    return ",".join(tokens) if tokens else "read"


# 行程状态机合法状态（planning 计划中 → traveling 进行中 → completed 已完成 → archived 已归档）
TRIP_STATUSES = ("planning", "traveling", "completed", "archived")


def _normalize_trip_status(status_s: str) -> str:
    """校验行程状态，非法值回退 planning。"""
    return status_s if status_s in TRIP_STATUSES else "planning"


@app.get("/console")
def console_page(request: Request):
    """开发者控制台页面（需登录 session；面向开发者，风格与旅行者界面分离）。"""
    html = (BASE / "static" / "console.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/console/me")
def console_me(request: Request):
    return _public_user(current_user(request))


@app.get("/api/console/keys")
def console_list_keys(request: Request):
    """列出当前用户的 API Key（不含 raw_key，只显示前缀）。"""
    user = current_user(request)
    return {"keys": db.list_api_keys(user["id"])}


@app.post("/api/console/keys")
def console_create_key(request: Request, name: str = Form(""), scope: str = Form("read"),
                       expire_days: int = Form(0)):
    """自助创建 API Key。任何登录用户都能给自己建 Key（挂在当前用户名下）；
    admin scope 仅管理员可授予。raw_key 仅返回这一次。"""
    user = current_user(request)
    scope = _normalize_scope(scope)
    if "admin" in scope and not user["is_admin"]:
        raise HTTPException(403, "仅管理员可授予 admin 权限")
    name = (name or "").strip() or "未命名"
    raw_key, prefix, key_id = db.create_api_key(user["id"], name, scope,
                                                expire_days=expire_days or None)
    db.add_audit(None, user["id"], "key.create", "CONSOLE", "/api/console/keys",
                 scope, "ok", detail=f"name={name}")
    return {"ok": True, "id": key_id, "key": raw_key, "prefix": prefix, "scope": scope,
            "note": "raw_key 仅返回这一次，请立即保存"}


@app.delete("/api/console/keys/{key_id}")
def console_delete_key(request: Request, key_id: int):
    """删除 API Key（硬删除；审计日志保留，api_key_id 置空）。"""
    user = current_user(request)
    if not db.delete_api_key(key_id, user["id"]):
        raise HTTPException(404, "Key 不存在或不属于当前用户")
    db.add_audit(None, user["id"], "key.delete", "CONSOLE", f"/api/console/keys/{key_id}",
                 "", "ok", detail=f"key_id={key_id}")
    return {"ok": True}


@app.get("/api/console/usage")
def console_usage(request: Request):
    """按 Key 聚合的用量统计（调用次数 / 成功 / token 消耗）。"""
    user = current_user(request)
    return {"usage": db.usage_stats(user["id"])}


@app.get("/api/console/audit")
def console_audit(request: Request, limit: int = 100):
    """审计日志（管理员看全部，普通用户只看自己）。"""
    user = current_user(request)
    rows = db.list_audit(user_id=user["id"] if not user["is_admin"] else None, limit=limit)
    return {"audit": rows}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8084)
