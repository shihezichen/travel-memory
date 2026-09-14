"""旅行记忆系统 — FastAPI 主入口（多用户版）"""
import io
import os
import collections
import datetime
import logging
import base64
import json
import time
import shutil
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
import download  # 媒体「另存为」（Content-Disposition: attachment，见 download.py）
import media as media_mod
import media_purge  # 删媒体时一并清掉 OSS 上的对象（见 media_purge.py）
import oss           # OSS 直传签名（见 oss.py）
import oss_backfill  # 直传之后把对象拉回本机存档（见 oss_backfill.py）
import transcode  # 视频清晰度分层转码（后台线程，见 transcode.py 顶部的诊断说明）
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

from routers import agent_api
from routers import console as console_mod
from routers import share as share_mod
from routers.agent_api import router as agent_api_router
from routers.console import router as console_router
from routers.share import router as share_router

from config import BASE, AMAP_KEY, AMAP_SECURITY, OSRM_URL, OSRM_TIMEOUT, MEDIA_BASE_URL, media_url  # noqa: F401
UPLOAD_DIR = BASE / "uploads"
# 全新克隆没有 uploads/（它在 .gitignore 里），而下面 app.mount 的 StaticFiles
# 在目录不存在时会直接抛异常 —— 服务连启动都启动不了。这里先建好。
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
(UPLOAD_DIR / "thumbs").mkdir(parents=True, exist_ok=True)
(UPLOAD_DIR / "variants").mkdir(parents=True, exist_ok=True)   # 视频清晰度档位

# 管理员令牌（从 token.txt 读取，作为管理员直登凭据；可不存在）
ADMIN_TOKEN = ""
_token_file = BASE / "token.txt"
if _token_file.exists():
    ADMIN_TOKEN = _token_file.read_text(encoding="utf-8").strip()


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
PUBLIC_PATHS = ("/", "/app", "/welcome",
                "/api/login", "/api/register", "/logout", "/api/route",
                "/api/gate", "/api/gate/status", "/health")


# 上传大小上限。原来一个上限都没有：raw = file.read() 是把整个文件读进内存，
# 一个 200MB 的视频就是 200MB RSS 峰值，并发几个就能把进程打到 OOM。
MAX_UPLOAD_PHOTO = int(os.environ.get("TRAVEL_MAX_UPLOAD_PHOTO_MB", "30")) * 1024 * 1024
MAX_UPLOAD_VIDEO = int(os.environ.get("TRAVEL_MAX_UPLOAD_VIDEO_MB", "200")) * 1024 * 1024
# 浏览器自己抽的那张缩略图的上限。512px 的 JPEG 撑死一两百 KB，2MB 已经很宽松了。
CLIENT_THUMB_MAX = int(os.environ.get("TRAVEL_CLIENT_THUMB_MAX_KB", "2048")) * 1024


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/static/"):
        return True
    # 媒体文件（图片/视频）：分享页匿名访问必需，且为只读静态资源
    if path.startswith("/uploads/"):
        return True
    # 站长验证文件：必须在登录和访问口令门禁之外，否则验证方拿到的是 401
    if path.endswith(".txt") and path.count("/") == 1:
        return True
    # 视频清晰度档位（只读 GET）：分享页是匿名的，它要能查「转码好了没」，
    # 否则轮询一路 401，档位菜单永远停在「转码中」。返回的只是档位文件名，
    # 而 /uploads/ 本身就是公开目录，不比原始文件名更敏感。
    # 只放行 /api/media/{id}/variants 本身 —— /variants/retry 是写操作，
    # 多一层路径段就落不进这个分支，仍旧要登录 + require_owner。
    if (path.startswith("/api/media/") and path.endswith("/variants")
            and path[len("/api/media/"):-len("/variants")].isdigit()):
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


# ---------- 资源版本号广播 ----------
# 已经开着的页面（尤其是加到桌面的 PWA）可能几天都不重新加载 index.html，于是
# 一直跑着旧 JS —— 2026-09-11 的「角标不刷新 / 还在走隧道上传」就是这么来的。
# 每个 /api 响应都带上当前版本号，前端 api() 一比对不一致就提示刷新（见 app.js）。
@app.middleware("http")
async def asset_version_header_middleware(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["X-Asset-Version"] = asset_version()
    return resp


# ---------- 规范域名跳转（www.top001.xin -> top001.xin）----------
# 注意注册顺序：Starlette 里后注册的中间件在**外层**，所以这段必须写在
# auth_middleware 之后 —— 它要在鉴权之前跑，否则 www 上的请求会先吃一个 401，
# 用户看到的是报错而不是跳转。
CANONICAL_HOST = os.environ.get("TRAVEL_DOMAIN", "").strip().lower()


@app.middleware("http")
async def canonical_host_middleware(request: Request, call_next):
    if not CANONICAL_HOST:
        return await call_next(request)
    # Host 头可能带端口（top001.xin:443），比较时要剥掉；IPv6 字面量形如 [::1]:443，
    # 不能无脑 split(":")，所以按最后一个冒号且不在方括号内的情况处理。
    raw = (request.headers.get("host") or "").strip().lower()
    host = raw
    if host.startswith("["):
        host = host[:host.index("]") + 1] if "]" in host else host
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    if host != "www." + CANONICAL_HOST:
        return await call_next(request)

    from starlette.responses import RedirectResponse
    # 保留路径和 query，换掉 netloc。用 301：规范域名是长期决定，让浏览器和搜索引擎缓存。
    target = request.url.replace(netloc=CANONICAL_HOST)
    return RedirectResponse(url=str(target), status_code=301)


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
            "llm_provider": llm_provider.resolve_provider(),
            # 直传是否可用 + 还有多少对象没拉回本机存档
            "oss_direct": oss.direct_upload_enabled(),
            "oss_backfill": oss_backfill.queue_status() if db_ok else None,
            # 最近几次上传的实测速率（见 _note_upload_speed）
            "recent_uploads": list(_upload_stats)}
    return JSONResponse(body, status_code=200 if db_ok else 503)


# 同步路由的并发上限。FastAPI 把 `def`（非 async）路由丢进 anyio 的默认线程池跑，
# 而那个池的默认容量是 **40** —— 本应用 81 条路由里有 76 条是同步 def，
# 也就是说最多同时处理 40 个请求，第 41 个开始排队。
#
# 平时这不是问题（一次库查询 2ms，40 个槽位够几千 req/s）。会出事的是那几条
# **等外部服务**的路由：OSRM 路线规划最长等 8 秒、高德地理编码 10 秒、
# 分享页抓图 20 秒。而前端画一条行程的路线时是 Promise.all 并发请求**所有路段**
# （库里最长的行程有 16 段）—— 一个用户碰上路线缓存未命中，就能一次占掉 16 个槽位
# 并持有好几秒。三个这样的用户就把 40 个槽位吃干净，其余所有人的请求，
# 哪怕只是查个列表，也得排队等 OSRM 超时。
#
# 提到 160：这些槽位等的是网络 I/O 不是 CPU，线程绝大多数时间挂在 epoll 上，
# 多开的成本只有栈空间（约 8KB/线程 → 1.3MB），换来的是「慢请求不再拖死快请求」。
# 真正的 CPU 并发仍受 GIL 限制，这个数字改不了那件事，也不试图改。
THREADPOOL_LIMIT = int(os.environ.get("TRAVEL_THREADPOOL_LIMIT", "160"))


