#!/usr/bin/env python3
"""N-21 未完成旅程识别 回归验证（verify_unfinished.py）

覆盖：规则层三类信号 + HTTP API + MCP 工具 + 鉴权。
用法：python3 scripts/verify_unfinished.py
"""
import os
import json
import sqlite3
import subprocess
import sys
import urllib.request

BASE = "http://127.0.0.1:8084"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def api_key(name):
    conn = sqlite3.connect("data.db")
    row = conn.execute(
        "SELECT key_prefix FROM api_keys WHERE name=?", (name,)).fetchone()
    conn.close()
    # 明文 key 从 vault 取（prefix 仅确认存在）
    out = subprocess.run(
        ["python3", os.path.join(os.environ.get("SECRETS_VAULT_DIR", ""), "scripts", "secrets_tool.py"), "get",
         {"companion-arthur": "TRAVEL_COMPANION_KEY"}.get(name, name)],
        capture_output=True, text=True)
    return out.stdout.strip() if row else None


def http(method, path, key, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    print("=== N-21 未完成旅程识别 回归 ===")

    # 1. 规则层单元（直接 import）
    sys.path.insert(0, ".")
    import db
    import domain.unfinished as u
    user = db.get_user_by_id(3)
    r = u.find_unfinished(user, db)
    check("规则层：arthur 有未完成信号", r["has_unfinished"] is True)
    names = [t["name"] for t in r["unclosed_trips"]]
    check("规则层：识别出「2025-巴黎-西班牙」走完未归档",
          any("巴黎" in n for n in names),
          str(names))
    check("规则层：17 点行程归入 unclosed（非 unfulfilled）",
          r["unfulfilled_destinations"] == [],
          f"unfulfilled={r['unfulfilled_destinations']}")
    check("规则层：nudge 已生成（LLM 或降级模板）",
          bool(r["nudge"]) and len(r["nudge"]) > 10,
          (r["nudge"] or "")[:40])

    # 2. HTTP API（arthur key）
    key = api_key("companion-arthur")
    check("API：拿到 arthur key", bool(key))
    if key:
        st, d = http("GET", "/api/ai/unfinished", key)
        check("API：200 + has_unfinished", st == 200 and d.get("has_unfinished") is True,
              f"status={st}")
        check("API：返回 nudge", bool(d.get("nudge")))

    # 3. 鉴权：无 key → 401
    st, _ = http("GET", "/api/ai/unfinished", None)
    check("API：无 key 返回 401", st == 401, f"status={st}")

    # 4. MCP Streamable HTTP 工具
    if key:
        req = urllib.request.Request(BASE + "/mcp", method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.data = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "find_unfinished_journeys", "arguments": {}},
        }).encode()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                d = json.loads(resp.read().decode())
            txt = d["result"]["content"][0]["text"]
            check("MCP：find_unfinished_journeys 可调用",
                  not d.get("error") and "has_unfinished" in txt,
                  txt[:60])
        except Exception as e:
            check("MCP：find_unfinished_journeys 可调用", False, str(e)[:80])

    print(f"\n=== {PASS}/{PASS+FAIL} 通过 ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
