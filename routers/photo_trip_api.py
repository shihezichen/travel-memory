"""照片 → 轨迹 的 HTTP 接口。

三个动作：
  POST /api/photo-trip/preview    解析一批照片的 EXIF，规划轨迹（不写库，让用户先看）
  POST /api/photo-trip/create     确认后建行程 + 轨迹点 + 挂照片
  POST /api/trips/{id}/photo-trip/append  往已有行程追加照片，扩充轨迹

两个关键取舍：
- **preview 不写库**：逆地理编码有网络往返（每个不同地点一次），直接写库再让人
  撤销会留下一堆垃圾行程。先给预览、用户确认再建库，是唯一干净的做法。
- **无 GPS/无时间的照片进「待分配」池**：不丢弃，media 行建在一个名为
  「📷 待分配照片」的特殊轨迹点上，用户可以在 UI 里手工拖到别的点。

坐标系：EXIF GPS 是 WGS-84，入库统一 GCJ-02，转换走 domain.schemas.wgs84_to_gcj02。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

import db
import media as media_mod
import photo_trip
from domain import regeo as domain_regeo
from domain.schemas import wgs84_to_gcj02

# UPLOAD_DIR 由 media.py 定义（与 app.py 同一份BASE/uploads），直接复用，
# 不要从 config import —— config 里没有这个符号。
UPLOAD_DIR = media_mod.UPLOAD_DIR

router = APIRouter()

# 待分配照片挂载点用的特殊轨迹点名
ORPHAN_POINT_NAME = "📷 待分配照片"

# 单次导入上限，防止一次拖进来几千张把浏览器和服务端一起拖垮
MAX_BATCH = 500

# 视频扩展名。app.py 里有权威的 media_type_for()，但它定义在 app.py 里，
# 从这里 import app 会形成循环依赖（app 又 include_router 本模块），
# 所以照它的判定规则就地复刻一份，只用于「视频不参与轨迹生成」这一步。
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv",
              ".3gp", ".ts", ".m4a", ".mp3", ".wav"}


def _is_photo(filename: str) -> bool:
    return (Path(filename or "").suffix.lower() not in _VIDEO_EXT)


def _require_owner(request: Request, trip_id: int, what: str = "行程"):
    from app import current_user, require_owner
    require_owner(current_user(request), db.get_trip_owner_id(trip_id), what)


def _read_photo_exif(path: Path):
    """只解析照片，不落media 行。返回 dict，含 filename/exif_time/exif_lat/exif_lng。"""
    dt, lat, lng = media_mod.read_exif(path)
    return {"filename": path.name, "exif_time": dt,
            "exif_lat": lat, "exif_lng": lng}


def _parse_uploads(files: list[UploadFile]):
    """把上传的文件落盘 + 读 EXIF。返回 (rows, stored[(ext, raw)])。

    **只落盘不建 media 行** —— 建行需要 point_id，而轨迹点还没生成。
    真正的 media 行在 create/append 阶段才写。
    """
    rows, stored = [], []
    for f in files[:MAX_BATCH]:
        raw = f.file.read()
        if not raw:
            continue
        if not _is_photo(f.filename or "x"):
            # 视频不进轨迹生成：轨迹点靠照片的空间信息，视频没有 EXIF GPS。
            # 视频仍可事后挂到任意轨迹点上（走正常的 media 上传接口）。
            continue
        ext = Path(f.filename or "unknown").suffix.lower() or ".jpg"
        name = f"{uuid.uuid4().hex}{ext}"
        src = UPLOAD_DIR / name
        src.write_bytes(raw)
        row = _read_photo_exif(src)
        row["_src_path"] = str(src)
        rows.append(row)
        stored.append((name, str(src)))
    return rows, stored


def _ensure_orphan_point(trip_id: int, user):
    """建（或复用）「待分配照片」轨迹点。它不参与路线规划，坐标给 0。"""
    from app import db as _db
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT id FROM points WHERE trip_id=? AND name=?",
        (trip_id, ORPHAN_POINT_NAME)).fetchone()
    conn.close()
    if row:
        return row["id"]
    return _db.create_point(trip_id, ORPHAN_POINT_NAME, 0.0, 0.0, None, None,
                            "由照片生成轨迹时暂存缺少时间或位置的图片，可手工移到别的地点",
                            "line", 1)


def _attach_orphans(trip_id: int, orphans, stored_by_name: dict):
    """把无 GPS/无时间的照片建成 media，挂在「待分配」点上。"""
    if not orphans:
        return 0
    pid = _ensure_orphan_point(trip_id, None)
    n = 0
    for o in orphans:
        name = o["filename"]
        if name not in stored_by_name:
            continue
        src_path = Path(stored_by_name[name])
        try:
            thumb = media_mod.make_thumb(src_path, "photo")
            mid = db.add_media(pid, "photo", name, thumb,
                               o.get("exif_time"), o.get("exif_lat"),
                               o.get("exif_lng"))
            n += 1
        except Exception:
            continue
    return n


@router.post("/api/photo-trip/preview")
async def photo_trip_preview(request: Request,
                             files: list[UploadFile] = File(...)):
    """解析照片 → 规划轨迹。**不写库**，只返回蓝图给用户确认。"""
    rows, stored = _parse_uploads(files)
    if not rows:
        raise HTTPException(400, "没有可用的照片（仅支持带 EXIF 的照片文件）")

    plan = photo_trip.plan_trip(rows)

    # 逆地理：只对「成点」的代表坐标查，不查每张照片 —— 点数远少于照片数
    pts = plan["points"]
    names = []
    if pts:
        coords = [(p["wgs_lng"], p["wgs_lat"]) for p in pts]
        results = domain_regeo.regeo_many(coords)
        for p, r in zip(pts, results):
            if r:
                p["name"] = r["name"]
                p["coord_sys"] = "gcj02"
                p["gcj_lng"], p["gcj_lat"] = r["gcj02"]["lng"], r["gcj02"]["lat"]
            else:
                # 逆地理失败不阻断：坐标照存（自己转），名字用时间戳
                glng, glat = wgs84_to_gcj02(p["wgs_lng"], p["wgs_lat"])
                p["gcj_lng"], p["gcj_lat"] = round(glng, 6), round(glat, 6)
                p["coord_sys"] = "gcj02"
                p["named"] = False

    # 预览态的照片还没建 media 行，返回上传后的临时文件名供后续 create 使用
    plan["stored"] = [{"filename": n} for n, _ in stored]
    plan["named_count"] = sum(1 for p in pts if p.get("named", True))
    return {"plan": plan}


@router.post("/api/photo-trip/create")
async def photo_trip_create(request: Request,
                            trip_name: str = Form(...),
                            files: list[UploadFile] = File(...)):
    """确认后真正建库：行程 + 轨迹点 + 照片挂载 + 待分配池。"""
    from app import current_user
    user = current_user(request)
    if not trip_name.strip():
        raise HTTPException(400, "行程名不能为空")

    rows, stored = _parse_uploads(files)
    if not rows:
        raise HTTPException(400, "没有可用的照片")

    plan = photo_trip.plan_trip(rows)
    pts = plan["points"]

    names = []
    if pts:
        coords = [(p["wgs_lng"], p["wgs_lat"]) for p in pts]
        for p, r in zip(pts, domain_regeo.regeo_many(coords)):
            if r:
                p["name"] = r["name"]
                p["gcj_lng"], p["gcj_lat"] = r["gcj02"]["lng"], r["gcj02"]["lat"]
            else:
                glng, glat = wgs84_to_gcj02(p["wgs_lng"], p["wgs_lat"])
                p["gcj_lng"], p["gcj_lat"] = round(glng, 6), round(glat, 6)

    stats = plan["stats"]
    trip_id = db.create_trip(
        user, trip_name.strip(), stats["date_from"], stats["date_to"],
        f"由 {stats['photo_total']} 张照片自动生成", "planning")

    stored_by_name = {n: p for n, p in stored}
    for p in pts:
        pid = db.create_point(trip_id, p["name"], p["gcj_lat"], p["gcj_lng"],
                              p["date"], None,
                              f"自动生成 · {p['photo_count']} 张照片", "drive", 0)

    # 挂照片：按 (日期, 时间) 把每张照片归到对应的轨迹点
    _attach_photos_to_points(trip_id, plan, rows, stored)

    orphan_n = _attach_orphans(trip_id, plan["orphans"], stored_by_name)

    return {"trip_id": trip_id, "stats": stats, "orphan_count": orphan_n}


def _attach_photos_to_points(trip_id: int, plan, rows, stored):
    """把每张照片挂到它所属的轨迹点上。

    做法：重新按同样规则聚类一次，拿到 photo→cluster 的归属，再用 cluster 的
    idx 找到刚建的 point_id。不能复用 plan 里的 photo_id（那时还没建 media 行）。
    """
    usable, _ = photo_trip.classify_photos(rows)
    clusters = photo_trip.cluster_photos(usable)
    # clusters 的顺序与 plan["points"] 一致（都按 first_dt 排序）
    point_ids = _trip_point_ids(trip_id)
    if len(point_ids) != len(clusters):
        return 0

    stored_paths = {Path(p).name: p for _, p in stored}
    n = 0
    for cid, cluster in enumerate(clusters):
        pid = point_ids[cid]
        for ph in cluster["photos"]:
            src_path = Path(stored_paths.get(ph["filename"], ""))
            if not src_path.exists():
                continue
            try:
                thumb = media_mod.make_thumb(src_path, "photo")
                db.add_media(pid, "photo", src_path.name, thumb,
                             ph.get("exif_time"), ph.get("exif_lat"),
                             ph.get("exif_lng"))
                n += 1
            except Exception:
                continue
    return n


def _trip_point_ids(trip_id: int):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id FROM points WHERE trip_id=? ORDER BY "
        "COALESCE(start_date,''), sort_order, id", (trip_id,)).fetchall()
    conn.close()
    return [r["id"] for r in rows]


@router.post("/api/trips/{trip_id}/photo-trip/append")
async def photo_trip_append(request: Request, trip_id: int,
                            files: list[UploadFile] = File(...)):
    """往已有行程追加照片，扩充轨迹。新点按时间插到正确位置。"""
    _require_owner(request, trip_id)
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")

    rows, stored = _parse_uploads(files)
    if not rows:
        raise HTTPException(400, "没有可用的照片")

    plan = photo_trip.plan_trip(rows)
    pts = plan["points"]

    existing = _existing_media_keys(trip_id)
    added_points, skipped = 0, 0

    if pts:
        coords = [(p["wgs_lng"], p["wgs_lat"]) for p in pts]
        for p, r in zip(pts, domain_regeo.regeo_many(coords)):
            if r:
                p["name"] = r["name"]
                p["gcj_lng"], p["gcj_lat"] = r["gcj02"]["lng"], r["gcj02"]["lat"]
            else:
                glng, glat = wgs84_to_gcj02(p["wgs_lng"], p["wgs_lat"])
                p["gcj_lng"], p["gcj_lat"] = round(glng, 6), round(glat, 6)

        usable, _ = photo_trip.classify_photos(rows)
        clusters = photo_trip.cluster_photos(usable)

        stored_paths = {Path(p).name: p for _, p in stored}
        for cid, p in enumerate(pts):
            cluster = clusters[cid] if cid < len(clusters) else {"photos": []}
            fresh = [ph for ph in cluster["photos"]
                     if (ph.get("exif_time"), ph.get("exif_lat"), ph.get("exif_lng"))
                     not in existing]
            skipped += len(cluster["photos"]) - len(fresh)
            if not fresh:
                continue
            pid = db.create_point(trip_id, p["name"], p["gcj_lat"], p["gcj_lng"],
                                  p["date"], None,
                                  f"照片追加 · {len(fresh)} 张照片", "drive", 0)
            added_points += 1
            for ph in fresh:
                src_path = Path(stored_paths.get(ph["filename"], ""))
                if not src_path.exists():
                    continue
                try:
                    thumb = media_mod.make_thumb(src_path, "photo")
                    mid = db.add_media(pid, "photo", src_path.name, thumb,
                                       ph.get("exif_time"), ph.get("exif_lat"),
                                       ph.get("exif_lng"))
                    existing.add((ph.get("exif_time"), ph.get("exif_lat"),
                                  ph.get("exif_lng")))
                except Exception:
                    continue

    orphan_n = _attach_orphans(trip_id, plan["orphans"],
                              {Path(p).name: p for _, p in stored})

    return {"trip_id": trip_id, "stats": plan["stats"],
            "added_points": added_points, "skipped_photos": skipped,
            "orphan_count": orphan_n}


def _existing_media_keys(trip_id: int):
    """该行程已有媒体的 (exif_time, lat, lng) 集合，用于导入去重。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT m.exif_time, m.exif_lat, m.exif_lng FROM media m "
        "JOIN points p ON p.id=m.point_id WHERE p.trip_id=?", (trip_id,)).fetchall()
    conn.close()
    return {(r["exif_time"], r["exif_lat"], r["exif_lng"]) for r in rows}