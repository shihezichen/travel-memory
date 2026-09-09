#!/usr/bin/env python3
# 复现/验证：点击照片图层缩略图 → 灯箱是否打开、图片是否加载
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
        const fd = new FormData();
        fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
    }}""")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(1200)

    # 切到有照片的行程
    trips = page.evaluate("Array.from(document.querySelectorAll('#trip-select option')).map(o=>o.value)")
    target = next((t for t in trips if t == '5'), trips[0])
    page.evaluate("selectTrip(" + json.dumps(target) + ")")
    page.wait_for_timeout(1200)

    # 打开图层并勾选照片
    page.evaluate("document.querySelector('#btn-layers').click()")
    page.wait_for_timeout(300)
    page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)

    n = page.evaluate("photoLayerMarkers.length")
    check("照片图层 marker 生成", n > 0, f"markers={n}")

    # 点击第一个照片堆
    page.evaluate("""() => {
        const m = photoLayerMarkers[0];
        m.emit ? m.emit('click', {}) : null;
    }""")
    # 更真实：直接点击 DOM
    page.wait_for_timeout(500)
    page.evaluate("""() => {
        const el = document.querySelector('.amap-marker .photo-stack');
        if (el) el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    }""")
    page.wait_for_timeout(1500)

    lb_visible = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
    check("点击缩略图后灯箱打开", lb_visible)

    if lb_visible:
        stage_html = page.evaluate("document.querySelector('#lightbox-stage').innerHTML")
        imgs = page.evaluate("Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>({src:i.src, complete:i.complete, naturalWidth:i.naturalWidth}))")
        check("灯箱内有图片元素", '<img' in stage_html, stage_html[:120])
        if imgs:
            ok_img = all(i["naturalWidth"] > 0 for i in imgs)
            check("图片加载成功(naturalWidth>0)", ok_img, json.dumps(imgs))
        else:
            check("图片加载成功", False, "无 img 元素")

    check("无 JS 报错", len(errors) == 0, "; ".join(errors[:5]) if errors else "")

    browser.close()

print("\n===== 汇总 =====")
bad = [r for r in results if not r[1]]
print(f"通过 {len(results)-len(bad)}/{len(results)}")
if bad:
    for n,_,d in bad:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("全部通过 ✅")
