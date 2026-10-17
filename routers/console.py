"""开发者控制台（/console + /api/console/*）—— 面向「人」的 API Key 管理面板。

从 app.py 原样搬过来的，路由定义一字未改，只把 `@app.` 换成了 `@router.`。
和 routers/agent_api.py 的区别：这里走的是**浏览器 session 认证**（开发者本人登录
后管理自己的 Key），而领域 API 走的是 API Key + Bearer（agent 调用）。
两套认证体系、两个路径命名空间，所以分成两个 router。
"""
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path

import db
from routers.deps import client_ip, current_user, _public_user

BASE = Path(__file__).resolve().parent.parent

router = APIRouter()


# ============================================================

VALID_SCOPES = ("read", "write", "share", "admin")


def _normalize_scope(scope_s: str) -> str:
    """校验并规范化 scope：去重、排序、只保留合法 token（read/write/share/admin）。

    write 蕴含 read：一个只有 write 的 Key 能写进去却读不回来，创建者自己都查不了
    自己刚写的数据 —— 没有任何场景需要这种 Key。控制台的勾选框也是这么联动的，
    这里再兜一道，免得绕过页面直接调接口建出个残废 Key。
    """
    tokens = []
    for t in (scope_s or "read").replace(",", " ").split():
        if t in VALID_SCOPES and t not in tokens:
            tokens.append(t)
    if "write" in tokens and "read" not in tokens:
        tokens.append("read")
    return ",".join(tokens) if tokens else "read"


@router.get("/console")
def console_page(request: Request):
    """开发者控制台页面（需登录 session；面向开发者，风格与旅行者界面分离）。

    页脚注入一个版本标识：改了东西却还看到老样子时，一眼能分辨是没生效还是
    浏览器拿着旧页面不放（手机浏览器对 no-store 并不总是当回事）。
    跟主应用操作菜单底部那个版本号是一个用途。
    """
    path = BASE / "static" / "console.html"
    html = path.read_text(encoding="utf-8")
    stamp = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    return HTMLResponse(html.replace("__CONSOLE_VER__", stamp),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/api/console/me")
def console_me(request: Request):
    return _public_user(current_user(request))


@router.get("/api/console/keys")
def console_list_keys(request: Request):
    """列出当前用户的 API Key（不含 raw_key，只显示前缀）。"""
    user = current_user(request)
    return {"keys": db.list_api_keys(user["id"])}


@router.post("/api/console/keys")
def console_create_key(request: Request, name: str = Form(""), scope: str = Form("read"),
                       expire_days: int = Form(0)):
    """自助创建 API Key。任何登录用户都能给自己建 Key（挂在当前用户名下）；
    admin scope 仅管理员可授予。raw_key 仅返回这一次。"""
    user = current_user(request)
    scope = _normalize_scope(scope)
    if "admin" in scope and not user["is_admin"]:
        raise HTTPException(403, "仅管理员可授予 admin 权限")
    name = (name or "").strip() or "未命名"
    if db.api_key_name_taken(user["id"], name):
        raise HTTPException(400, f"已经有一把叫「{name}」的 Key 了，换个名字")
    raw_key, prefix, key_id = db.create_api_key(user["id"], name, scope,
                                                expire_days=expire_days or None)
    db.add_audit(None, user["id"], "key.create", "CONSOLE", "/api/console/keys",
                 scope, "ok", detail=f"name={name}", client_ip=client_ip(request))
    return {"ok": True, "id": key_id, "key": raw_key, "prefix": prefix, "scope": scope,
            "note": "raw_key 仅返回这一次，请立即保存"}


@router.delete("/api/console/keys/{key_id}")
def console_delete_key(request: Request, key_id: int):
    """删除 API Key（硬删除；审计日志保留，api_key_id 置空）。"""
    user = current_user(request)
    if not db.delete_api_key(key_id, user["id"]):
        raise HTTPException(404, "Key 不存在或不属于当前用户")
    db.add_audit(None, user["id"], "key.delete", "CONSOLE", f"/api/console/keys/{key_id}",
                 "", "ok", detail=f"key_id={key_id}", client_ip=client_ip(request))
    return {"ok": True}


@router.get("/api/console/usage")
def console_usage(request: Request):
    """按 Key 聚合的用量统计（调用次数 / 成功 / token 消耗）。"""
    user = current_user(request)
    return {"usage": db.usage_stats(user["id"])}


@router.get("/api/console/audit")
def console_audit(request: Request, limit: int = 100, key_id: int = 0):
    """审计日志（管理员看全部，普通用户只看自己）。key_id>0 时只看该 Key 的调用。"""
    user = current_user(request)
    if key_id and not db.api_key_belongs_to(key_id, user["id"]):
        raise HTTPException(404, "Key 不存在或不属于当前用户")
    rows = db.list_audit(user_id=user["id"] if not user["is_admin"] else None,
                         limit=limit, api_key_id=key_id or None)
    return {"audit": rows}


# ============================================================
# 用户上传配额（管理员调整）
# ============================================================
#限量是为了保护服务器不被一次上传拖死，但每个用户的合理用量差很多。
# 所以四项上限全部落库、由管理员逐用户调整，保存即生效 ——
# 业务代码一律通过 db.get_limits() 取，**不在别处再写数字常量**，
# 否则会出现「改了一处、另一处还是老规矩」。


class LimitsIn(BaseModel):
    max_photos_per_upload: int | None = None
    max_videos_per_upload: int | None = None
    max_video_seconds: int | None = None
    max_storage_bytes: int | None = None


def _require_admin(request: Request):
    u = current_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "仅管理员可调整用户配额")
    return u


