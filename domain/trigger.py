"""领域 API 层 — 主动叙事触发器清单（N-10）

化学反应 7：定义 agent 主动生成叙事的触发条件，让 agent 从「被动响应」
走向「主动陪伴」。这是 P2-1（周年提醒定时化）、N-16（记忆再阐释）、
P2-2（行程推荐）共同的前置地基。

触发器类型（TriggerType）：
1. anniversary  周年型 —— 精确到「月-日」的整年周年（复用 recall.find_anniversaries）
2. seasonal     季节型 —— 当前季节与历史同季节行程的粗粒度匹配（「又到夏天了，去年夏天你在…」）
3. revisit      重访型 —— 跨数据巧合：同一地点在不同年份反复出现（「你三次来过这座城市」）
4. location     位置型 —— 用户当前位置接近曾去过的地方（预留，待位置数据接入）

每个触发器类型配一个生成模板（TRIGGER_TEMPLATES）。scan_triggers 聚合所有
类型产出「今日候选触发器」，供 agent 决定是否/如何主动叙事。

不依赖 embedding；对外坐标库内 GCJ-02 → 转 WGS-84（与 memory.py 一致）。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

import db
from .recall import find_anniversaries
from .schemas import gcj02_to_wgs84

# ===== 触发器类型 =====
TRIGGER_ANNIVERSARY = "anniversary"
TRIGGER_SEASONAL = "seasonal"
TRIGGER_REVISIT = "revisit"
TRIGGER_LOCATION = "location"

TRIGGER_TYPES = (TRIGGER_ANNIVERSARY, TRIGGER_SEASONAL,
                 TRIGGER_REVISIT, TRIGGER_LOCATION)


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _season(month: int) -> str:
    """北半球季节映射。"""
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


def _user_points(user_id: int) -> list[dict]:
    user = db.get_user_by_id(user_id)
    return db.list_searchable_points(user or {"id": user_id, "is_admin": 0})


# ===== 1. 周年型：复用 recall.find_anniversaries =====
def detect_anniversary(user_id: int, today: date | None = None,
                       lookahead_days: int = 7) -> list[dict]:
    anniversaries = find_anniversaries(user_id, today, lookahead_days)
    return [{"type": TRIGGER_ANNIVERSARY, **a} for a in anniversaries]


# ===== 2. 季节型：当前季节 vs 历史同季节行程 =====
def detect_seasonal(user_id: int, today: date | None = None) -> list[dict]:
    today = today or date.today()
    cur_season = _season(today.month)
    points = _user_points(user_id)

    # 按行程聚合，取该行程最早 start_date 作为季节锚点
    trips: dict[int, dict] = {}
    for p in points:
        d = _parse_date(p["start_date"])
        if not d or d.year >= today.year:
            continue  # 今年及未来不算「历史季节回忆」
        tid = p["trip_id"]
        if tid not in trips or d < trips[tid]["date"]:
            trips[tid] = {
                "trip_id": tid,
                "trip_name": p["trip_name"],
                "date": d,
            }

    out = []
    for t in trips.values():
        if _season(t["date"].month) != cur_season:
            continue
        years_ago = today.year - t["date"].year
        out.append({
            "type": TRIGGER_SEASONAL,
            "trip_id": t["trip_id"],
            "trip_name": t["trip_name"],
            "season": cur_season,
            "season_date": t["date"].isoformat(),
            "years_ago": years_ago,
        })
    out.sort(key=lambda x: x["years_ago"])  # 最近的年份优先
    return out


# ===== 3. 重访型：同一地点在不同年份反复出现 =====
def detect_revisit(user_id: int) -> list[dict]:
    """跨数据巧合：按地点名分组，找出在 >=2 个不同年份出现过的地点。

    这是 N-16（记忆再阐释/意义串联）的触发器来源——同一地点多次到访
    是「纵向串联赋新意义」的原材料。
    """
    points = _user_points(user_id)
    by_name: dict[str, dict] = {}
    for p in points:
        d = _parse_date(p["start_date"])
        if not d:
            continue
        name = (p["name"] or "").strip()
        if not name:
            continue
        key = name.lower()
        rec = by_name.setdefault(key, {
            "name": name,
            "years": set(),
            "visits": [],
        })
        rec["years"].add(d.year)
        wgs_lng, wgs_lat = gcj02_to_wgs84(p["lng"] or 0, p["lat"] or 0)
        rec["visits"].append({
            "date": p["start_date"],
            "trip_name": p["trip_name"],
            "lat": round(wgs_lat, 6),
            "lng": round(wgs_lng, 6),
        })

    out = []
    for rec in by_name.values():
        if len(rec["years"]) < 2:
            continue
        rec["visits"].sort(key=lambda v: v["date"])
        out.append({
            "type": TRIGGER_REVISIT,
            "name": rec["name"],
            "visit_count": len(rec["visits"]),
            "distinct_years": len(rec["years"]),
            "years": sorted(rec["years"]),
            "visits": rec["visits"],
        })
    out.sort(key=lambda x: -x["visit_count"])  # 重访次数多的优先
    return out


# ===== 4. 位置型：预留（待位置数据接入） =====
def detect_location(user_id: int, lat: float | None = None,
                    lng: float | None = None) -> list[dict]:
    """用户当前位置接近曾去过的地方时触发。

    当前无位置数据源（手机 GPS / 手动打卡尚未接入），返回空。
    未来接入后：给定用户当前 (lat, lng)，在历史点里找距离 < 阈值的点。
    """
    if lat is None or lng is None:
        return []  # 无位置输入，不触发
    points = _user_points(user_id)
    out = []
    for p in points:
        if p["lat"] is None or p["lng"] is None:
            continue
        dlat = abs(p["lat"] - lat)
        dlng = abs(p["lng"] - lng)
        # 粗略 1 度 ≈ 111km；阈值 0.05 度 ≈ 5.5km
        if dlat < 0.05 and dlng < 0.05:
            out.append({
                "type": TRIGGER_LOCATION,
                "name": p["name"],
                "trip_name": p["trip_name"],
                "distance_deg": round((dlat ** 2 + dlng ** 2) ** 0.5, 5),
            })
    return out


# ===== 生成模板：每个触发器类型对应的叙事生成 prompt =====
TRIGGER_TEMPLATES = {
    TRIGGER_ANNIVERSARY: (
        "你是一位温暖、贴心的旅行记忆伴侣。用户过去在今天（或未来几天内）有过「整年周年」的旅行纪念。\n"
        "请生成一段有温度的唤醒语，帮用户回忆起这些经历。\n"
        "要求：第一人称「你」、自然带出地名与时间（「去年今天」「两年前的今天」）、"
        "只基于给定信息不编造、100-180 字、像老朋友轻轻提醒。"
    ),
    TRIGGER_SEASONAL: (
        "你是一位温暖、贴心的旅行记忆伴侣。现在正值某个季节，用户过去几年里也在这个季节旅行过。\n"
        "请用「季节」作为切入点，生成一段有温度的唤醒语，勾起用户对这个季节旅行的回忆。\n"
        "要求：第一人称「你」、自然带出季节与地名（「又到了夏天，去年这个时候你在…」）、"
        "只基于给定信息不编造、100-180 字。"
    ),
    TRIGGER_REVISIT: (
        "你是一位温暖、贴心的旅行记忆伴侣。用户曾经多次到访过同一个地方（跨不同年份）。\n"
        "请从「重访」这个角度，生成一段有温度的唤醒语，点出这个地点对用户的意义。\n"
        "要求：第一人称「你」、自然带出「你第 N 次来到这里」的脉络与时间跨度、"
        "只基于给定信息不编造、100-180 字、可以略带怀旧与深情。"
    ),
    TRIGGER_LOCATION: (
        "你是一位温暖、贴心的旅行记忆伴侣。用户现在的位置，离他过去去过的一个地方很近。\n"
        "请生成一段有温度的唤醒语，提醒用户「你曾经来过这附近」。\n"
        "要求：第一人称「你」、自然带出地点、只基于给定信息不编造、80-150 字。"
    ),
}


# ===== 统一入口：聚合所有触发器 =====
def scan_triggers(user_id: int, today: date | None = None,
                  lookahead_days: int = 7) -> dict:
    """扫描所有触发器类型，产出「今日候选触发器清单」。

    返回 {"today", "triggers": {...类型: [候选...]}, "summary": {...计数}}。
    供 agent 决定是否/如何主动叙事；无任何触发时 triggers 各类型为空列表。
    """
    today = today or date.today()
    result = {
        "today": today.isoformat(),
        "triggers": {
            TRIGGER_ANNIVERSARY: detect_anniversary(user_id, today, lookahead_days),
            TRIGGER_SEASONAL: detect_seasonal(user_id, today),
            TRIGGER_REVISIT: detect_revisit(user_id),
            TRIGGER_LOCATION: detect_location(user_id),
        },
    }
    result["summary"] = {
        k: len(v) for k, v in result["triggers"].items()
    }
    result["total"] = sum(result["summary"].values())
    return result