@app.on_event("startup")
def _startup_widen_threadpool():
    """把同步路由的线程池容量从 anyio 默认的 40 提到 THREADPOOL_LIMIT。

    必须在启动钩子里做：拿默认 limiter 要求当前有事件循环在跑。
    """
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_LIMIT
    except Exception as e:                                   # noqa: BLE001
        print("[startup] 调整线程池容量失败（沿用默认 40）:", e)


@app.on_event("startup")
def _startup_resume_backfill():
    """把上次没拉完的 OSS 回源任务重新排进队列。

    直传的媒体在回源完成之前，本机是没有原件的 —— 缩略图、清晰度档位、备份
    全都还欠着。进程重启把这件事忘掉的话，那些文件会永远只存在于 OSS。
    不能让它影响启动：任何异常都只记一条日志。
    """
    try:
        oss_backfill.resume_pending()
    except Exception as e:                                   # noqa: BLE001
        print("[startup] OSS 回源队列恢复失败:", e)


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


# 这三个请求级辅助函数搬到了 routers/deps.py —— 拆分出去的 routers/* 也要用它们，
# 而 routers 不能反过来 import app（循环）。在这里 import 进来，
# app.current_user / app.require_owner 等历史访问点保持不变。
from routers.deps import (  # noqa: F401
    current_user, require_owner, _public_user,
    amap_version, amap_key_notice_html, _route_cache_key,
    asset_version, stamp_assets,
)


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


