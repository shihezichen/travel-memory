"""N-11 年度旅行报告（Annual Travel Wrapped）：把一年的足迹折叠成一张卡片。

规则层（零 token）聚合该年全部行程/点/媒体/情绪元数据，产出结构化统计；
LLM 层（DeepSeek）把统计翻译成一段有温度的年度叙事（可降级规则模板）。

设计要点：
- 机制先行：任何年份都能出报告；数据不足时输出「轻量版」，不硬编。
- 12 月后跑即得真实首份年报；历史年份回溯同样可用。
- N-4 禁模板化：LLM prompt 硬性要求引用具体地点名/数字，禁止套话。
"""
from __future__ import annotations

from datetime import datetime

import db
from .generation import _llm
from .labels import extract_city


def _parse_dt(s: str) -> datetime | None:
    """解析 '2025-12-23' / '2026-08-26 14:40:00' / None。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _year_of(s: str) -> int | None:
    dt = _parse_dt(s)
    return dt.year if dt else None


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    """两点球面距离（km），Haversine。"""
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


_SEASONS = [
    (3, 5, "春"), (6, 8, "夏"), (9, 11, "秋"),
]


def _season_of(date_str: str) -> str:
    dt = _parse_dt(date_str)
    if not dt:
        return "未知"
    for m0, m1, name in _SEASONS:
        if m0 <= dt.month <= m1:
            return name
    return "冬"


_WRAPPED_SYSTEM = (
    "你是旅行记忆助手。用户给你一年的结构化旅行统计（行程数/城市/里程/照片/月份分布/"
    "情绪标签），请写一段 200 字以内的年度旅行叙事，风格像一个老朋友翻完相册后的感慨。"
    "硬性要求：① 必须引用至少 2 个具体地名；② 必须化用至少 1 条统计数字（里程/照片数/城市数）；"
    "③ 如果该年有情绪标签，化用其中一条；④ 禁止「时光飞逝/精彩的一年」这类空泛套话；"
    "⑤ 禁止编造统计里没有的信息。"
)


def annual_report(user: dict, year: int | None = None, db_module=None) -> dict:
    """生成某年（默认当前年）的年度旅行报告。纯规则层统计 + LLM 叙事。

    年份归属规则：行程 start_date 或 end_date 落在该年（或 created_at 落在
    该年）即计入；点级日期兜底（行程无日期时用点日期）。
    """
    dbm = db_module or db
    now = datetime.now()
    year = year or now.year

    trips = dbm.list_trips(user)
    year_trips = []
    for t in trips:
        sy = _year_of(t.get("start_date"))
        ey = _year_of(t.get("end_date"))
        cy = _year_of(t.get("created_at"))
        if year in (sy, ey, cy):
            year_trips.append(t)

    # ---- 规则层统计 ----
    cities: set[str] = set()
    month_counter: dict[int, int] = {}
    season_counter: dict[str, int] = {}
    mood_counter: dict[str, int] = {}
    n_points = n_photos = n_videos = 0
    total_km = 0.0
    prev_pt = None
    completed = 0
    point_dates: list[str] = []

    for t in year_trips:
        if (t.get("status") or "") == "completed":
            completed += 1
        pts = dbm.list_points(t["id"])
        for p in pts:
            n_points += 1
            city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
            if city:
                cities.add(city)
            d = p.get("start_date") or t.get("start_date")
            if d:
                point_dates.append(d)
                dt = _parse_dt(d)
                if dt:
                    month_counter[dt.month] = month_counter.get(dt.month, 0) + 1
                season_counter[_season_of(d)] = season_counter.get(_season_of(d), 0) + 1
            if prev_pt and p.get("lat") is not None and prev_pt.get("lat") is not None:
                total_km += _haversine_km(prev_pt["lat"], prev_pt["lng"], p["lat"], p["lng"])
            prev_pt = p
        # 媒体
        for m in dbm.list_trip_media(t["id"]):
            if m.get("type") == "photo":
                n_photos += 1
            elif m.get("type") == "video":
                n_videos += 1
        # 情绪元数据
        for meta in dbm.list_point_meta_by_trip(t["id"]):
            mood = (meta.get("mood") or "").strip()
            if mood:
                mood_counter[mood] = mood_counter.get(mood, 0) + 1

    top_cities = sorted(cities)[:5]
    top_months = sorted(month_counter.items(), key=lambda kv: -kv[1])[:3]
    top_moods = sorted(mood_counter.items(), key=lambda kv: -kv[1])[:3]

    stats = {
        "year": year,
        "trip_count": len(year_trips),
        "completed_count": completed,
        "point_count": n_points,
        "city_count": len(cities),
        "top_cities": top_cities,
        "total_distance_km": round(total_km, 1),
        "photo_count": n_photos,
        "video_count": n_videos,
        "month_distribution": {str(k): v for k, v in sorted(month_counter.items())},
        "top_months": [{"month": m, "points": c} for m, c in top_months],
        "season_distribution": season_counter,
        "mood_tags": [{"mood": m, "count": c} for m, c in top_moods],
    }

    empty = n_points == 0 and len(year_trips) == 0
    result = {"year": year, "has_data": not empty, "stats": stats, "narrative": None}

    # ---- LLM 档：有数据才生成叙事（降级：规则模板）----
    if not empty:
        try:
            narrative, _ = _llm(_WRAPPED_SYSTEM, _summarize(stats), max_tokens=400)
            result["narrative"] = narrative.strip()
        except Exception:
            result["narrative"] = _fallback_narrative(stats)
    else:
        result["narrative"] = f"{year} 年还没有留下旅行足迹——新的一年，第一站想去哪里？"
    return result


def _summarize(stats: dict) -> str:
    parts = [
        f"年份：{stats['year']}",
        f"行程数：{stats['trip_count']}（完成 {stats['completed_count']}）",
        f"地点数：{stats['point_count']}，城市数：{stats['city_count']}",
        f"城市：{'、'.join(stats['top_cities']) or '无'}",
        f"总里程：{stats['total_distance_km']} 公里",
        f"照片 {stats['photo_count']} 张 / 视频 {stats['video_count']} 个",
        f"月度分布：{stats['month_distribution']}",
        f"季节分布：{stats['season_distribution']}",
        f"情绪标签：{stats['mood_tags']}",
    ]
    return "\n".join(parts)


def _fallback_narrative(stats: dict) -> str:
    """LLM 不可用时的规则模板（引用真实统计，非空话）。"""
    parts = []
    if stats["trip_count"]:
        parts.append(f"{stats['year']} 年走了 {stats['trip_count']} 趟行程")
    if stats["top_cities"]:
        parts.append("最常出现在 " + "、".join(stats["top_cities"][:3]))
    if stats["total_distance_km"] >= 1:
        parts.append(f"轨迹连起来约 {stats['total_distance_km']:.0f} 公里")
    if stats["photo_count"]:
        parts.append(f"按下快门 {stats['photo_count']} 次")
    if stats["mood_tags"]:
        parts.append(f"记忆里最多的情绪是「{stats['mood_tags'][0]['mood']}」")
    return "，".join(parts) + "。" if parts else f"{stats['year']} 年还没留下足迹。"