def _fmt_bytes(n) -> str:
    """把**用量**说成人话。0 就是 0 B，不是「不限」。

    早先这个函数和 _fmt_cap 合用，0 一律显示「不限」——
    于是零用量的用户在列表里显示「用量不限 / 上限 5 GB」，看着像
    已经装满还溢出了。两件事的 0 含义不同，必须分开。
    """
    n = int(n or 0)
    if n < 0:
        return "不限"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024.0
    return "%.1f TB" % v


def _fmt_cap(n) -> str:
    """把**配额上限**说成人话。<=0 表示不限制。"""
    n = int(n or 0)
    return "不限" if n <= 0 else _fmt_bytes(n)


@router.get("/api/console/limits")
def console_limits(request: Request):
    """所有用户的用量与配额（管理员）。用量要现算，所以逐个查。"""
    _require_admin(request)
    limits = db.list_all_limits()
    conn = db.get_conn()
    users = conn.execute(
        "SELECT id, username, display_name, is_admin FROM users ORDER BY id"
    ).fetchall()
    conn.close()

    out = []
    for u in users:
        uid = u["id"]
        use = db.user_storage_usage(uid)
        lim = db.get_limits(uid)
        custom = uid in limits     # 是否被单独设置过
        out.append({
            "id": uid,
            "username": u["username"],
            "display_name": u["display_name"] or u["username"],
            "is_admin": bool(u["is_admin"]),
            "customized": custom,
            "limits": lim,
            "usage": use,
            "usage_text": _fmt_bytes(use["bytes"]),
            "cap_text": _fmt_cap(lim["max_storage_bytes"]),
            "percent": (round(100.0 * use["bytes"] / lim["max_storage_bytes"], 1)
                        if lim["max_storage_bytes"] > 0 else None),
            "updated_at": limits.get(uid, {}).get("updated_at"),
        })
    return {"users": out, "defaults": db.DEFAULT_LIMITS}


