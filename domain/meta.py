"""领域 API 层 — 点级情绪/场景元数据抽取（N-7）

化学反应 3（感官记忆重建）的地基：给每个旅行点补「情绪·天气·场景·时段·季节」
五维元数据，让记忆从「地名 + 坐标」升级为「可感知的体验片段」。

- 季节：规则兜底（日期 → 北半球季节），确定性、零 token。
- 情绪/天气/场景/时段：DeepSeek 从「名称 + 描述」语义推断。
- 全部落 point_meta 表（稀疏、点唯一、可增量更新）；meta_json 预留给
  未来气味/声音/温度等扩展感官维度。

不依赖 embedding；本模块只做元数据，不涉及坐标换算。
"""
from __future__ import annotations

import json
from datetime import datetime

from .generation import _llm


def derive_season(start_date: str | None) -> str | None:
    """规则兜底：从日期推断北半球季节（无法解析返回 None）。"""
    if not start_date:
        return None
    month = None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            month = datetime.strptime(start_date[:10], fmt).month
            break
        except ValueError:
            continue
    if month is None:
        return None
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


_EXTRACT_META_PROMPT = (
    "你是一位旅行记忆分析师。根据给定的地点信息，抽取该地点的「感官元数据」，"
    "帮助用户在未来重建这段旅行的感官记忆。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字。字段如下：\n"
    '{"mood":"情绪标签(2-4字，如 宁静/兴奋/怀旧/震撼/惬意)","weather":"天气(如 晴/阴雨/海边微风)","scene":"场景(4-10字具体描述，如 海边日落/古城街巷/雪山之巅)","time_of_day":"时段(如 清晨/黄昏/夜晚)"}\n'
    "要求：\n"
    "1. 只基于给定信息推断，禁止臆测；无法确定的字段输出空字符串。\n"
    "2. 全部字段留空时输出 {}（不要编造）。"
)


def _parse_meta_json(text: str) -> dict:
    """容错解析 LLM 输出的元数据 JSON（剥离 markdown 代码块/多余文本）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k in ("mood", "weather", "scene", "time_of_day"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def extract_point_meta(point: dict) -> dict:
    """从单个点抽取情绪/天气/场景/时段元数据（season 规则兜底）。

    point 需含 name（必填）、description/start_date/trip_name（可选）。
    返回 {"mood","weather","scene","time_of_day","season","tokens_used","raw"}。
    """
    name = (point.get("name") or "").strip()
    if not name:
        return {"mood": None, "weather": None, "scene": None,
                "time_of_day": None, "season": None, "tokens_used": 0, "raw": ""}

    # 季节：规则优先（确定性、零 token）
    season = derive_season(point.get("start_date"))

    desc = (point.get("description") or "").strip()
    trip_name = (point.get("trip_name") or "").strip()

    lines = [f"地点名称：{name}"]
    if trip_name:
        lines.append(f"所属行程：{trip_name}")
    if point.get("start_date"):
        lines.append(f"日期：{point['start_date']}")
    if desc:
        lines.append(f"描述：{desc}")

    user = "\n".join(lines) + "\n\n请抽取该地点的感官元数据。"
    text, tokens = _llm(_EXTRACT_META_PROMPT, user, max_tokens=200)
    meta = _parse_meta_json(text)

    return {
        "mood": meta.get("mood"),
        "weather": meta.get("weather"),
        "scene": meta.get("scene"),
        "time_of_day": meta.get("time_of_day"),
        "season": season,
        "tokens_used": tokens,
        "raw": text if not meta else "",
    }
