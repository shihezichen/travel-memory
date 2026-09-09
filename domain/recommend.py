"""领域 API 层 — 行程推荐（P2-2，N-15 人格推荐的地基版）

基于用户历史足迹 + 长期偏好，让 DeepSeek 推荐「还没去过、但可能喜欢」
的目的地，每个推荐带可解释理由（「因为你上次在 X 最开心」）。

两段式：
1. 规则层（零 token）：从足迹点提取已去城市 / 主题 / 季节，构造画像摘要。
2. LLM 层（DeepSeek）：喂画像 + 已去城市，生成推荐清单（还没去过 + 可解释）。

区别于 N-15（人格驱动的下一个目的地）：本版是「补全式推荐」——基于
「你去过哪里」推「你还该去哪儿」，不依赖尚未完成的 N-5 人格库，是
N-15 的前置地基。坐标不涉及（推荐的是目的地，不是已有坐标点）。
"""
from __future__ import annotations

import json

import db
from .generation import _llm
from .memory import _CITY_THEMES

# 城市 → 主题 反向索引（识别用户已去城市）
_CITY_TO_THEME = {}
for _theme, _cities in _CITY_THEMES.items():
    for _c in _cities:
        _CITY_TO_THEME[_c] = _theme


def _extract_footprint(user_id: int) -> dict:
    """从用户全部足迹点提取画像摘要（已去城市/主题/季节），零 token。"""
    user = db.get_user_by_id(user_id)
    points = db.list_searchable_points(user) if user else []

    cities: dict[str, int] = {}      # 城市 -> 出现次数
    themes: dict[str, int] = {}      # 主题 -> 出现次数
    seasons: dict[str, int] = {}     # 季节 -> 出现次数
    trip_names: list[str] = []

    for p in points:
        text = f"{p['name'] or ''} {p['trip_name'] or ''}"
        for city in _CITY_TO_THEME:
            if city in text:
                cities[city] = cities.get(city, 0) + 1
                themes[_CITY_TO_THEME[city]] = themes.get(_CITY_TO_THEME[city], 0) + 1
        if p.get("trip_name") and p["trip_name"] not in trip_names:
            trip_names.append(p["trip_name"])
        # 季节（从 start_date 的月份粗判）
        d = p.get("start_date") or ""
        if d and len(d) >= 7:
            try:
                m = int(d[5:7])
                s = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
                     6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}.get(m)
                if s:
                    seasons[s] = seasons.get(s, 0) + 1
            except ValueError:
                pass

    top_cities = sorted(cities, key=lambda c: -cities[c])[:20]
    top_themes = sorted(themes, key=lambda t: -themes[t])[:10]
    top_seasons = sorted(seasons, key=lambda s: -seasons[s])
    return {
        "n_points": len(points),
        "n_trips": len(trip_names),
        "cities": top_cities,
        "themes": top_themes,
        "seasons": top_seasons,
        "trip_names": trip_names[:10],
    }


_RECOMMEND_SYSTEM = (
    "你是一位懂旅行的目的地推荐顾问。根据用户的历史旅行足迹与偏好，"
    "推荐「用户还没去过、但很可能喜欢」的目的地。\n"
    "要求：\n"
    "1. 绝不推荐用户已去过的城市（已去列表见输入），也避免重复推荐同一地区。\n"
    "2. 推荐要有可解释性：每个目的地给出「为什么适合这个人」的理由，"
    "   尽量关联 TA 的已有偏好（例如「因为你上次在海边城市最开心」）。\n"
    "3. 只输出一个 JSON 数组，不要输出任何其他文字。每项格式：\n"
    '   {"destination":"目的地名","reason":"可解释理由","theme":"类型","confidence":0.0到1.0}\n'
    "   输出 3-6 项，按推荐强度从高到低排列。"
)


def _parse_recommend_json(text: str) -> list[dict]:
    """容错解析 LLM 输出的推荐 JSON（剥离 markdown 代码块/多余文字）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # 兜底：提取第一个 [...] 片段
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def recommend_destinations(user_id: int, count: int = 5, style: str = None) -> dict:
    """推荐还没去过但可能喜欢的目的地。count 为返回推荐数（3-6 之间截断）。"""
    fp = _extract_footprint(user_id)
    if fp["n_points"] == 0:
        return {"recommendations": [], "footprint": fp,
                "note": "暂无历史足迹，先去几个地方再回来推荐。"}

    prefs = db.list_prefs(user_id)
    pref_text = "、".join(p["pref_value"] for p in prefs[:15]) or "（暂无沉淀偏好）"

    user_prompt = (
        f"用户历史足迹概览：\n"
        f"- 共 {fp['n_trips']} 段旅程、{fp['n_points']} 个地点\n"
        f"- 去过的城市：{'、'.join(fp['cities']) or '（未识别到具体城市）'}\n"
        f"- 偏好的地点类型：{'、'.join(fp['themes']) or '（未识别）'}\n"
        f"- 偏好的季节：{'、'.join(fp['seasons']) or '（未识别）'}\n"
        f"- 旅程名：{'、'.join(fp['trip_names']) or '（无）'}\n"
        f"- 沉淀偏好：{pref_text}\n\n"
        f"请推荐 {count} 个还没去过但可能喜欢的目的地。"
    )

    text, tokens = _llm(_RECOMMEND_SYSTEM, user_prompt, max_tokens=600)
    recs = _parse_recommend_json(text)
    if style:
        pass  # style 预留，未来接 N-5 人格后做风格化推荐

    return {
        "recommendations": recs[:count],
        "footprint": {
            "n_trips": fp["n_trips"],
            "n_points": fp["n_points"],
            "cities": fp["cities"],
            "themes": fp["themes"],
            "seasons": fp["seasons"],
        },
        "tokens_used": tokens,
    }
