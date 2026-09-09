#!/usr/bin/env python3
# 全面验证：1)灯箱图片可显示 2)角标右上角 3)刷新保留图层勾选+灯箱
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

    # 登录
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.evaluate(f"""async () => {{
        const fd = new FormData(); fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
    }}""")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(1200)

    # 打开照片图层
    page.evaluate("document.querySelector('#btn-layers').click()")
    page.wait_for_timeout(300)
    page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)

    # ---- Fix 1: 点击缩略图 → 灯箱图片加载 ----
    n = page.evaluate("photoLayerMarkers.length")
    check("照片图层 marker 生成", n > 0, f"markers={n}")
    # 等待地图动画停稳（marker 坐标两次采样一致）再点击，避免时序抖动
    def stable_center(sel, tries=8):
        last = None
        for _ in range(tries):
            box = page.evaluate("""(sel) => { const el=document.querySelector(sel); if(!el) return null;
                const r=el.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; }""", sel)
            if box and last and abs(box["x"]-last["x"]) < 0.5 and abs(box["y"]-last["y"]) < 0.5:
                return box
            last = box
            page.wait_for_timeout(400)
        return last
    box = stable_center(".amap-marker .photo-stack")
    check("缩略图位置稳定可点", box is not None, str(box))
    if box:
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(1500)
    lb = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
    check("点击缩略图打开灯箱", lb)
    if lb:
        imgs = page.evaluate("""() => Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>({src:i.src, ok:i.naturalWidth>0}))""")
        check("灯箱图片加载成功", all(i["ok"] for i in imgs) and len(imgs) > 0, json.dumps(imgs))

    # ---- Fix 2: 角标右上角 ----
    badge_pos = page.evaluate("""() => {
        const b = document.querySelector('.photo-stack-badge');
        if (!b) return null;
        const cs = getComputedStyle(b);
        const r = b.getBoundingClientRect();
        const stack = b.closest('.photo-stack').getBoundingClientRect();
        return {top: cs.top, bottom: cs.bottom, badgeY: r.y, stackTop: stack.y, badgeH: r.height};
    }""")
    check("角标在右上角(top 定位)", badge_pos and badge_pos["top"] == "-5px" and badge_pos["badgeY"] < badge_pos["stackTop"] + badge_pos["badgeH"] * 0.6, json.dumps(badge_pos))

    # ---- Fix 3a: 刷新保留图层勾选 ----
    # 记录刷新前 localStorage
    before = page.evaluate("localStorage.getItem('tm_layer_state')")
    check("图层状态已写入 localStorage", before and '"photos":true' in before, before)
    # 记录灯箱状态
    lb_state = page.evaluate("localStorage.getItem('tm_lightbox_state')")
    check("灯箱状态已写入 localStorage", lb_state is not None and '"point_id"' in lb_state, lb_state)

    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(2000)

    photos_checked = page.evaluate("document.querySelector('#lay-photos').checked")
    check("刷新后照片图层仍勾选", photos_checked is True)
    n2 = page.evaluate("photoLayerMarkers ? photoLayerMarkers.length : -1")
    check("刷新后照片 marker 恢复", n2 == n, f"before={n} after={n2}")
    lb2 = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
    check("刷新后灯箱恢复打开", lb2)
    if lb2:
        imgs2 = page.evaluate("""() => Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>({src:i.src, ok:i.naturalWidth>0}))""")
        check("刷新后灯箱图片加载", all(i["ok"] for i in imgs2) and len(imgs2) > 0, json.dumps(imgs2))

    # 关闭灯箱 → 状态清除
    page.evaluate("document.querySelector('#lightbox-close').click()")
    page.wait_for_timeout(300)
    cleared = page.evaluate("localStorage.getItem('tm_lightbox_state')")
    check("关闭灯箱后状态清除", cleared is None)

    # 取消勾选图层 → 状态更新
    page.evaluate("document.querySelector('#lay-photos').checked = false; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(300)
    after = page.evaluate("localStorage.getItem('tm_layer_state')")
    check("取消勾选后状态更新", after and '"photos":false' in after, after)

    # 无 JS 报错（过滤已知噪音：预登录 401、令牌提示、沙箱 Autocomplete 插件加载失败）
    NOISE = ("401", "未登录或令牌无效", "Autocomplete")
    real = [e for e in errors if not any(n in e for n in NOISE)]
    check("无 JS 报错", len(real) == 0, "; ".join(real[:5]) if real else ("噪音已过滤" if errors else ""))
    browser.close()

print("\n===== 汇总 =====")
bad = [r for r in results if not r[1]]
print(f"通过 {len(results)-len(bad)}/{len(results)}")
if bad:
    for n,_,d in bad: print(f"  - {n}: {d}")
    sys.exit(1)
print("全部通过 ✅")
