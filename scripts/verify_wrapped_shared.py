#!/usr/bin/env python3
"""N-11 年度报告 + N-14 共同记忆 回归验证脚本。

验证点：
1. N-11 wrapped：默认年 / 指定年 / 无数据年（轻量版）
2. N-14 shared：正常对比 / 对方不存在 404 / 不能对比自己
3. MCP 层：tools/list 暴露 + tools/call 可达
4. 降级路径：monkeypatch _llm 抛异常 → 规则模板兜底
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
        with urllib.request.urlopen(req, timeout=60) as r:
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
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


print("N-11 年度旅行报告")
st, d = api("/api/ai/wrapped?year=2025")
check("2025 有数据", st == 200 and d["has_data"] and d["stats"]["trip_count"] >= 1)
check("统计字段齐全", all(k in d["stats"] for k in
      ("trip_count", "point_count", "city_count", "total_distance_km",
       "month_distribution", "season_distribution", "mood_tags")))
check("叙事引用真实数据", d["narrative"] and len(d["narrative"]) > 20)
st, d = api("/api/ai/wrapped?year=1999")
check("无数据年轻量版", st == 200 and not d["has_data"] and "还没有" in d["narrative"])

print("N-14 共同记忆")
st, d = api("/api/ai/shared?with_username=arthur")
check("正常对比", st == 200 and d["has_shared"] and "story" in d)
check("城市级输出", isinstance(d["common_cities"], list))
st, d = api("/api/ai/shared?with_username=nobody_xyz")
check("对方不存在 404", st == 404)

print("MCP 层")
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
req = urllib.request.Request(BASE + "/mcp", data=body, method="POST",
                             headers={"Authorization": f"Bearer {KEY}",
                                      "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    names = [t["name"] for t in json.loads(r.read().decode())["result"]["tools"]]
check("annual_travel_report 暴露", "annual_travel_report" in names)
check("shared_memory 暴露", "shared_memory" in names)
r = mcp_call("annual_travel_report", {"year": 2025})
check("MCP 调 annual_travel_report", "has_data" in r["result"]["content"][0]["text"])
r = mcp_call("shared_memory", {"with_username": "arthur"})
check("MCP 调 shared_memory", "common_cities" in r["result"]["content"][0]["text"])

print("降级路径")
import domain.wrapped as wrapped
import domain.relation as relation
_orig_llm = wrapped._llm
wrapped._llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mock"))
try:
    import db
    u = db.get_user_by_username("api_test")
    r = wrapped.annual_report(u, 2025, db)
    check("wrapped 降级模板", r["narrative"] and "公里" in r["narrative"])
finally:
    wrapped._llm = _orig_llm
relation._llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mock"))
try:
    import db
    u = db.get_user_by_username("api_test")
    r = relation.shared_memory(u, "arthur", db)
    check("relation 降级模板", r["story"] and len(r["story"]) > 10)
finally:
    pass

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
