"""领域 API 层 — API Key 认证 + scope 分级 + 审计

API Key 制（类似 GitHub PAT），与用户登录 session 分离。
认证方式：`Authorization: Bearer tm_xxx`。
scope 分级：read / write / share / admin（逗号分隔）。
"""
from __future__ import annotations

import hashlib

import db

# scope 权限矩阵：每个动作需要的最小 scope
SCOPE_ACTIONS = {
    "read": ["read", "write", "share", "admin"],
    "write": ["write", "admin"],
    "share": ["share", "admin"],
    "admin": ["admin"],
}


def resolve_key(auth_header: str):
    """解析 Bearer token → api_key dict（含 user_id）。无效/过期/吊销返回 None。"""
    if not auth_header:
        return None
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    raw = parts[1].strip()
    if not raw:
        return None
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return db.get_api_key_by_hash(key_hash)


def has_scope(api_key: dict, required_scope: str) -> bool:
    """检查 api_key 是否具备某 scope。"""
    if not api_key:
        return False
    granted = set(s.strip() for s in api_key["scope"].split(","))
    return required_scope in granted or "admin" in granted


def authorize(auth_header: str, required_scope: str):
    """统一鉴权入口：返回 (api_key, err_msg)。err_msg 非空表示鉴权失败。"""
    key = resolve_key(auth_header)
    if not key:
        return None, "invalid_api_key"
    if not has_scope(key, required_scope):
        return None, "insufficient_scope"
    db.touch_api_key(key["id"])
    return key, None
