"""只读分享：/share/{token}、/api/share/*、行程分享管理，以及分享缩略图 / 二维码。

从 app.py 原样搬过来的，路由定义一字未改，只把 `@app.` 换成了 `@router.`。
这批路由的共同点是**匿名可访问**（is_public_path 放行），所以它们既是最省事的
分享入口，也是这个服务对公网暴露面最大的一块 —— 缩略图那条会现场用 PIL 合成
PNG、拉高德瓦片、查 OSRM，成本远高于普通接口，因此带了 ETag + Cache-Control。
把它们收在一个文件里，是为了以后要加匿名限流 / 缓存策略时有个统一的挂载点。

注意：UPLOAD_DIR 和 _TILE_DIR 是本模块的模块级全局，测试靠 monkeypatch 重绑它们
做目录隔离（见 tests/conftest.py）——别改成从别处 import 进来的常量，否则测试会
写进生产 uploads/。
"""
import hashlib
import io
import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

import db
import transcode
from config import AMAP_KEY, AMAP_SECURITY, OSRM_URL
from routers.deps import (
    current_user, require_owner, amap_version, amap_key_notice_html, _route_cache_key,
)

BASE = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE / "uploads"

router = APIRouter()


# ---------- 只读分享 ----------
@router.get("/share/{token}")
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


@router.post("/api/trips/{trip_id}/share")
def api_create_share(request: Request, trip_id: int, expire_days: int = Form(None),
                     password: str = Form("")):
    """为行程生成只读分享链接（需本人或管理员）；expire_days=None 表示永久有效；
    password 可选，非空则访问需密码"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    token = db.create_share(trip_id, expire_days, password or None)
    return {"token": token, "has_password": bool(password)}


@router.delete("/api/trips/{trip_id}/share")
def api_delete_share(request: Request, trip_id: int):
    """撤销某行程的全部分享链接（需本人或管理员）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    db.delete_shares_for_trip(trip_id)
    return {"ok": True}


@router.get("/api/trips/{trip_id}/share-count")
def api_share_count(request: Request, trip_id: int):
    """当前行程有效分享链接数量（需本人或管理员）"""
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), "行程")
    return {"count": db.count_shares_for_trip(trip_id)}


@router.get("/api/share/{token}")
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
        # 带上视频清晰度档位，分享页也能选清晰度 / 自动降级
        p["media"] = transcode.attach_qualities(db.list_media(p["id"]))
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


@router.get("/api/share/{token}/qrcode")
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


@router.get("/api/share/{token}/thumbnail")
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