class UploadStaticFiles(CacheHeaderStaticFiles):
    """/uploads 的缓存策略。原来一律 `no-cache, must-revalidate`，
    等于每次重播视频都要回源问一次 —— 这是诊断出来的次要卡顿来源。

    分三档，故意不给原图/原片一年期 immutable：
      - `?v=` 查询串：沿用父类的一年 immutable（前端主动打的版本号）。
      - `variants/`：一年 immutable。档位文件名是 `<uuid>_<档位>.mp4`，
        内容永不改变、也永不被覆盖，缓存死了也没关系。
      - 其余（原片/原图/缩略图）：1 天 public。**不能给 immutable** ——
        上传后的 faststart 重封装会原地覆盖同名原片（transcode.ensure_faststart），
        真给了一年 immutable，客户端就会把重封装之前那份「卡顿版」一直留着。
    """

    async def get_response(self, path, scope):
        norm = str(path).replace("\\", "/").lstrip("/")
        # 断点续传的半成品分片就落在 uploads/.chunks/ 下（见 CHUNK_DIR）。
        # 它在 StaticFiles 的根目录里，不挡住就等于开了一条「猜到 session_id
        # 就能下载别人正在传的文件」的路。这里直接当不存在。
        if norm.startswith(".chunks/") or norm == ".chunks":
            raise HTTPException(404, "Not Found")
        resp = await super().get_response(path, scope)
        qs = (scope.get("query_string") or b"").decode()
        if "v=" in qs:
            return resp                       # 父类已经设成一年 immutable
        if norm.startswith("variants/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp


class MdRedirectStaticFiles(CacheHeaderStaticFiles):
    """.md 文件不直接下载，301 重定向到 /md/ 渲染端点（在线浏览）"""

    async def get_response(self, path: str, scope):
        if path.lower().endswith(".md"):
            from starlette.responses import RedirectResponse
            return RedirectResponse(url=f"/md/{path}", status_code=301)
        return await super().get_response(path, scope)


app.mount("/static", MdRedirectStaticFiles(directory=BASE / "static"), name="static")
app.mount("/uploads", UploadStaticFiles(directory=UPLOAD_DIR), name="uploads")


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




def render_landing(cacheable: bool) -> HTMLResponse:
    """公开的宣传页。完全自包含（不引 style.css / app.js / 高德），
    所以除了备案号没有别的占位符要填。

    cacheable 只有 /welcome 能给 True。/ 的响应体是随 cookie 变的
    （登录了给应用、没登录给宣传页），一旦标成 public，中间的 CDN / 反代
    就可能把宣传页缓存下来发给已登录的人，反过来也一样。
    """
    html = (BASE / "static" / "landing.html").read_text(encoding="utf-8")
    html = html.replace("__ICP_FOOTER__", icp_footer_html())
    cc = "public, max-age=300" if cacheable else "no-cache, no-store, must-revalidate"
    return HTMLResponse(html, headers={"Cache-Control": cc})


def render_app(request: Request) -> HTMLResponse:
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("__AMAP_KEY__", AMAP_KEY)
    html = html.replace("__AMAP_SECURITY__", AMAP_SECURITY)
    html = html.replace("__MEDIA_BASE__", MEDIA_BASE_URL)
    # 前端据此决定走直传还是走隧道分片上传。任何一环没配好都回落到 ""（关闭），
    # 上传链路完整保留原样。
    html = html.replace("__OSS_DIRECT__", "1" if oss.direct_upload_enabled() else "")
    html = html.replace("__AMAP_VERSION__", amap_version(request))
    html = html.replace("__ICP_FOOTER__", icp_footer_html())
    html = html.replace("<body>", "<body>" + amap_key_notice_html(), 1)
    # 静态资源版本号由文件 mtime/size 算出来，不再手写（见 deps.asset_version）
    html = stamp_assets(html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/")
def index(request: Request):
    """根路径按登录态分流：没登录的访客看宣传页，登录过的人直接进应用。

    刻意不用 302 —— 跳转会在浏览器历史里多留一格，退出登录后按后退就在
    「/」和宣传页之间来回弹。同一个 URL 给不同内容，前进后退都是干净的。
    宣传页上的「登录」指向 /app，那条路无论登没登录都进应用，不会绕回来。
    """
    if not db.get_session_user(request.cookies.get(COOKIE_NAME)):
        return render_landing(cacheable=False)
    return render_app(request)


@app.get("/app")
def app_page(request: Request):
    """应用本体的固定入口。未登录时照旧由前端弹登录浮层。"""
    return render_app(request)


@app.get("/welcome")
def welcome(request: Request):
    """宣传页的固定入口 —— 登录状态下也想再看一眼介绍时走这里。"""
    return render_landing(cacheable=True)


# ---------- 站长验证文件 ----------
# 微信、Google、百度这类「证明你是网站管理员」的流程，都要求在根目录放一个
# 指定文件名和内容的 txt。每来一个就加一条硬编码路由太蠢 —— 统一从 verify/
# 目录取，加文件即生效，不用改代码也不用重启。
VERIFY_DIR = BASE / "verify"


def verify_file_path(fname: str):
    """把根目录的 <fname>.txt 映射到 verify/ 里的文件；不合法或不存在返回 None。

    文件名白名单只留字母数字和 _ - ，路径穿越（..、/、%2f）在这一步就断了，
    不依赖后面的 resolve 兜底。
    """
    if not fname or len(fname) > 80:
        return None
    if not all(c.isalnum() or c in "_-" for c in fname):
        return None
    p = VERIFY_DIR / (fname + ".txt")
    return p if p.is_file() else None


@app.get("/{fname}.txt")
def root_verify_txt(fname: str):
    p = verify_file_path(fname)
    if not p:
        raise HTTPException(404, "not found")
    # 验证方大多不带 If-None-Match，缓存久了反而不好排查，给个短的
    return FileResponse(p, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


# ============================================================
# 只读分享（/share/{token}、/api/share/*）+ 缩略图 / 二维码
# —— 已拆分到 routers/share.py，同样在原位置 include 保持注册顺序不变。
# ============================================================
app.include_router(share_router)


# ---------- 路线规划 ----------

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
    variants = db.get_trip_variant_files(trip_id)   # 必须在 delete 之前捞（行会级联消失）
    db.delete_trip(trip_id)
    media_purge.purge_file_pairs(UPLOAD_DIR, media_mod.THUMB_DIR, files, variants)
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
# 点数超过这个数就改走服务端网格聚合。在这之下按原样返回精确坐标——
# 几百个点的响应本来就小，聚合只会让位置变糊，没有好处。
FOOTPRINT_AGG_THRESHOLD = 2000
# 聚合之后最多还能有多少个格子。超了就把格子调粗再聚一次：低缩放下看整个
# 中国也不该往前端灌几万个圈，画不出细节，只会卡。
FOOTPRINT_MAX_CELLS = 4000
# 一个格子在屏幕上大约多少像素。热力图的圈本身直径就有几十像素，
# 比这更细的格子画出来是重叠的，白发。
FOOTPRINT_CELL_PX = 40


def footprint_cell_deg(zoom: float) -> float:
    """给定地图缩放级别，算出一个格子该有多少度。

    Web 墨卡托下整个世界宽 256 × 2^zoom 像素、跨 360 度，
    所以 FOOTPRINT_CELL_PX 像素 = 360 / (256 × 2^zoom) × FOOTPRINT_CELL_PX 度。
    """
    zoom = max(0.0, min(22.0, float(zoom)))
    return 360.0 / (256.0 * (2.0 ** zoom)) * FOOTPRINT_CELL_PX


def parse_bbox(raw):
    """`minLng,minLat,maxLng,maxLat` → 元组。不合法就当没传，回落到全局。"""
    if not raw:
        return None
    try:
        parts = [float(x) for x in str(raw).split(",")]
    except (TypeError, ValueError):
        return None
    if len(parts) != 4:
        return None
    min_lng, min_lat, max_lng, max_lat = parts
    if min_lng > max_lng or min_lat > max_lat:
        return None
    if not (-180 <= min_lng <= 180 and -180 <= max_lng <= 180
            and -90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        return None
    return (min_lng, min_lat, max_lng, max_lat)


@app.get("/api/footprint")
def api_footprint(request: Request, zoom: float = 10.0, bbox: str = None):
    """全量足迹（用户所有行程的所有点坐标），供地图足迹热力图使用。

    `bbox` 是地图当前可视范围。**带上它很重要**：不带的话就是「聚合全世界」，
    而用户放大地图时只看得到一条街 —— 按缩放级别算出来的细格子会让全世界
    的点都争这几千个格子上限，结果被逼着越聚越粗，放大了反而看不到细节。
    只看视野内的点，格子就能老老实实按缩放级别走。

    点少的时候（见 FOOTPRINT_AGG_THRESHOLD）返回精确到 5 位小数的原始坐标，
    行为和以前完全一样。点多了就在**服务端**做网格聚合，响应里带
    `aggregated: true`，前端据此知道视野变了要重新拉。
    """
    user = current_user(request)
    box = parse_bbox(bbox)
    total = db.count_all_points(user, box)

    if total <= FOOTPRINT_AGG_THRESHOLD:
        points = db.list_all_points(user, box)
        # 精简为热力图所需坐标 + 权重（同坐标聚合为更高 count，环线多次经过更亮）
        from collections import Counter
        counter = Counter()
        for p in points:
            counter[(round(p["lng"], 5), round(p["lat"], 5))] += 1
        data = [
            {"lng": lng, "lat": lat, "count": cnt}
            for (lng, lat), cnt in counter.items()
        ]
        return {"count": len(data), "data": data, "points": total,
                "aggregated": False, "zoom": zoom}

    cell = footprint_cell_deg(zoom)
    data = db.aggregate_footprint(user, cell, box)
    # 兜底：视野特别大（缩到看整个地球）或者点密得反常时，还是可能超上限。
    # 翻倍再来，格子涨到 360 度就是整个世界一格，必然收敛。
    while len(data) > FOOTPRINT_MAX_CELLS and cell < 360.0:
        cell *= 2
        data = db.aggregate_footprint(user, cell, box)
    return {"count": len(data), "data": data, "points": total,
            "aggregated": True, "zoom": zoom, "cell_deg": cell}


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
    variants = db.get_point_variant_files(point_id)  # 必须在 delete 之前捞（行会级联消失）
    db.delete_point(point_id)
    media_purge.purge_file_pairs(UPLOAD_DIR, media_mod.THUMB_DIR, files, variants)
    return {"ok": True}


# ---------- 媒体 ----------
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def media_type_for(filename: str) -> str:
    """按扩展名判定 photo / video。不认识的类型直接 400。

    单请求上传和分片上传都走这里 —— 两条路必须对「什么能传」有完全一致的判断，
    否则分片就成了绕过类型限制的后门。
    """
    ext = Path(filename or "").suffix.lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    raise HTTPException(400, f"不支持的文件类型: {ext}")


@app.get("/api/points/{point_id}/media")
def api_list_media(request: Request, point_id: int):
    require_owner(current_user(request), db.get_point_owner_id(point_id), "地点")
    # attach_urls 要在最后：url 由服务端算（要查 OSS 同步水位），前端别自己拼
    return media_mod.attach_urls(transcode.attach_qualities(db.list_media(point_id)))


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

    ext = Path(file.filename or "unknown").suffix.lower()
    mtype = media_type_for(file.filename or "unknown")

    limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
    if len(raw) > limit:
        raise HTTPException(
            413, f"{'照片' if mtype == 'photo' else '视频'}最大 {limit // (1024 * 1024)}MB，"
                 f"当前 {len(raw) / (1024 * 1024):.1f}MB")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    src_path = UPLOAD_DIR / stored_name
    src_path.write_bytes(raw)
    return register_uploaded_file(point_id, src_path, mtype)


def register_uploaded_file(point_id: int, src_path: Path, mtype: str) -> dict:
    """文件已经落到 uploads/ 之后的收尾：缩略图 → EXIF → 写库 → 丢后台转码。

    单请求上传和分片合并共用这一段。**分片合并也必须走这里**，否则合出来的
    视频不会进转码队列（没有 faststart、没有低码率档位），播放体验直接倒退回
    「等整片下完才起播」。
    """
    thumb_name = media_mod.make_thumb(src_path, mtype)
    exif_time = exif_lat = exif_lng = None
    if mtype == "photo":
        exif_time, exif_lat, exif_lng = media_mod.read_exif(src_path)

    mid = db.add_media(point_id, mtype, src_path.name, thumb_name,
                       exif_time, exif_lat, exif_lng)
    transcode.on_media_uploaded(mid, src_path, mtype)   # 视频丢后台转码，不阻塞本请求
    return {
        "id": mid, "type": mtype, "filename": src_path.name,
        "thumb": thumb_name, "exif_time": exif_time,
        "exif_lat": exif_lat, "exif_lng": exif_lng,
    }


# ---------- 分片上传 / 断点续传 ----------
# 为什么要有这一组接口：手机上一段视频动辄 100MB+，走单请求上传时网络一抖就得
# 从 0 重来，弱网下基本传不上去。这里把文件切成分片逐个传，服务端记住「收到哪些片」，
# 断了之后接着传没收到的那些。
#
# 边界（刻意保守，别在这里开口子）：
#   · 原来的单请求接口 POST /api/points/{id}/media **原样保留**，小文件继续走它。
#   · 体积上限两道关：建会话时按声明的 filesize 挡一次，合并前按真实字节数再挡一次。
#     每个分片的长度也必须精确等于「按 chunk_size 推出来的应有长度」——
#     这样「所有片都在」就等价于「总字节数正好等于 filesize」，谎报没有意义。
#   · 归属校验在**每一个**分片请求上都做，不是只在建会话时查一次。
CHUNK_SIZE_MIN = 256 * 1024
CHUNK_SIZE_MAX = 16 * 1024 * 1024
CHUNK_COUNT_MAX = 4096          # 挡住「1 字节一片」把 inode 刷爆
UPLOAD_SESSION_TTL_HOURS = 24   # 这么久没动静的会话连同临时分片一起回收
# 同一个会话的 complete 串行化：两个并发的 complete 会合并出两份文件、写两条 media。
_COMPLETE_LOCKS = {}
_COMPLETE_LOCKS_GUARD = threading.Lock()
# 过期清理的节流（进程内）：建会话时顺手清一次，但最多每小时一次。
_PRUNE_LAST = [0.0]


def _chunk_root() -> Path:
    """分片暂存根目录。写成函数而不是模块常量：测试会重绑 UPLOAD_DIR，
    常量会在 import 时就冻结成生产路径。"""
    return UPLOAD_DIR / ".chunks"


def _safe_session_id(session_id: str) -> str:
    """会话 id 是服务端生成的 32 位十六进制。这里再校验一次形状 ——
    它会被拼进文件路径，不能让 `../` 之类的东西混进来。"""
    sid = str(session_id or "")
    if len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
        raise HTTPException(404, "上传会话不存在")
    return sid


def _chunk_dir(session_id: str) -> Path:
    return _chunk_root() / _safe_session_id(session_id)


def _session_lock(session_id: str) -> threading.Lock:
    with _COMPLETE_LOCKS_GUARD:
        return _COMPLETE_LOCKS.setdefault(session_id, threading.Lock())


def _drop_session_files(session_id: str):
    shutil.rmtree(_chunk_dir(session_id), ignore_errors=True)
    with _COMPLETE_LOCKS_GUARD:
        _COMPLETE_LOCKS.pop(session_id, None)


def _expected_chunk_size(sess: dict, idx: int) -> int:
    """第 idx 片应有的字节数。最后一片是余数，其余都是满片。"""
    total = int(sess["total_chunks"])
    if idx < total - 1:
        return int(sess["chunk_size"])
    return int(sess["filesize"]) - int(sess["chunk_size"]) * (total - 1)


def _session_payload(sess: dict, resumed: bool = False) -> dict:
    """一个上传会话对外的样子。直传和隧道两条路共用它。

    直传会话的分片在 OSS 上，本机的 upload_chunks 表里一条都没有 —— 「传到哪儿了」
    得去问 OSS。这里**按需**问（只对还在传的直传会话），问失败就当 0，
    不能让一次 OSS 抖动把整个点详情的加载带崩。
    """
    oss_mode = bool(sess.get("oss_upload_id"))
    received = []
    received_bytes = 0
    if sess["status"] == "uploading":
        if oss_mode:
            try:
                parts = oss.list_parts(sess["oss_key"], sess["oss_upload_id"])
                # 对外统一用 0 基的分片序号（隧道那条就是 0 基），OSS 是 1 基
                received = [p["number"] - 1 for p in parts]
                received_bytes = sum(p["size"] for p in parts)
            except Exception:
                pass
        else:
            received = db.list_upload_chunks(sess["session_id"])
            received_bytes = db.upload_chunks_total_size(sess["session_id"])
    return {
        "mode": "oss" if oss_mode else "chunk",
        "session_id": sess["session_id"],
        "point_id": sess["point_id"],
        "filename": sess["filename"],
        "fingerprint": sess["fingerprint"],
        "filesize": sess["filesize"],
        "chunk_size": sess["chunk_size"],
        "total_chunks": sess["total_chunks"],
        "type": sess["mtype"],
        "status": sess["status"],
        "media_id": sess["media_id"],
        "received": received,
        "received_bytes": received_bytes,
        "updated_at": sess["updated_at"],
        "resumed": resumed,
    }


def _load_session(request: Request, session_id: str) -> dict:
    """取会话并校验归属。**每个分片请求都调它** —— 只在建会话时查一次归属，
    等于把 session_id 变成一张「谁捡到谁能往里写」的通行证。"""
    sess = db.get_upload_session(_safe_session_id(session_id))
    if not sess:
        raise HTTPException(404, "上传会话不存在")
    require_owner(current_user(request), sess["owner_id"], "上传会话")
    return sess


def prune_upload_sessions(hours: int = UPLOAD_SESSION_TTL_HOURS) -> dict:
    """回收过期会话：删库里的行 + 删磁盘上的临时分片。

    再顺手扫一遍 .chunks/ 下的孤儿目录（库里已经没有对应会话了，
    比如手工删库、或者进程在建目录和写库之间被杀）。
    """
    removed = 0
    aborted = 0
    for s in db.list_stale_upload_sessions(hours):
        # 直传会话过期时必须去 OSS 那边把没拼起来的碎片也撤掉 ——
        # 未完成的 multipart 分片会一直占空间计费，谁也不会主动去清。
        if s.get("oss_upload_id") and s.get("oss_key"):
            try:
                oss.abort_multipart(s["oss_key"], s["oss_upload_id"])
                aborted += 1
            except Exception:
                pass
        _drop_session_files(s["session_id"])
        db.delete_upload_session(s["session_id"])
        removed += 1
    orphans = 0
    root = _chunk_root()
    if root.is_dir():
        cutoff = time.time() - hours * 3600
        for d in root.iterdir():
            try:
                if not d.is_dir() or d.stat().st_mtime > cutoff:
                    continue
                if db.get_upload_session(d.name):
                    continue
                shutil.rmtree(d, ignore_errors=True)
                orphans += 1
            except OSError:
                continue
    return {"sessions": removed, "orphan_dirs": orphans, "oss_aborted": aborted}


def _prune_upload_sessions_throttled():
    """建会话时顺手清一次垃圾，每小时最多一次。

    这样即使运维没装 systemd timer，临时分片也不会无限堆积；
    装了 timer 的话这里几乎每次都直接跳过，不花钱。
    """
    now = time.time()
    if now - _PRUNE_LAST[0] < 3600:
        return
    _PRUNE_LAST[0] = now
    try:
        prune_upload_sessions()
    except Exception:            # 清理失败绝不能连累正在发生的上传
        pass


@app.post("/api/points/{point_id}/upload-sessions")
def api_create_upload_session(request: Request, point_id: int,
                              fingerprint: str = Form(...),
                              filename: str = Form(...),
                              filesize: int = Form(...),
                              chunk_size: int = Form(4 * 1024 * 1024)):
    """建（或复用）一个分片上传会话。

    同一个 (点, 用户, 文件指纹) 只会有一个活跃会话：重复调用直接把老会话
    连同「已经收到哪些片」一起还回去 —— 这就是续传的入口。
    """
    user = current_user(request)
    require_owner(user, db.get_point_owner_id(point_id), "地点")
    if not db.get_point(point_id):
        raise HTTPException(404, "点不存在")

    fp = (fingerprint or "").strip()[:128]
    if not fp:
        raise HTTPException(400, "缺少文件指纹")
    name = Path(filename or "").name[:200]
    mtype = media_type_for(name)
    limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
    if filesize <= 0:
        raise HTTPException(400, "空文件")
    if filesize > limit:
        raise HTTPException(
            413, f"{'照片' if mtype == 'photo' else '视频'}最大 {limit // (1024 * 1024)}MB，"
                 f"当前 {filesize / (1024 * 1024):.1f}MB")
    chunk_size = max(CHUNK_SIZE_MIN, min(CHUNK_SIZE_MAX, int(chunk_size or 0)))
    total_chunks = (filesize + chunk_size - 1) // chunk_size
    if total_chunks > CHUNK_COUNT_MAX:
        raise HTTPException(400, "分片数过多，请调大分片大小")

    _prune_upload_sessions_throttled()

    old = db.find_upload_session(point_id, user["id"], fp)
    if old:
        if (int(old["filesize"]) == filesize
                and int(old["chunk_size"]) == chunk_size
                and int(old["total_chunks"]) == total_chunks):
            _chunk_dir(old["session_id"]).mkdir(parents=True, exist_ok=True)
            return _session_payload(old, resumed=True)
        # 声明变了（换了分片大小，或指纹撞车但文件其实不同）→ 老的作废重来，
        # 否则会拿着新的分片长度去拼老的偏移，合出来的文件是坏的。
        _drop_session_files(old["session_id"])
        db.abort_upload_session(old["session_id"])

    sid = uuid.uuid4().hex
    db.create_upload_session(sid, point_id, user["id"], fp, name,
                             filesize, chunk_size, total_chunks, mtype)
    _chunk_dir(sid).mkdir(parents=True, exist_ok=True)
    return _session_payload(db.get_upload_session(sid), resumed=False)


@app.get("/api/points/{point_id}/upload-sessions")
def api_list_point_upload_sessions(request: Request, point_id: int):
    """这个点上「传了一半还没传完」的会话。前端据此显示未完成占位格。"""
    user = current_user(request)
    require_owner(user, db.get_point_owner_id(point_id), "地点")
    return [_session_payload(s)
            for s in db.list_point_upload_sessions(point_id, user["id"])]


@app.get("/api/upload-sessions/{session_id}")
def api_get_upload_session(request: Request, session_id: str):
    return _session_payload(_load_session(request, session_id))


@app.put("/api/upload-sessions/{session_id}/chunks/{idx}")
def api_put_upload_chunk(request: Request, session_id: str, idx: int,
                         file: UploadFile = File(...)):
    """上传单个分片。同一片重传是幂等的（覆盖写 + 主键覆盖）。

    同步 def 的理由和单请求上传一样：这里有整片写盘，不能占着事件循环。
    """
    sess = _load_session(request, session_id)
    if sess["status"] != "uploading":
        raise HTTPException(409, "上传会话已结束")
    if idx < 0 or idx >= int(sess["total_chunks"]):
        raise HTTPException(400, "分片序号越界")

    want = _expected_chunk_size(sess, idx)
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > want + (1 << 20):
        raise HTTPException(413, "分片过大")
    data = file.file.read()
    if len(data) != want:
        raise HTTPException(400, f"分片大小不符：应为 {want} 字节，收到 {len(data)}")

    d = _chunk_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子改名：合并那一步绝不能读到写了一半的分片。
    tmp = d / f"{idx}.part"
    tmp.write_bytes(data)
    os.replace(tmp, d / str(idx))
    db.add_upload_chunk(session_id, idx, len(data))
    return {"ok": True, "idx": idx, "size": len(data)}


@app.post("/api/upload-sessions/{session_id}/complete")
def api_complete_upload_session(request: Request, session_id: str):
    """所有分片都到齐之后合并成一个文件，然后走和单请求上传完全相同的收尾。"""
    sess = _load_session(request, session_id)
    with _session_lock(sess["session_id"]):
        sess = db.get_upload_session(sess["session_id"])
        if not sess:
            raise HTTPException(404, "上传会话不存在")
        # 幂等：重复 complete（比如客户端超时重试）直接把上次的结果还回去，
        # 不会合出第二份文件、写第二条 media。
        if sess["status"] == "completed" and sess["media_id"]:
            m = db.get_media(sess["media_id"])
            if m:
                return {"id": m["id"], "type": m["type"], "filename": m["filename"],
                        "thumb": m["thumb_filename"], "exif_time": m["exif_time"],
                        "exif_lat": m["exif_lat"], "exif_lng": m["exif_lng"],
                        "duplicate": True}
        if sess["status"] != "uploading":
            raise HTTPException(409, "上传会话已结束")

        total = int(sess["total_chunks"])
        got = set(db.list_upload_chunks(sess["session_id"]))
        missing = [i for i in range(total) if i not in got]
        if missing:
            raise HTTPException(409, {"error": "分片不全", "missing": missing[:64]})
        size_sum = db.upload_chunks_total_size(sess["session_id"])
        if size_sum != int(sess["filesize"]):
            raise HTTPException(400, "分片总大小与声明不符")

        mtype = sess["mtype"] or media_type_for(sess["filename"])
        limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
        if size_sum > limit:
            _drop_session_files(sess["session_id"])
            db.abort_upload_session(sess["session_id"])
            raise HTTPException(413, f"文件过大，上限 {limit // (1024 * 1024)}MB")

        ext = Path(sess["filename"]).suffix.lower()
        stored_name = f"{uuid.uuid4().hex}{ext}"
        src_path = UPLOAD_DIR / stored_name
        d = _chunk_dir(sess["session_id"])
        written = 0
        tmp_path = src_path.with_suffix(src_path.suffix + ".merging")
        try:
            with open(tmp_path, "wb") as out:
                for i in range(total):
                    part = d / str(i)
                    if not part.exists():
                        raise HTTPException(409, f"分片 {i} 丢失，请重传该分片")
                    with open(part, "rb") as fh:
                        while True:
                            buf = fh.read(1 << 20)
                            if not buf:
                                break
                            out.write(buf)
                            written += len(buf)
            if written != int(sess["filesize"]):
                raise HTTPException(500, "合并结果大小不符，请重试")
            os.replace(tmp_path, src_path)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except OSError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(500, f"合并失败：{e}")

        payload = register_uploaded_file(sess["point_id"], src_path, mtype)
        db.finish_upload_session(sess["session_id"], payload["id"])
    _note_upload_speed(sess, "tunnel")     # 和直传的数字放一起比，才看得出隧道有多贵
    _drop_session_files(sess["session_id"])
    return payload


@app.delete("/api/upload-sessions/{session_id}")
def api_abort_upload_session(request: Request, session_id: str):
    """用户点「放弃」：会话作废，临时分片立刻删掉。"""
    sess = _load_session(request, session_id)
    db.abort_upload_session(sess["session_id"])
    _drop_session_files(sess["session_id"])
    return {"ok": True}


# ---------- OSS 直传（浏览器 → OSS，绕开 frp 隧道） ----------
# 为什么要这条路：媒体从浏览器传到本机，全程挤在 4Mbps 的 frp 隧道里
# （见 ops/sync-oss.sh 顶部的实测）。一段 150MB 的视频理论最快也要 5 分钟，
# 期间整站 API 跟着排队。直传之后这段流量走用户自己的上行，一个字节都不进隧道。
#
# 服务端在这条路上**只做三件事**，一个字节的媒体都不经手：
#   1) 建 OSS 分片上传，签一批只对「这一个对象的这一片」有效的临时 URL；
#   2) 客户端传完之后，**以 OSS 的 ListParts 为准**核对，再让 OSS 把片拼起来；
#   3) 写 media 行，并把「把对象拉回本机存档」排进后台队列（oss_backfill）。
#
# 安全边界：对象名（key）永远由服务端生成，客户端报的只有文件名和大小；
# 签出去的 URL 只能 PUT 那一个 key 的那一片，且 1 小时过期。
# 归属校验和分片上传一样，**每个请求都做**，不是只在建会话时查一次。

OSS_URL_BATCH = 64          # 一次最多签这么多片的 URL，剩下的客户端再来要

# 最近几次上传的实测速率。为什么要有它：「上传有点慢」这句话里，慢在用户自己的
# 上行、慢在 4Mbps 的 frp 隧道、还是慢在服务端，光看代码永远分不出来 ——
# 2026-09-11 排查时是靠翻 OSS 的 ListParts 时间戳才算出那次是 61KB/s 的。
# 现在每次上传完成直接记一条，/health 里能看到，下次不用再做考古。
log = logging.getLogger("travel.upload")

UPLOAD_STATS_KEEP = 20
_upload_stats = collections.deque(maxlen=UPLOAD_STATS_KEEP)


def _note_upload_speed(sess: dict, via: str):
    """按「会话创建 → 现在」算一次上传的端到端速率，记进环形缓冲和日志。

    起点取会话创建时刻而不是第一片开始：用户中途暂停/切后台的时间也算进去 ——
    那正是我们想看见的东西（体感慢就是慢，不管慢在哪个环节）。
    """
    try:
        t0 = datetime.datetime.strptime(str(sess["created_at"]), "%Y-%m-%d %H:%M:%S")
        secs = max(0.001, (datetime.datetime.now() - t0).total_seconds())
        mb = int(sess["filesize"] or 0) / (1024 * 1024)
        rec = {"at": datetime.datetime.now().strftime("%m-%d %H:%M:%S"),
               "via": via, "file": str(sess["filename"] or "")[-40:],
               "mb": round(mb, 1), "secs": round(secs, 1),
               "kbps": round(mb * 1024 / secs, 1)}
        _upload_stats.append(rec)
        log.info("上传完成 via=%s %s %.1fMB 用时 %.0fs = %.0f KB/s",
                 via, rec["file"], mb, secs, rec["kbps"])
    except Exception as e:                     # 埋点绝不能影响上传本身
        log.debug("上传埋点失败: %s", e)


def _oss_session(request: Request, session_id: str) -> dict:
    """取直传会话并校验归属。非直传会话（没有 oss_upload_id）一律拒。"""
    sess = _load_session(request, session_id)
    if not sess.get("oss_upload_id") or not sess.get("oss_key"):
        raise HTTPException(400, "这不是一个 OSS 直传会话")
    return sess


def _oss_session_payload(sess: dict, parts: list = None, urls: dict = None,
                         resumed: bool = False) -> dict:
    return {
        "mode": "oss",
        "session_id": sess["session_id"],
        "filename": sess["filename"],
        "filesize": int(sess["filesize"]),
        "part_size": int(sess["chunk_size"]),
        "total_parts": int(sess["total_chunks"]),
        "mtype": sess["mtype"],
        "received": parts or [],
        "urls": urls or {},
        "resumed": resumed,
    }


def _sign_parts(sess: dict, numbers) -> dict:
    total = int(sess["total_chunks"])
    out = {}
    for n in numbers:
        n = int(n)
        if 1 <= n <= total:
            out[str(n)] = oss.sign_part_url(sess["oss_key"], sess["oss_upload_id"], n)
    return out


@app.post("/api/points/{point_id}/oss-uploads")
def api_create_oss_upload(request: Request, point_id: int,
                          fingerprint: str = Form(...),
                          filename: str = Form(...),
                          filesize: int = Form(...)):
    """建（或复用）一个直传会话，顺带签第一批分片 URL。

    复用的判断和隧道分片上传一致：同一个 (点, 属主, 指纹) 且声明的大小没变，
    就接着上次那个 uploadId 传 —— 断点续传靠的就是这个。
    """
    if not oss.direct_upload_enabled():
        raise HTTPException(503, "OSS 直传未启用")
    user = current_user(request)
    require_owner(user, db.get_point_owner_id(point_id), "地点")
    if not db.get_point(point_id):
        raise HTTPException(404, "点不存在")

    mtype = media_type_for(filename)
    filesize = int(filesize or 0)
    if filesize <= 0:
        raise HTTPException(400, "空文件")
    limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
    if filesize > limit:
        raise HTTPException(413, f"文件过大，上限 {limit // (1024 * 1024)}MB")

    fp = (fingerprint or "").strip()[:128]
    if not fp:
        raise HTTPException(400, "缺少文件指纹")

    _prune_upload_sessions_throttled()

    old = db.find_upload_session(point_id, user["id"], fp)
    if old and old.get("oss_upload_id"):
        if int(old["filesize"]) == filesize:
            try:
                parts = oss.list_parts(old["oss_key"], old["oss_upload_id"])
            except oss.OSSError:
                # uploadId 在云上没了（过期/被清理）：作废重来，别让客户端拿着
                # 一个已经不存在的 uploadId 一直传 404
                db.abort_upload_session(old["session_id"])
                parts = None
            if parts is not None:
                got = {p["number"] for p in parts}
                todo = [n for n in range(1, int(old["total_chunks"]) + 1) if n not in got]
                return _oss_session_payload(old, parts,
                                            _sign_parts(old, todo[:OSS_URL_BATCH]),
                                            resumed=True)
        else:
            # 声明变了（指纹撞车但文件其实不同）→ 老的作废，否则会把两个文件的片拼一起
            try:
                oss.abort_multipart(old["oss_key"], old["oss_upload_id"])
            except Exception:
                pass
            db.abort_upload_session(old["session_id"])

    part_size, total_parts = oss.plan_parts(filesize)
    ext = Path(filename or "unknown").suffix.lower()
    key = f"uploads/{uuid.uuid4().hex}{ext}"
    try:
        upload_id = oss.init_multipart(key, filename)
    except oss.OSSError as e:
        raise HTTPException(502, f"OSS 初始化失败：{e}")

    sid = uuid.uuid4().hex
    db.create_oss_upload_session(sid, point_id, user["id"], fp, filename,
                                 filesize, part_size, total_parts, mtype,
                                 key, upload_id)
    sess = db.get_upload_session(sid)
    return _oss_session_payload(sess, [],
                                _sign_parts(sess, range(1, min(total_parts, OSS_URL_BATCH) + 1)))


@app.post("/api/oss-uploads/{session_id}/sign")
def api_sign_oss_parts(request: Request, session_id: str, parts: str = Form(...)):
    """再要一批分片 URL。URL 有 1 小时时效，传到一半过期了也从这里续。"""
    sess = _oss_session(request, session_id)
    if sess["status"] != "uploading":
        raise HTTPException(409, "上传会话已结束")
    try:
        nums = [int(x) for x in (parts or "").split(",") if x.strip()][:OSS_URL_BATCH]
    except ValueError:
        raise HTTPException(400, "parts 只能是逗号分隔的分片序号")
    return {"urls": _sign_parts(sess, nums)}


@app.get("/api/oss-uploads/{session_id}")
def api_get_oss_upload(request: Request, session_id: str):
    """查这个会话在 OSS 那边已经收到哪些片（断点续传用）。"""
    sess = _oss_session(request, session_id)
    try:
        parts = oss.list_parts(sess["oss_key"], sess["oss_upload_id"])
    except oss.OSSError as e:
        raise HTTPException(502, f"OSS 查询失败：{e}")
    got = {p["number"] for p in parts}
    todo = [n for n in range(1, int(sess["total_chunks"]) + 1) if n not in got]
    return _oss_session_payload(sess, parts, _sign_parts(sess, todo[:OSS_URL_BATCH]))


@app.post("/api/oss-uploads/{session_id}/complete")
def api_complete_oss_upload(request: Request, session_id: str):
    """让 OSS 把分片拼成对象，然后写 media 行、把回源存档排进后台队列。

    **不接受客户端报的分片清单** —— 一律现查 ListParts。客户端说了不算，
    它可能少报、错报，甚至报一个根本没传上去的 ETag。
    """
    sess = _oss_session(request, session_id)
    with _session_lock(sess["session_id"]):
        sess = db.get_upload_session(sess["session_id"])
        if not sess:
            raise HTTPException(404, "上传会话不存在")
        # 幂等：重复 complete（客户端超时重试）把上次的结果原样还回去
        if sess["status"] == "completed" and sess["media_id"]:
            m = db.get_media(sess["media_id"])
            if m:
                return {"id": m["id"], "type": m["type"], "filename": m["filename"],
                        "thumb": m["thumb_filename"], "exif_time": m["exif_time"],
                        "exif_lat": m["exif_lat"], "exif_lng": m["exif_lng"],
                        "duplicate": True}
        if sess["status"] != "uploading":
            raise HTTPException(409, "上传会话已结束")

        total = int(sess["total_chunks"])
        try:
            parts = oss.list_parts(sess["oss_key"], sess["oss_upload_id"])
        except oss.OSSError as e:
            raise HTTPException(502, f"OSS 查询分片失败：{e}")
        got = {p["number"]: p for p in parts}
        missing = [n for n in range(1, total + 1) if n not in got]
        if missing:
            raise HTTPException(409, {"error": "分片不全", "missing": missing[:64]})
        size_sum = sum(p["size"] for p in parts)
        if size_sum != int(sess["filesize"]):
            raise HTTPException(400, "分片总大小与声明不符")

        mtype = sess["mtype"] or media_type_for(sess["filename"])
        limit = MAX_UPLOAD_PHOTO if mtype == "photo" else MAX_UPLOAD_VIDEO
        if size_sum > limit:
            oss.abort_multipart(sess["oss_key"], sess["oss_upload_id"])
            db.abort_upload_session(sess["session_id"])
            raise HTTPException(413, f"文件过大，上限 {limit // (1024 * 1024)}MB")

        try:
            oss.complete_multipart(sess["oss_key"], sess["oss_upload_id"],
                                   [{"number": n, "etag": got[n]["etag"]}
                                    for n in range(1, total + 1)])
            head = oss.head_object(sess["oss_key"])
        except oss.OSSError as e:
            raise HTTPException(502, f"OSS 合并失败：{e}")
        if head["size"] != int(sess["filesize"]):
            raise HTTPException(500, f"合并结果大小不符：{head['size']}")

        stored_name = sess["oss_key"].split("/")[-1]
        # 缩略图和 EXIF 都要读原件，而原件此刻只在 OSS 上 —— 留空，等回源补
        # （前端会先把自己在本地抽的那一帧传上来，见 /api/media/{id}/thumb）。
        mid = db.add_media(sess["point_id"], mtype, stored_name, None, None, None, None)
        db.finish_upload_session(sess["session_id"], mid)
    _note_upload_speed(sess, "oss-direct")
    oss_backfill.enqueue(mid, sess["oss_key"], int(sess["filesize"]), mtype)
    return {
        "id": mid, "type": mtype, "filename": stored_name,
        "thumb": None, "exif_time": None, "exif_lat": None, "exif_lng": None,
        "url": media_url("/uploads/" + stored_name),
        # 前端据此知道「原件还没回本机，缩略图/清晰度档位会晚一点到」
        "pending_backfill": True,
    }


@app.delete("/api/oss-uploads/{session_id}")
def api_abort_oss_upload(request: Request, session_id: str):
    """用户点「放弃」：会话作废，并让 OSS 把已传的碎片删掉（不然会一直计费）。"""
    sess = _oss_session(request, session_id)
    oss.abort_multipart(sess["oss_key"], sess["oss_upload_id"])
    db.abort_upload_session(sess["session_id"])
    return {"ok": True}


@app.post("/api/media/{media_id}/thumb")
def api_put_client_thumb(request: Request, media_id: int, file: UploadFile = File(...)):
    """浏览器在本地抽好的那张缩略图。

    直传的原件要等回源才回到本机，缩略图也就要等那么久 —— 相册里会先空一大片。
    而浏览器上传前本来就已经解码过这个文件（占位格里那张预览就是），
    把那一帧顺手传上来即可：几十 KB，走隧道毫无压力。

    **只在还没有缩略图时接受**：回源生成的那张才是权威的，不能被客户端顶掉。
    """
    require_owner(current_user(request), db.get_media_owner_id(media_id), "媒体")
    m = db.get_media(media_id)
    if not m:
        raise HTTPException(404, "媒体不存在")
    if m["thumb_filename"]:
        return {"ok": True, "thumb": m["thumb_filename"], "skipped": True}

    raw = file.file.read(CLIENT_THUMB_MAX + 1)
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > CLIENT_THUMB_MAX:
        raise HTTPException(413, "缩略图过大")
    # 必须真的是一张能解码的图：别人传什么进来都往 uploads/thumbs/ 里落，
    # 那目录是公开可读的静态目录。用 PIL 解一遍再重存，顺手把 EXIF 之类的附加
    # 数据剥掉，落盘的永远是我们自己编码出来的 JPEG。
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "不是有效的图片")
    img.thumbnail(media_mod.THUMB_SIZE)
    name = Path(m["filename"]).stem + ".jpg"
    dest = UPLOAD_DIR / "thumbs" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(dest, "JPEG", quality=82)
    db.set_media_thumb(media_id, name)
    # 立刻推 OSS（几十 KB，一次 PUT 百毫秒级）。不推的话这张图在下一趟 sync-oss
    # 之前只能从本机取 —— 而**下面这行 media_url 正是在此刻算的**，算出来会是
    # 本机路径，前端拿着它直接就把图片请求打进了隧道。
    try:
        oss.publish_local(dest)
    except Exception as e:                     # 推不上去只是慢一点，缩略图本身是成功的
        log.warning("缩略图推 OSS 失败（等 sync-oss 兜底）%s: %s", name, e)
    return {"ok": True, "thumb": name, "thumb_url": media_url("/uploads/thumbs/" + name)}


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
        # 转码档位文件也要一起清掉，否则删了视频还留着一堆 720p/480p 占盘
        variants = db.delete_variants(media_id)
        db.delete_media(media_id)
        media_purge.purge_media_row(UPLOAD_DIR, media_mod.THUMB_DIR, m, variants)
    return {"ok": True}


# ---------- 视频清晰度档位 ----------
@app.get("/api/media/{media_id}/variants")
def api_media_variants(media_id: int):
    """某个视频的可用清晰度档位。

    **刻意不鉴权**：/uploads/ 本身就是公开静态目录（app.py:138 的 is_public_path，
    分享页匿名访问必需），档位文件名并不比原始文件名更敏感。这样分享页
    （share.js）不用另开一套带 token 的接口就能轮询转码进度。
    """
    m = db.get_media(media_id)
    if not m:
        raise HTTPException(404, "媒体不存在")
    # 直传的媒体在回源之前，本机既没有原件也没有缩略图。前端上传完之后就靠
    # 这两个字段等它们出现 —— 否则要等下一次打开点详情才看得到封面。
    # 和档位信息一样不鉴权：缩略图本来就落在公开的 /uploads/thumbs/ 下。
    bf = db.get_oss_backfill(media_id)
    common = {
        "backfilling": bool(bf and bf["status"] in ("pending", "running")),
        "thumb_url": (media_url("/uploads/thumbs/" + m["thumb_filename"])
                      if m["thumb_filename"] else None),
    }
    if m["type"] != "video":
        return {"media_id": media_id, "type": m["type"], "qualities": [],
                "transcoding": False, **common}
    qualities = transcode.build_quality_list(m, db.list_variants(media_id))
    pending = [q for q in qualities if q["status"] in ("pending", "running")]
    return {
        "media_id": media_id, "type": "video",
        "qualities": qualities,
        # 前端据此决定要不要继续轮询
        "transcoding": bool(pending),
        **common,
    }


@app.post("/api/media/{media_id}/variants/retry")
def api_retry_variants(request: Request, media_id: int):
    """重试失败的档位（需本人或管理员）。转码失败不影响播放，这只是补救入口。"""
    require_owner(current_user(request), db.get_media_owner_id(media_id), "媒体")
    m = db.get_media(media_id)
    if not m:
        raise HTTPException(404, "媒体不存在")
    if m["type"] != "video":
        raise HTTPException(400, "只有视频才有清晰度档位")
    queued = transcode.retry_failed(media_id, UPLOAD_DIR / m["filename"])
    return {"ok": True, "queued": queued}


# ---------- 媒体下载（另存为） ----------
@app.get("/api/media/{media_id}/download")
def api_media_download(request: Request, media_id: int, quality: str = "original"):
    """把原图 / 原片 / 某个清晰度档位以「附件」形式下载（需本人或管理员）。

    为什么不直接给 /uploads/ 的链接：那条路的 Content-Type 是 image/video，
    浏览器一律内联打开；只有 Content-Disposition: attachment 才是下载。

    **鉴权和「能不能看」一致，不更宽**：这条走 require_owner，别人的私有媒体
    一律 404。分享页的匿名访客走另一条 /api/share/{token}/media/{id}/download，
    在那边校验 token + 密码 + 媒体归属 —— 两条路各自对应一种「能看」的方式。

    quality 只认 download.ALLOWED_QUALITIES 里的字面量（original/1080p/…），
    拼不出 ../；照片传非 original 直接 404。
    """
    require_owner(current_user(request), db.get_media_owner_id(media_id), "媒体")
    return download.build_response(media_id, quality, UPLOAD_DIR)


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
        r["url"] = media_url(f"/uploads/{r['filename']}")
        r["thumb_url"] = media_url(f"/uploads/thumbs/{r['thumb_filename']}") if r.get("thumb_filename") else None
    return {"results": results, "count": len(results)}


@app.get("/api/trips/{trip_id}/media")
def api_trip_media(request: Request, trip_id: int):
    """行程内全部媒体的扁平列表，按拍摄时间升序（相册网格用，前端自行筛选/排序）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    items = transcode.attach_qualities(db.list_trip_media(trip_id))
    return {
        "items": [{
            # 视频的可用清晰度档位（照片没有这个键）
            **({"qualities": m["qualities"]} if m.get("qualities") else {}),
            "id": m["id"],
            "type": m["type"],
            "point_id": m["point_id"],
            "point_name": m.get("point_name"),
            "filename": m["filename"],
            "thumb_filename": m.get("thumb_filename"),
            "url": media_url(f"/uploads/{m['filename']}"),
            "thumb_url": (media_url(f"/uploads/thumbs/{m['thumb_filename']}")
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
        "cover_url": media_url(f"/uploads/thumbs/{cover['thumb_filename']}") if cover.get("thumb_filename") else None,
        "media": [{
            "id": it["id"], "type": it["type"],
            "url": media_url(f"/uploads/{it['filename']}"),
            "thumb_url": media_url(f"/uploads/thumbs/{it['thumb_filename']}") if it.get("thumb_filename") else None,
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
# 领域 API 层（/api/ai/*）+ MCP —— 已拆分到 routers/agent_api.py
# 在原来的位置 include，保证注册顺序（进而路由匹配顺序）与拆分前完全一致。
# ============================================================
app.include_router(agent_api_router)


# ============================================================
# 开放平台 / 开发者控制台（/console）—— 已拆分到 routers/console.py
# 与领域 API（/api/ai/*）彻底分离：这是给「人」管理 Key 的面板，走 session 认证；
# 领域 API 是给「agent」调用的能力，走 API Key。二者认证体系和命名空间都不同。
# 同样在原位置 include，保持注册顺序不变。
# ============================================================
app.include_router(console_router)


# ---------- 兼容别名（路由拆分的过渡层） ----------
# 下面这些辅助函数已经随各自的路由搬进 routers/*，app.py 自身不再使用它们。
# 保留别名是为了让 `app.<名字>` 这个历史访问点（主要是单元测试）在整个拆分过程中
# 保持不变 —— 这样测试套件就是一个「未改动的对照组」：拆分期间任何测试失败都
# 意味着真的出了行为回归，而不是我顺手改测试改出来的噪音。
# 等拆分全部完成、测试改为直接引用 routers.* 之后，这一段可以删掉。
TRIP_STATUSES = agent_api.TRIP_STATUSES
_normalize_trip_status = agent_api._normalize_trip_status
_media_ext = agent_api._media_ext
VALID_SCOPES = console_mod.VALID_SCOPES
_normalize_scope = console_mod._normalize_scope
_deg2num = share_mod._deg2num




if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8084)
