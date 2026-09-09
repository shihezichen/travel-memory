import os
#!/usr/bin/env python3
"""N-19 记忆创作引擎 回归验证（verify_write.py）

覆盖：三文体生成（prose/poem/story）+ 质量约束（引用地点/长度）+ 幂等缓存 + 落库 + API + MCP。
用法：python3 scripts/verify_write.py
"""
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


def http_form(path, key, fields):
    import urllib.parse
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    print("=== N-19 记忆创作引擎 回归 ===")
    sys.path.insert(0, ".")

    out = subprocess.run(
        ["python3", os.path.join(os.environ.get("SECRETS_VAULT_DIR", ""), "scripts", "secrets_tool.py"), "get",
         "TRAVEL_COMPANION_KEY"], capture_output=True, text=True)
    key = out.stdout.strip()
    check("拿到 arthur key", bool(key))

    # 1. 领域层三文体生成（trip 5，17 点真实行程）
    import domain.generation as g
    for kind, min_len in (("prose", 100), ("poem", 40), ("story", 150)):
        r = g.generate(kind, 5)
        check(f"领域层：{kind} 生成成功", bool(r["text"]) and len(r["text"]) >= min_len,
              f"{len(r['text'])}字 tokens={r['tokens_used']}")
        check(f"领域层：{kind} 引用具体地点", any(
            name in r["text"] for name in ("巴黎", "巴塞罗那", "马德里", "塞维利亚", "格拉纳达")),
            r["text"][:40].replace("\n", " "))

    # 2. 幂等缓存：同参数第二次命中
    r1 = g.generate("poem", 5)
    check("幂等：同参数二次命中缓存", r1["cached"] is True)

    # 3. 落库
    conn = sqlite3.connect("data.db")
    cnt = conn.execute(
        "SELECT COUNT(*) FROM generations WHERE trip_id=5 AND kind IN ('prose','poem','story')"
    ).fetchone()[0]
    conn.close()
    check("落库：generations 有新 kind 记录", cnt >= 3, f"count={cnt}")

    # 4. HTTP API（表单 kind 透传）
    st, d = http_form("/api/ai/generate", key,
                      {"kind": "prose", "trip_id": "5"})
    check("API：POST generate kind=prose", st == 200 and d and "text" in d,
          f"status={st}")
    check("API：返回带 kind/trip_id", d and d.get("kind") == "prose" and d.get("trip_id") == 5)

    # 5. 非法 kind 不炸（回退 trip_summary 行为）
    st, d = http_form("/api/ai/generate", key,
                      {"kind": "not_a_kind", "trip_id": "5"})
    check("API：未知 kind 安全降级", st == 200 and d and "text" in d, f"status={st}")

    # 6. MCP generate 工具
    req = urllib.request.Request(BASE + "/mcp", method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.data = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "generate",
                   "arguments": {"kind": "poem", "trip_id": 5}},
    }).encode()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            d = json.loads(resp.read().decode())
        txt = d["result"]["content"][0]["text"]
        check("MCP：generate 工具 poem 可调用",
              not d.get("error") and ("巴黎" in txt or "铁塔" in txt),
              txt[:50].replace("\n", " "))
    except Exception as e:
        check("MCP：generate 工具 poem 可调用", False, str(e)[:80])

    print(f"\n=== {PASS}/{PASS+FAIL} 通过 ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
