"""N-21 未完成旅程识别：把「没说出口的遗憾」变成前进动力。

化学反应 18（动机维度）：agent 从记忆里自动发现那些「计划了没成行、
去了没走完、提过没实现」的痕迹——不指责，而是把遗憾翻译成下一步的
动力（「你 4 月计划过挪威极光，今年冬天还来得及」）。

两段式：
1. 规则层（零 token）：扫描行程状态机 + 点级城市标签，识别三类信号——
   - stalled_plans 搁置计划：planning 状态超过 90 天未推进
   - unclosed_trips 未收尾旅程：traveling 状态但 end_date 已过 30 天未归档
   - unfulfilled_destinations 未成行目的地：planning 行程里的城市，从未
     在任何 completed 行程出现过（计划过但没去成）
2. LLM 层（DeepSeek）：把信号喂给模型，生成一句有温度的「遗憾→动力」
   提示语；无信号返回 empty 不调 LLM（零成本空跑）。

依赖：N-8 的 status 字段语义 + N-13 点级城市标签（labels.extract_city）。
"""
from __future__ import annotations

from datetime import datetime

import db
from .generation import _llm
from .labels import extract_city

# 搁置计划阈值：planning 超过该天数视为「搁置」
STALL_DAYS = 90
# 未收尾阈值：traveling 行程 end_date 超过该天数仍未归档
UNCLOSED_DAYS = 30
# 细化度阈值：planning 行程点数 >= 该值视为「疑似已走完」（实际走过忘了归档）
DONE_POINT_THRESHOLD = 5

_NUDGE_SYSTEM = (
    "你是旅行记忆助手，语气温暖、克制、不说教。用户有一批「未完成」的旅行痕迹："
    "搁置的计划、没归档的旅程、计划了却没去成的地方。"
    "请把它们重新讲述成「前进动力」——具体引用行程/地点名，给一个可执行的下一步建议，"
    "不超过 120 字。不要鸡汤套话，不要编造用户没提过的信息。"
    "如果用户没有任何未完成痕迹，输出「你的旅行清单都很清爽」即可。"
)


