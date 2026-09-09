"""领域 API 层 — 地理编码（高德 geocode/geo）

让 agent（搭子）只给地名也能拿到准确坐标，不再靠 LLM 瞎估。
高德地理编码返回 GCJ-02（火星坐标），与库内存储坐标一致。

对外约定：
- geocode() 返回 GCJ-02（供后端 add_point 直接入库）
- geocode_wgs84() 返回 WGS-84（供对外端点/搭子查看，保持领域 API 对外 WGS-84 约定）

缓存：以 address 归一化为 key，避免同一地名反复请求高德（限流友好）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from .schemas import gcj02_to_wgs84

# 高德 key 从 app.py 环境拿不到（避免循环 import），从 amap_key.txt 直接读
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_amap_key():
    try:
        p = os.path.join(_BASE, "amap_key.txt")
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return os.environ.get("AMAP_KEY", "")


def _load_amap_security():
    try:
        p = os.path.join(_BASE, "amap_security.txt")
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return os.environ.get("AMAP_SECURITY", "")


AMAP_KEY = _load_amap_key()
AMAP_SECURITY = _load_amap_security()

# JS API 2.0 白名单 appname（与 district 端点一致，复刻 JS 请求参数）。
# 必须和你在高德控制台给这个 Key 配的白名单一致，所以做成可配置，
# 别把某一台机器的地址写死在源码里。
_APPNAME = os.environ.get("AMAP_APPNAME", "http://localhost:8084/")

_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

# 进程内缓存：{address_norm: {"lng":.., "lat":.., "formatted":.., "ts":..}}
_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL = 3600 * 24 * 30  # 30 天（地名坐标基本不变）


def _norm(address: str) -> str:
    return (address or "").strip().lower()


def geocode(address: str) -> dict | None:
    """正向地理编码 → 返回 GCJ-02 坐标 dict：{lng, lat, formatted}。失败返回 None。"""
    addr = (address or "").strip()
    if not addr:
        return None
    key = _norm(addr)

    with _LOCK:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit["ts"]) < _TTL:
            return {"lng": hit["lng"], "lat": hit["lat"], "formatted": hit["formatted"]}

    if not AMAP_KEY:
        return None

    url = (_GEOCODE_URL
           + "?platform=JS&s=rsv3&logversion=2.0"
           + "&key=" + urllib.parse.quote(AMAP_KEY)
           + "&sdkversion=2.3.5.6"
           + "&appname=" + urllib.parse.quote(_APPNAME, safe="")
           + "&jscode=" + urllib.parse.quote(AMAP_SECURITY)
           + "&address=" + urllib.parse.quote(addr) + "&output=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if data.get("status") != "1" or not data.get("geocodes"):
        return None

    g = data["geocodes"][0]
    loc = g.get("location", "")
    if "," not in loc:
        return None
    try:
        lng, lat = loc.split(",")
        lng, lat = float(lng), float(lat)
    except ValueError:
        return None

    result = {"lng": lng, "lat": lat,
              "formatted": g.get("formatted_address", addr)}
    with _LOCK:
        _CACHE[key] = {"lng": lng, "lat": lat,
                       "formatted": result["formatted"], "ts": time.time()}
    return result


def geocode_wgs84(address: str) -> dict | None:
    """正向地理编码 → 返回 WGS-84 坐标（对外约定）。失败返回 None。"""
    r = geocode(address)
    if not r:
        return None
    wgs_lng, wgs_lat = gcj02_to_wgs84(r["lng"], r["lat"])
    return {"lng": round(wgs_lng, 6), "lat": round(wgs_lat, 6),
            "formatted": r["formatted"], "gcj02": {"lng": r["lng"], "lat": r["lat"]}}
