"""N-20 时间旅行（Time Travel）：三种模式在记忆的时间轴上穿行。

1. rewind（回溯）：回到过去某一天，看那天你在哪、在做什么——输出当日地点快照
   + 邻近行程的叙事重构（基于真实数据）。
2. preview（预演）：看向未来某天，列出已规划（planning）的行程与计划中的目的地。
3. counterfactual（反事实）：「如果当时换一条路」——基于真实行程与地点，
   推演另一种可能。**必须明确标注【虚构推演】**，与真实记忆严格区分。

规则层零 token 兜底：按日期过滤真实数据；反事实严格基于真实地点清单，
禁止编造新地点。LLM 层生成叙事，可降级规则模板。
"""
from __future__ import annotations

from datetime import datetime, timedelta

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


_REWIND_SYSTEM = (
    "你是旅行记忆助手。用户要回到过去某一天。给你当天的真实地点快照（地点名/时间/描述），"
    "写 180 字以内的回溯叙事，像翻到旧照片那一页。硬性要求：① 只引用快照里的地点名；"
    "② 具体经历细节（餐厅/建筑内部/活动）只允许基于快照描述合理展开，禁止编造快照完全没有的"
    "经历，可用'那天下午/傍晚'的所见所感代替；③ 禁止「时光倒流/感慨」类套话；"
    "④ 收尾落在'那天之后'的一个真实变化上。"
)

_PREVIEW_SYSTEM = (
    "你是旅行记忆助手。用户要看未来某天的旅行计划。给你已规划的行程清单，"
    "写 150 字以内的预告，语气像出发前的期待。硬性要求：① 只引用清单里的目的地；"
    "② 禁止编造清单外的安排；③ 禁止「说走就走/期待满满」类空话。"
)

_COUNTERFACTUAL_SYSTEM = (
    "你是旅行记忆助手。用户要玩「如果当时换一条路」的思想实验。"
    "给你真实行程的地点清单，推演'如果当时去了清单里的另一个地方'会怎样。"
    "硬性要求：① 只使用清单里的地点；② 开头必须写【虚构推演】四个字；"
    "③ 结尾回到'现实中的你'作对比；④ 禁止编造清单外地点。"
)


def _snapshot_on(user: dict, date_str: str, dbm) -> dict:
    """回溯：某天的真实地点快照。"""
    target = _parse_dt(date_str)
    if not target:
        raise ValueError("date 格式应为 YYYY-MM-DD")
    day = target.strftime("%Y-%m-%d")
    found = []
    trips = dbm.list_trips(user)
    for t in trips:
        pts = dbm.list_points(t["id"])
        for p in pts:
            d0 = _parse_dt(p.get("start_date"))
            d1 = _parse_dt(p.get("end_date"))
            hit = False
            if d0 and d1:
                hit = d0.date() <= target.date() <= d1.date()
            elif d0:
                hit = d0.strftime("%Y-%m-%d") == day
            if hit:
                found.append({
                    "trip": t.get("name") or "未命名",
                    "point": p.get("name") or "",
                    "desc": (p.get("description") or "").strip(),
                    "date": day,
                })
    return {"date": day, "places": found}


def _plans_on(user: dict, date_str: str, dbm) -> dict:
    """预演：未来某天已规划的行程。"""
    target = _parse_dt(date_str)
    if not target:
        raise ValueError("date 格式应为 YYYY-MM-DD")
    plans = []
    for t in dbm.list_trips(user):
        if (t.get("status") or "") != "planning":
            continue
        d0 = _parse_dt(t.get("start_date"))
        d1 = _parse_dt(t.get("end_date"))
        if not d0 or not d1:
            continue
        if d0.date() <= target.date() <= d1.date():
            pts = dbm.list_points(t["id"])
            cities = sorted({extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
                             for p in pts} - {None})
            plans.append({
                "trip": t.get("name") or "未命名",
                "period": f"{t.get('start_date')} ~ {t.get('end_date')}",
                "destinations": cities,
            })
    return {"date": date_str, "plans": plans}


