#!/usr/bin/env python3
# 验证登录门禁（2026-08-26 刚哥修正版）：
# 1) 未登录打开首页 → 强制弹出登录框（用户名+密码，取消访问口令）
# 2) 登录后进入应用，正常加载行程
# 3) 轨迹播放速度按钮 1x→2x→4x→8x→1x 循环
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
    browser = p.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # ---- 场景 1：未登录 → 登录框强制弹出 ----
    page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function(
            "() => { const ov = document.getElementById('login-overlay'); return ov && !ov.classList.contains('hidden'); }",
            timeout=8000)
        check("未登录打开首页 → 登录框弹出", True)
    except Exception as e:
        check("未登录打开首页 → 登录框弹出", False, str(e)[:100])

    # 登录层包含用户名+密码输入框（普通用户访问方式）
    has_user = page.is_visible("#login-username")
    has_pwd = page.is_visible("#login-password")
    check("登录框含用户名+密码输入", has_user and has_pwd, f"user={has_user} pwd={has_pwd}")

    # 不应再有访问口令弹层
    gate_gone = page.evaluate("() => !document.getElementById('gate-overlay')")
    check("访问口令弹层已移除", gate_gone)

    # ---- 场景 2：管理员令牌登录 → 进入应用 ----
    page.click("#btn-token-mode")
    page.fill("#login-token", TOKEN)
    page.click("#token-form button[type=submit]")
    try:
        page.wait_for_function(
            "() => document.getElementById('login-overlay').classList.contains('hidden')",
            timeout=8000)
        check("登录后进入应用", True)
    except Exception as e:
        check("登录后进入应用", False, str(e)[:100])

    page.wait_for_timeout(2000)
    try:
        page.wait_for_function("document.querySelectorAll('#trip-select option').length > 0", timeout=10000)
        check("行程列表加载", True)
    except Exception as e:
        check("行程列表加载", False, str(e)[:100])

    # ---- 场景 3：x8 速度选项（先选中行程并启动回放，回放条才可见）----
    try:
        page.select_option("#trip-select", index=0)
        page.wait_for_timeout(800)
        page.click("#replay-launch")
        page.wait_for_function("() => !document.getElementById('replay-speed').closest('.replay-bar').classList.contains('hidden')", timeout=8000)
        page.wait_for_timeout(300)
        spd = page.text_content("#replay-speed")
        seq = [spd]
        for _ in range(4):
            page.click("#replay-speed")
            page.wait_for_timeout(120)
            seq.append(page.text_content("#replay-speed"))
        expect = ["1x", "2x", "4x", "8x", "1x"]
        check("速度按钮循环 1x→2x→4x→8x→1x", seq == expect, " → ".join(seq))
    except Exception as e:
        check("速度按钮循环 1x→2x→4x→8x→1x", False, str(e)[:100])

    js_err = [e for e in errors if "401" not in e and "Failed to load" not in e
              and "未登录或令牌无效" not in e and "AMap.Autocomplete" not in e]
    check("无 JS 错误", len(js_err) == 0, "; ".join(js_err[:3]) if js_err else "clean")

    browser.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"=== {passed}/{len(results)} 通过 ===")
sys.exit(0 if passed == len(results) else 1)
