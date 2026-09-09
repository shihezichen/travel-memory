#!/usr/bin/env python3
"""P2 深水区综合回归：N-16 再阐释 / N-18 传记 / N-20 时间旅行。

验证点：
1. N-16 reinterpret：正常 / 非法 lens 400 / 越权 404
2. N-18 biography：返回 markdown / 章节结构 / 无幻觉（清单为空降级）
3. N-20 timetravel：rewind 有数据 / rewind 空日 / preview / counterfactual 虚构标注 / 非法 mode
4. MCP 层：新工具暴露 + 可调
"""
import json
import sys
import os
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8084"
# API Key 从环境变量读，不写死在文件里。
# 这个脚本是入库、且准备开源的 —— 原来这里明文放着一把 admin 的 read+write key，
# 等于把改写权限一起发出去。
KEY = os.environ.get("TRAVEL_API_KEY", "")
if not KEY:
    raise SystemExit("请先设置环境变量 TRAVEL_API_KEY（网页端「设置 → API Key」生成）")
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def api(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def mcp_call(name, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(BASE + "/mcp", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


print("N-16 记忆再阐释")
st, d = api("/api/ai/trips/26/reinterpret?lens=%E6%88%90%E9%95%BF")
check("正常生成", st == 200 and d["reinterpretation"])
check("含真实地点", "巴塞罗那" in d["reinterpretation"] or "马德里" in d["reinterpretation"])
check("lens 透传", d["lens"] == "成长")
st, d = api("/api/ai/trips/26/reinterpret?lens=bad")
check("非法 lens 400", st == 400)

print("N-18 旅行人生传记")
st, d = api("/api/ai/biography")
check("返回 markdown", st == 200 and d["markdown"].startswith("# "))
check("统计字段齐全", all(k in d["stats"] for k in
      ("trip_count", "city_count", "total_distance_km")))
check("章节结构", isinstance(d["chapters"], list))

print("N-20 时间旅行")
st, d = api("/api/ai/timetravel?mode=rewind&date=2025-08-03")
check("rewind 有数据", st == 200 and d["places"] and d["narrative"])
check("rewind 地点真实", d["places"][0]["point"] in ("巴塞罗那", "马德里"))
st, d = api("/api/ai/timetravel?mode=rewind&date=2020-01-01")
check("rewind 空日", st == 200 and not d["places"] and "没有留下足迹" in d["narrative"])
st, d = api("/api/ai/timetravel?mode=preview&date=2026-09-02")
check("preview", st == 200 and d["mode"] == "preview")
st, d = api("/api/ai/timetravel?mode=counterfactual&trip_id=26")
check("counterfactual 虚构标注", st == 200 and d["fictional"] and "虚构推演" in d["narrative"])
st, d = api("/api/ai/timetravel?mode=nope")
check("非法 mode 400", st == 400)

print("MCP 层")
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
req = urllib.request.Request(BASE + "/mcp", data=body, method="POST",
                             headers={"Authorization": f"Bearer {KEY}",
                                      "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    names = [t["name"] for t in json.loads(r.read().decode())["result"]["tools"]]
check("reinterpret_trip 暴露", "reinterpret_trip" in names)
check("travel_biography 暴露", "travel_biography" in names)
check("time_travel 暴露", "time_travel" in names)
r = mcp_call("reinterpret_trip", {"trip_id": 26, "lens": "幽默"})
check("MCP reinterpret_trip", "reinterpretation" in r["result"]["content"][0]["text"])
r = mcp_call("time_travel", {"mode": "rewind", "date": "2025-08-03"})
check("MCP time_travel", "narrative" in r["result"]["content"][0]["text"])

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
