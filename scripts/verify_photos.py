#!/usr/bin/env python3
# 验证照片图层：地图上各点照片缩略图堆叠显示 + 可关闭
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

    # 切到 arthur 的西班牙行程（含照片点）
    trips = page.evaluate("Array.from(document.querySelectorAll('#trip-select option')).map(o=>o.value)")
    # admin 登录会看到所有行程，切到有照片的行程（巴黎/巴塞罗那/马德里所在行程 id=5）
    target = next((t for t in trips if t == '5'), trips[0])
    page.evaluate("selectTrip(" + json.dumps(target) + ")")
    page.wait_for_timeout(1200)

    # 1. 打开图层面板
    page.evaluate("document.querySelector('#btn-layers').click()")
    page.wait_for_timeout(300)
    panel_visible = page.evaluate("!document.querySelector('#layer-panel').classList.contains('hidden')")
    check("图层面板可打开", panel_visible)

    # 2. 勾选 lay-photos
    page.evaluate("document.querySelector('#lay-photos').checked = true; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(2500)

    # 3. 验证 photoLayerMarkers 生成
    n_markers = page.evaluate("photoLayerMarkers.length")
    check("照片图层 marker 生成（>0）", n_markers > 0, f"markers={n_markers}")

    # 4. 验证多图堆叠 + 角标
    stacks = page.evaluate("""() => {
        return document.querySelectorAll('.amap-marker .photo-stack').length
    }""")
    check("地图上渲染照片堆 DOM", stacks > 0, f"photo-stack DOM={stacks}")

    # 5. 验证 +N 角标（马德里 5 张 → badge 显示 5）
    badges = page.evaluate("""() => Array.from(document.querySelectorAll('.photo-stack-badge')).map(b=>b.textContent)""")
    check("多图角标渲染", len(badges) > 0, f"badges={badges}")

    # 6. 验证单点内多图堆叠（巴黎 3 张 → 一个 stack 内 img 数=3）
    img_counts = page.evaluate("""() => Array.from(document.querySelectorAll('.photo-stack')).map(s=>s.querySelectorAll('img').length)""")
    has_stack3 = any(c >= 3 for c in img_counts)
    check("单点多图堆叠（含 3 张的点）", has_stack3, f"各 stack img 数={img_counts}")

    # 7. 关闭图层
    page.evaluate("document.querySelector('#lay-photos').checked = false; document.querySelector('#lay-photos').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(800)
    stacks_after = page.evaluate("document.querySelectorAll('.amap-marker .photo-stack').length")
    check("关闭图层后照片堆移除", stacks_after == 0, f"剩余={stacks_after}")

    browser.close()

print("\n===== 汇总 =====")
bad = [r for r in results if not r[1]]
print(f"通过 {len(results)-len(bad)}/{len(results)}")
if bad:
    for n,_,d in bad:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("全部通过 ✅")
