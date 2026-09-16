"""开放平台：领域 API（/api/ai/*）+ MCP 远程端点（/mcp）。

从 app.py 原样搬过来的，路由定义一字未改 —— 只把 `@app.` 换成了 `@router.`。
这批路由和 Web UI 那批完全不相交（前端一次都没调用过 /api/ai/*）：
它们走 API Key + Bearer 鉴权，有自己的限流 / 审计 / 幂等，负载特性也不同
（Web UI 是毫秒级查询，这里是分钟级 LLM 调用）。分开放便于以后单独加中间件、
单独限流，甚至单独拆进程。

注意：UPLOAD_DIR 在本模块也是模块级全局，测试靠 monkeypatch 重绑它做目录隔离
（见 tests/conftest.py），所以别改成从别处 import 进来的常量。
"""
import base64
import json
import re
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
from routers import deps
import agent_media
import media as media_mod
import media_purge
import mcp_http
import transcode

from domain import memory as domain_memory
from domain import association as domain_assoc
from domain import generation as domain_gen
from domain import llm_provider
from domain import search as domain_search
from domain import recall as domain_recall
from domain import trigger as domain_trigger
from domain import meta as domain_meta
from domain import pattern as domain_pattern
from domain import recommend as domain_recommend
from domain import unfinished as domain_unfinished
from domain import wrapped as domain_wrapped
from domain import relation as domain_relation
from domain import reinterpret as domain_reinterpret
from domain import biography as domain_biography
from domain import timetravel as domain_timetravel
from domain import token_auth
from domain import schemas as domain_schemas
from domain import geocode as domain_geocode
from domain import rate_limit

BASE = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE / "uploads"

router = APIRouter()


# ============================================================
# 领域 API 层（/api/ai/*）—— 记忆 / 联想 / 生成 + API Key 认证
# ============================================================

def _api_key_auth(request: Request, required_scope: str):
    """API Key 鉴权：成功返回 api_key dict，失败抛 401。"""
    auth = request.headers.get("Authorization", "")
    key, err = token_auth.authorize(auth, required_scope)
    if not key:
        msg = {"invalid_api_key": "API Key 无效或已过期/吊销",
               "insufficient_scope": f"API Key 缺少 {required_scope} 权限"}[err]
        raise HTTPException(401, msg)
    _enforce_rate_limit(request, key, required_scope)
    # 把来源 IP 挂在 key 上，_audit 就能带上它而不用给几十个调用点都加参数。
    key["client_ip"] = deps.client_ip(request)
    return key