def _parse_dt(s: str) -> datetime | None:
    """解析 '2025-12-23' 或 '2026-08-26 14:40:00' 或 None。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _days_since(dt: datetime | None, now: datetime) -> int | None:
    if dt is None:
        return None
    return (now - dt).days


def find_unfinished(user: dict, db_module=None) -> dict:
    """识别用户所有「未完成旅程」信号。纯规则层（零 token），LLM 档可选。

    信号语义：
    - stalled_plans 搁置计划：planning 且细化度低（点 < 5、end_date 未过），
      创建超过 STALL_DAYS 天仍未推进。
    - unclosed_trips 未收尾旅程：① traveling 但 end_date 已过 UNCLOSED_DAYS 天；
      ② planning 但细化度高（点 >= 5 或 end_date 已过）——实际走完忘了归档。
    - unfulfilled_destinations 未成行目的地：真计划（点 < 5）行程里的城市，
      从未在任何 completed / 已走完行程中出现。
    """
    dbm = db_module or db
    now = datetime.now()

    trips = dbm.list_trips(user)
    stalled_plans: list[dict] = []
    unclosed_trips: list[dict] = []

    completed_cities: set[str] = set()
    planning_cities: set[str] = set()
    plan_trip_names: dict[str, str] = {}

    # 1) 行程级状态机扫描（合并点数判断「走完没归档」）
    for t in trips:
        status = t.get("status") or "planning"
        created = _parse_dt(t.get("created_at"))
        ended = _parse_dt(t.get("end_date"))
        pts = dbm.list_points(t["id"])
        point_count = len(pts)
        # 疑似已实际走完：细化度高（>=5 点）或有地点且 end_date 已过
        looks_done = (
            status == "planning"
            and (
                point_count >= DONE_POINT_THRESHOLD
                or (point_count > 0 and ended is not None and ended < now)
            )
        )

        if status == "completed":
            for p in pts:
                city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
                if city:
                    completed_cities.add(city)
        elif status == "planning" and looks_done:
            # 走完没归档 → 未收尾旅程（城市按已去算，不误报「没去成」）
            for p in pts:
                city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
                if city:
                    completed_cities.add(city)
            unclosed_trips.append({
                "trip_id": t["id"], "name": t["name"],
                "end_date": t.get("end_date") or t.get("start_date") or "",
                "reason": f"行程已有 {point_count} 个地点但状态仍是「计划中」，疑似走完未归档",
            })
        elif status == "planning" and point_count == 0 and ended is not None and ended < now:
            # 计划过期未成行：0 个地点且结束日期已过
            unclosed_trips.append({
                "trip_id": t["id"], "name": t["name"],
                "end_date": t.get("end_date"),
                "reason": f"计划结束日期（{t.get('end_date')}）已过，但未添加任何地点，仍未成行或归档",
            })
        elif status == "planning":
            # 真计划：记录城市 + 判断是否搁置
            for p in pts:
                city = extract_city(f"{p.get('name') or ''} {p.get('description') or ''}")
                if city:
                    planning_cities.add(city)
                    plan_trip_names.setdefault(city, t["name"])
            days = _days_since(created, now)
            if days is not None and days >= STALL_DAYS:
                stalled_plans.append({
                    "trip_id": t["id"], "name": t["name"],
                    "planned_at": t.get("created_at"),
                    "days_stalled": days,
                    "description": t.get("description") or "",
                })
        elif status == "traveling":
            if ended is not None:
                days = (now - ended).days
                if days >= UNCLOSED_DAYS:
                    unclosed_trips.append({
                        "trip_id": t["id"], "name": t["name"],
                        "end_date": t.get("end_date"),
                        "days_unclosed": days,
                        "reason": f"行程 {days} 天前已到结束日期，仍未归档",
                    })

    # 3) 未成行目的地：真计划的城市 vs 已去城市
    unfulfilled = sorted(
        (
            {
                "city": c,
                "planned_in": plan_trip_names.get(c, ""),
            }
            for c in planning_cities if c not in completed_cities
        ),
        key=lambda d: d["city"],
    )

    signals = {
        "stalled_plans": stalled_plans,
        "unclosed_trips": unclosed_trips,
        "unfulfilled_destinations": unfulfilled,
    }
    has_unfinished = bool(stalled_plans or unclosed_trips or unfulfilled)

    result = {
        "has_unfinished": has_unfinished,
        **signals,
        "nudge": None,
    }

    # LLM 档：有信号才生成提示语（降级：规则模板）
    if has_unfinished:
        try:
            nudge, _ = _llm(_NUDGE_SYSTEM, _summarize(signals), max_tokens=300)
            result["nudge"] = nudge.strip()
        except Exception:
            result["nudge"] = _fallback_nudge(signals)
    return result


def _summarize(signals: dict) -> str:
    parts = []
    if signals["stalled_plans"]:
        parts.append("搁置的计划：" + "；".join(
            f"{s['name']}（{s['days_stalled']} 天前创建）" for s in signals["stalled_plans"]))
    if signals["unclosed_trips"]:
        parts.append("没归档的旅程：" + "；".join(
            f"{s['name']}（{s['end_date']} 结束）" for s in signals["unclosed_trips"]))
    if signals["unfulfilled_destinations"]:
        parts.append("计划过没去成的地方：" + "；".join(
            f"{d['city']}（在《{d['planned_in']}》里计划过）" for d in signals["unfulfilled_destinations"]))
    return "\n".join(parts)


def _fallback_nudge(signals: dict) -> str:
    """LLM 不可用时的规则模板（有实质内容，非空话）。"""
    parts = []
    if signals["stalled_plans"]:
        s = signals["stalled_plans"][0]
        parts.append(f"《{s['name']}》躺在计划里 {s['days_stalled']} 天了——把它翻出来，重新排进今年的日历？")
    if signals["unclosed_trips"]:
        s = signals["unclosed_trips"][0]
        parts.append(f"《{s['name']}》其实已经走完了，花两分钟给它画上句号吧。")
    if signals["unfulfilled_destinations"]:
        d = signals["unfulfilled_destinations"][0]
        parts.append(f"你计划过 {d['city']} 但还没动身——它还在等你。")
    return " ".join(parts) if parts else "你的旅行清单都很清爽。"