@router.post("/api/console/limits/{user_id}")
def console_set_limits(request: Request, user_id: int, body: LimitsIn):
    """调整某用户的配额，保存即生效。负数表示不限。"""
    admin = _require_admin(request)
    if not db.get_user_by_id(user_id):
        raise HTTPException(404, "用户不存在")
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(400, "没有要修改的项")
    for k, v in payload.items():
        if v is not None and v < -1:
            raise HTTPException(400, "%s 不能小于 -1" % k)
    try:
        lim = db.set_limits(user_id, payload, updated_by=admin["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add_audit(None, admin["id"], "limits.set", "CONSOLE",
                 f"/api/console/limits/{user_id}", "admin", "ok",
                 detail=str(payload), client_ip=client_ip(request))
    use = db.user_storage_usage(user_id)
    return {"ok": True, "limits": lim, "usage": use,
            "usage_text": _fmt_bytes(use["bytes"])}


@router.post("/api/console/limits/{user_id}/reset")
def console_reset_limits(request: Request, user_id: int):
    """清掉该用户的单独设置，回到系统默认值。"""
    admin = _require_admin(request)
    conn = db.get_conn()
    conn.execute("DELETE FROM user_limits WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    db.add_audit(None, admin["id"], "limits.reset", "CONSOLE",
                 f"/api/console/limits/{user_id}/reset", "admin", "ok",
                 client_ip=client_ip(request))
    return {"ok": True, "limits": db.get_limits(user_id)}


# ============================================================
# 存储用量（任何登录用户可查自己；管理员可查任何人）
# ============================================================

@router.get("/api/storage/usage")
def storage_usage(request: Request):
    """当前用户的媒体占用 + 配额 + 超限状态。

    前端在选文件前就调它来提示「你只剩 12 MB 了」，而不是等上传
    被服务端拒了才知道。
    """
    u = current_user(request)
    use = db.user_storage_usage(u["id"])
    lim = db.get_limits(u["id"])
    cap = lim["max_storage_bytes"]
    return {
        "usage": use,
        "limits": lim,
        "usage_text": _fmt_bytes(use["bytes"]),
        "cap_text": _fmt_cap(cap),
        "free_bytes": max(0, cap - use["bytes"]) if cap > 0 else None,
        "over": bool(cap > 0 and use["bytes"] > cap),
    }


@router.get("/api/storage/items")
def storage_items(request: Request, limit: int = 200):
    """列出当前用户占用最大的媒体，供「存储空间不足」时清理。

    **按体积倒序**：用户要腾空间，最该先看到的是那些大文件（尤其是视频）。
    按时间倒序没用 —— 他要的是「删哪个最划算」，不是「最近加了哪个」。

    大小优先用 media.filesize（本次改动后写入的有），缺失的老数据退回
    os.stat 实测，两者都拿不到才显示 0。
    """
    u = current_user(request)
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT m.id AS id, m.type AS type, m.filename AS filename, "
        "       m.filesize AS filesize, m.thumb_filename AS thumb_filename, "
        "       p.name AS point_name, t.name AS trip_name, t.id AS trip_id, "
        "       m.exif_time AS exif_time "
        "  FROM media m "
        "  JOIN points p ON p.id = m.point_id "
        "  JOIN trips  t ON t.id = p.trip_id "
        " WHERE t.user_id = ?", (u["id"],)).fetchall()
    conn.close()

    from config import UPLOAD_ROOT
    from media import THUMB_DIR   # THUMB_DIR 定义在 media.py，不在 config

    def _size(r):
        n = r["filesize"] if "filesize" in r.keys() else None
        if n:
            return int(n)
        tot = 0
        p = UPLOAD_ROOT / (r["filename"] or "")
        try:
            tot += p.stat().st_size
        except OSError:
            pass
        if r["thumb_filename"]:
            p = THUMB_DIR / r["thumb_filename"]
            try:
                tot += p.stat().st_size
            except OSError:
                pass
        return tot

    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "type": r["type"] or "photo",
            "name": r["filename"] or "",
            "size": _size(r),
            "point_name": r["point_name"],
            "trip_name": r["trip_name"],
            "trip_id": r["trip_id"],
            "exif_time": r["exif_time"],
        })
    items.sort(key=lambda x: x["size"], reverse=True)
    items = items[:max(1, min(1000, limit))]

    usage = db.user_storage_usage(u["id"])
    lim = db.get_limits(u["id"])
    cap = lim["max_storage_bytes"]
    return {
        "items": items,
        "usage": usage,
        "usage_text": _fmt_bytes(usage["bytes"]),
        "cap_text": _fmt_cap(cap),
        "free_bytes": max(0, cap - usage["bytes"]) if cap > 0 else None,
        "over": bool(cap > 0 and usage["bytes"] > cap),
    }
