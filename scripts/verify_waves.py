#!/usr/bin/env python3
# 验证四波 UI 改动（第二波2/4、第三波状态保持、第四波统计弹窗）
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
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = ctx.new_page()

    # 1. 登录（admin token 直登）
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.evaluate(f"""async () => {{
        const fd = new FormData();
        fd.append('token', {json.dumps(TOKEN)});
        await fetch('/api/login', {{method:'POST', body: fd}});
    }}""")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # 等待 trip-select 有值（已登录加载行程）
    page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
    page.wait_for_timeout(1200)

    # ---- 第二波2：add-point-bar 只在 points tab 显示 ----
    page.evaluate("switchTab('points')")
    page.wait_for_timeout(200)
    points_visible = page.evaluate("!document.querySelector('#add-point-bar').classList.contains('hidden')")
    check("第二波2: points tab 时 add-point-bar 显示", points_visible)

    page.evaluate("switchTab('detail')")
    page.wait_for_timeout(200)
    detail_hidden = page.evaluate("document.querySelector('#add-point-bar').classList.contains('hidden')")
    check("第二波2: detail tab 时 add-point-bar 隐藏", detail_hidden)

    # ---- 第四波：top-stats 点击弹统计弹窗 ----
    page.evaluate("document.querySelector('#top-stats').click()")
    page.wait_for_timeout(300)
    stats_visible = page.evaluate("!document.querySelector('#stats-modal').classList.contains('hidden')")
    check("第四波: 点击 top-stats 弹出统计弹窗", stats_visible)
    if stats_visible:
        body_txt = page.evaluate("document.querySelector('#stats-modal-body').innerText || ''")
        check("第四波: 弹窗含汇总行", "汇总" in body_txt, body_txt[:40].replace('\n',' '))
    page.evaluate("document.querySelector('#stats-modal-close').click()")
    page.wait_for_timeout(200)

    # ---- 第三波：状态保存（localStorage） ----
    state = page.evaluate("() => { try { return JSON.parse(localStorage.getItem('tm_ui_state')||'null'); } catch(e){ return null; } }")
    has_tripid = bool(state and state.get("currentTripId"))
    check("第三波: 保存 currentTripId", has_tripid, f"currentTripId={state.get('currentTripId') if state else None}")

    # 手动保存一个视野并验证 saveTripState 结构
    page.evaluate("saveTripState({zoom: 9, center: [120.0, 30.0], panelHeight: 555, pointId: 999, tab:'points'})")
    state2 = page.evaluate("() => JSON.parse(localStorage.getItem('tm_ui_state'))")
    tid = state2.get("currentTripId")
    trip_st = (state2.get("trips") or {}).get(str(tid)) if tid else None
    check("第三波: per-trip 状态写入", bool(trip_st and trip_st.get("zoom")==9 and trip_st.get("panelHeight")==555),
          f"trip={tid} st={trip_st}")

    # ---- 第二波4：replay-close 最小化（不中断） ----
    # 需要 ≥2 个点才能回放。检查绑定逻辑：replay-close onclick 引用 minimizeReplay
    close_src = page.evaluate("document.querySelector('#replay-close').onclick.toString()")
    check("第二波4: replay-close 绑定 minimizeReplay", "minimizeReplay" in close_src, close_src[:50])

    # ---- 第三波：刷新后恢复 trip（不回到第一条） ----
    trips = page.evaluate("Array.from(document.querySelectorAll('#trip-select option')).map(o=>({id:o.value, t:o.textContent}))")
    if len(trips) >= 2:
        # 切到第二个 trip（真实 selectTrip 会写 currentTripId）
        page.evaluate("selectTrip(" + json.dumps(trips[1]["id"]) + ")")
        page.wait_for_timeout(1000)
        saved_tid = page.evaluate("JSON.parse(localStorage.getItem('tm_ui_state')).currentTripId")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=8000)
        page.wait_for_timeout(1500)
        cur_tid = page.evaluate("document.querySelector('#trip-select').value")
        check("第三波: 刷新后恢复上次行程", str(cur_tid) == str(saved_tid),
              f"期望 {saved_tid} 实际 {cur_tid} (trips={[t['id'] for t in trips]})")
    else:
        check("第三波: 刷新后恢复上次行程", True, "仅 1 个行程，跳过")

    browser.close()

print("\n===== 汇总 =====")
bad = [r for r in results if not r[1]]
print(f"通过 {len(results)-len(bad)}/{len(results)}")
if bad:
    print("失败项：")
    for n,_,d in bad:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("全部通过 ✅")
