#!/usr/bin/env python3
# 详细追踪：cookie、缩略图加载、灯箱全图加载
import os
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8084"
TOKEN = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token.txt")).read().strip()

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    page.on("response", lambda r: print(f"  [resp] {r.status} {r.url}") if ("uploads" in r.url or "photo-layer" in r.url or "api/me" in r.url) else None)

    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.evaluate(f"""async () => {{
        const fd = new FormData();
        fd.append('token', {json.dumps(TOKEN)});
        const r = await fetch('/api/login', {{method:'POST', body: fd}});
        return r.status;
    }}""")
    cookies = ctx.cookies()
    print("cookies after login:", [(c['name'], c['value'][:8]+'...') for c in cookies])

    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(1200)
    trips = page.evaluate("Array.from(document.querySelectorAll('#trip-select option')).map(o=>o.value)")
    target = next((t for t in trips if t == '5'), trips[0])
    page.evaluate("selectTrip(" + json.dumps(target) + ")")
    page.wait_for_timeout(1200)

    page.evaluate("document.querySelector('#btn-layers').click()")
    page.wait_for_timeout(300)
    page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)

    # 堆叠缩略图加载状态
    stack_imgs = page.evaluate("""() => Array.from(document.querySelectorAll('.photo-stack img')).map(i=>({src:i.src.split('/').pop(), ok:i.naturalWidth>0}))""")
    print("stack thumbs:", stack_imgs)

    # 真实鼠标点击第一个 stack
    page.evaluate("""() => {
        const el = document.querySelector('.amap-marker .photo-stack');
        el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    }""")
    page.wait_for_timeout(1200)
    lb_visible = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
    print("lightbox visible:", lb_visible)
    if lb_visible:
        info = page.evaluate("""() => Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>({src:i.src, ok:i.naturalWidth>0}))""")
        print("lightbox imgs:", info)
        counter = page.evaluate("document.querySelector('#lightbox-counter').textContent")
        print("counter:", counter)
    browser.close()
