"""N-14 共同记忆 v1（Shared Memory）：两个人的足迹叠在一起会讲出什么故事。

规则层（零 token）合并两个用户的足迹：
- common_cities 共同去过的城市
- time_overlaps 时间重叠（同一天/同一月出现在同一城市）
- exclusive_a / exclusive_b 各自独享的目的地（互补视角）

LLM 层（DeepSeek）把共同点翻译成一段「你们的故事」（可降级规则模板）。

隐私边界：只输出城市级 + 日期级信息，绝不输出对方行程详情/坐标/媒体。
v1 仅支持按用户名查询（对方必须已注册）；多租户隔离不破坏——
本模块只读「城市集合 + 日期」，不暴露任何单方私有字段。
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


def _day_key(date_str: str) -> str | None:
    dt = _parse_dt(date_str)
    return dt.strftime("%Y-%m-%d") if dt else None


def _month_key(date_str: str) -> str | None:
    dt = _parse_dt(date_str)
    return dt.strftime("%Y-%m") if dt else None


def _user_footprint(user: dict, dbm) -> dict:
    """用户足迹：城市集合 + (城市→日期集合, 城市→月份集合)。"""
    cities: set[str] = set()
    city_days: dict[str, set] = {}
    city_months: dict[str, set] = {}
    for p in dbm.list_all_points(user):
        city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
        if not city:
            continue
        cities.add(city)
        city_days.setdefault(city, set())
        city_months.setdefault(city, set())
        d = _day_key(p.get("start_date"))
        if d:
            city_days[city].add(d)
            city_months[city].add(_month_key(p.get("start_date")))
    return {"cities": cities, "days": city_days, "months": city_months}


_RELATION_SYSTEM = (
    "你是旅行记忆助手。给你两个用户（甲/乙）足迹对比的规则层结果：共同去过的城市、"
    "时间重叠、各自独享的目的地。请写一段 180 字以内的「共同记忆」叙事，语气像朋友翻着"
    "两本相册对照着看。硬性要求：① 必须引用至少 2 个具体地名；② 有共同城市时聚焦共同点，"
    "没有时坦诚说「还没一起走过」并给出可执行的同行建议；③ 禁止「缘分/命中注定」这类空泛套话；"
    "④ 禁止编造统计里没有的信息。"
)


def shared_memory(user: dict, with_username: str, db_module=None) -> dict:
    """两个用户的共同记忆 v1。with_username 必须存在且不能是自己。

    返回 {"with_user", "has_shared", "common_cities", "time_overlaps",
          "exclusive_a", "exclusive_b", "story"}。
    """
    dbm = db_module or db
    other = dbm.get_user_by_username(with_username)
    if not other:
        raise ValueError(f"用户 {with_username} 不存在")
    if other["id"] == user["id"]:
        raise ValueError("不能和自己对比共同记忆")

    me = _user_footprint(user, dbm)
    them = _user_footprint(other, dbm)

    # 共同城市（按名字排序，稳定输出）
    common = sorted(me["cities"] & them["cities"])

    # 时间重叠：同一天同一城市 > 同一月同一城市
    overlaps = []
    for city in common:
        same_days = sorted(me["days"].get(city, set()) & them["days"].get(city, set()))
        for d in same_days[:3]:
            overlaps.append({"city": city, "date": d, "level": "same_day"})
        if not same_days:
            same_months = sorted(me["months"].get(city, set()) & them["months"].get(city, set()))
            for m in same_months[:3]:
                overlaps.append({"city": city, "month": m, "level": "same_month"})

    exclusive_a = sorted(me["cities"] - them["cities"])
    exclusive_b = sorted(them["cities"] - me["cities"])

    result = {
        "with_user": {"id": other["id"], "username": other["username"]},
        "has_shared": bool(common or overlaps),
        "common_cities": common,
        "time_overlaps": overlaps,
        "exclusive_a": exclusive_a[:10],
        "exclusive_b": exclusive_b[:10],
        "story": None,
    }

    # LLM 档（降级规则模板）
    try:
        story, _ = _llm(_RELATION_SYSTEM, _summarize(result), max_tokens=400)
        result["story"] = story.strip()
    except Exception:
        result["story"] = _fallback_story(result)
    return result


def _summarize(r: dict) -> str:
    parts = [
        f"共同去过的城市：{'、'.join(r['common_cities']) or '无'}",
        f"时间重叠：{r['time_overlaps']}",
        f"甲独享：{'、'.join(r['exclusive_a'][:6]) or '无'}",
        f"乙独享：{'、'.join(r['exclusive_b'][:6]) or '无'}",
    ]
    return "\n".join(parts)


def _fallback_story(r: dict) -> str:
    if r["common_cities"]:
        head = "你们都在 " + "、".join(r["common_cities"][:3]) + " 留下过脚印"
    else:
        head = "你们还没一起去过同一个城市"
    if r["time_overlaps"]:
        o = r["time_overlaps"][0]
        if o["level"] == "same_day":
            tail = f"——{o['date']} 你们同在 {o['city']}，也许擦肩而过。"
        else:
            tail = f"——{o['month']} 你们都在 {o['city']}。"
    else:
        tail = "——下次把两本相册翻到同一页？"
    return head + tail
