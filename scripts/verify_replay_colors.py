#!/usr/bin/env python3
# 验证轨迹播放双色线：播放中 replayDoneLine（绿）随进度增长、replayTodoLine（灰）随进度缩短；停止后清理
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

    # 选择一个行程并等待路线加载
    page.evaluate("""() => {
        const sel = document.querySelector('#trip-select');
        if (sel && sel.options.length) { sel.value = sel.options[0].value; sel.dispatchEvent(new Event('change')); }
    }""")
    page.wait_for_timeout(4000)
    n_pts = page.evaluate("lastPoints ? lastPoints.length : 0")
    check("行程已加载", n_pts >= 2, f"points={n_pts}")

    # 打开回放
    page.evaluate("document.querySelector('#replay-launch').click()")
    page.wait_for_timeout(500)
    bar_visible = page.evaluate("!document.querySelector('#replay-bar').classList.contains('hidden')")
    check("回放条已打开", bar_visible)

    # 播放 1.2s 后采样双色线
    page.wait_for_timeout(1200)
    s1 = page.evaluate("""() => ({
        done: replayDoneLine ? replayDoneLine.getPath().length : 0,
        todo: replayTodoLine ? replayTodoLine.getPath().length : 0,
        t: replayT, total: replayTotal,
        doneColor: replayDoneLine ? replayDoneLine.getOptions().strokeColor : null,
        todoColor: replayTodoLine ? replayTodoLine.getOptions().strokeColor : null,
    })""")
    check("双色线已创建", s1["done"] > 0 and s1["todo"] > 0, f"done={s1['done']}pts todo={s1['todo']}pts")
    check("颜色正确(绿/灰)", s1["doneColor"] == "#16a34a" and s1["todoColor"] == "#94a3b8",
          f"{s1['doneColor']}/{s1['todoColor']}")
    check("已播放进度 >0", s1["t"] > 0, f"t={s1['t']:.0f}/{s1['total']:.0f}m")

    # 再等 1.5s，done 应增长、todo 应缩短
    page.wait_for_timeout(1500)
    s2 = page.evaluate("""() => ({
        done: replayDoneLine ? replayDoneLine.getPath().length : 0,
        todo: replayTodoLine ? replayTodoLine.getPath().length : 0,
        t: replayT,
    })""")
    check("done 随播放增长", s2["done"] > s1["done"], f"{s1['done']} → {s2['done']}")
    check("todo 随播放缩短", s2["todo"] < s1["todo"], f"{s1['todo']} → {s2['todo']}")

    # 暂停：线保留；停止：线清理
    page.evaluate("pauseReplay()")
    page.wait_for_timeout(300)
    kept = page.evaluate("replayDoneLine !== null && replayTodoLine !== null")
    check("暂停时双色线保留", kept)
    page.evaluate("stopReplay(true)")
    page.wait_for_timeout(300)
    cleared = page.evaluate("replayDoneLine === null && replayTodoLine === null")
    check("停止后双色线清理", cleared)

    # 播完：整条变绿（done 长度 == 全路径）
    page.evaluate("startReplay()")
    page.wait_for_timeout(100)
    page.evaluate("replayT = replayTotal; updateReplayLines()")
    s3 = page.evaluate("""() => ({
        done: replayDoneLine ? replayDoneLine.getPath().length : 0,
        todo: replayTodoLine ? replayTodoLine.getPath().length : 0,
    })""")
    check("播完整条变绿(todo 只剩终点)", s3["todo"] == 1 and s3["done"] >= 2, f"done={s3['done']} todo={s3['todo']}")
    page.evaluate("stopReplay(true)")

    js_errs = [e for e in errors if '401' not in e and 'Autocomplete' not in e and '未登录或令牌无效' not in e]
    check("无 JS 报错", len(js_errs) == 0, "; ".join(js_errs[:3]) if js_errs else ("噪音已过滤" if errors else ""))

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
sys.exit(1 if failed else 0)
