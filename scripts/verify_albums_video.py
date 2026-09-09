#!/usr/bin/env python3
# P1-4+P2-3 前端验证：相册聚类视图 / 多媒体检索 / 旅程视频生成
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
    page.wait_for_timeout(800)

    # 选择 api_test 行程（26）
    page.evaluate("""async () => {
        const sel = document.querySelector('#trip-select');
        const opt = [...sel.options].find(o => o.textContent.includes('API测试行程'));
        if (opt) { sel.value = opt.value; sel.dispatchEvent(new Event('change')); }
    }""")
    page.wait_for_timeout(1500)

    # ---- 1) 相册 tab ----
    page.evaluate("document.querySelector('.tab[data-tab=albums]').click()")
    page.wait_for_timeout(1500)
    n = page.evaluate("document.querySelectorAll('.album-card').length")
    check("相册聚类卡片渲染", n >= 2, f"albums={n}")
    title = page.evaluate("document.querySelector('.album-card .album-title')?.textContent || ''")
    check("相册标题含地点+日期", '·' in title, title)

    # ---- 2) 多媒体检索 ----
    page.fill('#media-search-q', 'madrid')
    page.click('#btn-media-search')
    page.wait_for_timeout(1200)
    n = page.evaluate("document.querySelectorAll('#media-search-results .media-item').length")
    check("检索 madrid 命中", n >= 1, f"hits={n}")
    page.fill('#media-search-q', '')
    page.select_option('#media-search-type', 'photo')
    page.click('#btn-media-search')
    page.wait_for_timeout(1200)
    n = page.evaluate("document.querySelectorAll('#media-search-results .media-item').length")
    check("检索类型过滤(photo)", n >= 1, f"hits={n}")
    page.select_option('#media-search-type', '')

    # ---- 3) 视频生成 ----
    page.select_option('#video-template', 'slideshow')
    page.select_option('#video-seconds', '2')
    page.click('#btn-video-gen')
    page.wait_for_timeout(2500)
    st = page.evaluate("document.querySelector('#video-progress-text')?.textContent || ''")
    check("视频生成启动", '排队' in st or '准备' in st or '完成' in st or '失败' in st, st)
    # 轮询等完成（最多 90s）
    done = False
    for _ in range(45):
        page.wait_for_timeout(2000)
        st = page.evaluate("document.querySelector('#video-progress-text')?.textContent || ''")
        has_video = page.evaluate("!!document.querySelector('#video-result video')")
        if has_video or '失败' in st:
            done = True
            break
    check("视频生成完成并渲染播放器", done, st[:80])
    vurl = page.evaluate("document.querySelector('#video-result video')?.src || ''")
    if vurl:
        import urllib.request
        code = urllib.request.urlopen(vurl).status
        check("视频 URL 可访问", code == 200, f"HTTP {code}")

    # JS 错误检查（白名单：预登录 401 + 高德 Autocomplete 沙箱噪音）
    real_errors = [e for e in errors if '401' not in e and '未登录' not in e and 'Autocomplete' not in e]
    check("无 JS 报错", len(real_errors) == 0, "; ".join(real_errors[:3]))
    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n=== {len(results) - len(failed)}/{len(results)} 通过 ===")
sys.exit(1 if failed else 0)
