#!/usr/bin/env python3
# 验证分享页轨迹回放播完边界：进度条应显示「17/17 · 巴塞罗那」（而非卡在 16/17 锡切斯）
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
    # 登录并创建 trip 5（巴黎-西班牙，17 点）的分享链接
    share_url = page.evaluate(f"""async () => {{
        const fd = new FormData(); fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
        const r = await fetch('/api/trips/5/share', {{method:'POST', body: new FormData()}});
        const j = await r.json();
        return '/share/' + j.token;
    }}""")
    check("分享链接已创建", share_url.startswith('/share/'), share_url)

    page.goto(BASE + share_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.wait_for_function("typeof shareData !== 'undefined' && shareData && shareData.points && shareData.points.length >= 2", timeout=10000)
    n_pts = page.evaluate("shareData.points.length")
    last_name = page.evaluate("shareData.points[shareData.points.length - 1].name")
    check("分享页行程加载(17点)", n_pts == 17, f"points={n_pts} 终点={last_name}")

    # 打开回放，起点 1/17
    page.evaluate("document.querySelector('#replay-launch').click()")
    page.wait_for_timeout(400)
    s0 = page.evaluate("document.querySelector('#replay-name').textContent")
    check("起点显示 1/17", s0.startswith("1/17"), s0)

    # 播放 3s 后快进到距终点 100 米，等自然播完
    page.wait_for_timeout(3000)
    s1 = page.evaluate("document.querySelector('#replay-name').textContent")
    check("播放中未提前到 17/17", not s1.startswith("17/17"), s1)
    page.evaluate("replayT = replayTotal - 100;")
    # 轮询等待播完（箭头函数形式访问 let 变量；固定等待会撞 rAF 节流偶发失败）
    page.wait_for_function("() => replayActive === false", timeout=8000)
    page.wait_for_timeout(300)
    s2 = page.evaluate("""() => ({
        name: document.querySelector('#replay-name').textContent,
        fill: document.querySelector('#replay-fill').style.width,
        active: replayActive,
    })""")
    check("播完显示 17/17 · 巴塞罗那", s2["name"] == "17/17 · 巴塞罗那", s2["name"])
    check("进度条 100%", s2["fill"] == "100%", s2["fill"])
    check("播放已停止", s2["active"] is False, f"active={s2['active']}")

    real = [e for e in errors if "401" not in e and "Autocomplete" not in e and "AMap" not in e and "未登录或令牌无效" not in e]
    check("无 JS 错误", len(real) == 0, "; ".join(real[:3]) if real else "clean")

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n=== {len(results)-len(failed)}/{len(results)} 通过 ===")
sys.exit(1 if failed else 0)
