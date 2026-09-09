"""领域 API 层 — 记忆语义检索（N-8，P0 补地基）

让外部 agent 用自然语言查询「我的旅行记忆」——「我去过哪些海边？」
「哪里拍过日落？」「去年这个时候我在哪儿？」。

两段式（无 embedding 依赖，对齐单进程 + SQLite + DeepSeek 栈）：
1. 规则召回（零 token）：词典命中（主题词 + 城市地标）+ 子串重叠，
   从用户所有点里粗筛 Top-N 候选，保证不遗漏。
2. LLM 精排（DeepSeek）：把 query + 候选记忆喂 LLM，做真正的语义理解，
   返回 Top-K 记忆片段 + 匹配依据。

对外坐标：库内 GCJ-02 → 返回给 agent 时转 WGS-84（与 memory.py 一致）。
"""
from __future__ import annotations

import json

import db
from .memory import _THEME_KEYWORDS, _CITY_THEMES
from .schemas import gcj02_to_wgs84
from .labels import LANDMARK_CITY, label_point_city, _CITY_TO_THEME

# 通用停用词（召回时从 query 剔除，避免噪声命中）
_STOPWORDS = {
    "我", "的", "了", "吗", "呢", "啊", "吧", "去", "过", "在", "哪", "里",
    "是", "有", "什么", "哪些", "时候", "时间", "地方", "哪里", "怎么",
    "和", "与", "跟", "到", "想", "看", "找", "查", "搜", "一", "个", "都",
}


def _themes_for_text(text: str) -> set[str]:
    """给一段文本打主题标签（复用 memory.py 的词典，规则抽取）。"""
    t = (text or "").lower()
    themes = set()
    for theme, kws in _THEME_KEYWORDS.items():
        if any(k.lower() in t for k in kws):
            themes.add(theme)
    for theme, cities in _CITY_THEMES.items():
        if any(c.lower() in t for c in cities):
            themes.add(theme)
    return themes


def _query_signals(query: str):
    """从自然语言 query 提取召回信号：命中的主题词 + 城市地标 + 有效子串。"""
    q = (query or "").lower()
    hit_themes = {t for t, kws in _THEME_KEYWORDS.items()
                  if any(k.lower() in q for k in kws)}
    hit_cities = set()
    hit_city_themes = set()
    for theme, cities in _CITY_THEMES.items():
        for c in cities:
            if c.lower() in q:
                hit_cities.add(c)
                hit_city_themes.add(theme)
    # 地标→城市 映射（N-13 ①）：query 里的地标词反推城市，让「埃菲尔铁塔」关联「巴黎」
    for landmark, city in LANDMARK_CITY.items():
        if landmark.lower() in q and city not in hit_cities:
            hit_cities.add(city)
            if city in _CITY_TO_THEME:
                hit_city_themes.add(_CITY_TO_THEME[city])
    # 有效子串：2~3 字片段（剔停用词），用于对点文本做包含匹配
    grams = set()
    for n in (2, 3):
        for i in range(len(query) - n + 1):
            g = query[i:i + n]
            if any(w in g for w in _STOPWORDS):
                continue
            grams.add(g.lower())
    return hit_themes, hit_cities, hit_city_themes, grams


def _point_text(p: dict) -> str:
    return f"{p['name']} {p['description'] or ''}"


def _trip_text(p: dict) -> str:
    return p["trip_name"] or ""


def recall(user_id: int, query: str, top_n: int = 20) -> list[dict]:
    """规则召回：从用户全部旅行点里粗筛与 query 相关的候选，按相关度降序。

    语义约束（刚哥 2026-08-25 拍板）：「记忆」既包含已发生，也包含计划中
    行程。故不按日期过滤，未来「计划中」的点同样参与召回，由结果中的
    trip_status 字段区分「已发生/进行中/计划中」。点自身（name/desc）命中
    权重高于行程名命中，避免混合行程名（如「巴黎-西班牙」）污染召回。
    """
    user = db.get_user_by_id(user_id)
    points = db.list_searchable_points(user or {"id": user_id, "is_admin": 0})
    if not points:
        return []

    hit_themes, hit_cities, hit_city_themes, grams = _query_signals(query)

    scored = []
    for p in points:
        pt_text = _point_text(p).lower()
        trip_text = _trip_text(p).lower()
        pt_themes = _themes_for_text(pt_text)
        trip_themes = _themes_for_text(trip_text)
        score = 0
        # 点级城市标签（N-13 ①）：点 name/desc 里的地标→城市 反推，命中 query 城市词最高权重
        pt_city = label_point_city(p["name"], p.get("description") or "").get("city")
        if pt_city and pt_city in hit_cities:
            score += 10
        # 主题词：点自身命中高分，行程名命中低分
        score += len(hit_themes & pt_themes) * 6
        score += len(hit_themes & trip_themes) * 2
        score += len(hit_city_themes & pt_themes) * 6
        score += len(hit_city_themes & trip_themes) * 2
        # 城市地标：点自身命中高分，行程名命中低分
        for c in hit_cities:
            if c.lower() in pt_text:
                score += 8
            elif c.lower() in trip_text:
                score += 3
        # 有效子串：点自身命中高分，行程名命中低分
        if grams:
            score += sum(1 for g in grams if g in pt_text) * 4
            score += sum(1 for g in grams if g in trip_text)
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:top_n]]


