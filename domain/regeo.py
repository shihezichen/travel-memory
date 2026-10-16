"""领域 API 层 — 逆地理编码（高德 regeo）：把一组 GPS 坐标变成人能读的地名。

为什么需要它：轨迹点的 `name` 是必填的，而照片 EXIF 里只有坐标没有地名。
让用户手打「西湖断桥」既慢又容易写成地理编码查不到的样子；调一次高德就能
直接得到「浙江省杭州市西湖区北山街道」这种规范地址。

与本模块的正向 geocode() 保持同一套约定：
- regeo() 返回 GCJ-02（库内存储坐标），可直接喂给 db.create_point
- EXIF 里的 GPS 是 WGS-84 原生坐标，**入库前必须先经 wgs84_to_gcj02 转一道**，
  否则所有点会整体偏移 300~600 米（这个坑在 db.py 的 coord_sys 字段注释里
  也写明了：库内统一 GCJ-02）

限流友好：按坐标格点（默认 6 位小数，约 0.1 米）归一化做key，同一个景点连拍
几百张也只打高德一次。批量接口 regeo_many() 内部串行 + 每批之间留间隔，
并且**任何一步失败都返回 None 而不是抛异常** —— 轨迹生成是增强功能，
逆地理挂了不该阻断建轨迹，名字退回时间戳即可。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .schemas import wgs84_to_gcj02

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

# JS API 2.0 白名单 appname，与 geocode.py 保持一致（同一个高德 Key 的白名单配置）
_APPNAME = os.environ.get("AMAP_APPNAME", "http://localhost:8084/")

_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

# 缓存：坐标格点 -> 地名信息。格点取 5 位小数（约 1.1 米），比 6 位略粗，
# 目的是让「同一个景点不同角度拍的几张」命中同一条缓存。
_CACHE: dict = {}
_LOCK = threading.Lock()
_TTL = 3600 * 24 * 30


def _norm(lng: float, lat: float) -> str:
    """坐标归一化成格点 key。5 位小数 ≈ 1.1 米，聚合同一拍摄位置足够。"""
    return f"{round(float(lng), 5)}:{round(float(lat), 5)}"


def _pick_name(regeo: dict) -> str:
    """从高德 regeo 响应里挑一个最适合当轨迹点名的东西。

    高德会给一组候选：formatted_address / addressComponent.township /
    business.district。按「越具体越好但不含门牌号」的顺序取，避免出现
    「浙江省杭州市西湖区北山街道断桥」这种把门牌也塞进轨迹点的长串。
    """
    addr = (regeo.get("formatted_address") or "").strip()
    comp = (regeo.get("addressComponent") or {})

    # 优先 township（街道/镇），它比 formatted_address 短且足够表意
    town = (comp.get("township") or "").strip()
    if isinstance(town, list):
        town = town[0] if town else ""
    town = (town or "").strip()

    dist = ((regeo.get("business") or {}).get("district") or "").strip()
    if isinstance(dist, list):
        dist = dist[0] if dist else ""
    dist = (dist or "").strip()

    # 门牌号是给快递看的，不是给人记地点的，砍掉尾部「NNN号」
    if addr:
        import re
        cleaned = re.sub(r"\d+号(楼)?.*$", "", addr).strip()
        if cleaned:
            addr = cleaned

    for cand in (town, dist, addr):
        if cand:
            return cand
    return ""


def regeo(lng: float, lat: float) -> dict | None:
    """WGS-84 坐标 → 地名。返回 {"name", "gcj02": {"lng","lat"}, "formatted"}。

    失败返回 None（不抛异常）—— 调用方据此退回时间戳命名。
    """
    try:
        wlng, wlat = float(lng), float(lat)
    except (TypeError, ValueError):
        return None
    if not (-180 <= wlng <= 180 and -90 <= wlat <= 90):
        return None

    key = _norm(wlng, wlat)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit["ts"]) < _TTL:
            return dict(hit["data"])

    if not AMAP_KEY:
        return None

    # 入库坐标统一 GCJ-02，所以**先转再查**，让高德返回的 formatted_address
    # 与我们最终存的坐标是同一个基准下的结果。
    glng, glat = wgs84_to_gcj02(wlng, wlat)

    url = (_REGEO_URL
           + "?platform=JS&s=rsv3&logversion=2.0"
           + "&key=" + urllib.parse.quote(AMAP_KEY)
           + "&sdkversion=2.3.5.6"
           + "&appname=" + urllib.parse.quote(_APPNAME, safe="")
           + "&jscode=" + urllib.parse.quote(AMAP_SECURITY)
           + "&location=" + urllib.parse.quote(f"{glng},{glat}")
           + "&extensions=base&output=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if data.get("status") != "1" or not data.get("regeocode"):
        return None

    rg = data["regeocode"]
    name = _pick_name(rg)
    if not name:
        return None

    result = {"name": name,
              "formatted": (rg.get("formatted_address") or "").strip(),
              "gcj02": {"lng": round(glng, 6), "lat": round(glat, 6)}}
    with _LOCK:
        _CACHE[key] = {"data": result, "ts": time.time()}
    return result


def regeo_many(coords, min_interval: float = 0.12, progress=None):
    """批量逆地理。coords 为 [(wgs_lng, wgs_lat), ...]。

    返回与入参等长的list，元素为 dict 或 None（失败占位，保证下标对得上）。
    串行 + 间隔，避免把高德打到限流。
    """
    out = []
    last = 0.0
    for i, c in enumerate(coords):
        r = regeo(c[0], c[1]) if c else None
        out.append(r)
        if i + 1 < len(coords):
            gap = min_interval - (time.time() - last)
            if gap > 0:
                time.sleep(gap)
        last = time.time()
        if progress:
            progress(i + 1, len(coords))
    return out