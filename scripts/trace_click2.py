#!/usr/bin/env python3
# 真实鼠标点击测试：marker-dot 与 photo-stack
import os
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8084"
TOKEN = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token.txt")).read().strip()

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
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
    trips = page.evaluate("Array.from(document.querySelectorAll('#trip-select option')).map(o=>o.value)")
    target = next((t for t in trips if t == '5'), trips[0])
    page.evaluate("selectTrip(" + json.dumps(target) + ")")
    page.wait_for_timeout(1200)

    print("AMap version:", page.evaluate("AMap.version"))

    # 1) 先测 marker-dot 真实点击
    dot_box = page.evaluate("""() => {
        const el = document.querySelector('.amap-marker .marker-dot');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width/2, y: r.y + r.height/2, text: el.textContent};
    }""")
    print("dot box:", dot_box)
    if dot_box:
        page.mouse.click(dot_box["x"], dot_box["y"])
        page.wait_for_timeout(800)
        detail_visible = page.evaluate("""() => {
            const d = document.querySelector('#point-detail, .point-detail, #detail-panel');
            return d ? !d.classList.contains('hidden') : false;
        }""")
        print("after dot click, detail visible:", detail_visible)

    # 2) 照片图层
    page.evaluate("document.querySelector('#btn-layers').click()")
    page.wait_for_timeout(300)
    page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)

    stack_box = page.evaluate("""() => {
        const el = document.querySelector('.amap-marker .photo-stack');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width/2, y: r.y + r.height/2};
    }""")
    print("stack box:", stack_box)
    if stack_box:
        page.mouse.click(stack_box["x"], stack_box["y"])
        page.wait_for_timeout(1200)
        lb = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
        print("after stack real click, lightbox visible:", lb)
        if lb:
            info = page.evaluate("""() => Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>({src:i.src.split('/').pop(), ok:i.naturalWidth>0}))""")
            print("lightbox imgs:", info)

    # 3) 关闭灯箱再点 photo-marker 📷 角标
    page.evaluate("document.querySelector('#lightbox-close').click()")
    page.wait_for_timeout(300)
    pm_box = page.evaluate("""() => {
        const el = document.querySelector('.amap-marker .photo-marker');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width/2, y: r.y + r.height/2};
    }""")
    print("photo-marker box:", pm_box)
    if pm_box:
        page.mouse.click(pm_box["x"], pm_box["y"])
        page.wait_for_timeout(1200)
        lb = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
        print("after photo-marker click, lightbox visible:", lb)

    browser.close()
