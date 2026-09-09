"""N-6 跨旅程模式发现：行为聚类，产出「旅行画像」可分享卡片。

化学反应 2（懂你）：从用户所有行程中自动发现用户自己都未必意识到的模式——
重访地点、季节偏好、地点类型偏好、路线节奏，提炼成可分享的一句话画像。

纯规则聚类（零 token），不调 LLM；可扩展 LLM 档做深层模式叙事。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

# 地点类型关键词（规则档，粗略判断主题倾向；命中点数越多越可信）
THEME_KEYWORDS = {
    "海边": ["海", "滩", "滨", "岛", "港", "渔", "湾", "沙滩", "码头", "海风"],
    "山地": ["山", "峰", "岭", "谷", "峡", "高原", "雪山", "徒步", "登"],
    "古城古镇": ["古城", "古镇", "老城", "旧城", "胡同", "老街", "遗址", "城墙", "古"],
    "人文博物": ["博物馆", "美术馆", "教堂", "寺", "庙", "宫", "殿", "塔", "剧院", "图书馆", "城堡"],
    "湖泊江河": ["湖", "江", "河", "瀑布", "泉", "溪"],
    "自然公园": ["公园", "森林", "草原", "沙漠", "湿地", "峡谷", "溶洞", "洞穴"],
}


def _season_of(month: int) -> str:
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


def _month_of(date_str) -> int | None:
    """从 'YYYY-MM-DD' 或 'YYYY-MM' 提取月份。"""
    if not date_str:
        return None
    try:
        return int(str(date_str)[5:7])
    except (ValueError, IndexError):
        return None


def _year_of(date_str) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, IndexError):
        return None


def _theme_of(name: str, description: str = "") -> str | None:
    text = f"{name or ''} {description or ''}"
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return theme
    return None


def _days_between(start, end) -> int | None:
    """行程天数（含首尾）。"""
    if not start:
        return None
    s = str(start)[:10]
    e = str(end)[:10] if end else s
    try:
        d0 = datetime.strptime(s, "%Y-%m-%d")
        d1 = datetime.strptime(e, "%Y-%m-%d")
        return (d1 - d0).days + 1
    except ValueError:
        return None


def discover_patterns(user, db) -> dict:
    """发现用户跨旅程的模式，返回「旅行画像」结构。"""
    trips = db.list_trips(user)
    points = db.list_all_points(user)

    # 按 trip_id 分组点
    trip_points: dict[int, list] = {}
    for p in points:
        trip_points.setdefault(p["trip_id"], []).append(p)

    # 1) 重访地点：同一名称（归一化）出现在 ≥2 个不同行程
    name_to_trips: dict[str, set] = {}
    for p in points:
        nm = (p.get("name") or "").strip()
        if nm:
            name_to_trips.setdefault(nm, set()).add(p["trip_id"])
    revisits = [
        {"name": nm, "times": len(tids), "trip_ids": sorted(tids)}
        for nm, tids in name_to_trips.items() if len(tids) >= 2
    ]
    revisits.sort(key=lambda x: (-x["times"], x["name"]))

    # 2) 季节偏好：按行程 start_date 月份（一次行程=一次出行）
    season_counter = Counter()
    for t in trips:
        m = _month_of(t.get("start_date"))
        if m:
            season_counter[_season_of(m)] += 1
    season_rank = [{"season": s, "count": c} for s, c in season_counter.most_common()]
    dominant_season = season_rank[0]["season"] if season_rank else None

    # 3) 地点类型偏好：点名称/描述关键词命中
    theme_counter = Counter()
    theme_examples: dict[str, list] = {}
    for p in points:
        th = _theme_of(p.get("name"), p.get("description"))
        if th:
            theme_counter[th] += 1
            if len(theme_examples.setdefault(th, [])) < 3:
                theme_examples[th].append(p.get("name"))
    theme_rank = [
        {"theme": th, "count": c, "examples": theme_examples.get(th, [])}
        for th, c in theme_counter.most_common()
    ]

    # 4) 出行节奏：平均每行程天数 + 平均每行程点数
    days = [d for t in trips if (d := _days_between(t.get("start_date"), t.get("end_date"))) is not None]
    avg_days = round(sum(days) / len(days), 1) if days else None
    avg_points = round(len(points) / len(trips), 1) if trips else None

    # 5) 年份跨度
    years = {_year_of(t.get("start_date")) for t in trips}
    years = sorted(y for y in years if y)

    # 6) 可分享的一句话画像
    share_card = _compose_share_card(
        trip_count=len(trips), point_count=len(points),
        dominant_season=dominant_season, season_counter=season_counter,
        theme_rank=theme_rank, revisits=revisits,
        avg_days=avg_days, years=years)

    return {
        "trip_count": len(trips),
        "point_count": len(points),
        "year_span": [years[0], years[-1]] if years else None,
        "dominant_season": dominant_season,
        "season_distribution": season_rank,
        "theme_tendency": theme_rank,
        "revisit_places": revisits,
        "pace": {"avg_days_per_trip": avg_days, "avg_points_per_trip": avg_points},
        "share_card": share_card,
    }


def _compose_share_card(trip_count, point_count, dominant_season, season_counter,
                        theme_rank, revisits, avg_days, years) -> str:
    parts = []
    if years:
        parts.append(f"从 {years[0]} 到 {years[-1]}")
    parts.append(f"走过 {trip_count} 段旅程、{point_count} 个地方")
    if dominant_season:
        total = sum(season_counter.values()) or 1
        parts.append(f"最爱在{dominant_season}天出发（{season_counter[dominant_season]}/{total} 次）")
    if theme_rank:
        top = theme_rank[0]
        parts.append(f"偏爱{top['theme']}（{top['count']} 处）")
    if revisits:
        top_re = revisits[0]
        parts.append(f"有 {len(revisits)} 处念念不忘，{top_re['name']}去了 {top_re['times']} 次")
    if avg_days:
        parts.append(f"平均每程 {avg_days} 天")
    return "，".join(parts) + "。"
