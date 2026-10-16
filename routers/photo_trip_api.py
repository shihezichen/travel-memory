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
    """建（或复用）「待分配照片」轨迹点。

    这个点**不是一个真正的地点**：
      · 没有位置（坐标 0,0，只是个占位）
      · 不参与路线规划（前端靠 is_pending_pool 把它排除在地图与折线之外）
      · 只是给缺时间/缺位置的照片一个落脚处，等用户手工分配

    所以它标记 is_waypoint=1（不参与定线）**并且**名字固定 —— 前端按名字
    识别并从地图上剔除，改名了就失效，这也是为什么这里不把它做成可配置项。
    """
    from app import db as _db
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT id FROM points WHERE trip_id=? AND name=?",
        (trip_id, ORPHAN_POINT_NAME)).fetchone()
    conn.close()
    if row:
        return row["id"]
    return _db.create_point(trip_id, ORPHAN_POINT_NAME, 0.0, 0.0, None, None,
                            "待分配：这些照片缺少拍摄时间或位置，暂存在这里，"
                            "可以手工拖到别的地点",
                            "line", 1)


def _attach_orphans(trip_id: int, orphans, stored_by_name: dict):
    """把无 GPS/无时间的照片建成 media，挂在「待分配」点上。返回新增数量。"""
    added, _ = _attach_orphans_with_report(trip_id, orphans, stored_by_name,
                                           _existing_media_keys(trip_id))
    return added


def _attach_orphans_with_report(trip_id: int, orphans, stored_by_name: dict,
                                existing: set):
    """同上，但额外报告哪些是重复的。返回 (重复明细, 实际新增数)。"""
    if not orphans:
        return [], 0
    pid = _ensure_orphan_point(trip_id, None)
    n = 0
    dupes = []
    for o in orphans:
        name = o["filename"]
        if name not in stored_by_name:
            continue
        key = (o.get("exif_time"), o.get("exif_lat"), o.get("exif_lng"))
        if key in existing:
            dupes.append({
                "filename": name,
                "time": (o.get("exif_time") or "")[:16].replace("T", " "),
            })
            continue
        src_path = Path(stored_by_name[name])
        try:
            thumb = media_mod.make_thumb(src_path, "photo")
            mid = db.add_media(pid, "photo", name, thumb,
                               o.get("exif_time"), o.get("exif_lat"),
                               o.get("exif_lng"))
            existing.add(key)
            n += 1
        except Exception:
            continue
    return dupes, n


@router.post("/api/photo-trip/preview")
async def photo_trip_preview(request: Request,
                             files: list[UploadFile] = File(...),
                             trip_id: str = Form("")):
    """解析照片 → 规划轨迹。**不写库**，只返回蓝图让用户确认。

    2026-10-16 增加 `trip_id`：追加模式下把它一起传上来，服务端顺手比对
    「哪些照片这个行程已经有了」，把重复数在**确认之前**就告诉用户。
    之前只有 append 的响应里才有skipped_photos，用户点完按钮看到
    「已追加 0 个点」却不知道为什么，像是坏了。
    """
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

    # 预览态的照片还没建media 行，返回上传后的临时文件名供后续 create 使用
    plan["stored"] = [{"filename": n} for n, _ in stored]
    plan["named_count"] = sum(1 for p in pts if p.get("named", True))

    out = {"plan": plan}

    # 追加模式：顺便报重复（不写库，只是比对）
    if trip_id:
        try:
            tid = int(trip_id)
            _require_owner(request, tid)
            existing = _existing_media_keys(tid)
            dupes = []
            for r in rows:
                key = (r.get("exif_time"), r.get("exif_lat"), r.get("exif_lng"))
                if key in existing:
                    dupes.append({
                        "filename": r["filename"],
                        "time": (r.get("exif_time") or "")[:16].replace("T", " "),
                    })
            out["duplicate_count"] = len(dupes)
            out["checked_count"] = len(rows)
            out["duplicate_photos"] = dupes[:12]
        except Exception:
            # 预检失败不该阻断预览 —— 只是少一条提示而已
            out["duplicate_count"] = None

    return out


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
    """往已有行程追加照片，扩充轨迹。新点按时间插到正确位置。

    **去重要看得见**（2026-10-16 改）：原先返回 `skipped_photos` 一个数字，
    前端 toast 只说「已追加 N 个轨迹点」，用户看到 0 个点被加进去却没有任何
    解释，只能猜是不是坏了。现在返回结构化的重复清单（哪些照片、按什么判定），
    由前端明确告知「这些照片已存在，未重复上传」。

    去重键仍沿用 (exif_time, lat, lng)：同一张照片重复导入一定命中；同一个
    景点同一秒拍的两张不同照片会被误判成重复，这是刻意的取舍——宁可漏一张，
    也不要重复添加让相册出现重影。
    """
    _require_owner(request, trip_id)
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")

    rows, stored = _parse_uploads(files)
    if not rows:
        raise HTTPException(400, "没有可用的照片")

    plan = photo_trip.plan_trip(rows)
    pts = plan["points"]

    existing = _existing_media_keys(trip_id)
    added_points = 0
    added_photos = 0
    # 重复照片明细：[{filename, time_label}]，前端要逐条告知用户
    duplicates: list = []

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
            fresh = []
            for ph in cluster["photos"]:
                key = (ph.get("exif_time"), ph.get("exif_lat"), ph.get("exif_lng"))
                if key in existing:
                    duplicates.append({
                        "filename": ph["filename"],
                        "time": ph.get("_dt").strftime("%Y-%m-%d %H:%M")
                                if ph.get("_dt") else "",
                    })
                    continue
                fresh.append(ph)
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
                    added_photos += 1
                    existing.add((ph.get("exif_time"), ph.get("exif_lat"),
                                  ph.get("exif_lng")))
                except Exception:
                    continue

    orphan_dupes, orphan_added = _attach_orphans_with_report(
        trip_id, plan["orphans"], {Path(p).name: p for _, p in stored}, existing)

    return {
        "trip_id": trip_id,
        "stats": plan["stats"],
        "added_points": added_points,
        "added_photos": added_photos,
        "duplicate_photos": duplicates + orphan_dupes,
        "orphan_count": orphan_added,
        # 全量都没进去时给一个明确标志，前端据此显示「全部已存在」而不是
        # 「已追加 0 个点」这种容易被当成出错的话
        "nothing_added": added_points == 0 and orphan_added == 0
                          and bool(duplicates or orphan_dupes),
    }


def _existing_media_keys(trip_id: int):
    """该行程已有媒体的 (exif_time, lat, lng) 集合，用于导入去重。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT m.exif_time, m.exif_lat, m.exif_lng FROM media m "
        "JOIN points p ON p.id=m.point_id WHERE p.trip_id=?", (trip_id,)).fetchall()
    conn.close()
    return {(r["exif_time"], r["exif_lat"], r["exif_lng"]) for r in rows}