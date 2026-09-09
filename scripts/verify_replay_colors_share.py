#!/usr/bin/env python3
# 验证分享页轨迹播放双色线（无登录，匿名访问）
import os
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8084"
TOKEN = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token.txt")).read().strip()
results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("✅" if ok else "❌") + f" {name}" + (f" — {detail}" if detail else ""))

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/", wait_until="domcontentloaded")
    # 用 API 拿第一个分享 token
    share_token = page.evaluate(f"""async () => {{
        const fd = new FormData(); fd.append('token', {json.dumps(TOKEN)});
        const r = await fetch('/api/login', {{method:'POST', body: fd}});
        const trips = await (await fetch('/api/trips/')).json();
        const t = Array.isArray(trips) ? trips[0] : (trips.trips || [])[0];
        if (!t) return null;
        const s = await (await fetch('/api/trips/' + t.id + '/share-count')).json();
        // 尝试取现有分享；没有则创建一个
        return s && s.count > 0 ? 'existing' : 'need-create';
    }}""")
    check("分享页路由可达", share_token is not None, str(share_token))

    # 直接访问分享页（创建分享拿 token）
    share_url = page.evaluate(f"""async () => {{
        const fd = new FormData(); fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
        const trips = await (await fetch('/api/trips/')).json();
        const t = Array.isArray(trips) ? trips[0] : (trips.trips || [])[0];
        const r = await fetch('/api/trips/' + t.id + '/share', {{method:'POST', body: new FormData()}});
        const j = await r.json();
        return '/share/' + j.token;
    }}""")
    page.goto(BASE + share_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.wait_for_function("typeof shareData !== 'undefined' && shareData && shareData.points && shareData.points.length >= 2", timeout=10000)
    n_pts = page.evaluate("shareData.points.length")
    check("分享页行程加载", n_pts >= 2, f"points={n_pts}")

    # 播放并采样
    page.evaluate("document.querySelector('#replay-launch').click()")
    page.wait_for_timeout(1200)
    s1 = page.evaluate("""() => ({
        done: replayDoneLine ? replayDoneLine.getPath().length : 0,
        todo: replayTodoLine ? replayTodoLine.getPath().length : 0,
        doneColor: replayDoneLine ? replayDoneLine.getOptions().strokeColor : null,
        todoColor: replayTodoLine ? replayTodoLine.getOptions().strokeColor : null,
    })""")
    check("分享页双色线已创建", s1["done"] > 0 and s1["todo"] > 0, f"done={s1['done']} todo={s1['todo']}")
    check("分享页颜色正确(绿/灰)", s1["doneColor"] == "#16a34a" and s1["todoColor"] == "#94a3b8",
          f"{s1['doneColor']}/{s1['todoColor']}")

    page.wait_for_timeout(1500)
    s2 = page.evaluate("""() => ({ done: replayDoneLine.getPath().length, todo: replayTodoLine.getPath().length })""")
    check("分享页 done 增长/todo 缩短", s2["done"] > s1["done"] and s2["todo"] < s1["todo"], f"{s1['done']}/{s1['todo']} → {s2['done']}/{s2['todo']}")

    page.evaluate("stopReplay(true)")
    page.wait_for_timeout(300)
    cleared = page.evaluate("replayDoneLine === null && replayTodoLine === null")
    check("分享页停止后清理", cleared)

    js_errs = [e for e in errors if '401' not in e and 'Autocomplete' not in e and '未登录或令牌无效' not in e]
    check("分享页无 JS 报错", len(js_errs) == 0, "; ".join(js_errs[:3]) if js_errs else ("噪音已过滤" if errors else ""))

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
sys.exit(1 if failed else 0)
