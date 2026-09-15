"""领域 API 层 — 记忆层（纯 SQL 聚合 + 规则主题词，不调 LLM）

把分散在 trips/points/media 的"数据行"聚合成结构化旅行记忆，
作为 agent 的上下文单元。
"""
from __future__ import annotations

import db
from .schemas import gcj02_to_wgs84, TripMemory, MemorySummary, TimelineItem

# 「海 / 湾 / 滨」这几个字在内陆地名里太常见了，直接拿单字当关键词会把高原
# 行程打成「海边」——滇西北那趟就是这么中招的：丽江的描述里写了「先适应海拔」，
# 一个「海」字就够了。同类的还有海子（高原湖）、洱海、青海、金沙江第一湾。
# 这些词里没有海，匹配前先抹掉；真正的海只认「海边/海滩/海岸…」这类复合词。
_THEME_NOISE = (
    "海拔", "海子", "洱海", "青海", "中南海", "云海", "花海", "人海", "脑海",
    "林海", "火海", "苦海", "海鲜", "海关", "第一湾", "江湾", "河湾", "港澳",
)


def mask_theme_noise(text: str) -> str:
    """抹掉「含海字但跟海无关」的词，供所有规则档主题匹配复用。"""
    t = text or ""
    for w in _THEME_NOISE:
        t = t.replace(w, "")
    return t


# 浅层主题词表（规则抽取，非 LLM）：按关键词命中点名称/描述
# 注意：别往「海边」里塞单字。见上面 _THEME_NOISE 的说明。
_THEME_KEYWORDS = {
    "海边": ["海边", "海滩", "海岸", "海湾", "海岛", "大海", "看海", "滨海", "海景",
            "沙滩", "湾", "岛", "码头", "港", "beach", "bay", "coast", "island"],
    "古城": ["古城", "老城", "城堡", "宫", "教堂", "寺", "庙", "castle", "palace", "cathedral"],
    "美食": ["餐厅", "美食", "小吃", "夜市", "咖啡", "酒", "restaurant", "food", "cafe"],
    "日落": ["日落", "夕阳", "黄昏", "sunset"],
    "山野": ["山", "峰", "谷", "湖", "公园", "mountain", "lake", "park", "valley"],
    "博物馆": ["博物馆", "美术馆", "展览", "museum", "gallery"],
}

# 城市/地标词典：纯城市名（无关键词）也能命中主题，改善联想与偏好画像
# 覆盖刚哥常去 + 常见旅游目的地（欧洲 + 国内）
_CITY_THEMES = {
    "古城": ["巴黎", "罗马", "佛罗伦萨", "威尼斯", "马德里", "托莱多", "塞维利亚",
            "格拉纳达", "科尔多瓦", "巴塞罗那", "里斯本", "波尔图", "布拉格",
            "维也纳", "布达佩斯", "雅典", "伊斯坦布尔", "西安", "洛阳", "开封",
            "南京", "北京", "平遥", "大理", "丽江", "敦煌", "嘉峪关", "郎木寺"],
    "海边": ["尼斯", "戛纳", "马拉加", "圣托里尼", "米科诺斯", "瓦伦西亚",
            "厦门", "青岛", "三亚", "大连", "北海", "西港", "海口"],
    "山野": ["龙达", "孔苏埃格拉", "卢塞恩", "因特拉肯", "张家界", "桂林",
            "九寨沟", "稻城", "香格里拉", "青海湖", "可可西里", "祁连", "若尔盖",
            "新都桥", "塔公", "红原", "马尔康", "冷湖", "茫崖", "德令哈", "茶卡"],
}


def build_memory(trip_id: int) -> dict:
    """构建某行程的结构化旅行记忆。返回 dict（供生成/联想层复用）。"""
    trip = db.get_trip(trip_id)
    if not trip:
        return None

    points = db.list_points(trip_id)
    media_counts = db.get_point_media_counts(trip_id)

    timeline = []
    themes_hit = set()
    media_highlights = []
    n_media = 0
    n_photos = 0
    n_videos = 0

    for p in points:
        # 库内是 GCJ-02，出库给 agent 转 WGS-84
        wgs_lng, wgs_lat = gcj02_to_wgs84(p["lng"], p["lat"])
        cnt = media_counts.get(p["id"], {"photo": 0, "video": 0})
        n_media += cnt["photo"] + cnt["video"]
        n_photos += cnt["photo"]
        n_videos += cnt["video"]

        timeline.append(TimelineItem(
            name=p["name"],
            lat=round(wgs_lat, 6),
            lng=round(wgs_lng, 6),
            date=p["start_date"],
            desc=p["description"],
            media_n=cnt["photo"] + cnt["video"],
            route_mode=p.get("route_mode", "drive"),
        ).model_dump())

        # 主题抽取：点名称 + 描述（先过噪声掩码，否则「海拔」会命中「海边」）
        text = mask_theme_noise(f"{p['name']} {p['description'] or ''}").lower()
        for theme, kws in _THEME_KEYWORDS.items():
            if any(k.lower() in text for k in kws):
                themes_hit.add(theme)
        # 城市/地标词典兜底（纯城市名无关键词也能命中）
        for theme, cities in _CITY_THEMES.items():
            if any(c.lower() in text for c in cities):
                themes_hit.add(theme)

        # 照片打点摘要
        if cnt["photo"] + cnt["video"] > 0:
            media_highlights.append({
                "point": p["name"],
                "n": cnt["photo"] + cnt["video"],
                "n_photos": cnt["photo"],
                "n_videos": cnt["video"],
            })

    # 日期范围
    dates = [p["start_date"] for p in points if p.get("start_date")]
    date_range = None
    if dates:
        date_range = f"{min(dates)} ~ {max(dates)}"

    summary = MemorySummary(
        name=trip["name"],
        date_range=date_range,
        n_points=len(points),
        n_media=n_media,
        n_photos=n_photos,
        n_videos=n_videos,
    )

    return TripMemory(
        trip_id=trip_id,
        summary=summary,
        timeline=timeline,
        themes=sorted(themes_hit),
        media_highlights=media_highlights,
    ).model_dump()