def _candidate_lines(candidates: list[dict]) -> list[dict]:
    """把候选点压成 LLM 可读的行（含坐标转 WGS-84 + 唯一索引 + 行程状态）。"""
    lines = []
    for i, p in enumerate(candidates):
        wgs_lng, wgs_lat = gcj02_to_wgs84(p["lng"] or 0, p["lat"] or 0)
        lines.append({
            "idx": i,
            "point_id": p["id"],
            "trip_id": p["trip_id"],
            "trip_name": p["trip_name"],
            "name": p["name"],
            "desc": (p["description"] or "")[:60],
            "date": p["start_date"],
            "status": p.get("trip_status") or "planning",
            "lat": round(wgs_lat, 6),
            "lng": round(wgs_lng, 6),
        })
    return lines


_SEMANTIC_RANK_PROMPT = (
    "你是一位旅行记忆检索助手。用户用自然语言问关于自己旅行记忆的问题，"
    "我从记忆库里召回了一批候选地点。请选出与问题语义最相关的地点。\n"
    "注意：候选地点可能包含「计划中」的行程，请依据用户问题的实际意图取舍"
    "（问「去过/回忆」侧重已发生，问「打算/计划」侧重计划中）。\n"
    "只输出一个 JSON 数组，不要输出任何其他文字。每项格式：\n"
    '{"idx":候选地点的 idx 数字,"reason":"为什么相关(一句话,中文)"}\n'
    "按相关度从高到低排序，最多输出 5 项；无关的不要输出。"
)


