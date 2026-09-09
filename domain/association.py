"""领域 API 层 — 联想层（跨行程涌现连接）

分两档：
- 规则档（现做，确定性）：经纬度邻近 + 主题词共现 + 时间模式 → 候选连接
- LLM 精修（后置）：候选连接喂 DeepSeek 生成自然语言叙事

沉淀：稳定模式写入 user_prefs，形成长期偏好画像。
"""
from __future__ import annotations

import math

import db
from .memory import build_memory

# 邻近阈值（米）
_NEAR_M = 800.0


def _haversine(lat1, lng1, lat2, lng2):
    """两点球面距离（米）"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _season(date_str):
    if not date_str:
        return None
    try:
        m = int(date_str[5:7])
    except (ValueError, IndexError):
        return None
    if m in (3, 4, 5):
        return "春"
    if m in (6, 7, 8):
        return "夏"
    if m in (9, 10, 11):
        return "秋"
    return "冬"


def _list_user_trips(user_id: int):
    """按真实用户身份列行程（admin 看全部，普通用户看自己的），与 ai_list_trips 一致。"""
    user = db.get_user_by_id(user_id)
    return db.list_trips(user or {"id": user_id, "is_admin": 0})


def get_associations(user_id: int, trip_id: int = None, limit: int = 5) -> list[dict]:
    """对某行程（或某用户全部行程）生成跨行程联想。返回候选连接列表。"""
    trips = _list_user_trips(user_id)
    if trip_id:
        trips = [t for t in trips if t["id"] == trip_id]
    if len(trips) < 2:
        return []

    memories = []
    for t in trips:
        m = build_memory(t["id"])
        if m:
            memories.append(m)

    results = []

    # 1) 位置邻近：不同行程中两点距离 < 阈值 → 疑似重访同一地点
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            ma, mb = memories[i], memories[j]
            for a in ma["timeline"]:
                for b in mb["timeline"]:
                    d = _haversine(a["lat"], a["lng"], b["lat"], b["lng"])
                    if d < _NEAR_M:
                        results.append({
                            "type": "location_revisit",
                            "desc": f"「{a['name']}」与「{b['name']}」距离仅 {int(d)}m，疑似重访同一地点",
                            "trips": [ma["trip_id"], mb["trip_id"]],
                            "confidence": round(max(0.5, 1 - d / _NEAR_M), 2),
                            "evidence": {"point_a": a["name"], "point_b": b["name"], "dist_m": int(d)},
                        })

    # 2) 主题共现：跨行程出现相同主题
    theme_trips = {}
    for m in memories:
        for th in m["themes"]:
            theme_trips.setdefault(th, []).append(m["trip_id"])
    for th, trip_ids in theme_trips.items():
        if len(set(trip_ids)) >= 2:
            results.append({
                "type": "theme_recurrence",
                "desc": f"多次行程都出现「{th}」主题",
                "trips": sorted(set(trip_ids)),
                "confidence": 0.7,
                "evidence": {"theme": th},
            })
            # 沉淀偏好
            db.upsert_pref(user_id, f"theme:{th}", th, strength_delta=0.15)

    # 3) 季节模式
    season_trips = {}
    for m in memories:
        for it in m["timeline"]:
            s = _season(it["date"])
            if s:
                season_trips.setdefault(s, set()).add(m["trip_id"])
    for s, trip_ids in season_trips.items():
        if len(trip_ids) >= 2:
            results.append({
                "type": "season_preference",
                "desc": f"你偏好在{s}季出行",
                "trips": sorted(trip_ids),
                "confidence": 0.6,
                "evidence": {"season": s},
            })
            db.upsert_pref(user_id, f"season:{s}", s, strength_delta=0.1)

    # 去重 + 按置信度排序 + 截断
    seen = set()
    dedup = []
    for r in sorted(results, key=lambda x: -x["confidence"]):
        key = (r["type"], tuple(sorted(r["trips"])))
        if key not in seen:
            seen.add(key)
            dedup.append(r)
    return dedup[:limit]


def get_preferences(user_id: int) -> list[dict]:
    """返回用户长期偏好画像。"""
    return db.list_prefs(user_id)


def derive_preferences(user_id: int) -> int:
    """从用户所有行程的 memory 幂等沉淀偏好（theme + season）。

    不依赖跨行程共现——单行程也能沉淀（初始 strength 较弱，0.3）。
    已在库的 key 跳过，不重复累加。返回本次新增偏好数。
    """
    trips = _list_user_trips(user_id)
    existing = {p["pref_key"] for p in db.list_prefs(user_id)}
    added = 0

    for t in trips:
        m = build_memory(t["id"])
        if not m:
            continue
        # 主题偏好
        for th in m["themes"]:
            key = f"theme:{th}"
            if key not in existing:
                db.upsert_pref(user_id, key, th, strength_delta=0.3)
                existing.add(key)
                added += 1
        # 季节偏好
        seasons = set()
        for it in m["timeline"]:
            s = _season(it["date"])
            if s:
                seasons.add(s)
        for s in seasons:
            key = f"season:{s}"
            if key not in existing:
                db.upsert_pref(user_id, key, s, strength_delta=0.3)
                existing.add(key)
                added += 1

    return added