# ===== N-12 记忆共享协议（Inter-Agent Memory Protocol v1.0）=====
# 标准化「记忆全景」bundle：让 agent 舰队（龙虾/爱马仕等）一次性拉取
# 某个用户的旅行记忆全景，无需逐个行程调用。schema 版本号内嵌，供
# 跨 agent 契约校验。坐标对外统一 WGS-84。

_MEMORY_BUNDLE_SCHEMA = "travel-memory.memory-bundle.v1"


def build_memory_overview(user_id: int) -> dict:
    """构建用户旅行记忆全景 bundle（N-12 核心入口）。

    返回 {"schema", "generated_at", "person", "overview", "trips",
          "persona", "footprint"}，是 agent 舰队理解「这个人去过哪里、
    是什么旅行人格」的一站式上下文。坐标统一 WGS-84。
    """
    from datetime import datetime, timezone

    user = db.get_user_by_id(user_id) or {"id": user_id, "is_admin": 0,
                                          "display_name": ""}
    trips = db.list_trips(user)

    # 逐行程聚合：主题 + 点数 + 媒体数
    trip_briefs = []
    n_points = 0
    n_media = 0
    n_photos = 0
    n_videos = 0
    all_dates = []
    for t in trips:
        m = build_memory(t["id"])
        if not m:
            trip_briefs.append({
                "id": t["id"], "name": t["name"],
                "start_date": t["start_date"], "end_date": t["end_date"],
                "status": t.get("status") or "planning",
                "n_points": 0, "n_media": 0, "themes": [],
            })
            continue
        s = m["summary"]
        n_points += s["n_points"]
        n_media += s["n_media"]
        n_photos += s["n_photos"]
        n_videos += s["n_videos"]
        if s["date_range"]:
            all_dates.append(s["date_range"])
        trip_briefs.append({
            "id": t["id"], "name": t["name"],
            "start_date": t["start_date"], "end_date": t["end_date"],
            "status": t.get("status") or "planning",
            "n_points": s["n_points"], "n_media": s["n_media"],
            "themes": m["themes"],
        })

    # 足迹范围（WGS-84）+ 唯一点数
    points = db.list_all_points(user)
    wgs_points = [(gcj02_to_wgs84(p["lng"], p["lat"])) for p in points]
    bounds = None
    if wgs_points:
        lngs = [x for x, _ in wgs_points]
        lats = [y for _, y in wgs_points]
        bounds = {
            "min_lng": round(min(lngs), 6), "max_lng": round(max(lngs), 6),
            "min_lat": round(min(lats), 6), "max_lat": round(max(lats), 6),
        }

    # 人格画像（偏好 + 跨行程联想，延迟 import 避免循环依赖）
    from .association import get_preferences, get_associations
    prefs = get_preferences(user_id)
    assoc = get_associations(user_id, limit=10)

    date_range = None
    if all_dates:
        all_dates.sort()
        date_range = f"{all_dates[0].split(' ~ ')[0]} ~ {all_dates[-1].split(' ~ ')[-1]}"

    return {
        "schema": _MEMORY_BUNDLE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "person": {"user_id": user_id, "display_name": user.get("display_name", "")},
        "overview": {
            "n_trips": len(trip_briefs),
            "n_points": n_points,
            "n_media": n_media,
            "n_photos": n_photos,
            "n_videos": n_videos,
            "date_range": date_range,
        },
        "trips": trip_briefs,
        "persona": {
            "preferences": prefs,
            "associations": assoc,
        },
        "footprint": {
            "n_unique_points": len(wgs_points),
            "bounds": bounds,
        },
    }


def memory_to_text(memory: dict) -> str:
    """把结构化记忆压成自然语言上下文（喂 LLM 用），控制 token。"""
    if not memory:
        return ""
    s = memory["summary"]
    lines = [
        f"行程「{s['name']}」({s['date_range'] or '日期未标注'})",
        f"共 {s['n_points']} 个地点、{s['n_media']} 张照片/视频"
        f"（照片 {s['n_photos']}、视频 {s['n_videos']}）",
    ]
    if memory.get("themes"):
        lines.append("主题：" + "、".join(memory["themes"]))
    lines.append("时间线：")
    for it in memory["timeline"]:
        seg = f"  - {it['date'] or '?'} {it['name']}"
        if it.get("desc"):
            seg += f"（{it['desc'][:40]}）"
        if it["media_n"]:
            seg += f" [{it['media_n']} 张媒体]"
        lines.append(seg)
    return "\n".join(lines)
