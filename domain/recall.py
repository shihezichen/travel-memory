"""领域 API 层 — 记忆唤醒（N-2，手动触发版 demo）

「去年今天你在巴黎」—— 基于日期周年，把用户「去过」的记忆在合适的时刻
主动唤醒。这是「回忆后」窗口的核心情感价值验证（D 类），也是 P2-1
（周年提醒）的低成本前置形态。

不依赖 embedding，纯日期匹配 + DeepSeek 组织成有温度的唤醒语。
对外坐标：库内 GCJ-02 → 返回给 agent 时转 WGS-84（与 memory.py 一致）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import db
from .schemas import gcj02_to_wgs84


def _parse_date(s: str) -> date | None:
    """容错解析 ISO 日期（YYYY-MM-DD）。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def find_anniversaries(user_id: int, today: date | None = None,
                       lookahead_days: int = 7) -> list[dict]:
    """找出用户「去过」的记忆里，今天或未来 N 天内的「周年纪念」。

    规则：点的 start_date 的「月-日」落在 [今天, 今天+lookahead_days]，
    且该点年份早于今年（才算周年，排除今年新记录）。
    返回按「距今几天」升序（今天的排最前），含 years_ago（几年整周年）。
    """
    today = today or date.today()
    user = db.get_user_by_id(user_id)
    points = db.list_searchable_points(user or {"id": user_id, "is_admin": 0})
    if not points:
        return []

    anniversaries = []
    for p in points:
        d = _parse_date(p["start_date"])
        if not d:
            continue
        if d.year >= today.year:
            continue  # 今年及未来的不算周年
        try:
            anni = date(today.year, d.month, d.day)
        except ValueError:
            continue  # 2/29 等非法日期
        delta = (anni - today).days
        if 0 <= delta <= lookahead_days:
            years_ago = today.year - d.year
            wgs_lng, wgs_lat = gcj02_to_wgs84(p["lng"] or 0, p["lat"] or 0)
            anniversaries.append({
                "point_id": p["id"],
                "trip_id": p["trip_id"],
                "trip_name": p["trip_name"],
                "name": p["name"],
                "date": p["start_date"],
                "years_ago": years_ago,
                "anniversary_date": anni.isoformat(),
                "days_from_now": delta,
                "is_today": delta == 0,
                "lat": round(wgs_lat, 6),
                "lng": round(wgs_lng, 6),
            })

    anniversaries.sort(key=lambda x: (x["days_from_now"], -x["years_ago"]))
    return anniversaries


_RECALL_PROMPT = (
    "你是一位温暖、贴心的旅行记忆伴侣，负责在合适的时候唤醒用户对过去旅行的回忆。\n"
    "我会给你：今天的日期，以及从用户记忆库里找到的「周年纪念」——也就是几年整周年的今天（或未来几天内），用户曾经去过这些地方。\n"
    "请生成一段有温度的唤醒语，帮用户回忆起这些经历。\n"
    "要求：\n"
    "1. 用第一人称「你」的口吻（把用户称作「你」），像老朋友轻轻提醒。\n"
    "2. 自然带出地名和时间（如「去年今天」「两年前的今天」「3 天后就是…的一周年」）。\n"
    "3. 只基于提供的纪念信息作答，不要编造没有的地点或细节。\n"
    "4. 100-180 字，简洁有温度，不要罗列编号，像一句自然的问候。\n"
    "5. 只输出唤醒语正文，不要加前缀或解释。"
)


def compose_recall(user_id: int, today: date | None = None,
                   lookahead_days: int = 7) -> dict:
    """N-2 主入口：找周年纪念 + 用 LLM 组织成唤醒语。

    返回 {"today", "anniversaries":[...], "message", "tokens_used", "empty"}。
    无周年时 empty=True，message 为空（不调用 LLM）。
    """
    today = today or date.today()
    anniversaries = find_anniversaries(user_id, today, lookahead_days)
    if not anniversaries:
        return {
            "today": today.isoformat(),
            "anniversaries": [],
            "message": "",
            "tokens_used": 0,
            "empty": True,
        }

    # 压成 LLM 可读的纪念清单
    def _fmt(a):
        when = ("今天" if a["is_today"]
                else f"{a['days_from_now']} 天后")
        if a["years_ago"] == 1:
            span = "去年"
        else:
            span = f"{a['years_ago']} 年前"
        return (f"{span}的{when}（{a['anniversary_date'][5:]}），"
                f"你在「{a['trip_name']}」去过 {a['name']}")

    ctx_lines = "\n".join(f"- {_fmt(a)}" for a in anniversaries)

    from .generation import _llm
    user_msg = (f"今天是 {today.isoformat()}。\n\n"
                f"用户的周年纪念记忆：\n{ctx_lines}\n\n请生成唤醒语。")
    raw, tokens = _llm(_RECALL_PROMPT, user_msg, max_tokens=400)

    return {
        "today": today.isoformat(),
        "anniversaries": anniversaries,
        "message": raw.strip(),
        "tokens_used": tokens,
        "empty": False,
    }