def _enforce_rate_limit(request: Request, key, required_scope: str):
    """P0-1 限流：Key 维度（读/写分级）+ IP 维度兜底。超限抛 429。"""
    allowed, retry = rate_limit.check_key(key["id"], required_scope)
    if not allowed:
        raise HTTPException(429, f"请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    ip = request.client.host if request.client else ""
    allowed, retry = rate_limit.check_ip(ip)
    if not allowed:
        raise HTTPException(429, f"请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})


# ---------- P0-2 写操作幂等（client_request_id 去重） ----------
def _idempotency_check(key, client_request_id: str, action: str):
    """写操作幂等守卫：client_request_id 非空时去重。

    返回 None（首次执行，继续）或 dict（幂等命中，直接返回缓存结果）。
    并发冲突（pending 未完成）抛 409。
    """
    if not client_request_id:
        return None
    if db.idempotency_claim(key["id"], client_request_id, action):
        return None  # 首次，抢占成功
    rec = db.idempotency_get(key["id"], client_request_id)
    if rec and rec.get("status") == "completed" and rec.get("result"):
        try:
            return json.loads(rec["result"])
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(409, "幂等冲突：请求正在处理中，请勿重复提交")
    raise HTTPException(409, "幂等冲突：相同 client_request_id 正在处理中，请勿重复提交")


def _idempotency_save(key, client_request_id: str, result: dict):
    """执行成功后缓存结果，供幂等命中时原样返回。"""
    if client_request_id:
        db.idempotency_complete(key["id"], client_request_id,
                                json.dumps(result, ensure_ascii=False))


def _idempotency_fail(key, client_request_id: str):
    """执行失败后丢弃 pending 记录，允许客户端修正参数后重试。"""
    if client_request_id:
        db.idempotency_discard(key["id"], client_request_id)


def _audit(api_key, action, status, detail=None, tokens_used=0, latency_ms=None):
    db.add_audit(
        api_key_id=api_key["id"] if api_key else None,
        user_id=api_key["user_id"] if api_key else None,
        action=action, method="AI", path=action, scope=api_key["scope"] if api_key else "",
        status=status, detail=detail, tokens_used=tokens_used, latency_ms=latency_ms,
        client_ip=api_key.get("client_ip") if api_key else None,
    )


@router.get("/api/ai/trips")
def ai_list_trips(request: Request):
    """列出当前 API Key 所属用户的行程（简表），供 agent 枚举资源。"""
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    trips = db.list_trips(user or {"id": key["user_id"], "is_admin": 0})
    brief = [
        {"id": t["id"], "name": t["name"], "start_date": t["start_date"],
         "end_date": t["end_date"], "description": t["description"],
         "status": t.get("status") or "planning"}
        for t in trips
    ]
    _audit(key, "trips.list", "ok", detail=f"n={len(brief)}")
    return {"trips": brief}


@router.get("/api/ai/memory")
def ai_memory_overview(request: Request):
    """N-12 记忆共享协议：返回用户旅行记忆全景 bundle（标准化 schema）。

    让外部 agent（爱马仕总控等）一次性拉取「这个人去过哪里、是什么
    旅行人格」的一站式上下文，无需逐个行程调用。坐标统一 WGS-84。
    read scope；纯聚合无 LLM 调用，不受生成限额约束。
    """
    key = _api_key_auth(request, "read")
    try:
        result = domain_memory.build_memory_overview(key["user_id"])
        _audit(key, "memory.overview", "ok",
               detail=f"trips={result['overview']['n_trips']} "
                      f"points={result['overview']['n_points']}")
        return result
    except Exception as e:
        _audit(key, "memory.overview", "error", detail=str(e))
        raise HTTPException(500, f"记忆全景构建失败：{e}")


@router.get("/api/ai/patterns")
def ai_discover_patterns(request: Request):
    """N-6 跨旅程模式发现：行为聚类，产出「旅行画像」可分享卡片。

    read scope，纯规则聚类（零 token，不调 LLM）。发现用户自己都未必
    意识到的模式：重访地点、季节偏好、地点类型偏好、出行节奏。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_pattern.discover_patterns(user, db)
    _audit(key, "patterns.discover", "ok",
           detail=f"trips={result['trip_count']} points={result['point_count']}")
    return result


@router.get("/api/ai/unfinished")
def ai_unfinished(request: Request):
    """N-21 未完成旅程识别：把「没说出口的遗憾」变成前进动力。

    规则层（零 token）扫描三类信号——搁置的计划（planning > 90 天）、
    没归档的旅程（traveling 超期）、计划过没去成的地方（planning 城市
    从未在 completed 行程出现）；有信号时 DeepSeek 生成一句
    「遗憾→动力」提示语（可降级规则模板）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_unfinished.find_unfinished(user, db)
    _audit(key, "unfinished.scan", "ok",
           detail=f"stalled={len(result['stalled_plans'])} "
                  f"unclosed={len(result['unclosed_trips'])} "
                  f"unfulfilled={len(result['unfulfilled_destinations'])}")
    return result


@router.get("/api/ai/wrapped")
def ai_wrapped(request: Request, year: int | None = None):
    """N-11 年度旅行报告：把一年的足迹折叠成一张卡片。

    规则层（零 token）聚合该年行程/地点/城市/里程/媒体/情绪统计；LLM 层
    生成有温度的年度叙事（可降级规则模板）。机制先行：任何年份可回溯，
    数据不足输出轻量版；12 月后自动可出首份真实年报。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_wrapped.annual_report(user, year, db)
    _audit(key, "wrapped.annual", "ok",
           detail=f"year={result['year']} trips={result['stats']['trip_count']} "
                  f"points={result['stats']['point_count']}")
    return result


@router.get("/api/ai/shared")
def ai_shared(request: Request, with_username: str = ""):
    """N-14 共同记忆 v1：两个人的足迹叠在一起的故事。

    规则层（零 token）合并两用户足迹——共同城市、时间重叠（同日/同月）、
    各自独享目的地；LLM 层生成「共同记忆」叙事（可降级规则模板）。
    隐私边界：只输出城市级+日期级信息。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    if not with_username:
        raise HTTPException(422, "缺少参数 with_username")
    try:
        result = domain_relation.shared_memory(user, with_username, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _audit(key, "relation.shared", "ok",
           detail=f"with={with_username} common={len(result['common_cities'])}")
    return result


@router.get("/api/ai/trips/{trip_id}/reinterpret")
def ai_reinterpret(request: Request, trip_id: int, lens: str = "成长"):
    """N-16 记忆再阐释：给过去的旅程换一个视角重新讲述。

    规则层提取行程原始摘要+真实地点清单（防 LLM 编造地名）；LLM 层按
    lens（成长/遗憾/同行/幽默）重讲，可降级规则模板。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    if lens not in domain_reinterpret.LENSES:
        raise HTTPException(400, f"lens 必须是 {'/'.join(domain_reinterpret.LENSES)} 之一")
    try:
        result = domain_reinterpret.reinterpret_trip(user, trip_id, lens, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    _audit(key, "reinterpret.run", "ok",
           detail=f"trip={trip_id} lens={result['lens']}")
    return result


@router.get("/api/ai/biography")
def ai_biography(request: Request):
    """N-18 旅行人生传记：把一生的足迹写成一本分章节的书。

    规则层按时间排序全部已成行行程生成章节骨架 + 全局统计；LLM 逐章
    生成纪实体散文（无地点清单的行程降级规则模板防编造）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    result = domain_biography.biography(user, db)
    _audit(key, "biography.generate", "ok",
           detail=f"trips={result['stats']['trip_count']} "
                  f"chapters={len(result['chapters'])}")
    return result


@router.get("/api/ai/timetravel")
def ai_timetravel(request: Request, mode: str = "", date: str = "",
                  trip_id: int | None = None):
    """N-20 时间旅行：rewind 回溯（date=YYYY-MM-DD 当天足迹快照）/
    preview 预演（date 当天规划行程）/ counterfactual 反事实
    （trip_id 行程的「如果换一条路」推演，带【虚构推演】标注）。read scope。
    """
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    try:
        result = domain_timetravel.time_travel(
            user, mode, db, date=date or None, trip_id=trip_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(key, "timetravel.run", "ok",
           detail=f"mode={result['mode']} date={date or '-'} trip={trip_id or '-'}")
    return result


@router.post("/api/ai/recommend")
def ai_recommend(request: Request, count: int = Form(5), style: str = Form("")):
    """P2-2 行程推荐：基于历史足迹推荐「还没去过、但可能喜欢」的目的地。

    每个推荐带可解释理由（「因为你上次在海边城市最开心」）。是 N-15
    （人格驱动的下一个目的地）的地基版——不依赖尚未完成的 N-5 人格库。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    count = max(1, min(int(count), 6))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"推荐过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_recommend.recommend_destinations(key["user_id"], count, style)
        _audit(key, "memory.recommend", "ok",
               detail=f"n={len(result['recommendations'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.recommend", "error", detail=str(e))
        raise HTTPException(500, f"推荐失败：{e}")


# ===== N-17 记忆可携带/数字遗产：全量导出 + 全量导入 =====
# 数据主权叙事（N-1）的技术闭环：用户数据不锁定在平台，可随时全量
# 带走（export）、迁移到另一账号（import）、传承给继承人（export+import）。
# schema: travel-memory.full-bundle.v1 —— 媒体文件二进制不打包，仅导出
# 元数据（filename/exif 等），文件本体需单独迁移（诚实告知，不假装完美）。

_FULL_BUNDLE_SCHEMA = "travel-memory.full-bundle.v1"


def _point_to_bundle(p: dict) -> dict:
    """把库内点（GCJ-02）转成可携带 bundle 点（WGS-84），带情绪元数据 + 媒体元数据。"""
    try:
        wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
    except (TypeError, ValueError):
        wlng, wlat = p["lng"], p["lat"]
    meta = db.get_point_meta(p["id"])
    out = {
        "name": p["name"], "lat": wlat, "lng": wlng,
        "start_date": p["start_date"], "end_date": p["end_date"],
        "description": p["description"], "route_mode": p["route_mode"] or "drive",
        "is_waypoint": int(p["is_waypoint"] or 0),
    }
    if meta:
        out["meta"] = {k: meta[k] for k in
                       ("mood", "weather", "scene", "time_of_day", "season", "meta_json")
                       if meta.get(k)}
    media = db.list_media(p["id"])
    if media:
        out["media"] = [{
            "type": m["type"], "filename": m["filename"],
            "thumb_filename": m["thumb_filename"], "exif_time": m["exif_time"],
            "exif_lat": m["exif_lat"], "exif_lng": m["exif_lng"],
            # 带有效期的签名链接。之前这里只给裸文件名，Agent 根本拼不出能用的
            # 地址 —— 这正是「Agent 分享香格里拉照片时说没接口」的根因之一。
            "url": agent_media.urls_for(f"/uploads/{m['filename']}"),
            "download_url": agent_media.urls_for(
                f"/uploads/{m['filename']}", download=True,
                filename=m["filename"]),
            "thumb_url": (agent_media.urls_for(
                f"/uploads/thumbs/{m['thumb_filename']}")
                if m.get("thumb_filename") else None),
        } for m in media]
    return out


@router.get("/api/ai/memory/export")
def ai_memory_export(request: Request):
    """N-17 全量打包：导出当前用户全部旅行记忆（行程+点+情绪元数据+媒体元数据+人格画像+联想）。

    read scope，纯聚合无 LLM。返回可导入/迁移/传承的 full-bundle.v1。
    """
    from datetime import datetime, timezone
    key = _api_key_auth(request, "read")
    user = db.get_user_by_id(key["user_id"])
    trips = db.list_trips(user)

    bundle_trips = []
    for t in trips:
        bundle_trips.append({
            "name": t["name"], "start_date": t["start_date"],
            "end_date": t["end_date"], "description": t["description"],
            "status": t.get("status") or "planning",
            "points": [_point_to_bundle(p) for p in db.list_points(t["id"])],
        })

    prefs = db.list_prefs(key["user_id"])
    assoc = domain_assoc.get_associations(key["user_id"], limit=50)

    payload = {
        "schema": _FULL_BUNDLE_SCHEMA,
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "person": {"display_name": user.get("display_name", "") if user else ""},
        "note": "媒体文件二进制未打包，仅导出元数据（filename/exif）；文件本体需单独迁移。",
        "trips": bundle_trips,
        "persona": {"preferences": prefs, "associations": assoc},
    }
    n_points = sum(len(t["points"]) for t in bundle_trips)
    _audit(key, "memory.export", "ok",
           detail=f"trips={len(bundle_trips)} points={n_points}")
    return payload


@router.post("/api/ai/memory/import")
async def ai_memory_import(request: Request, client_request_id: str = None):
    """N-17 全量导入：把 full-bundle.v1 打包恢复到当前账号（迁移/传承/灾备恢复）。

    write scope。坐标 WGS-84 → 库内 GCJ-02。媒体仅导入元数据。
    幂等：client_request_id 走 query 参数（body 为 JSON bundle，避免 form/json 流冲突）。
    """
    key = _api_key_auth(request, "write")
    client_request_id = client_request_id or ""

    # 幂等编排
    if client_request_id:
        idem = _idempotency_check(key, client_request_id, "memory.import")
        if idem is not None:
            return idem

    try:
        body = await request.json()
    except Exception:
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, "无法解析 JSON（需为 full-bundle.v1 导出格式）")

    if not isinstance(body, dict) or body.get("schema") != _FULL_BUNDLE_SCHEMA:
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, f"不支持的 bundle schema（应为 {_FULL_BUNDLE_SCHEMA}）")

    trips = body.get("trips") or []
    if not isinstance(trips, list):
        if client_request_id:
            _idempotency_fail(key, client_request_id)
        raise HTTPException(400, "trips 必须是数组")

    user = db.get_user_by_id(key["user_id"])
    n_trips = n_points = n_media = 0
    for t in trips:
        if not isinstance(t, dict):
            continue
        name = (t.get("name") or "").strip()
        if not name:
            continue
        status = t.get("status")
        if status not in ("planning", "traveling", "completed", "archived"):
            status = "planning"
        tid = db.create_trip(user, name, t.get("start_date"), t.get("end_date"),
                             t.get("description") or "", status)
        n_trips += 1

        for p in t.get("points") or []:
            if not isinstance(p, dict):
                continue
            pname = (p.get("name") or "").strip()
            if not pname:
                continue
            # WGS-84 → 库内 GCJ-02
            try:
                glng, glat = domain_schemas.wgs84_to_gcj02(
                    float(p["lng"]), float(p["lat"]))
            except (TypeError, ValueError, KeyError):
                glng, glat = p.get("lng"), p.get("lat")
            pid = db.create_point(tid, pname, glat, glng, p.get("start_date"),
                                  p.get("end_date"), p.get("description") or "",
                                  p.get("route_mode") or "drive",
                                  int(p.get("is_waypoint") or 0))
            n_points += 1

            meta = p.get("meta")
            if isinstance(meta, dict):
                db.upsert_point_meta(
                    pid, mood=meta.get("mood"), weather=meta.get("weather"),
                    scene=meta.get("scene"), time_of_day=meta.get("time_of_day"),
                    season=meta.get("season"), meta_json=meta.get("meta_json"),
                    source=meta.get("source") or "import")

            for m in p.get("media") or []:
                if not isinstance(m, dict):
                    continue
                db.add_media(pid, m.get("type") or "photo", m.get("filename") or "",
                             m.get("thumb_filename"), m.get("exif_time"),
                             m.get("exif_lat"), m.get("exif_lng"))
                n_media += 1

    # 人格偏好导入（strength 经 upsert 重新累加，evidence 从 1 起，可接受失真）
    for pr in (body.get("persona") or {}).get("preferences") or []:
        if not isinstance(pr, dict):
            continue
        k = pr.get("pref_key") or pr.get("key")
        if not k:
            continue
        db.upsert_pref(key["user_id"], k, pr.get("pref_value") or pr.get("value"))

    result = {"imported_trips": n_trips, "imported_points": n_points,
              "imported_media": n_media,
              "note": "媒体文件本体未迁移，仅元数据已导入；文件需另行上传。"}
    if client_request_id:
        _idempotency_save(key, client_request_id, result)
    _audit(key, "memory.import", "ok",
           detail=f"trips={n_trips} points={n_points} media={n_media}")
    return result


@router.get("/api/ai/trips/{trip_id}/media")
def ai_trip_media(request: Request, trip_id: int, limit: int = 50,
                  offset: int = 0):
    """导出整个行程的媒体（read scope），**分页**返回带签名的下载链接。

    为什么分页而不是一次性全给：一条行程上千张照片很正常，一次性返回既慢
    又会把调用方（Agent 的上下文窗口）撑爆 —— 你提的「不能让人等十几分钟」
    说的就是这个。这里每页默认 50 条，调用方按 total 自己翻页，翻到哪用哪。

    链接是签名 + 24 小时有效期的临时链，**不需要服务端打包**：不用生成 zip、
    不用等任务队列，请求即返回。这也是 A 方案相比「异步导出任务」更简单的地方。
    """
    key = _api_key_auth(request, "read")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    limit = max(1, min(int(limit), 200))          # 夹紧，防一次拉爆
    offset = max(0, int(offset))
    rows = db.list_trip_media(trip_id, limit=limit, offset=offset)
    total = db.count_trip_media(trip_id)
    _audit(key, "media.trip_export", "ok",
           detail=f"trip={trip_id} offset={offset} n={len(rows)}")
    return {
        "trip_id": trip_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "expires_in": agent_media.AGENT_URL_TTL,
        "note": "链接为 24 小时有效的签名 URL，过期后重新调用本接口获取。",
        "items": [dict(agent_media.enrich_media(m),
                       point_id=m["point_id"], point_name=m.get("point_name"),
                       point_date=m.get("point_date")) for m in rows],
    }


@router.get("/api/ai/trips/{trip_id}/generations")
def ai_list_generations(request: Request, trip_id: int, kind: str = None, limit: int = 20):
    """查询行程的 AI 生成历史（P1-2 落库后按行程追溯）。read scope。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    _audit(key, "trips.generations", "ok", detail=f"trip={trip_id}")
    return {"generations": db.list_generations(trip_id, kind, min(limit, 100))}


@router.get("/api/ai/trips/{trip_id}/memory")
def ai_get_memory(request: Request, trip_id: int):
    key = _api_key_auth(request, "read")
    # 归属校验。这一行原来漏了 —— 同目录另外 19 个按 trip_id 取数的路由都有，
    # 只有这个没有，任何 read scope 的 Key 遍历 trip_id 就能读到别人整条行程
    # （行程名、日期、每个点的名称/坐标/描述）。实测已确认可越权读取。
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    mem = domain_memory.build_memory(trip_id)
    if not mem:
        raise HTTPException(404, "行程不存在")
    _audit(key, "memory.get", "ok")
    return mem


@router.get("/api/ai/associations")
def ai_get_associations(request: Request, trip_id: int = None, limit: int = 5):
    key = _api_key_auth(request, "read")
    result = domain_assoc.get_associations(key["user_id"], trip_id=trip_id, limit=limit)
    _audit(key, "association.get", "ok", detail=f"n={len(result)}")
    return {"associations": result}


@router.get("/api/ai/preferences")
def ai_get_preferences(request: Request):
    key = _api_key_auth(request, "read")
    # 惰性沉淀：先幂等派生偏好（单行程也沉淀），再返回
    added = domain_assoc.derive_preferences(key["user_id"])
    result = domain_assoc.get_preferences(key["user_id"])
    _audit(key, "preferences.get", "ok", detail=f"n={len(result)} new={added}")
    return {"preferences": result, "newly_derived": added}


@router.post("/api/ai/trips/{trip_id}/profile")
def ai_extract_profile(request: Request, trip_id: int):
    """N-5 旅行人格画像库（LLM 档）：从行程抽取细粒度偏好，upsert 到 user_prefs。

    与规则档（theme/season，derive_preferences）互补：规则档幂等惰性沉淀，
    本端点显式触发 DeepSeek 抽取（目的地类型/活动/时间/住宿/消费/拍照/饮食）。
    偏好 key 用 `llm:{类别}` 前缀，与规则档 `theme:`/`season:` 区分。
    """
    key = _api_key_auth(request, "read")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    # LLM 抽取同样受生成限额约束（最贵操作）
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"抽取请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_gen.extract_profile(trip_id)
        # 同类别的多值合并（value 顿号连接，strength 取均值）
        merged = {}
        for p in result["prefs"]:
            cat = p["key"]
            m = merged.setdefault(cat, {"values": [], "strength": 0.0, "n": 0})
            m["values"].append(p["value"])
            m["strength"] += p["strength"]
            m["n"] += 1
        for cat, m in merged.items():
            val = "、".join(m["values"])
            s = m["strength"] / m["n"]
            db.upsert_pref(key["user_id"], f"llm:{cat}", val, strength_delta=s)
        added = len(merged)
        _audit(key, "profile.extract", "ok", detail=f"trip={trip_id} new={added}",
               tokens_used=result["tokens_used"], latency_ms=int((time.time() - t0) * 1000))
        return {"trip_id": trip_id, "added": added, "prefs": result["prefs"],
                "tokens_used": result["tokens_used"]}
    except ValueError as e:
        _audit(key, "profile.extract", "error", detail=str(e))
        raise HTTPException(404, str(e))
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "profile.extract", "error", detail=str(e))
        raise HTTPException(500, f"偏好抽取失败：{e}")


@router.post("/api/ai/points/{point_id}/meta")
def ai_extract_point_meta(request: Request, point_id: int):
    """N-7：单点情绪/场景元数据采集（LLM 抽取 + 落 point_meta）。read scope。"""
    key = _api_key_auth(request, "read")
    point = db.get_point(point_id)
    if not point:
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    trip = db.get_trip(point["trip_id"])
    pd = dict(point)
    pd["trip_name"] = trip["name"] if trip else ""
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"抽取请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        meta = domain_meta.extract_point_meta(pd)
        db.upsert_point_meta(point_id, mood=meta["mood"], weather=meta["weather"],
                             scene=meta["scene"], time_of_day=meta["time_of_day"],
                             season=meta["season"], source="llm")
        _audit(key, "meta.extract", "ok", detail=f"point={point_id}",
               tokens_used=meta["tokens_used"], latency_ms=int((time.time() - t0) * 1000))
        return {"point_id": point_id,
                "meta": {k: meta[k] for k in ("mood", "weather", "scene",
                                              "time_of_day", "season")},
                "tokens_used": meta["tokens_used"]}
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "meta.extract", "error", detail=str(e))
        raise HTTPException(500, f"元数据抽取失败：{e}")


@router.post("/api/ai/trips/{trip_id}/meta")
def ai_extract_trip_meta(request: Request, trip_id: int, max_points: int = Form(50)):
    """N-7：整行程所有点的元数据批量采集（串行 LLM，逐点占生成限额）。read scope。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(403, "无权访问该行程")
    points = db.list_points(trip_id)
    if not points:
        return {"trip_id": trip_id, "total": 0, "processed": 0,
                "errors": [], "tokens_used": 0, "results": []}
    trip = db.get_trip(trip_id)
    trip_name = trip["name"] if trip else ""
    max_points = max(1, min(int(max_points or 50), 200))
    points = points[:max_points]

    results, errors = [], []
    total_tokens = 0
    t0 = time.time()
    for p in points:
        allowed, retry = rate_limit.check_generate(key["id"])
        if not allowed:
            errors.append({"point_id": p["id"], "name": p["name"],
                           "error": f"生成限额已耗尽，请 {retry}s 后重试"})
            break
        pd = dict(p)
        pd["trip_name"] = trip_name
        try:
            meta = domain_meta.extract_point_meta(pd)
            db.upsert_point_meta(p["id"], mood=meta["mood"], weather=meta["weather"],
                                 scene=meta["scene"], time_of_day=meta["time_of_day"],
                                 season=meta["season"], source="llm")
            total_tokens += meta["tokens_used"]
            results.append({"point_id": p["id"], "name": p["name"],
                            "meta": {k: meta[k] for k in ("mood", "weather", "scene",
                                                          "time_of_day", "season")}})
        except llm_provider.LLMBusy:
            # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
            raise
        except Exception as e:
            errors.append({"point_id": p["id"], "name": p["name"], "error": str(e)})
    _audit(key, "meta.extract_trip", "ok",
           detail=f"trip={trip_id} processed={len(results)} errors={len(errors)}",
           tokens_used=total_tokens, latency_ms=int((time.time() - t0) * 1000))
    return {"trip_id": trip_id, "total": len(points), "processed": len(results),
            "errors": errors, "tokens_used": total_tokens, "results": results}


@router.get("/api/ai/trips/{trip_id}/meta")
def ai_get_trip_meta(request: Request, trip_id: int):
    """N-7：查询行程下已采集的点级元数据（纯查询，不占生成限额）。read scope。

    返回坐标统一 WGS-84（库内 GCJ-02），meta_json 解析为对象。
    """
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    metas = db.list_trip_meta(trip_id)
    out = []
    for m in metas:
        m = dict(m)
        if m.get("meta_json"):
            try:
                m["meta_json"] = json.loads(m["meta_json"])
            except Exception:
                pass
        if m.get("lat") is not None and m.get("lng") is not None:
            wgs_lng, wgs_lat = domain_schemas.gcj02_to_wgs84(m["lng"], m["lat"])
            m["lng"], m["lat"] = round(wgs_lng, 6), round(wgs_lat, 6)
        out.append(m)
    _audit(key, "meta.list", "ok", detail=f"trip={trip_id} n={len(out)}")
    return {"trip_id": trip_id, "count": len(out), "meta": out}


@router.post("/api/ai/search")
def ai_search(request: Request, query: str = Form(""), limit: int = Form(5)):
    """N-8 记忆语义检索：自然语言查询旅行记忆，供外部 agent 跨 agent 调用。

    「我去过哪些海边？」「哪里拍过日落？」「去年这个时候我在哪儿？」
    两段式：规则召回（零 token）+ DeepSeek 语义精排（可降级）。
    read scope；LLM 精排受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    if not query or not query.strip():
        raise HTTPException(400, "缺少 query")
    query = query.strip()
    limit = max(1, min(int(limit), 10))
    # LLM 精排受生成限额约束
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"检索请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_search.semantic_search(key["user_id"], query, limit)
        _audit(key, "memory.search", "ok",
               detail=f"q={query[:40]} n={len(result['matches'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.search", "error", detail=str(e))
        raise HTTPException(500, f"检索失败：{e}")


@router.post("/api/ai/ask")
def ai_ask(request: Request, question: str = Form(""), limit: int = Form(5)):
    """N-9 记忆可对话：自然语言「问」自己的旅行记忆，返回一段对话式回答。

    「我上次在巴黎吃的小馆子叫什么？」「我是不是每次去海边都住海景房？」
    建立在 N-8 语义检索之上：先检索匹配记忆，再用 LLM 组织成有温度的回答。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    if not question or not question.strip():
        raise HTTPException(400, "缺少 question")
    question = question.strip()
    limit = max(1, min(int(limit), 10))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"提问过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_search.answer_question(key["user_id"], question, limit)
        _audit(key, "memory.ask", "ok",
               detail=f"q={question[:40]} n={len(result['matches'])}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.ask", "error", detail=str(e))
        raise HTTPException(500, f"问答失败：{e}")


@router.get("/api/ai/recall")
def ai_recall(request: Request, lookahead_days: int = 7):
    """N-2 记忆唤醒（手动触发版）：今天/未来 N 天内的周年纪念 + LLM 唤醒语。

    「去年今天你在巴黎」—— 找用户「去过」记忆里「月-日」落在 [今天,
    今天+lookahead_days] 且年份早于今年的点，用 DeepSeek 组织成有温度的唤醒语。
    read scope；LLM 生成受生成限额约束（最贵操作）。
    """
    key = _api_key_auth(request, "read")
    lookahead_days = max(0, min(int(lookahead_days or 7), 30))
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"唤醒请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_recall.compose_recall(key["user_id"], None, lookahead_days)
        _audit(key, "memory.recall", "ok",
               detail=f"n={len(result['anniversaries'])} empty={result['empty']}",
               tokens_used=result.get("tokens_used", 0),
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, "memory.recall", "error", detail=str(e))
        raise HTTPException(500, f"唤醒失败：{e}")


@router.get("/api/ai/triggers")
def ai_triggers(request: Request, lookahead_days: int = 7):
    """N-10 主动叙事触发器清单：扫描所有触发器类型，产出「今日候选触发器」。

    化学反应 7 —— 让 agent 从「被动响应」走向「主动陪伴」。返回 anniversary /
    seasonal / revisit / location 四类候选触发器 + summary 计数。纯聚合
    （复用 recall 的周年匹配），不调 LLM、不占生成限额，read scope。
    """
    key = _api_key_auth(request, "read")
    lookahead_days = max(0, min(int(lookahead_days or 7), 30))
    t0 = time.time()
    try:
        result = domain_trigger.scan_triggers(key["user_id"], None, lookahead_days)
        _audit(key, "memory.triggers", "ok",
               detail=f"total={result['total']} {result['summary']}",
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except Exception as e:
        _audit(key, "memory.triggers", "error", detail=str(e))
        raise HTTPException(500, f"触发器扫描失败：{e}")


@router.post("/api/ai/generate")
def ai_generate(request: Request, kind: str = Form(""), trip_id: int = Form(0),
                style: str = Form("旅行者视角"), language: str = Form("zh")):
    key = _api_key_auth(request, "read")
    if not kind or not trip_id:
        raise HTTPException(400, "缺少 kind 或 trip_id")
    # P0-1：LLM 生成额外加更严限额（最贵操作，防刷 token）
    allowed, retry = rate_limit.check_generate(key["id"])
    if not allowed:
        raise HTTPException(429, f"生成请求过于频繁，请 {retry}s 后重试",
                            headers={"Retry-After": str(retry)})
    t0 = time.time()
    try:
        result = domain_gen.generate(kind, trip_id, style, language)
        _audit(key, f"generate.{kind}", "ok",
               detail=f"trip={trip_id}", tokens_used=result["tokens_used"],
               latency_ms=int((time.time() - t0) * 1000))
        return result
    except ValueError as e:
        _audit(key, f"generate.{kind}", "error", detail=str(e))
        raise HTTPException(404, str(e))
    except llm_provider.LLMBusy:
        # 并发闸门拒绝：这不是「生成失败」而是「现在忙」，别被下面的宽 except 兜成 500。
        raise
    except Exception as e:
        _audit(key, f"generate.{kind}", "error", detail=str(e))
        raise HTTPException(500, f"生成失败：{e}")


# ---------- 领域 API 层 — 写操作（write scope） ----------
# 让 agent（搭子/其他智能体）不止能"读"旅行记忆，还能"记"新地点/新行程。
# 坐标约定：对外接收 WGS-84（GPS 原生），入库前统一转 GCJ-02（高德前端坐标系）。

def _key_is_owner(key, owner_id) -> bool:
    """API Key 用户是否为资源 owner（admin 放行）。"""
    if owner_id is None:
        return False
    if owner_id == key["user_id"]:
        return True
    user = db.get_user_by_id(key["user_id"])
    return bool(user and user.get("is_admin"))


@router.post("/api/ai/trips")
def ai_create_trip(request: Request, name: str = Form(...), start_date: str = Form(""),
                   end_date: str = Form(""), description: str = Form(""),
                   status: str = Form("planning"), client_request_id: str = Form("")):
    """创建新行程（write scope）。返回新行程 id。

    client_request_id（可选）：幂等键，网络重试时重复提交不会重复建行程，
    直接返回首次结果。
    """
    key = _api_key_auth(request, "write")
    cached = _idempotency_check(key, client_request_id, "trips.create")
    if cached is not None:
        return cached
    try:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "行程名称不能为空")
        status = _normalize_trip_status(status)
        user = {"id": key["user_id"], "is_admin": 0}
        tid = db.create_trip(user, name, _normalize_date(start_date, "start_date"),
                             _normalize_date(end_date, "end_date"),
                             description, status)
        _audit(key, "trips.create", "ok", detail=f"trip={tid} name={name}")
        result = {"id": tid}
        _idempotency_save(key, client_request_id, result)
        return result
    except HTTPException:
        _idempotency_fail(key, client_request_id)
        raise


@router.put("/api/ai/trips/{trip_id}")
def ai_update_trip(request: Request, trip_id: int, name: str = Form(None),
                   start_date: str = Form(None), end_date: str = Form(None),
                   description: str = Form(None), status: str = Form(None)):
    """更新行程（write scope）。**只改传了的字段**：名称、起止日期、描述、状态机。

    日期/描述传 "null" 表示清空；名称不能清空（空串按「不改」处理 —— 只改状态的
    调用方习惯带一个空 name 过来，照收会把行程名抹掉）。
    """
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    new_name = None if name is None or not name.strip() else name.strip()
    db.update_trip(trip_id, new_name,
                   _optional_date(start_date, "start_date"),
                   _optional_date(end_date, "end_date"),
                   _optional_text(description),
                   _normalize_trip_status(status) if status else None)
    _audit(key, "trips.update", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


def _resolve_point_coords(name: str, lat_s: str, lng_s: str):
    """解析地点坐标 → (gcj_lng, gcj_lat, source, formatted)。

    优先用调用方提供的 WGS-84 坐标（转 GCJ-02 入库）；
    缺失时用高德地理编码按地名兜底（返回 GCJ-02 直接入库）。
    坐标越界（lat -90~90 / lng -180~180）或 geocode 失败 → 抛 HTTPException。
    formatted 为解析到的规范化地址（geocode 时返回，供核对；manual 时为空）。
    """
    if lat_s and lng_s:
        try:
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            raise HTTPException(400, "坐标格式错误")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(400, f"坐标越界：lat={lat} lng={lng}")
        gcj_lng, gcj_lat = domain_schemas.wgs84_to_gcj02(lng, lat)
        return gcj_lng, gcj_lat, "manual", ""
    geo = domain_geocode.geocode(name)
    if not geo:
        raise HTTPException(400, "未提供坐标且地名地理编码失败，请提供准确经纬度")
    return geo["lng"], geo["lat"], "geocode", geo.get("formatted", "")


@router.get("/api/ai/geocode")
def ai_geocode(request: Request, address: str = ""):
    """按地名查询坐标（read scope）。返回 WGS-84（对外约定）。供 agent 校验/补坐标。"""
    key = _api_key_auth(request, "read")
    address = (address or "").strip()
    if not address:
        raise HTTPException(400, "缺少 address 参数")
    geo = domain_geocode.geocode_wgs84(address)
    if not geo:
        raise HTTPException(404, f"未能解析地名「{address}」")
    _audit(key, "geocode.lookup", "ok", detail=f"addr={address}")
    return {"address": address, "formatted": geo["formatted"],
            "lng": geo["lng"], "lat": geo["lat"]}


@router.post("/api/ai/trips/{trip_id}/points")
def ai_create_point(request: Request, trip_id: int, name: str = Form(...),
                    lat: str = Form(""), lng: str = Form(""),
                    start_date: str = Form(""), end_date: str = Form(""),
                    description: str = Form(""), route_mode: str = Form("drive"),
                    is_waypoint: str = Form("0"), sort_order: str = Form(None),
                    client_request_id: str = Form("")):
    """为行程添加地点（write scope）。lat/lng 为 WGS-84（可选），缺失时按地名地理编码。

    sort_order（可选）：同一天内的插入位次，0 是当天第一个。不传 = 追加到当天末尾；
    给了值就插在那个位次，原先排在它及之后的点顺延一格。跨天顺序仍由 start_date 决定。

    client_request_id（可选）：幂等键，网络重试不会重复建点。
    """
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    cached = _idempotency_check(key, client_request_id, "points.create")
    if cached is not None:
        return cached
    try:
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "地点名称不能为空")
        mode = route_mode if route_mode in ("drive", "line") else "drive"
        wp = 1 if is_waypoint in ("1", "true", "on", "yes") else 0
        gcj_lng, gcj_lat, src, fmt = _resolve_point_coords(name, lat, lng)
        order = None if sort_order is None else _parse_sort_order(sort_order)
        pid = db.create_point(trip_id, name, gcj_lat, gcj_lng,
                              _normalize_date(start_date, "start_date"),
                              _normalize_date(end_date, "end_date"), description, mode, wp, order)
        _audit(key, "points.create", "ok", detail=f"trip={trip_id} name={name} coord={src}")
        result = {"id": pid, "coord_source": src, "formatted": fmt}
        _idempotency_save(key, client_request_id, result)
        return result
    except HTTPException:
        _idempotency_fail(key, client_request_id)
        raise


@router.put("/api/ai/points/{point_id}")
def ai_update_point(request: Request, point_id: int, name: str = Form(None),
                    lat: str = Form(""), lng: str = Form(""),
                    start_date: str = Form(None), end_date: str = Form(None),
                    description: str = Form(None), route_mode: str = Form(None),
                    is_waypoint: str = Form(None), sort_order: str = Form(None),
                    regeocode: str = Form(None)):
    """更新地点（write scope）。**只改传了的字段**，没传的保持原值。

    坐标：传了 lat/lng（WGS-84）就用新坐标；都不传就**沿用原坐标**，不重新地理编码。
    以前这里是「不传坐标 = 按地名重新 geocode」，于是「只改个日期」这种调用也要
    走一次高德，地名稍偏一点就 400「地理编码失败」，整次更新全废 —— 改日期和查坐标
    本来就是两件事。确实想按当前地名重新解析，显式传 regeocode=1。

    想把日期或描述清空，显式传 "null"。
    """
    key = _api_key_auth(request, "write")
    point = db.get_point(point_id)
    if not point:
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(400, "地点名称不能为空")
    mode = None if route_mode is None else (route_mode if route_mode in ("drive", "line") else "drive")
    wp = None if is_waypoint is None else (1 if is_waypoint in ("1", "true", "on", "yes") else 0)
    order = None if sort_order is None else _parse_sort_order(sort_order)

    want_regeocode = (regeocode or "").strip().lower() in ("1", "true", "on", "yes")
    if lat and lng:
        gcj_lng, gcj_lat, src, fmt = _resolve_point_coords(name or point["name"], lat, lng)
    elif want_regeocode:
        gcj_lng, gcj_lat, src, fmt = _resolve_point_coords(name or point["name"], "", "")
    else:
        gcj_lng = gcj_lat = None      # None = 不改这两列
        src, fmt = "unchanged", ""

    db.update_point(point_id, name, gcj_lat, gcj_lng,
                    _optional_date(start_date, "start_date"),
                    _optional_date(end_date, "end_date"),
                    _optional_text(description), mode, wp, order)
    _audit(key, "points.update", "ok", detail=f"point={point_id} coord={src}")
    return {"ok": True, "coord_source": src, "formatted": fmt}


@router.delete("/api/ai/points/{point_id}")
def ai_delete_point(request: Request, point_id: int):
    """删除地点（write scope）。"""
    key = _api_key_auth(request, "write")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    # 必须在 delete 之前捞：行一删，媒体记录跟着级联消失，文件名就无从查起了。
    # （这里原来什么文件都不删，删完点位磁盘和 OSS 上全是查无主的孤儿。）
    files = db.get_point_media_files(point_id)
    variants = db.get_point_variant_files(point_id)
    db.delete_point(point_id)
    media_purge.purge_file_pairs(UPLOAD_DIR, media_mod.THUMB_DIR, files, variants)
    _audit(key, "points.delete", "ok", detail=f"point={point_id}")
    return {"ok": True}


@router.delete("/api/ai/trips/{trip_id}")
def ai_delete_trip(request: Request, trip_id: int):
    """删除行程（write scope），级联删除媒体文件。"""
    key = _api_key_auth(request, "write")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    files = db.get_trip_media_files(trip_id)
    variants = db.get_trip_variant_files(trip_id)   # 必须在 delete 之前捞（行会级联消失）
    db.delete_trip(trip_id)
    media_purge.purge_file_pairs(UPLOAD_DIR, media_mod.THUMB_DIR, files, variants)
    _audit(key, "trips.delete", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


@router.get("/api/ai/trips/{trip_id}/points")
def ai_list_points(request: Request, trip_id: int):
    """列出行程下所有地点（read scope），**带 point_id**。坐标对外转 WGS-84。

    这个接口原来是缺的：get_memory / export_trip / get_stats 都不返回 point_id，
    而 update_point / delete_point / upload_media 又都要 point_id —— 读接口拿不到
    id、写接口非要 id，改一个已有地点的日期都做不到，只能删整条行程重建（连带丢照片）。
    这里把那个断口补上。
    """
    key = _api_key_auth(request, "read")
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    points = db.list_points(trip_id)
    counts = db.get_point_media_counts(trip_id)
    out = []
    for idx, p in enumerate(points):
        try:
            wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
        except (TypeError, ValueError):
            wlng, wlat = p["lng"], p["lat"]
        c = counts.get(p["id"], {})
        photo, video = int(c.get("photo", 0)), int(c.get("video", 0))
        out.append({
            "point_id": p["id"],
            "index": idx,                       # 在时间线上的实际位次（0 起），跨天连续
            "sort_order": int(p["sort_order"] or 0),   # 库内同日期组内的位次
            "name": p["name"], "lat": wlat, "lng": wlng,
            "start_date": p["start_date"], "end_date": p["end_date"],
            "description": p["description"],
            "route_mode": p["route_mode"] or "drive",
            "is_waypoint": int(p["is_waypoint"] or 0),
            "media_n": photo + video, "photo_n": photo, "video_n": video,
        })
    _audit(key, "points.list", "ok", detail=f"trip={trip_id} points={len(out)}")
    return {"trip_id": trip_id, "trip_name": trip["name"],
            "coord_sys": "wgs84", "count": len(out), "points": out}


@router.get("/api/ai/trips/{trip_id}/export")
def ai_export_trip(request: Request, trip_id: int):
    """导出行程为 JSON（read scope）。坐标对外转 WGS-84（符合领域 API 约定），
    便于其他 agent 备份/迁移/二次加工。"""
    key = _api_key_auth(request, "read")
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    points = db.list_points(trip_id)
    payload = {
        "format": "travel-memory",
        "version": 1,
        "coord_sys": "wgs84",  # 对外统一 WGS-84
        "trip": {
            "name": trip["name"], "start_date": trip["start_date"],
            "end_date": trip["end_date"], "description": trip["description"],
        },
        "points": [],
    }
    for p in points:
        try:
            wlng, wlat = domain_schemas.gcj02_to_wgs84(float(p["lng"]), float(p["lat"]))
        except (TypeError, ValueError):
            wlng, wlat = p["lng"], p["lat"]
        payload["points"].append({
            "name": p["name"], "lat": wlat, "lng": wlng,
            "start_date": p["start_date"], "end_date": p["end_date"],
            "description": p["description"], "route_mode": p["route_mode"] or "drive",
            "is_waypoint": int(p["is_waypoint"] or 0),
        })
    _audit(key, "trips.export", "ok", detail=f"trip={trip_id} points={len(payload['points'])}")
    return payload


@router.get("/api/ai/trips/{trip_id}/stats")
def ai_trip_stats(request: Request, trip_id: int):
    """行程统计（read scope）：点数/媒体数/起止日期。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    stats = db.get_trip_stats(trip_id)
    _audit(key, "trips.stats", "ok", detail=f"trip={trip_id}")
    return stats


@router.post("/api/ai/trips/{trip_id}/share")
def ai_create_share(request: Request, trip_id: int, expire_days: int = Form(None),
                    password: str = Form("")):
    """为行程生成只读分享链接（share scope），返回 token（供 agent 分享给他人）。"""
    key = _api_key_auth(request, "share")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    token = db.create_share(trip_id, expire_days, password or None)
    _audit(key, "trips.share", "ok", detail=f"trip={trip_id}")
    return {"token": token, "has_password": bool(password),
            "url": f"/share/{token}"}


@router.delete("/api/ai/trips/{trip_id}/share")
def ai_delete_share(request: Request, trip_id: int):
    """撤销行程的全部分享链接（share scope）。"""
    key = _api_key_auth(request, "share")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    db.delete_shares_for_trip(trip_id)
    _audit(key, "trips.share.revoke", "ok", detail=f"trip={trip_id}")
    return {"ok": True}


@router.get("/api/ai/trips/{trip_id}/share-count")
def ai_share_count(request: Request, trip_id: int):
    """当前行程有效分享链接数量（read scope）。"""
    key = _api_key_auth(request, "read")
    if not db.get_trip(trip_id):
        raise HTTPException(404, "行程不存在")
    if not _key_is_owner(key, db.get_trip_owner_id(trip_id)):
        raise HTTPException(404, "行程不存在")
    return {"count": db.count_shares_for_trip(trip_id)}


# ---------- 领域 API 层：媒体（P1-1，供 agent 上传/查询/删除照片视频） ----------
def _media_ext(fname: str, content_type: str) -> str:
    """根据文件名或 content_type 推断扩展名，返回带点后缀（可能为空）。"""
    ext = Path(fname or "").suffix.lower()
    if ext:
        return ext
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/heic": ".heic",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    }.get((content_type or "").split(";")[0].strip(), "")


@router.get("/api/ai/points/{point_id}/media")
def ai_list_media(request: Request, point_id: int):
    """列出某地点的媒体（read scope）。返回元数据（含 filename/thumb/exif）。"""
    key = _api_key_auth(request, "read")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    _audit(key, "media.list", "ok", detail=f"point={point_id}")
    # 每条媒体补上带有效期的签名链接（url / download_url / thumb_url）。
    # 只加字段：老的 filename / thumb_filename 原样保留，老 Agent 不受影响。
    return [agent_media.enrich_media(m) for m in db.list_media(point_id)]


@router.post("/api/ai/points/{point_id}/media")
def ai_upload_media(request: Request, point_id: int, data_base64: str = Form(...),
                    filename: str = Form(""), content_type: str = Form("")):
    """上传媒体（write scope）。data_base64 为文件内容 base64（HTTP transport 无法传 multipart）。

    返回媒体元数据（含 exif 提取的时间/坐标）。filename/content_type 用于推断类型。
    """
    key = _api_key_auth(request, "write")
    if not db.get_point(point_id):
        raise HTTPException(404, "地点不存在")
    if not _key_is_owner(key, db.get_point_owner_id(point_id)):
        raise HTTPException(404, "地点不存在")
    try:
        raw = base64.b64decode(data_base64)
    except Exception:
        raise HTTPException(400, "data_base64 解码失败，请传标准 base64 编码内容")
    if not raw:
        raise HTTPException(400, "空文件")

    ext = _media_ext(filename, content_type)
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"):
        mtype = "photo"
    elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        mtype = "video"
    else:
        raise HTTPException(400, f"不支持的文件类型（需在 filename 或 content_type 中指明）: {ext or '未知'}")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    src_path = UPLOAD_DIR / stored_name
    src_path.write_bytes(raw)

    thumb_name = media_mod.make_thumb(src_path, mtype)
    exif_time = exif_lat = exif_lng = None
    if mtype == "photo":
        exif_time, exif_lat, exif_lng = media_mod.read_exif(src_path)

    mid = db.add_media(point_id, mtype, stored_name, thumb_name,
                       exif_time, exif_lat, exif_lng)
    transcode.on_media_uploaded(mid, src_path, mtype)   # 视频丢后台转码，不阻塞本请求
    _audit(key, "media.upload", "ok", detail=f"point={point_id} type={mtype}")
    # 刚落的文件还在「本地 → OSS」的 2 分钟同步空窗里，签名会在 OSS 上 404，
    # urls_for 会自动回落到本机路径（跟网页链路同款处理），不会给出死链。
    return {"id": mid, "type": mtype, "filename": stored_name, "thumb": thumb_name,
            "exif_time": exif_time, "exif_lat": exif_lat, "exif_lng": exif_lng,
            "url": agent_media.urls_for(f"/uploads/{stored_name}"),
            "download_url": agent_media.urls_for(f"/uploads/{stored_name}",
                                                 download=True,
                                                 filename=stored_name),
            "thumb_url": (agent_media.urls_for(f"/uploads/thumbs/{thumb_name}")
                          if thumb_name else None)}


@router.delete("/api/ai/media/{media_id}")
def ai_delete_media(request: Request, media_id: int):
    """删除媒体（write scope）。"""
    key = _api_key_auth(request, "write")
    if not _key_is_owner(key, db.get_media_owner_id(media_id)):
        raise HTTPException(404, "媒体不存在")
    m = db.get_media(media_id)
    if m:
        variants = db.delete_variants(media_id)
        db.delete_media(media_id)
        media_purge.purge_media_row(UPLOAD_DIR, media_mod.THUMB_DIR, m, variants)
    _audit(key, "media.delete", "ok", detail=f"media={media_id}")
    return {"ok": True}


# ---------- MCP Streamable HTTP 远程端点（开放平台：远程 Agent 接入） ----------
@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP 端点 — 远程 Agent 通过标准 MCP 协议接入。

    认证：Authorization: Bearer tm_xxx（调用者**自己的** API Key，每个请求独立鉴权）。
    tools/call 按工具所需 scope（read/write/share）校验；
    工具执行时携带调用者 key 调领域 API → 数据隔离由领域 API 层保证。
    无状态实现（stateless server），不维护 MCP session。
    """
    _mcp_headers = {"MCP-Protocol-Version": mcp_http.PROTOCOL_VERSION}
    auth = request.headers.get("Authorization", "")
    key = token_auth.resolve_key(auth)
    raw_key = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    if not key or not raw_key:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": "invalid_api_key"}},
            status_code=401, headers=_mcp_headers)
    # P0-1 限流（Key 维度 + IP 兜底）
    allowed, retry = rate_limit.limiter.check(f"key:{key['id']}:mcp", rate_limit.READ_LIMIT)
    if not allowed:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32029, "message": f"rate limited, retry in {retry}s"}},
            status_code=429, headers=_mcp_headers)
    ip = request.client.host if request.client else ""
    allowed, retry = rate_limit.check_ip(ip)
    if not allowed:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32029, "message": f"rate limited, retry in {retry}s"}},
            status_code=429, headers=_mcp_headers)
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "parse error"}},
            status_code=400, headers=_mcp_headers)
    msg_id = body.get("id")
    # tools/call：校验该工具所需 scope（防止低权限 key 调用高权限工具）
    if body.get("method") == "tools/call":
        tool = (body.get("params") or {}).get("name")
        need = mcp_http.TOOL_SCOPE.get(tool, "read")
        if tool and not token_auth.has_scope(key, need):
            return JSONResponse(mcp_http.scope_error_response(msg_id, tool),
                                status_code=403, headers=_mcp_headers)
    # 领域 API 调用可能耗时（如 generate 调 LLM），放线程池避免阻塞事件循环
    resp = await run_in_threadpool(mcp_http.handle_jsonrpc, body, raw_key,
                                   deps.client_ip(request))
    if resp is None:
        return Response(status_code=202, headers=_mcp_headers)  # 通知类消息
    return JSONResponse(resp, headers=_mcp_headers)

# 行程状态机合法状态（planning 计划中 → traveling 进行中 → completed 已完成 → archived 已归档）
TRIP_STATUSES = ("planning", "traveling", "completed", "archived")


def _normalize_trip_status(status_s: str) -> str:
    """校验行程状态，非法值回退 planning。"""
    return status_s if status_s in TRIP_STATUSES else "planning"


# agent 写日期的花样比人多：「2026/10/8」「2026年10月8日」都见过。
# 这几种能无歧义还原的直接收下，剩下的（「10月8日」缺年份、「下周二」是相对的）
# 一律 400 —— 原来是原样塞进库里，时间线上就多一个排不进去的点。
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"),
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$"),
)


# 想在局部更新里表达「把这个字段清空」得有个说法。空串是指望不上的 ——
# FastAPI 的 Form 里「传了空串」和「根本没传」拿到的都是默认值，分不开。
# 所以约定一个明确的字面量，工具描述里写清楚。
_CLEAR_WORDS = ("null", "none", "空")


def _parse_sort_order(raw):
    """局部更新/插入位次用：非负整数，认不出来抛 400（别静默当成 0 插到队首）。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        n = int(text)
    except ValueError:
        raise HTTPException(400, f"sort_order 必须是非负整数：「{text}」")
    if n < 0:
        raise HTTPException(400, "sort_order 不能为负")
    return n


def _optional_text(raw):
    """局部更新用：没传 → 不改；传 "null" → 清空；其余原样。"""
    if raw is None:
        return None
    return db.CLEAR if raw.strip().lower() in _CLEAR_WORDS else raw


def _optional_date(raw, field: str):
    """同 _optional_text，但值要过一遍日期格式校验。"""
    if raw is None:
        return None
    if raw.strip().lower() in _CLEAR_WORDS:
        return db.CLEAR
    return _normalize_date(raw, field)


def _normalize_date(raw: str, field: str) -> str | None:
    """'2026/10/8' → '2026-10-08'。空值返回 None，认不出来抛 400。"""
    text = (raw or "").strip()
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            break
    raise HTTPException(400, f"{field} 格式无法识别：「{text}」，请用 YYYY-MM-DD")
