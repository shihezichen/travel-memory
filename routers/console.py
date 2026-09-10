"""开发者控制台（/console + /api/console/*）—— 面向「人」的 API Key 管理面板。

从 app.py 原样搬过来的，路由定义一字未改，只把 `@app.` 换成了 `@router.`。
和 routers/agent_api.py 的区别：这里走的是**浏览器 session 认证**（开发者本人登录
后管理自己的 Key），而领域 API 走的是 API Key + Bearer（agent 调用）。
两套认证体系、两个路径命名空间，所以分成两个 router。
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pathlib import Path

import db
from routers.deps import current_user, _public_user

BASE = Path(__file__).resolve().parent.parent

router = APIRouter()


# ============================================================

VALID_SCOPES = ("read", "write", "share", "admin")


def _normalize_scope(scope_s: str) -> str:
    """校验并规范化 scope：去重、排序、只保留合法 token（read/write/share/admin）。"""
    tokens = []
    for t in (scope_s or "read").replace(",", " ").split():
        if t in VALID_SCOPES and t not in tokens:
            tokens.append(t)
    return ",".join(tokens) if tokens else "read"


@router.get("/console")
def console_page(request: Request):
    """开发者控制台页面（需登录 session；面向开发者，风格与旅行者界面分离）。"""
    html = (BASE / "static" / "console.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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
    raw_key, prefix, key_id = db.create_api_key(user["id"], name, scope,
                                                expire_days=expire_days or None)
    db.add_audit(None, user["id"], "key.create", "CONSOLE", "/api/console/keys",
                 scope, "ok", detail=f"name={name}")
    return {"ok": True, "id": key_id, "key": raw_key, "prefix": prefix, "scope": scope,
            "note": "raw_key 仅返回这一次，请立即保存"}


@router.delete("/api/console/keys/{key_id}")
def console_delete_key(request: Request, key_id: int):
    """删除 API Key（硬删除；审计日志保留，api_key_id 置空）。"""
    user = current_user(request)
    if not db.delete_api_key(key_id, user["id"]):
        raise HTTPException(404, "Key 不存在或不属于当前用户")
    db.add_audit(None, user["id"], "key.delete", "CONSOLE", f"/api/console/keys/{key_id}",
                 "", "ok", detail=f"key_id={key_id}")
    return {"ok": True}


@router.get("/api/console/usage")
def console_usage(request: Request):
    """按 Key 聚合的用量统计（调用次数 / 成功 / token 消耗）。"""
    user = current_user(request)
    return {"usage": db.usage_stats(user["id"])}


@router.get("/api/console/audit")
def console_audit(request: Request, limit: int = 100):
    """审计日志（管理员看全部，普通用户只看自己）。"""
    user = current_user(request)
    rows = db.list_audit(user_id=user["id"] if not user["is_admin"] else None, limit=limit)
    return {"audit": rows}