def _parse_rank_json(text: str) -> list[dict]:
    """容错解析 LLM 精排结果（剥离 markdown/多余文本）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx", -1))
        except (ValueError, TypeError):
            continue
        if idx < 0:
            continue
        out.append({"idx": idx, "reason": str(item.get("reason", "")).strip()})
    return out


# 状态中文标签（供 LLM 区分已发生/计划中，N-8/N-9 共用）
_STATUS_CN = {"planning": "计划中", "traveling": "进行中",
              "completed": "已结束", "archived": "已归档"}


def semantic_search(user_id: int, query: str, limit: int = 5) -> dict:
    """自然语言查询旅行记忆（N-8 主入口）。

    返回 {"query", "matches":[{trip_id,point_id,name,trip_name,date,status,lat,lng,reason}]}。
    status 区分 planning/traveling/completed/archived（「记忆」含计划行程）。
    召回为空时降级返回最近记录，LLM 失败时降级返回规则召回 Top-N。
    """
    candidates = recall(user_id, query, top_n=20)
    if not candidates:
        # 空召回降级：query 太泛，返回最近的记录兜底（含计划中行程）
        user = db.get_user_by_id(user_id)
        recent = db.list_searchable_points(
            user or {"id": user_id, "is_admin": 0})
        candidates = recent[:limit]
    if not candidates:
        return {"query": query, "matches": [], "empty": True}

    cand_lines = _candidate_lines(candidates)

    # LLM 精排（可降级）
    matched = []
    used_tokens = 0
    llm_fallback = False
    try:
        from .generation import _llm
        user_msg = (
            f"用户的问题：{query}\n\n候选地点（按 idx 引用）：\n"
            + "\n".join(
                f"[{c['idx']}] {c['name']}（行程「{c['trip_name']}」"
                f"{c['date'] or '?'}，状态：{_STATUS_CN.get(c['status'], c['status'])}）"
                f"{('：' + c['desc']) if c['desc'] else ''}"
                for c in cand_lines
            )
        )
        raw, tokens = _llm(_SEMANTIC_RANK_PROMPT, user_msg, max_tokens=400)
        used_tokens = tokens
        ranked = _parse_rank_json(raw)
        by_idx = {c["idx"]: c for c in cand_lines}
        seen = set()
        for r in ranked:
            if r["idx"] in by_idx and r["idx"] not in seen:
                seen.add(r["idx"])
                c = by_idx[r["idx"]]
                matched.append({
                    "trip_id": c["trip_id"],
                    "point_id": c["point_id"],
                    "name": c["name"],
                    "trip_name": c["trip_name"],
                    "date": c["date"],
                    "status": c["status"],
                    "lat": c["lat"],
                    "lng": c["lng"],
                    "reason": r["reason"],
                })
        matched = matched[:limit]
        if not matched:
            llm_fallback = True
    except Exception:
        used_tokens = 0
        llm_fallback = True

    # 降级兜底：LLM 失败 或 返回空（认为无强相关）→ 规则召回 Top-N 原样返回
    if llm_fallback and not matched:
        for c in cand_lines[:limit]:
            matched.append({
                "trip_id": c["trip_id"],
                "point_id": c["point_id"],
                "name": c["name"],
                "trip_name": c["trip_name"],
                "date": c["date"],
                "status": c["status"],
                "lat": c["lat"],
                "lng": c["lng"],
                "reason": "",
            })

    return {
        "query": query,
        "matches": matched,
        "n_candidates": len(candidates),
        "tokens_used": used_tokens,
        "llm_fallback": llm_fallback,
    }


# ===== N-9 记忆可对话接口：自然语言回答 =====
_ANSWER_PROMPT = (
    "你是一位温暖、贴心的旅行记忆伴侣，帮助用户回忆自己的旅行经历。"
    "用户会用自然语言问你关于自己旅行记忆的问题。"
    "我会给你一些从记忆库里检索到的相关记忆片段（含地点、行程名、日期、坐标）。\n"
    "请基于这些记忆片段，用自然、口语化的中文回答用户的问题。\n"
    "要求：\n"
    "1. 用第一人称「你」的口吻（把用户称作「你」），像老朋友帮他回忆。\n"
    "2. 只基于提供的记忆片段作答，不要编造不存在的记忆；如果记忆片段不足以回答，就如实说「我暂时没找到相关记忆」。\n"
    "3. 回答简洁有温度，100-200 字即可，不要罗列编号。\n"
    "4. 如果涉及地点，可以自然带出地名和大致时间（如「去年 6 月」）。\n"
    "5. 只输出回答正文，不要加前缀或解释。"
)


def answer_question(user_id: int, question: str, limit: int = 5) -> dict:
    """N-9：自然语言「问」记忆，返回 LLM 生成的一段自然语言回答。

    建立在 N-8 semantic_search 之上：先检索匹配记忆，再用 LLM 组织成
    对话式回答（可读、有温度）。无匹配记忆时如实告知，不编造。
    """
    search = semantic_search(user_id, question, limit=limit)
    matches = search.get("matches", [])
    if not matches:
        return {
            "question": question,
            "answer": "我暂时没找到相关的旅行记忆。要不要换个说法，或者告诉我大概的时间或地点？",
            "matches": [],
            "tokens_used": 0,
            "empty": True,
        }

    # 把匹配记忆压成 LLM 可读的片段
    def _fmt(m):
        parts = [f"{m['name']}（行程「{m['trip_name']}」"]
        if m.get("date"):
            parts.append(f"，{m['date']}")
        parts.append(")")
        if m.get("status"):
            parts.append(f"[{_STATUS_CN.get(m['status'], m['status'])}]")
        if m.get("reason"):
            parts.append(f"——{m['reason']}")
        return "".join(parts)

    ctx_lines = "\n".join(f"- {_fmt(m)}" for m in matches)

    from .generation import _llm
    user_msg = f"用户的问题：{question}\n\n相关记忆片段：\n{ctx_lines}\n\n请回答用户。"
    raw, tokens = _llm(_ANSWER_PROMPT, user_msg, max_tokens=400)

    return {
        "question": question,
        "answer": raw.strip(),
        "matches": matches,
        "tokens_used": tokens,
        "empty": False,
    }
