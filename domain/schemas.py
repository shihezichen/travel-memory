"""领域 API 层 — Pydantic schema（输入校验 + 输出模型）

坐标系约定：领域 API 对外一律 WGS-84（GPS 原生，agent 友好），
进库时统一转 GCJ-02（高德前端坐标系）存。
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ---------- 坐标系转换 ----------
# GCJ-02（火星坐标）↔ WGS-84 偏移算法（标准实现，无第三方依赖）

_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * (abs(x) ** 0.5))
    ret += (20.0 * _sin(6.0 * x) + 20.0 * _sin(2.0 * x)) * 2.0 / 3.0
    ret += (20.0 * _sin(y) + 40.0 * _sin(y / 3.0)) * 2.0 / 3.0
    ret += (160.0 * _sin(y / 12.0) + 320 * _sin(y / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * (abs(x) ** 0.5))
    ret += (20.0 * _sin(6.0 * x) + 20.0 * _sin(2.0 * x)) * 2.0 / 3.0
    ret += (20.0 * _sin(x) + 40.0 * _sin(x / 3.0)) * 2.0 / 3.0
    ret += (150.0 * _sin(x / 12.0) + 300.0 * _sin(x / 30.0)) * 2.0 / 3.0
    return ret


def _sin(x):
    import math
    return math.sin(math.radians(x))


def wgs84_to_gcj02(lng, lat):
    """WGS-84 → GCJ-02（入库时调用）"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * 3.141592653589793
    magic = _sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = magic ** 0.5
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * 3.141592653589793)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * _cos(radlat) * 3.141592653589793)
    return lng + dlng, lat + dlat


def _cos(x):
    import math
    return math.cos(x)


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 → WGS-84（出库给 agent 时调用，近似逆变换）"""
    if _out_of_china(lng, lat):
        return lng, lat
    wgs_lng, wgs_lat = lng, lat
    for _ in range(3):
        glng, glat = wgs84_to_gcj02(wgs_lng, wgs_lat)
        wgs_lng += lng - glng
        wgs_lat += lat - glat
    return wgs_lng, wgs_lat


# ---------- 通用错误模型 ----------
class ErrorBody(BaseModel):
    code: str
    msg: str


# ---------- 记忆层输出 ----------
class TimelineItem(BaseModel):
    name: str
    lat: float          # WGS-84
    lng: float          # WGS-84
    date: Optional[str] = None
    desc: Optional[str] = None
    media_n: int = 0
    route_mode: str = "drive"


class MemorySummary(BaseModel):
    name: str
    date_range: Optional[str] = None
    n_points: int
    n_media: int
    n_photos: int = 0
    n_videos: int = 0


class TripMemory(BaseModel):
    trip_id: int
    summary: MemorySummary
    timeline: list[TimelineItem] = []
    themes: list[str] = []
    media_highlights: list[dict] = []


# ---------- 生成层 ----------
class GenerateRequest(BaseModel):
    kind: str = Field(..., description="trip_summary|route_guide|postcard|diary")
    trip_id: int
    style: str = "旅行者视角"
    language: str = "zh"

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v):
        allowed = {"trip_summary", "route_guide", "postcard", "diary"}
        if v not in allowed:
            raise ValueError(f"kind 必须是 {allowed}")
        return v


class GenerateResponse(BaseModel):
    kind: str
    trip_id: int
    text: str
    tokens_used: int
    cached: bool = False


# ---------- 联想层 ----------
class Association(BaseModel):
    type: str
    desc: str
    trips: list[int] = []
    confidence: float = 0.0
    evidence: dict = {}
