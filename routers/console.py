"""开发者控制台（/console + /api/console/*）—— 面向「人」的 API Key 管理面板。

从 app.py 原样搬过来的，路由定义一字未改，只把 `@app.` 换成了 `@router.`。
和 routers/agent_api.py 的区别：这里走的是**浏览器 session 认证**（开发者本人登录
后管理自己的 Key），而领域 API 走的是 API Key + Bearer（agent 调用）。
两套认证体系、两个路径命名空间，所以分成两个 router。
"""
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
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


# ---------- 取回完整 Key ----------
# 这是整个控制台唯一一个会把 Key 明文吐出来的接口，所以在密码之外再加一道限流：
# 密码是人记的，能猜；Key 一旦被猜出密码捞走，agent 的读写权限就一起没了。
# 进程内计数够用 —— 单进程部署，而且重启一次就放行本来也不是什么大问题
# （真正的门槛是密码本身，这里只是把在线爆破的速率压到没意义）。
_REVEAL_MAX_FAILS = 5
_REVEAL_LOCK_SEC = 300
_reveal_fails: dict[int, list[float]] = {}


def _reveal_guard(user_id: int):
    now = time.monotonic()
    fails = [t for t in _reveal_fails.get(user_id, []) if now - t < _REVEAL_LOCK_SEC]
    _reveal_fails[user_id] = fails
    if len(fails) >= _REVEAL_MAX_FAILS:
        wait = int(_REVEAL_LOCK_SEC - (now - fails[0]))
        raise HTTPException(429, f"密码错误次数过多，请 {wait} 秒后再试")


def _reveal_fail(user_id: int):
    _reveal_fails.setdefault(user_id, []).append(time.monotonic())


@router.post("/api/console/keys/{key_id}/reveal")
def console_reveal_key(request: Request, key_id: int, password: str = Form("")):
    """验登录密码后取回完整 Key（用来拼 Agent 接入提示词）。

    用 POST 而不是 GET：密码和 Key 都不该出现在 URL 里 —— 那会进浏览器历史、
    反代访问日志和 Referer。
    """
    user = current_user(request)
    _reveal_guard(user["id"])

    full = db.get_user_by_id(user["id"])
    if not password or not db.verify_password(password, full["salt"], full["password_hash"]):
        _reveal_fail(user["id"])
        db.add_audit(None, user["id"], "key.reveal", "CONSOLE",
                     f"/api/console/keys/{key_id}/reveal", "", "error",
                     detail=f"key_id={key_id} 密码错误", client_ip=client_ip(request))
        raise HTTPException(403, "密码不对")

    raw = db.reveal_api_key(key_id, user["id"])
    if raw is None:
        raise HTTPException(404, "这把 Key 没有可取回的存档（启用找回之前建的），"
                                 "删掉重新生成一把即可")
    _reveal_fails.pop(user["id"], None)
    db.add_audit(None, user["id"], "key.reveal", "CONSOLE",
                 f"/api/console/keys/{key_id}/reveal", "", "ok",
                 detail=f"key_id={key_id}", client_ip=client_ip(request))
    return {"key": raw}


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
