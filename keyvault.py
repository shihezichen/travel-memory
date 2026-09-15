"""API Key 的可找回存档 —— 让「验完密码再看一眼完整 Key」成为可能。

为什么要有它
------------
api_keys 表里存的是 sha256(raw_key)，单向的。所以控制台「Agent 接入」里那段
提示词以前只能填一个 `tm_abc123…（完整 Key 仅生成时可见）` 的占位符 ——
拷下来发给 agent 是跑不通的，而真正的 Key 只在创建那一刻闪过一次。

于是这里额外存一份**加密的**明文：平时谁也读不到，用户输入自己的登录密码
之后才解出来。key_hash 那条路照旧（鉴权仍然只比 hash），这份密文纯粹是给人
找回用的。

安全模型（说清楚，别以为它等价于原来的只存 hash）
------------------------------------------------
- 密文用一把**服务端主密钥**加密，主密钥放在 key_vault.txt（0600，不进 git）。
  所以「拿到 data.db」还不够，得同时拿到主密钥文件才能解。
- 但两者在同一台机器上。真被拖库 + 拿到文件系统权限，Key 就是可解的。
  这是为了「能找回」必然付出的代价 —— 原来的纯 hash 做不到找回。
- 因此取回这一步必须验登录密码 + 限流 + 记审计，见 routers/console.py。

为什么自己写而不是上 cryptography
--------------------------------
这个项目的 requirements 里一个加密库都没有，为了保护一个 45 字节的 token 拉一个
带 C 扩展的依赖进来不划算。这里用的是教科书构造：HMAC-SHA256 当 PRF 做 CTR
流加密，再 encrypt-then-MAC。没有自创算法，只是把标准件拼起来。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent
VAULT_FILE = BASE / "key_vault.txt"

_NONCE_LEN = 16
_TAG_LEN = 32
_master_cache: bytes | None = None


def master_key() -> bytes:
    """读主密钥；没有就生成一把并落盘（0600）。"""
    global _master_cache
    if _master_cache is not None:
        return _master_cache
    if VAULT_FILE.exists():
        raw = VAULT_FILE.read_text(encoding="utf-8").strip()
        if raw:
            _master_cache = base64.urlsafe_b64decode(raw)
            return _master_cache
    key = secrets.token_bytes(32)
    # 先建成 0600 再写，别让密钥有一瞬间是 0644
    fd = os.open(VAULT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base64.urlsafe_b64encode(key).decode())
    _master_cache = key
    return key


def _subkey(label: bytes, nonce: bytes) -> bytes:
    """从主密钥派生一次性子密钥。加密和认证各用一把，别共用。"""
    return hmac.new(master_key(), label + nonce, hashlib.sha256).digest()


def _keystream(k: bytes, n: int) -> bytes:
    """HMAC-SHA256 当 PRF 的 CTR 流：块 i = HMAC(k, i)。"""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(k, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def seal(plaintext: str) -> str:
    """加密，返回可直接入库的 base64 串。"""
    data = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(_subkey(b"enc", nonce), len(data))))
    tag = hmac.new(_subkey(b"mac", nonce), nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode()


def open_sealed(blob: str) -> str | None:
    """解密。密文被动过、或主密钥换过 → 返回 None，不抛异常。"""
    if not blob:
        return None
    try:
        raw = base64.urlsafe_b64decode(blob)
    except (ValueError, TypeError):
        return None
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        return None
    nonce, ct, tag = raw[:_NONCE_LEN], raw[_NONCE_LEN:-_TAG_LEN], raw[-_TAG_LEN:]
    want = hmac.new(_subkey(b"mac", nonce), nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        return None                      # 先验 MAC 再解密，篡改过的一律不碰
    data = bytes(a ^ b for a, b in zip(ct, _keystream(_subkey(b"enc", nonce), len(ct))))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
