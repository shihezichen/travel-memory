#!/usr/bin/env python3
# 分享页灯箱冒烟测试
import os
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8084"
import sqlite3
c = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.db"))
row = ("TyVwu8NziH3gcPLaaHo1rUdl-TYOJ19n",)
if not row:
    print("无 trip 5 分享链接，跳过"); sys.exit(0)
TOKEN = row[0]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(f"{BASE}/share/{TOKEN}", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.wait_for_function("document.querySelectorAll('.marker-dot, .photo-stack').length > 0", timeout=10000)
    page.wait_for_timeout(1000)

    # 打开照片图层（分享页如有）
    has_layer = page.evaluate("!!document.querySelector('#lay-photos')")
    if has_layer:
        page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
        page.wait_for_timeout(2000)
    n = page.evaluate("document.querySelectorAll('.amap-marker .photo-stack').length")
    print("photo-stack count:", n)
    if n:
        box = page.evaluate("""() => { const el = document.querySelector('.amap-marker .photo-stack'); const r = el.getBoundingClientRect(); return {x: r.x+r.width/2, y: r.y+r.height/2}; }""")
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(1500)
        lb = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
        print("lightbox:", lb)
        if lb:
            imgs = page.evaluate("Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>i.naturalWidth>0)")
            print("imgs loaded:", imgs)
            print("✅ 分享页灯箱 OK" if all(imgs) else "❌ 分享页灯箱图片失败")
    else:
        # 点轨迹点详情里的媒体网格
        page.mouse.click(400, 400)
        page.wait_for_timeout(800)
        grid = page.evaluate("document.querySelectorAll('#media-grid img').length")
        print("media-grid imgs:", grid)
        if grid:
            page.evaluate("document.querySelector('#media-grid img').click()")
            page.wait_for_timeout(1200)
            lb = page.evaluate("!document.querySelector('#lightbox').classList.contains('hidden')")
            ok = page.evaluate("Array.from(document.querySelectorAll('#lightbox-stage img')).map(i=>i.naturalWidth>0)")
            print("lightbox:", lb, "imgs:", ok)
            print("✅ 分享页灯箱 OK" if lb and all(ok) else "❌ 分享页灯箱失败")
    browser.close()
