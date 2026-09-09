"""N-16 记忆再阐释（Reinterpret）：给过去的旅程换一个视角重新讲述。

规则层（零 token）：提取行程原始记忆摘要（复用 N-6 build_memory 的主题/摘要/时间轴），
LLM 层按 lens（视角）重讲这段记忆——成长/遗憾/同行/幽默四种镜头。

机制先行：不依赖数据年限，任何行程随时可再阐释；数据越久，成长视角越有味道。
降级：LLM 不可用时按 lens 出规则模板（引用真实地名/时间，非空话）。
"""
from __future__ import annotations

from datetime import datetime

import db
from .generation import _llm
from .memory import build_memory

LENSES = ("成长", "遗憾", "同行", "幽默")

_REINTERPRET_SYSTEM = (
    "你是旅行记忆助手。给你一段用户过去的旅行记忆摘要（时间/地点/主题）和一个视角镜头，"
    "请用该镜头重新讲述这段旅程，220 字以内。"
    "硬性要求：① 必须引用至少 2 个「真实地点清单」里的地名，禁止出现清单外的任何地名；"
    "② 必须基于原文事实，禁止编造新信息；"
    "③ 镜头是「成长」时聚焦'当时的你 vs 现在的你'的变化；「遗憾」时聚焦没来得及做的事，"
    "但要落在'下次可以怎样补上'的积极收尾；「同行」时聚焦一起走过的人与关系；"
    "「幽默」时用自嘲式轻松口吻，但不轻浮；④ 禁止「时光荏苒/感慨万千」类空泛套话。"
)


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


def reinterpret_trip(user: dict, trip_id: int, lens: str = "成长",
                     db_module=None) -> dict:
    """再阐释指定行程。lens ∈ 成长/遗憾/同行/幽默。

    返回 {"trip_id", "trip_name", "lens", "original_summary",
          "reinterpretation", "age_days"}。
    """
    dbm = db_module or db
    if lens not in LENSES:
        raise ValueError(f"lens 必须是 {'/'.join(LENSES)} 之一")

    trip = dbm.get_trip(trip_id)
    if not trip:
        raise ValueError("行程不存在")
    # 越权检查：非 admin 只能再阐释自己的行程
    if not user.get("is_admin") and trip.get("user_id") != user.get("id"):
        raise ValueError("无权访问该行程")

    mem = build_memory(trip_id)
    # 真实地点清单（防 LLM 编造地名的关键事实源）
    points = dbm.list_points(trip_id)
    place_list = []
    for p in points[:12]:
        nm = p.get("name") or ""
        desc = (p.get("description") or "").strip()
        place_list.append(f"{nm}（{desc[:20]}）" if desc else nm)
    if mem:
        s = mem["summary"]
        original = {
            "date_range": s.get("date_range") or "",
            "themes": mem.get("themes") or [],
            "point_count": s.get("n_points") or 0,
            "media_count": s.get("n_media") or 0,
            "timeline": (s.get("timeline") or [])[:5],
            "places": place_list,
        }
    else:
        original = {
            "date_range": f"{trip.get('start_date') or '?'} ~ {trip.get('end_date') or '?'}",
            "themes": [],
            "point_count": len(points),
            "media_count": 0,
            "timeline": [],
            "places": place_list,
        }

    # 记忆年龄（天）：距行程结束（或开始/创建）多久
    ref = trip.get("end_date") or trip.get("start_date") or trip.get("created_at")
    age_days = None
    dt = _parse_dt(ref)
    if dt:
        age_days = max(0, (datetime.now() - dt).days)

    prompt = (
        f"行程名：{trip.get('name') or '未命名'}\n"
        f"时间：{original['date_range']}\n"
        f"主题：{'、'.join(original['themes']) or '无'}\n"
        f"地点数：{original['point_count']}，媒体数：{original['media_count']}\n"
        f"真实地点清单（只能引用这些地点，禁止出现清单外的地名）："
        f"{'、'.join(original['places']) or '无'}\n"
        f"时间轴摘录：{original['timeline']}\n"
        f"视角镜头：{lens}"
    )

    result = {
        "trip_id": trip_id,
        "trip_name": trip.get("name") or "未命名",
        "lens": lens,
        "original_summary": original,
        "age_days": age_days,
        "reinterpretation": None,
    }
    try:
        text, _ = _llm(_REINTERPRET_SYSTEM, prompt, max_tokens=450)
        result["reinterpretation"] = text.strip()
    except Exception:
        result["reinterpretation"] = _fallback(trip, original, lens)
    return result


def _fallback(trip: dict, original: dict, lens: str) -> str:
    """LLM 不可用时的规则模板：引用真实地名/时间。"""
    themes = "、".join(original["themes"][:3]) or "一段旅程"
    span = original["date_range"] or "某个时间"
    if lens == "成长":
        return (f"{span} 的「{themes}」，当时走完只觉得是路过；"
                f"现在回看，那 {original['point_count']} 个地点其实悄悄改写了看世界的方式。")
    if lens == "遗憾":
        return (f"{span} 的「{themes}」有 {original['point_count']} 个地点，"
                f"但总有些没来得及做的事——下次再去，把当时缺的那一步补上。")
    if lens == "同行":
        return (f"{span} 的「{themes}」，一起走过 {original['point_count']} 个地点的人，"
                f"才是这段旅程最长的记忆。")
    return (f"{span} 的「{themes}」，{original['point_count']} 个地点跑下来，"
            f"当时觉得是精心规划，现在看全是随机应变——挺好。")
