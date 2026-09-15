#!/usr/bin/env python3
"""用 playwright 录一段真实界面的操作，当宣传片的画面素材。

为什么不复用 tests/e2e 那套：e2e 把高德地图 stub 掉了（tests/e2e/amap_stub.js），
截出来的地图是假的。宣传片必须是真地图、真路线、真照片，所以这里直连线上服务
（默认 https://127.0.0.1:8443），用演示账号 test 走一遍。

每一步都各自 try/except：某个选择器失配时只丢掉那个镜头，不让整趟白录。
录完输出 webm，交给 build_promo_video.py 配音合成。

用法：
    .venv/bin/python scripts/record_promo_footage.py
    .venv/bin/python scripts/record_promo_footage.py --url https://top001.xin
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

OUT_DIR = ROOT / "build" / "promo"
VIDEO_DIR = OUT_DIR / "raw"
# 1280x720：合成出来就是 720p，放网页上够清楚，体积也不吓人。
VIEWPORT = {"width": 1280, "height": 720}


def beat(page, seconds, label=""):
    """停一拍。宣传片的节奏全靠这些停顿 —— 画面得给人看清的时间。"""
    if label:
        print("  · %s (%.1fs)" % (label, seconds))
    page.wait_for_timeout(int(seconds * 1000))


def shot(name):
    """把一个镜头包起来：失败只打印，不中断整趟录制。"""
    def deco(fn):
        def wrapped(page):
            try:
                fn(page)
            except Exception as e:                       # noqa: BLE001
                print("  ! 镜头[%s]跳过：%s" % (name, str(e)[:120]))
        wrapped.__name__ = name
        return wrapped
    return deco


@shot("登录并落到地图")
def scene_login(page):
    page.goto(page._base + "/app", wait_until="domcontentloaded")
    beat(page, 1.0)
    # 已登录会直接进应用；没登录才有表单
    if page.locator("#login-username, input[name=username]").count():
        page.fill("#login-username, input[name=username]", "test")
        page.fill("#login-password, input[name=password]", "test1234")
        page.click("#btn-login, button[type=submit]")
    page.wait_for_timeout(3500)


@shot("全览行程：地图 + 真实路线")
def scene_overview(page):
    page.click("#btn-fit-view")
    beat(page, 4.0, "整条路线收进视野")


@shot("路线回放")
def scene_replay(page):
    page.click("#replay-launch")
    beat(page, 9.0, "沿着路线走一遍")
    if page.locator("#replay-close").count():
        page.click("#replay-close")
    beat(page, 0.8)


@shot("点开一个地点：照片和说明")
def scene_point_detail(page):
    page.click(".point-item, [data-point-id]")
    beat(page, 3.5, "这个点当天拍了什么")
    page.mouse.wheel(0, 300)
    beat(page, 2.0)


@shot("相册墙")
def scene_albums(page):
    page.click('[data-tab="albums"]')
    beat(page, 3.0, "整趟的照片铺开")
    # 鼠标要先移到面板上，wheel 才滚得到那个容器
    box = page.locator("#albums-view").bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(6):
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(450)
    beat(page, 1.2)
    page.click('[data-tab="points"]')
    beat(page, 1.0)


@shot("足迹热力图")
def scene_heatmap(page):
    # 复选框在收起的图层面板里，得先把面板点开，否则 check() 一直等它可见。
    page.click("#btn-layers")
    page.wait_for_timeout(600)
    page.check("#lay-heatmap")
    beat(page, 1.0)
    page.click("#btn-fit-view")
    beat(page, 4.5, "去过的地方亮起来")
    # 关掉之前得把面板再点开 —— 上一句 fit-view 会把它收起来，
    # 直接 uncheck 会一直等一个不可见的元素。
    page.click("#btn-layers")
    page.wait_for_timeout(500)
    page.uncheck("#lay-heatmap")
    page.click("#btn-layers")
    beat(page, 0.8)


@shot("地图全屏")
def scene_fullscreen(page):
    page.click("#btn-map-fullscreen")
    beat(page, 3.5, "沉浸看图")
    page.keyboard.press("Escape")
    beat(page, 1.0)


SCENES = [scene_login, scene_overview, scene_replay,
          scene_point_detail, scene_albums, scene_heatmap, scene_fullscreen]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://127.0.0.1:8443")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    if VIDEO_DIR.exists():
        shutil.rmtree(VIDEO_DIR)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        # chrome-headless-shell 没有 GPU，高德底图走 WebGL/Canvas 渲染不出来
        # （路线和标记是 DOM 覆盖层，照常显示，所以第一版录出来是"白底上一条蓝线"）。
        # 用完整 chromium + SwiftShader 软件渲染，底图才出得来。
        browser = pw.chromium.launch(
            channel="chromium", headless=True,
            args=["--force-device-scale-factor=1", "--hide-scrollbars",
                  "--use-gl=angle", "--use-angle=swiftshader",
                  "--enable-unsafe-swiftshader", "--in-process-gpu"])
        ctx = browser.new_context(
            viewport=VIEWPORT,
            ignore_https_errors=True,              # 本机是自签证书
            record_video_dir=str(VIDEO_DIR),
            record_video_size=VIEWPORT,
            locale="zh-CN",
        )
        page = ctx.new_page()
        page._base = args.url.rstrip("/")
        print("录制中 -> %s" % args.url)
        for scene in SCENES:
            print("镜头：%s" % scene.__name__)
            scene(page)
        path = page.video.path()
        ctx.close()                                # 必须先关 context，视频才落盘
        browser.close()

    src = Path(path)
    dest = OUT_DIR / "footage.webm"
    shutil.move(str(src), dest)
    print("\n素材已保存：%s (%.1f MB)" % (dest, dest.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
