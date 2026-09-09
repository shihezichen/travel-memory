"""领域 API 层 — DeepSeek 生成层（总结/路书/明信片/日记）

幂等：以 (kind, trip_id, style, memory_hash) 为缓存 key，
命中直接返回缓存，不重复扣 token。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.request

from .memory import build_memory, memory_to_text

from . import llm_provider

# 兼容旧调用点：key 读取与 URL 常量保留，实际分发在 llm_provider 里
_get_api_key = llm_provider._deepseek_key
DEEPSEEK_URL = llm_provider.DEEPSEEK_URL
_MODEL = llm_provider.DEEPSEEK_MODEL

# 简单内存缓存：{(kind,trip_id,style,memory_hash): (text, tokens)}
_CACHE = {}


def _llm(system: str, user: str, max_tokens: int = 800) -> tuple[str, int]:
    """调用 LLM，返回 (text, tokens_used)。

    provider 由 TRAVEL_LLM_PROVIDER 决定：本机走 claude CLI，远程走 DeepSeek。
    重试/退避在 llm_provider.call 内部。
    """
    return llm_provider.call(system, user, max_tokens)


_SYSTEM_PROMPTS = {
    "trip_summary": "你是一位有文采的旅行记录者。根据给定的结构化行程信息，写一段生动、有画面感的旅程总结（200-400字），用第一人称「我」的口吻，突出旅途中的地点脉络与情感温度。",
    "route_guide": "你是一位旅行路书编辑。根据给定的结构化行程信息，生成一份实用的路书（按天/地点分点，含建议游玩顺序与要点），简洁清晰。",
    "postcard": "你帮用户写旅行明信片寄语。根据给定的结构化行程信息，写一段 50-80 字的明信片寄语，温暖、有纪念感。",
    "diary": "你以旅行者身份写当天的旅行日记。根据给定的结构化行程信息，写一段 150-300 字的日记，细腻、带情绪与细节。",
    "prose": "你是旅行散文家。根据给定的行程信息与地点情绪元数据，写一段 200-400 字的散文。硬性要求：① 必须引用至少 2 个具体地点名；② 必须化用至少 1 条情绪元数据（mood/场景）；③ 禁止「风景如画」「流连忘返」「美不胜收」「令人难忘」等套话；④ 用第一人称，有真实细节，不编造行程外的信息。",
    "poem": "你是旅行诗人。根据给定的行程信息与地点情绪元数据，写一首 8-16 行的短诗。硬性要求：① 至少出现 2 个具体地名或地标；② 化用至少 1 条情绪元数据；③ 禁止陈词滥调（夕阳/大海/远方堆砌），要有具体而意外的意象；④ 分行输出，不写注释。",
    "story": "你是旅行故事作者。根据给定的行程信息与地点情绪元数据，写一个 300-600 字的小故事（可虚构情节，但必须基于行程中的真实地点与氛围）。硬性要求：① 故事必须发生在至少 2 个具体地点；② 化用至少 1 条情绪元数据；③ 禁止「从此过上了幸福的生活」式烂尾；④ 要有转折或回味。",
}


def _point_meta_ctx(trip_id: int) -> str:
    """从 point_meta 表取地点情绪元数据（mood/scene/season），拼成 LLM 可引用文本。"""
    try:
        import db as _db
        rows = _db.list_point_meta_by_trip(trip_id)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows:
        parts = []
        if r.get("mood"):
            parts.append(f"情绪:{r['mood']}")
        if r.get("scene"):
            parts.append(f"场景:{r['scene']}")
        if r.get("season"):
            parts.append(f"季节:{r['season']}")
        if r.get("time_of_day"):
            parts.append(f"时段:{r['time_of_day']}")
        if parts:
            lines.append(f"- {r.get('point_name') or r.get('point_id')}：" + "，".join(parts))
    return "\n".join(lines)


def generate(kind: str, trip_id: int, style: str = "旅行者视角", language: str = "zh") -> dict:
    memory = build_memory(trip_id)
    if not memory:
        raise ValueError(f"行程 {trip_id} 不存在")

    ctx = memory_to_text(memory)
    # 幂等缓存 key（含 language，避免 zh/en 互相命中）
    mem_hash = hashlib.md5(ctx.encode()).hexdigest()[:16]
    cache_key = (kind, trip_id, style, language, mem_hash)
    if cache_key in _CACHE:
        text, tokens = _CACHE[cache_key]
        return {"kind": kind, "trip_id": trip_id, "text": text,
                "tokens_used": tokens, "cached": True}

    system = _SYSTEM_PROMPTS.get(kind, _SYSTEM_PROMPTS["trip_summary"])
    if style and style != "旅行者视角":
        system += f"\n风格要求：{style}。"
    if language == "en":
        system += "\n请用英文输出。"

    user = f"以下是行程的结构化信息：\n\n{ctx}\n\n请根据上述信息生成内容。"
    # N-19：创作类 kind 附加地点情绪元数据（point_meta 的 mood/scene），供引用
    if kind in ("prose", "poem", "story"):
        meta = _point_meta_ctx(trip_id)
        if meta:
            user += f"\n\n地点情绪元数据（务必化用）：\n{meta}"
    text, tokens = _llm(system, user, max_tokens=900 if kind == "route_guide" else 600)

    # P1-2: 非缓存命中时落库（可追溯 + 前端可查历史）
    try:
        import db as _db
        _db.save_generation(trip_id, kind, text, tokens, style=style, language=language)
    except Exception:
        pass  # 落库失败不影响主流程返回

    _CACHE[cache_key] = (text, tokens)
    return {"kind": kind, "trip_id": trip_id, "text": text,
            "tokens_used": tokens, "cached": False}


# ===== N-5 旅行人格画像库：偏好抽取 =====
_EXTRACT_PROFILE_PROMPT = (
    "你是一位旅行偏好分析师。根据给定的行程信息，抽取用户的旅行偏好画像。\n"
    "只输出一个 JSON 数组，不要输出任何其他文字。每项格式：\n"
    '{"key":"偏好类别","value":"具体偏好","strength":0.0到1.0的置信度,"evidence":"依据"}\n'
    "偏好类别限于：目的地类型、活动偏好、时间偏好、住宿偏好、消费风格、拍照偏好、饮食偏好。\n"
    "只抽取有明确依据的偏好，禁止臆测；没有依据的类别直接省略，不要输出 strength 为 0 或含「无明确」的占位项。输出 3-8 项。"
)


def _parse_pref_json(text: str) -> list[dict]:
    """容错解析 LLM 输出的偏好 JSON（剥离 markdown 代码块/多余文本）。"""
    text = text.strip()
    # 去掉可能的 ```json ... ``` 包裹
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    # 截取第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
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
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        if not key or not value:
            continue
        try:
            strength = float(item.get("strength", 0.5))
        except Exception:
            strength = 0.5
        strength = max(0.0, min(1.0, strength))
        # 过滤占位/无依据项：strength 过低，或 value 含否定/无依据措辞
        if strength < 0.05:
            continue
        if any(w in value for w in ("无明确", "无依据", "无法推断", "没有", "省略")):
            continue
        out.append({"key": key, "value": value, "strength": strength,
                    "evidence": str(item.get("evidence", "")).strip()})
    return out


def extract_profile(trip_id: int) -> dict:
    """从行程抽取旅行偏好画像（N-5）。返回偏好列表，供 app 层 upsert 到 user_prefs。"""
    memory = build_memory(trip_id)
    if not memory:
        raise ValueError(f"行程 {trip_id} 不存在")

    ctx = memory_to_text(memory)
    theme_hint = ""
    if memory.get("themes"):
        theme_hint = "\n规则抽取的主题线索：" + "、".join(memory["themes"])

    user = f"以下是行程的结构化信息：\n\n{ctx}{theme_hint}\n\n请抽取旅行偏好画像。"
    text, tokens = _llm(_EXTRACT_PROFILE_PROMPT, user, max_tokens=600)
    prefs = _parse_pref_json(text)
    return {"trip_id": trip_id, "prefs": prefs, "tokens_used": tokens,
            "raw": text if not prefs else ""}
