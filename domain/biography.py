"""N-18 旅行人生传记（Biography）：把一生的足迹写成一本分章节的书。

规则层（零 token）把用户全部行程按时间排序，生成章节骨架（每段旅程一章，
行程过多时按年合并），并聚合全局统计（行程数/城市数/总里程/照片数/时间跨度）。

LLM 层逐章生成（每章 220 字以内叙事，引用真实地名/日期），降级规则模板。
输出 markdown 文本，预留 PDF 导出（后续接 weasyprint/reportlab）。

设计约束（N-4 禁模板化）：每章 prompt 注入该章真实地点清单，禁止编造；
章节标题由「行程名 + 时间跨度」构成，不硬编文艺标题。
"""
from __future__ import annotations

from datetime import datetime

import db
from .generation import _llm
from .labels import extract_city


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


_CHAPTER_SYSTEM = (
    "你是旅行传记作家。用户正在把自己一生的旅行写成书，你负责写其中一章。"
    "给你本章的行程事实（行程名/时间/真实地点清单/地点数/媒体数），写 220 字以内的章节正文，"
    "语气像纪实体散文，有画面感。硬性要求：① 只允许引用「真实地点清单」里的地名，"
    "清单外的地名一律用泛称代替（如'城外/山间/江畔/巷子里'）；② 必须基于事实，禁止编造细节；"
    "③ 禁止「岁月如梭/人生如旅」类套话；④ 结尾留一个自然的悬念或回望，不强行升华。"
)


def biography(user: dict, db_module=None) -> dict:
    """生成用户旅行传记（markdown）。

    返回 {"title", "period", "stats", "chapters": [{trip_id, title, period, text}],
          "markdown"}。
    """
    dbm = db_module or db
    trips = dbm.list_trips(user)
    trips = [t for t in trips if t.get("status") not in ("planning", "archived")]
    trips.sort(key=lambda t: (_parse_dt(t.get("start_date")) or datetime.max).isoformat())

    # ---- 全局统计 ----
    all_cities: set[str] = set()
    n_photos = n_videos = 0
    total_km = 0.0
    for t in trips:
        pts = dbm.list_points(t["id"])
        prev = None
        for p in pts:
            city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
            if city:
                all_cities.add(city)
            if prev and p.get("lat") is not None and prev.get("lat") is not None:
                total_km += _haversine_km(prev["lat"], prev["lng"], p["lat"], p["lng"])
            prev = p
        for m in dbm.list_trip_media(t["id"]):
            if m.get("type") == "photo":
                n_photos += 1
            elif m.get("type") == "video":
                n_videos += 1

    dates = [d for d in (_parse_dt(t.get("start_date")) for t in trips) if d]
    period = ""
    if dates:
        period = f"{min(dates).strftime('%Y-%m-%d')} ~ {max(dates).strftime('%Y-%m-%d')}"

    stats = {
        "trip_count": len(trips),
        "city_count": len(all_cities),
        "total_distance_km": round(total_km, 1),
        "photo_count": n_photos,
        "video_count": n_videos,
        "year_span": (max(dates).year - min(dates).year + 1) if dates else 0,
    }

    chapters = []
    for t in trips:
        pts = dbm.list_points(t["id"])
        place_list = []
        for p in pts[:10]:
            nm = p.get("name") or ""
            desc = (p.get("description") or "").strip()
            place_list.append(f"{nm}（{desc[:15]}）" if desc else nm)
        span = f"{t.get('start_date') or '?'} ~ {t.get('end_date') or '?'}"
        facts = (
            f"行程名：{t.get('name') or '未命名'}\n"
            f"时间：{span}\n"
            f"真实地点清单（只能引用这些，禁止清单外的地名）：{'、'.join(place_list) or '无'}\n"
            f"地点数：{len(pts)}"
        )
        try:
            if not place_list:
                # 无真实地点清单 → 禁止 LLM 自由发挥编造地名，直接降级规则模板
                text = _fallback_chapter(t, place_list, pts)
            else:
                text, _ = _llm(_CHAPTER_SYSTEM, facts, max_tokens=400)
                text = text.strip()
        except Exception:
            text = _fallback_chapter(t, place_list, pts)
        chapters.append({
            "trip_id": t["id"],
            "title": f"{t.get('name') or '未命名'}（{span}）",
            "period": span,
            "text": text,
        })

    title = f"{user.get('username') or '旅行者'} 的旅行传记"
    md = [f"# {title}", "", f"*{period} · {stats['trip_count']} 段旅程 · "
          f"{stats['city_count']} 座城市 · 约 {stats['total_distance_km']} 公里 · "
          f"{stats['photo_count']} 张照片*", ""]
    for ch in chapters:
        md += [f"## {ch['title']}", "", ch["text"], ""]
    md += ["---", "*由旅行记忆系统自动生成，可导出 PDF。*"]

    return {
        "title": title,
        "period": period,
        "stats": stats,
        "chapters": chapters,
        "markdown": "\n".join(md),
    }


def _fallback_chapter(trip: dict, place_list: list, pts: list) -> str:
    span = f"{trip.get('start_date') or '?'} ~ {trip.get('end_date') or '?'}"
    places = "、".join(place_list[:4]) or "未知地点"
    return (f"{span}，这一程走过 {places}，共 {len(pts)} 个地点。"
            f"路线在记忆里渐渐连成一条线，当时的风和快门声却还清晰。")
