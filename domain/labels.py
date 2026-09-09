"""领域 API 层 — 点级城市/地标标签（N-13 ①）

解决 N-8 语义检索中「混合行程名（如『巴黎-西班牙』）导致点级召回粒度失真」
的数据质量问题。

核心思路：给每个点打「城市」标签，来源分两级——
1. 城市词直接命中（点 name/desc 里出现城市名，如「巴黎」「巴塞罗那」）
2. 地标→城市 映射（点 name 是地标而非城市，如「埃菲尔铁塔」→「巴黎」）

这样即使用户未来建「埃菲尔铁塔」「圣家堂」这种不含城市名的点，
问「我上次在巴黎」也能通过地标映射精准命中，不再依赖混合行程名的
模糊匹配。零外部依赖（高德 regeo 对海外坐标覆盖弱，不可靠，故弃用）。
"""
from __future__ import annotations

from .memory import _CITY_THEMES

# 城市 → 主题 反向索引（复用 memory.py 词典）
_CITY_TO_THEME: dict[str, str] = {}
for _theme, _cities in _CITY_THEMES.items():
    for _c in _cities:
        _CITY_TO_THEME[_c] = _theme

# 地标/别名 → 城市 映射（点 name 可能是地标而非城市名）
LANDMARK_CITY: dict[str, str] = {
    # 巴黎
    "埃菲尔铁塔": "巴黎", "卢浮宫": "巴黎", "凯旋门": "巴黎",
    "圣母院": "巴黎", "塞纳河": "巴黎", "香榭丽舍": "巴黎", "蒙马特": "巴黎",
    # 巴塞罗那
    "圣家堂": "巴塞罗那", "桂尔公园": "巴塞罗那", "古埃尔公园": "巴塞罗那",
    "米拉之家": "巴塞罗那", "巴特罗之家": "巴塞罗那", "兰布拉": "巴塞罗那",
    # 马德里
    "普拉多": "马德里", "太阳门": "马德里", "丽池公园": "马德里",
    # 塞维利亚
    "塞维利亚大教堂": "塞维利亚", "西班牙广场": "塞维利亚", "阿尔卡萨": "塞维利亚",
    # 格拉纳达
    "阿尔罕布拉宫": "格拉纳达", "赫内拉利费": "格拉纳达",
    # 托莱多 / 科尔多瓦 / 瓦伦西亚 / 龙达
    "托莱多大教堂": "托莱多", "清真寺大教堂": "科尔多瓦", "百花巷": "科尔多瓦",
    "艺术科学城": "瓦伦西亚", "新桥": "龙达",
    # 罗马 / 威尼斯 / 佛罗伦萨 / 米兰
    "斗兽场": "罗马", "梵蒂冈": "罗马", "万神殿": "罗马", "许愿池": "罗马",
    "圣马可": "威尼斯", "叹息桥": "威尼斯", "贡多拉": "威尼斯",
    "圣母百花": "佛罗伦萨", "乌菲兹": "佛罗伦萨", "大教堂": "佛罗伦萨",
    # 伦敦 / 布拉格 / 维也纳 / 布达佩斯
    "大本钟": "伦敦", "白金汉宫": "伦敦", "塔桥": "伦敦",
    "查理大桥": "布拉格", "美泉宫": "维也纳", "渔人堡": "布达佩斯",
    # 国内热门
    "兵马俑": "西安", "大雁塔": "西安", "城墙": "西安",
    "故宫": "北京", "长城": "北京", "天安门": "北京",
    "外滩": "上海", "东方明珠": "上海", "豫园": "上海",
    "鼓浪屿": "厦门", "栈桥": "青岛", "西湖": "杭州",
    "张家界": "张家界", "九寨沟": "九寨沟",
}


def extract_city(text: str) -> str | None:
    """从一段文本提取城市标签（城市词直接命中 > 地标→城市映射）。"""
    t = (text or "").strip()
    if not t:
        return None
    # 1) 城市词直接命中（优先，精确）
    for city in _CITY_TO_THEME:
        if city in t:
            return city
    # 2) 地标→城市映射（点 name 是地标）
    for landmark, city in LANDMARK_CITY.items():
        if landmark in t:
            return city
    return None


def label_point_city(name: str, description: str = "") -> dict:
    """给单个点打城市标签。返回 {city, source} 或 {}（无法识别）。

    优先从点 name 提取，其次 description（name 通常更精确，如「埃菲尔铁塔」）。
    """
    city = extract_city(name)
    source = "name"
    if not city and description:
        city = extract_city(description)
        source = "desc"
    if not city:
        return {}
    return {"city": city, "source": source}