def _counterfactual(user: dict, trip_id: int, dbm) -> dict:
    """反事实：基于真实行程地点，推演另一种可能。"""
    trip = dbm.get_trip(trip_id)
    if not trip:
        raise ValueError("行程不存在")
    if not user.get("is_admin") and trip.get("user_id") != user.get("id"):
        raise ValueError("无权访问该行程")
    pts = dbm.list_points(trip_id)
    places = []
    for p in pts[:10]:
        nm = p.get("name") or ""
        desc = (p.get("description") or "").strip()
        places.append(f"{nm}（{desc[:15]}）" if desc else nm)
    return {
        "trip_id": trip_id,
        "trip": trip.get("name") or "未命名",
        "period": f"{trip.get('start_date') or '?'} ~ {trip.get('end_date') or '?'}",
        "places": places,
    }


def time_travel(user: dict, mode: str, db_module=None, **kw) -> dict:
    """时间旅行三模式入口。mode ∈ rewind/preview/counterfactual。"""
    dbm = db_module or db
    if mode not in ("rewind", "preview", "counterfactual"):
        raise ValueError("mode 必须是 rewind/preview/counterfactual 之一")

    if mode == "rewind":
        date_str = kw.get("date")
        if not date_str:
            raise ValueError("rewind 需要 date 参数（YYYY-MM-DD）")
        snap = _snapshot_on(user, date_str, dbm)
        base = {"mode": "rewind", "date": snap["date"], "places": snap["places"]}
        if not snap["places"]:
            base["narrative"] = f"{snap['date']} 那天，你的记忆里没有留下足迹——也许是在赶路，也许只是普通的一天。"
            return base
        prompt = "\n".join(
            f"- {p['point']}（{p['trip']}，{p['date']}）：{p['desc']}" for p in snap["places"][:8]
        )
        try:
            text, _ = _llm(_REWIND_SYSTEM, prompt, max_tokens=380)
            base["narrative"] = text.strip()
        except Exception:
            names = "、".join(p["point"] for p in snap["places"][:4])
            base["narrative"] = f"{snap['date']}，你在 {names}。那天之后，这段路成了记忆的一部分。"
        return base

    if mode == "preview":
        date_str = kw.get("date")
        if not date_str:
            raise ValueError("preview 需要 date 参数（YYYY-MM-DD）")
        plans = _plans_on(user, date_str, dbm)["plans"]
        base = {"mode": "preview", "date": date_str, "plans": plans}
        if not plans:
            base["narrative"] = f"{date_str} 还没有排定行程——空白正好留给惊喜。"
            return base
        prompt = "\n".join(
            f"- {p['trip']}（{p['period']}）目的地：{'、'.join(p['destinations']) or '未定'}"
            for p in plans
        )
        try:
            text, _ = _llm(_PREVIEW_SYSTEM, prompt, max_tokens=320)
            base["narrative"] = text.strip()
        except Exception:
            names = "、".join(
                "、".join(p["destinations"]) or p["trip"] for p in plans[:3]
            )
            base["narrative"] = f"{date_str} 你在路上：计划中的 {names}。"
        return base

    # counterfactual
    trip_id = kw.get("trip_id")
    if not trip_id:
        raise ValueError("counterfactual 需要 trip_id 参数")
    cf = _counterfactual(user, trip_id, dbm)
    base = {"mode": "counterfactual", "fictional": True, **cf}
    if not cf["places"]:
        base["narrative"] = "【虚构推演】这段行程没有记录地点，无法推演另一种可能。"
        return base
    prompt = (
        f"行程：{cf['trip']}（{cf['period']}）\n"
        f"真实地点清单（只能使用这些）：{'、'.join(cf['places'])}\n"
        "请推演：如果当时在清单里换一个地方停留更久/换一条路线，故事会怎样？"
    )
    try:
        text, _ = _llm(_COUNTERFACTUAL_SYSTEM, prompt, max_tokens=380)
        base["narrative"] = text.strip()
    except Exception:
        names = "、".join(cf["places"][:3])
        base["narrative"] = (f"【虚构推演】如果当时在 {names} 多留两天，"
                             f"也许会有不一样的日落。但现实里，{cf['trip']} 已经写进了你的记忆。")
    return base
