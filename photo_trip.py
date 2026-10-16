"""照片 → 轨迹：把一批照片的 EXIF（时间 + GPS）重组成一条行程。

设计要点（都是被真实数据教育出来的）：

1. **聚合成点，不是每张照片一个点。** 同一个景点连拍 30 张，若一张一点，
   轨迹上会出现 30 个几乎重合的标记，缩放到全国尺度就是一团糊。按经纬度
   聚类（同组内任意两张距离 < CLUSTER_RADIUS_M）把邻近照片并成一个轨迹点。

2. **GPS 和时间缺一不可。** 只有 GPS 没时间的照片没法排顺序；只有时间没 GPS
   的（截图、微信转存）根本不知道在哪。这两类都不成点，但**照片本身要保留**——
   丢进行程级的「待分配」池子，用户可以事后手工挂到任意轨迹点上。

3. **坐标必须转 GCJ-02。** EXIF 的 GPS 是 WGS-84，库内统一存 GCJ-02（高德），
   不转会整体偏 300~600 米。转换在 regeo 模块里统一做。

4. **同一天的多个点共享日期边界。** points.start_date/end_date 是 TEXT，
   轨迹排序按 (start_date, sort_order)。生成时把一天里所有点的 start_date
   设成当天日期，end_date 留空 —— 前端按 start_date 分天渲染。

5. **增量追加不破坏已有轨迹。** 往已有行程加照片时，新点排在时间轴上对应的
   位置（db.create_point 的 sort_order 参数），而不是一股脑append 到末尾。

6. **幂等。** 同一张照片重复导入不会产生重复点：按 (filename, exif_time,
   exif_lat, exif_lng) 在该行程内查重。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

# 聚合半径：150 米。同一个景点/街区内的连拍会并成一点，跨街区则分开。
CLUSTER_RADIUS_M = 150.0

# 两个轨迹点时间上至少要隔这么久（小时），否则认为是同一地点的连续拍摄。
MIN_POINT_GAP_H = 1.5


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面距离（米）。"""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, a)))


def _parse_exif_time(raw: str):
    """EXIF 时间字符串 → datetime。read_exif 已把 ':' 换成 '-'，形如
    '2026-03-01T14:32:10'。也兼容 'YYYY-MM-DD HH:MM:SS' 和带时区的尾巴。"""
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "")
    # 去掉可能存在的毫秒/时区尾巴
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def classify_photos(media_rows):
    """把照片分成「可成点」与「待分配」两堆。

    media_rows: [{filename, exif_time, exif_lat, exif_lng, ...}, ...]
    返回 (usable, orphans)
      usable  —— 同时有 GPS 和时间，可参与轨迹生成
      orphans —— 缺 GPS 或缺时间，照片保留但不成点
    """
    usable, orphans = [], []
    for m in media_rows:
        dt = _parse_exif_time(m.get("exif_time"))
        lat, lng = m.get("exif_lat"), m.get("exif_lng")
        has_gps = lat is not None and lng is not None
        if dt and has_gps:
            usable.append({**m, "_dt": dt, "_lat": float(lat), "_lng": float(lng)})
        else:
            orphans.append({
                **m,
                "_reason": "no_time" if (has_gps and not dt) else
                           ("no_gps" if (dt and not has_gps) else "no_time_no_gps"),
            })
    usable.sort(key=lambda x: x["_dt"])
    return usable, orphans


def cluster_photos(usable, radius_m: float = CLUSTER_RADIUS_M,
                   min_gap_h: float = MIN_POINT_GAP_H):
    """按「空间邻近 + 时间接近」把照片聚成轨迹点。

    贪心聚类：按时间顺序遍历，当前照片与最近建立的簇中心距离在半径内**且**
    与簇内最后一张照片的时间差在 min_gap_h 内，就并入该簇，否则新建簇。
    两道条件缺一不可 —— 只有距离条件会把「上午和晚上都在西湖」并成一点；
    只有时间条件会把「当天市内快速移动」并成一点。
    """
    clusters = []          # [{center_lat, center_lng, first_dt, last_dt, photos:[]}]
    for p in usable:
        placed = False
        for c in clusters:
            d = _haversine_m(p["_lat"], p["_lng"], c["center_lat"], c["center_lng"])
            gap_h = abs((p["_dt"] - c["last_dt"]).total_seconds()) / 3600.0
            if d <= radius_m and gap_h <= min_gap_h:
                c["photos"].append(p)
                # 中心随新成员缓慢漂移，避免簇被单个离群点拉走
                n = len(c["photos"])
                c["center_lat"] += (p["_lat"] - c["center_lat"]) / n
                c["center_lng"] += (p["_lng"] - c["center_lng"]) / n
                if p["_dt"] > c["last_dt"]:
                    c["last_dt"] = p["_dt"]
                if p["_dt"] < c["first_dt"]:
                    c["first_dt"] = p["_dt"]
                placed = True
                break
        if not placed:
            clusters.append({
                "center_lat": p["_lat"], "center_lng": p["_lng"],
                "first_dt": p["_dt"], "last_dt": p["_dt"],
                "photos": [p],
            })
    clusters.sort(key=lambda c: c["first_dt"])
    return clusters


def name_from_cluster(cluster, fallback_index: int = 0):
    """轨迹点默认名。逆地理失败时用「MM-DD HH:MM」兜底，仍保留可辨认信息。"""
    dt = cluster["first_dt"]
    return dt.strftime("%m-%d %H:%M")


def plan_trip(media_rows, radius_m: float = CLUSTER_RADIUS_M):
    """规划：照片 → 轨迹点蓝图（含坐标、时间、命名）。**不写库**。

    返回 dict：
      points  —— [{lat, lng, date, time_label, photos:[media_id...], name, wgs_lat, wgs_lng}]
      orphans —— [{filename, reason, media_id}]
      stats   —— 汇总信息，给前端展示用
    """
    usable, orphans = classify_photos(media_rows)
    clusters = cluster_photos(usable, radius_m=radius_m)

    points = []
    for i, c in enumerate(clusters):
        dt = c["first_dt"]
        points.append({
            "idx": i,
            "wgs_lat": round(c["center_lat"], 6),
            "wgs_lng": round(c["center_lng"], 6),
            "date": dt.strftime("%Y-%m-%d"),
            "time_label": dt.strftime("%H:%M"),
            "datetime": dt.isoformat(),
            "name": name_from_cluster(c, i),
            "photo_count": len(c["photos"]),
            "photos": [p.get("id") for p in c["photos"] if p.get("id") is not None],
        })

    dates = sorted({p["date"] for p in points})
    stats = {
        "photo_total": len(media_rows),
        "usable": len(usable),
        "orphan": len(orphans),
        "point_count": len(points),
        "date_count": len(dates),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
    }
    return {"points": points, "orphans": orphans, "stats": stats}