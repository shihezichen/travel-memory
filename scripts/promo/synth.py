#!/usr/bin/env python3
"""把 synth.html 里的场景逐帧渲染成镜头片段。

不用录屏、改用逐帧截图，是因为录屏的帧率不可控 —— 同一个镜头两次录出来长短
能差出小半秒，配音就卡不住了。这里帧数 = 时长 × 30，画面由 render(p) 纯函数
决定，重跑一百次结果一模一样。

代价是慢（一帧一次截图），所以只给「界面里录不到」的镜头用。

用法：
    .venv/bin/python scripts/promo/synth.py            # 全部
    .venv/bin/python scripts/promo/synth.py wall_mess  # 只做一个，调画面时用
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import shots as S                                       # noqa: E402

OUT = ROOT / "build" / "promo"
CLIPS = OUT / "clips"
FPS = 30
W, H = 1280, 720


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(" ".join(str(c) for c in cmd[:12]), "...")
        print(r.stderr[-1200:])
        raise SystemExit("ffmpeg 失败")


def prepare_page(pw_page):
    """把照片和两张外部截图喂给页面。

    走 file:// 而不是起个 http 服务：照片就在 build/promo/photos 下，
    相对路径直接读得到，少一个要收拾的进程。
    """
    photos = sorted((OUT / "photos").glob("*.jpg"))
    if not photos:
        raise SystemExit("没有照片素材，先跑 fetch（见 build_promo_v2.py）")
    names = [p.name for p in photos]
    mapshot = "shot_duku.png" if (OUT / "shot_duku.png").exists() else "shot_long.png"
    phoneshot = "shot_phone.png" if (OUT / "shot_phone.png").exists() else mapshot
    pw_page.evaluate(
        """([names, mapshot, phoneshot]) => {
            window.PHOTOS = names.map(n => 'photos/' + n);
            window.MAPSHOT = mapshot;
            window.PHONESHOT = phoneshot;
            window.initSynth();
        }""", [names, mapshot, phoneshot])


def render(page, name, seconds, dest, frame_dir):
    """一个镜头：按 fps 铺帧，逐帧 render(p) 再截图，最后拼成 mp4。"""
    if frame_dir.exists():
        shutil.rmtree(frame_dir)
    frame_dir.mkdir(parents=True)
    n = max(2, int(round(seconds * FPS)))
    scene = name.replace("card:", "card_").replace("synth:", "")
    for i in range(n):
        p = i / (n - 1)
        page.evaluate("([s,p]) => window.renderShot(s,p)", [scene, p])
        page.screenshot(path=str(frame_dir / ("%04d.png" % i)))
    run(["ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
         "-i", str(frame_dir / "%04d.png"),
         "-vf", f"scale={W}:{H}", "-c:v", "libx264", "-preset", "medium",
         "-crf", "20", "-pix_fmt", "yuv420p", str(dest)])
    shutil.rmtree(frame_dir)
    return n


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    CLIPS.mkdir(parents=True, exist_ok=True)

    tl = S.timeline()
    todo = []
    for i, (num, a, b, d, src, mv, tr, line) in enumerate(tl):
        if not (src.startswith("synth:") or src.startswith("card:")):
            continue
        key = src.split(":", 1)[1]
        if only and key != only:
            continue
        # 多渲染一截给转场重叠用，不然拼完片子会短一大块
        todo.append((key, src, S.need_len(tl, i)))
    if not todo:
        raise SystemExit("没有匹配的镜头：%s" % only)

    from playwright.sync_api import sync_playwright
    html = (ROOT / "scripts" / "promo" / "synth.html").resolve()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chromium", headless=True,
            args=["--force-device-scale-factor=1", "--hide-scrollbars",
                  "--use-gl=angle", "--use-angle=swiftshader",
                  "--enable-unsafe-swiftshader", "--in-process-gpu",
                  "--allow-file-access-from-files"])
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=1, locale="zh-CN")
        page = ctx.new_page()
        # base 指到 build/promo/，页面里的 photos/xxx.jpg 才找得到
        page.goto((OUT / "_synth.html").as_uri(), wait_until="domcontentloaded")
        prepare_page(page)
        page.wait_for_timeout(700)                       # 等图片解码完
        for key, src, d in todo:
            print("  渲染 %-14s %.2fs" % (key, d), end="", flush=True)
            n = render(page, src, d, CLIPS / ("%s.mp4" % key), OUT / "_frames")
            print("  %d 帧" % n)
        ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    # synth.html 要跟 photos/ 同级才能用相对路径，复制一份过去
    shutil.copy(ROOT / "scripts" / "promo" / "synth.html", OUT / "_synth.html")
    sys.exit(main())
