#!/usr/bin/env python3
"""按镜头录真实界面，顺带截几张后面要用的静态图。

跟老的 record_promo_footage.py 的区别：那个一镜到底录一大段，剪的时候只能整体
加速，看着就像幻灯片。这里按「场景组」分段录，组内给每个镜头打起止时间戳，
后面 assemble.py 再按分镜表把每段掐到精确长度。

分组的依据是动作有没有连续性：独库那三镜是一气呵成的（点落下 → 路线生长 →
放大看弯道），拆开录就接不上了；上传和分享则各自独立。

产出：
    build/promo/raw/<组名>.webm
    build/promo/ui_marks.json      每个镜头在 webm 里的起止秒
    build/promo/shot_map_full.png  全屏地图（给「照片飞回地图」当底）
    build/promo/map_pins.json      地图上真实标记点的屏幕坐标
    build/promo/shot_duku.png      独库地图（给 MCP 镜头当配图）
    build/promo/shot_phone.png     只读分享页的竖屏截图

用法：
    .venv/bin/python scripts/promo/record.py [--url https://127.0.0.1:8443]
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import shots as S                                         # noqa: E402

# 录多久 = 成片里的时长 × 这个系数。1.3 是试出来的：再小，末尾容易赶上界面
# 切到下一个状态；再大，剪的时候要压到 1.5 倍以上，界面就开始显得毛躁。
TAKE_RATIO = 1.3
_TL = S.timeline()
_NEED = {t[4].split(":", 1)[1]: S.need_len(_TL, i) for i, t in enumerate(_TL)}


def take_sec(name, fallback=2.6):
    """这一镜该录多久。"""
    return round(_NEED.get(name, fallback) * TAKE_RATIO, 2)
OUT = ROOT / "build" / "promo"
RAW = OUT / "raw"
VIEW = {"width": 1280, "height": 720}

KANSAI = "2026-关西赏樱"
DUKU = "2025-独库公路"


class Rec:
    """一个场景组的录制上下文，负责给镜头打时间戳。"""

    def __init__(self, page, marks, group, t0):
        self.page = page
        self.marks = marks
        self.group = group
        self.t0 = t0

    def wait(self, sec):
        self.page.wait_for_timeout(int(sec * 1000))

    def hilite(self, sel, on=True):
        """给元素套/撤高亮。写内联 style 而不是加 class：站内 CSS 优先级高，
        class 常被覆盖掉（第一版的光晕就是这么消失的）。"""
        try:
            self.page.evaluate("""([sel, on]) => {
                const prev = window.__tmHi;
                if (prev && prev.el) {
                    prev.el.style.outline = prev.outline || '';
                    prev.el.style.boxShadow = prev.shadow || '';
                    prev.el.style.borderRadius = prev.radius || '';
                }
                window.__tmHi = null;
                if (!on) return;
                const el = document.querySelector(sel);
                if (!el) return;
                window.__tmHi = {el, outline: el.style.outline,
                                 shadow: el.style.boxShadow,
                                 radius: el.style.borderRadius};
                el.style.outline = '3px solid #FF6B35';
                el.style.borderRadius = '9px';
                el.style.boxShadow = '0 0 0 6px rgba(255,107,53,.28),'
                                   + ' 0 0 26px 10px rgba(255,107,53,.45)';
            }""", [sel, on])
        except Exception:                                 # noqa: BLE001
            pass

    def mark(self, name, sec, focus=None, glow=None):
        """录一个镜头：现在这一刻是起点，停 sec 秒之后是终点。

        focus 传一个选择器，就把那个元素在画面里的位置一并记下来。局部放大的
        锚点全靠它 —— 手填百分比会随界面改版失准，第一版的分享按钮特写就是
        这么放偏的（放大后对着的是轨迹点列表）。
        """
        if glow:
            self.hilite(glow)
            self.wait(0.25)                    # 让光晕先亮起来再开始计时
        a = time.monotonic() - self.t0
        self.wait(sec)
        b = time.monotonic() - self.t0
        rec = {"group": self.group, "start": round(a, 3), "end": round(b, 3)}
        if focus:
            try:
                box = self.page.locator(focus).first.bounding_box(timeout=2000)
                if box:
                    rec["focus"] = {"x": round(box["x"] + box["width"] / 2),
                                    "y": round(box["y"] + box["height"] / 2)}
            except Exception:                             # noqa: BLE001
                print("    ! 取不到焦点 %s" % focus)
        if glow:
            self.hilite(glow, False)
        self.marks[name] = rec
        print("    · %-14s %.2fs%s" % (name, b - a, "  ◎" if rec.get("focus") else ""))

    def try_click(self, sel, timeout=4000):
        try:
            self.page.click(sel, timeout=timeout)
            return True
        except Exception as e:                            # noqa: BLE001
            print("    ! 点不到 %s：%s" % (sel, str(e)[:70]))
            return False


def pick_trip(page, name):
    """切到指定行程。下拉框的 option 文本带前缀，用 label 匹配最省事。"""
    try:
        page.select_option("#trip-select", label=name, timeout=4000)
    except Exception:                                     # noqa: BLE001
        # 文本对不上就按 value 里含关键字来挑
        page.evaluate("""(kw) => {
            const s = document.getElementById('trip-select');
            const o = [...s.options].find(o => o.textContent.includes(kw));
            if (o) { s.value = o.value; s.dispatchEvent(new Event('change',{bubbles:true})); }
        }""", name.split("-", 1)[1])
    page.wait_for_timeout(2600)


def login(page, base):
    page.goto(base + "/app", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    form = page.locator("#login-username, input[name=username]").first
    if form.count() and form.is_visible():
        page.fill("#login-username, input[name=username]", "test")
        page.fill("#login-password, input[name=password]", "test1234")
        page.click("#btn-login, button[type=submit]")
    page.wait_for_timeout(3800)


# ============ 场景组 ============

def grp_duku(r):
    """独库公路：点落下 → 路线生长 → 放大看盘山那段。三镜连着录。"""
    pick_trip(r.page, DUKU)
    r.try_click("#btn-fit-view")
    r.wait(1.2)
    # 落点：把面板收起来，让地图占满，点位看得更清楚
    for sel in ("#btn-panel-toggle-wide", "#btn-panel-toggle", "#btn-panel-open"):
        if r.try_click(sel, timeout=1500):
            break
    r.wait(1.0)
    r.mark("duku_points", take_sec("duku_points"))
    r.try_click("#btn-fit-view")
    r.mark("duku_route", take_sec("duku_route"))
    # 盘山段：那拉提到库车，往地图左下角推几级
    r.page.mouse.move(430, 470)
    for _ in range(3):
        r.page.mouse.wheel(0, -240)
        r.wait(0.45)
    r.mark("duku_curve", take_sec("duku_curve"))


def grp_upload(r):
    """上传：拖照片进去 → 时间和坐标自动填上。"""
    pick_trip(r.page, KANSAI)
    r.wait(0.6)
    if not r.try_click("#point-list li"):
        return
    r.wait(1.6)
    # 缩略图在 OSS 上，等它们真解码完再开拍，否则是一格格灰方块
    try:
        r.page.wait_for_function(
            "() => { const g=document.querySelectorAll('#media-grid img, .media-grid img');"
            " return g.length>0 && [...g].every(i=>i.complete && i.naturalWidth>0); }",
            timeout=12000)
    except Exception:                                     # noqa: BLE001
        print("    ! 缩略图没等齐")
    r.wait(0.5)
    # 这个点当天拍的照片，连同末尾那个「＋」上传格
    r.mark("upload_drop", take_sec("upload_drop"), focus="#media-grid, .media-grid, .point-media")
    # 点开一张进灯箱，底部会写「大阪 · 2026-03-28 08:58」——
    # 时间和归属都是照片自己带来的，不用手填，这才是要拍的东西
    r.try_click("#media-grid img, .media-grid img, .point-media img")
    r.wait(2.0)
    r.mark("upload_exif", take_sec("upload_exif"), focus=".lb-caption-meta, .lb-caption")
    r.wait(0.8)
    r.page.keyboard.press("Escape")
    r.wait(0.6)


def grp_kansai(r):
    """关西：全览 → 相册墙 → 热力图 → 拉远 → 回放。

    录制顺序跟成片顺序不一样，这是故意的：回放一开始播就把面板锁住，tab 切不
    过去（第一版的相册镜头就因此拍成了一张地图）。所以把回放放到最后录，
    marks 按名字存，assemble 照分镜表取，谁先录无所谓。
    """
    pick_trip(r.page, KANSAI)
    r.try_click("#btn-fit-view")
    r.mark("kansai_fit", take_sec("kansai_fit"))

    # ---- 相册墙 ----
    r.try_click('[data-tab="albums"]')
    r.wait(1.0)
    r.wait(1.2)
    try:
        r.page.wait_for_function(
            "() => { const g=document.querySelectorAll('#albums-view img, #photo-grid img');"
            " return g.length>2 && [...g].filter(i=>i.complete&&i.naturalWidth>0).length>2; }",
            timeout=12000)
    except Exception:                                     # noqa: BLE001
        print("    ! 相册图没等齐")
    try:
        box = r.page.locator("#albums-view, #photo-grid").first.bounding_box()
        if box:
            r.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception:                                     # noqa: BLE001
        pass
    a = time.monotonic() - r.t0
    need = take_sec("albums")
    while time.monotonic() - r.t0 - a < need:
        r.page.mouse.wheel(0, 150)
        r.wait(0.42)
    r.marks["albums"] = {"group": r.group, "start": round(a, 3),
                         "end": round(time.monotonic() - r.t0, 3)}
    try:
        bx = r.page.locator("#albums-view, #photo-grid").first.bounding_box(timeout=2000)
        if bx:
            r.marks["albums"]["focus"] = {"x": round(bx["x"] + bx["width"] / 2),
                                          "y": round(bx["y"] + bx["height"] / 2)}
    except Exception:                                     # noqa: BLE001
        pass
    print("    · %-14s %.2fs%s" % ("albums", time.monotonic() - r.t0 - a,
                                    "  ◎" if r.marks["albums"].get("focus") else ""))
    r.wait(0.8)
    r.try_click('[data-tab="points"]', timeout=2500)
    r.wait(0.8)

    # ---- 热力图 ----
    r.try_click("#btn-layers")
    r.wait(0.7)
    try:
        r.page.check("#lay-heatmap", timeout=3000)
    except Exception:                                     # noqa: BLE001
        print("    ! 热力图开关没勾上")
    r.wait(0.5)
    r.mark("heatmap_on", take_sec("heatmap_on"))
    # 拉远：从关西一路退到东亚
    r.page.mouse.move(460, 400)
    a = time.monotonic() - r.t0
    need = take_sec("heatmap_out")
    while time.monotonic() - r.t0 - a < need:
        r.page.mouse.wheel(0, 300)
        r.wait(0.4)
    r.marks["heatmap_out"] = {"group": r.group, "start": round(a, 3),
                              "end": round(time.monotonic() - r.t0, 3)}
    print("    · %-14s %.2fs" % ("heatmap_out", time.monotonic() - r.t0 - a))
    r.try_click("#btn-layers")
    r.wait(0.4)
    try:
        r.page.uncheck("#lay-heatmap", timeout=2500)
    except Exception:                                     # noqa: BLE001
        pass
    r.try_click("#btn-fit-view")
    r.wait(1.6)

    # ---- 回放（放最后，它一播就锁面板）----
    r.try_click("#replay-launch")
    r.wait(1.0)
    if r.page.locator("#replay-toggle").count():
        playing = r.page.evaluate(
            "() => { const f=document.getElementById('replay-fill');"
            " return f ? parseFloat(f.style.width||'0') > 0 : false; }")
        r.wait(0.5)
        if not playing:
            r.try_click("#replay-toggle")
            r.wait(0.4)
    r.mark("kansai_replay", take_sec("kansai_replay"))


def grp_share(r):
    """分享：点按钮 → 出只读链接。"""
    pick_trip(r.page, KANSAI)
    r.wait(0.8)
    r.page.hover("#btn-share-trip")
    r.wait(0.5)
    r.mark("share_btn", take_sec("share_btn"), focus="#btn-share-trip", glow="#btn-share-trip")
    r.try_click("#btn-share-trip")
    r.wait(1.6)
    r.mark("share_link", take_sec("share_link"), focus="#modal, .modal, #modal-form")
    r.page.keyboard.press("Escape")


def grp_export(r):
    """导出：点导出，数据整份带走。"""
    pick_trip(r.page, KANSAI)
    r.wait(0.6)
    r.page.hover("#btn-export-trip")
    r.wait(0.6)
    r.mark("export", take_sec("export"), focus="#btn-export-trip", glow="#btn-export-trip")
    r.try_click("#btn-export-trip")
    r.wait(1.4)


GROUPS = [("duku", grp_duku), ("upload", grp_upload), ("kansai", grp_kansai),
          ("share", grp_share), ("export", grp_export)]


def grab_stills(pw, base, state):
    """录完之后补几张静态图。单开一个不录像的 context，省得混进 webm。"""
    browser = pw.chromium.launch(**LAUNCH)
    ctx = browser.new_context(viewport=VIEW, ignore_https_errors=True,
                              storage_state=state, locale="zh-CN")
    page = ctx.new_page()
    login(page, base)

    # 关西：全屏地图 + 真实标记点的屏幕坐标
    pick_trip(page, KANSAI)
    page.click("#btn-fit-view")
    page.wait_for_timeout(3500)
    try:
        page.click("#btn-map-fullscreen")
        page.wait_for_timeout(3000)
        page.click("#btn-fit-view")
        page.wait_for_timeout(6000)
    except Exception:                                     # noqa: BLE001
        pass
    page.screenshot(path=str(OUT / "shot_map_full.png"))
    pins = page.evaluate(PINS_JS)
    (OUT / "map_pins.json").write_text(json.dumps(pins, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print("  标记点 %d 个：%s" % (len(pins), [p["name"] for p in pins]))
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(900)
    except Exception:                                     # noqa: BLE001
        pass

    # 独库：给 MCP 那镜当配图
    pick_trip(page, DUKU)
    page.click("#btn-fit-view")
    page.wait_for_timeout(2000)
    try:
        page.click("#btn-map-fullscreen")
        page.wait_for_timeout(3000)
        page.click("#btn-fit-view")
        page.wait_for_timeout(6000)
    except Exception:                                     # noqa: BLE001
        pass
    page.screenshot(path=str(OUT / "shot_duku.png"))
    pins2 = page.evaluate(PINS_JS)
    (OUT / "duku_pins.json").write_text(json.dumps(pins2, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    print("  独库标记 %d 个：%s" % (len(pins2), [p["name"] for p in pins2]))
    ctx.close()

    # 只读分享页：竖屏，给手机框用
    ctx2 = browser.new_context(viewport={"width": 390, "height": 844},
                               ignore_https_errors=True, locale="zh-CN",
                               device_scale_factor=2)
    p2 = ctx2.new_page()
    try:
        p2.goto(base + "/", wait_until="domcontentloaded")
        p2.wait_for_timeout(2500)
        p2.screenshot(path=str(OUT / "shot_phone.png"))
    except Exception as e:                                # noqa: BLE001
        print("  ! 竖屏截图失败：%s" % str(e)[:80])
    ctx2.close()
    browser.close()


PINS_JS = """() => {
    const out = [];
    document.querySelectorAll('.marker-name').forEach(n => {
        const r = n.getBoundingClientRect();
        out.push({name: n.textContent.trim(),
                  x: Math.round(r.left + r.width/2),
                  y: Math.round(r.top + r.height/2)});
    });
    return out;
}"""


LAUNCH = dict(channel="chromium", headless=True,
              args=["--force-device-scale-factor=1", "--hide-scrollbars",
                    "--use-gl=angle", "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader", "--in-process-gpu"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://127.0.0.1:8443")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    from playwright.sync_api import sync_playwright
    RAW.mkdir(parents=True, exist_ok=True)
    marks = {}
    if (OUT / "ui_marks.json").exists() and args.only:
        marks = json.loads((OUT / "ui_marks.json").read_text(encoding="utf-8"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**LAUNCH)
        # 先登录一次，把会话存下来，后面每组直接复用，省掉重复的登录动画
        c0 = browser.new_context(viewport=VIEW, ignore_https_errors=True, locale="zh-CN")
        p0 = c0.new_page()
        login(p0, base)
        state = c0.storage_state()
        c0.close()

        for name, fn in GROUPS:
            if args.only == "stills":
                break
            if args.only and name != args.only:
                continue
            print("  组 %s" % name)
            vdir = RAW / name
            if vdir.exists():
                shutil.rmtree(vdir)
            vdir.mkdir(parents=True)
            ctx = browser.new_context(viewport=VIEW, ignore_https_errors=True,
                                      storage_state=state, locale="zh-CN",
                                      record_video_dir=str(vdir),
                                      record_video_size=VIEW)
            t_ctx = time.monotonic()          # 录像从这一刻开始，时间戳都以它为零点
            page = ctx.new_page()
            page.goto(base + "/app", wait_until="domcontentloaded")
            page.wait_for_timeout(3200)                   # 等地图底图铺完
            r = Rec(page, marks, name, t_ctx)
            try:
                fn(r)
            except Exception as e:                        # noqa: BLE001
                print("  ! 组 %s 出错：%s" % (name, str(e)[:120]))
            vpath = page.video.path()
            ctx.close()                                   # 先关 context 视频才落盘
            shutil.move(vpath, RAW / ("%s.webm" % name))
            shutil.rmtree(vdir, ignore_errors=True)

        browser.close()

        print("  补静态图")
        grab_stills(pw, base, state)

    (OUT / "ui_marks.json").write_text(json.dumps(marks, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print("\n录到 %d 个镜头" % len(marks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
