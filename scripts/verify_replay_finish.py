#!/usr/bin/env python3
# 验证轨迹回放播完边界：进度条应显示 17/17 巴塞罗那（而非卡在 16/17 锡切斯）
# 回归场景：刚哥报告「自动播放完毕后进度条显示 16/17 锡切斯，造成没走完的错觉」
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
    page.evaluate(f"""async () => {{
        const fd = new FormData(); fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
    }}""")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(1500)

    # 选择「2025-巴黎-西班牙」行程（17 点，终点巴塞罗那）
    picked = page.evaluate("""() => {
        const sel = document.querySelector('#trip-select');
        for (const opt of sel.options) {
            if (opt.textContent.includes('巴黎')) { sel.value = opt.value; sel.dispatchEvent(new Event('change')); return opt.textContent.trim(); }
        }
        return null;
    }""")
    check("已选中巴黎-西班牙行程", picked is not None, picked or "未找到")
    page.wait_for_timeout(4000)
    n_pts = page.evaluate("lastPoints ? lastPoints.length : 0")
    check("行程点数=17", n_pts == 17, f"points={n_pts}")

    # 打开回放，起点应显示 1/17
    page.evaluate("document.querySelector('#replay-launch').click()")
    page.wait_for_timeout(400)
    s0 = page.evaluate("""() => ({
        count: document.querySelector('#replay-count').textContent,
        name: document.querySelector('#replay-name').textContent,
    })""")
    check("起点显示 1/17", s0["count"] == "1/17", f"{s0['count']} {s0['name']}")

    # 播放 3s（30s 总长，约 10% 进度），不应提前显示 17/17
    page.wait_for_timeout(3000)
    s1 = page.evaluate("""() => ({
        count: document.querySelector('#replay-count').textContent,
        t: replayT, total: replayTotal,
    })""")
    check("播放中未提前到 17/17", s1["count"] != "17/17", f"{s1['count']} t={s1['t']:.0f}/{s1['total']:.0f}m")

    # 快进到距终点 100 米，等自然播完（playLoop 触发 finishReplay）
    page.evaluate("replayT = replayTotal - 100;")
    # 轮询等待播完（箭头函数形式访问 let 变量；固定等待会撞 rAF 节流偶发失败）
    page.wait_for_function("() => replayActive === false", timeout=8000)
    page.wait_for_timeout(300)
    s2 = page.evaluate("""() => ({
        count: document.querySelector('#replay-count').textContent,
        name: document.querySelector('#replay-name').textContent,
        fill: document.querySelector('#replay-fill').style.width,
        active: replayActive,
        icon: document.querySelector('#replay-toggle').innerHTML,
    })""")
    check("播完显示 17/17", s2["count"] == "17/17", f"{s2['count']} {s2['name']}")
    check("播完显示巴塞罗那", s2["name"] == "巴塞罗那", s2["name"])
    check("进度条 100%", s2["fill"] == "100%", s2["fill"])
    check("播放已停止(可重播)", s2["active"] is False and "<svg" in s2["icon"], f"active={s2['active']} icon={'svg' if '<svg' in s2['icon'] else repr(s2['icon'][:20])}")

    # JS 错误白名单过滤（预登录 401 / Autocomplete 沙箱噪音）
    real = [e for e in errors if "401" not in e and "Autocomplete" not in e and "AMap" not in e and "未登录或令牌无效" not in e]
    check("无 JS 错误", len(real) == 0, "; ".join(real[:3]) if real else "clean")

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n=== {len(results)-len(failed)}/{len(results)} 通过 ===")
sys.exit(1 if failed else 0)
