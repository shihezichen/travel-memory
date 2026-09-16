#!/usr/bin/env python3
"""首页轮播的图片加工：一张原图 -> PC / 手机 两个尺寸的 WebP。

原则：**只缩放，绝不裁切。**
--------------------
轮播容器的比例是跟着断点变的（桌面 16/9、手机 4/3），原图的比例跟它俩都对不上。
以前用 object-fit:cover 填容器，多出来的部分就被裁掉了 —— 原图比例和容器差得越远
切得越狠，P3（比例 1.91）在手机 4:3 上左右各切 15%，照片墙两头整列直接没了。

现在页面上改成 object-fit:contain：图完整放进去，比例不符的地方留边，
留边用容器同色，看起来是画面自带留白。既然不裁了，这边就只需要把像素缩到位。

各屏原图比例和留边量（留边是比例的差，不是这张图的毛病）：
    P2 1.610 -> PC 左右各留 4.7% / 手机 上下各留 8.6%
    P3 1.907 -> PC 上下各留 3.4% / 手机 上下各留 15.1%
    P4 1.461 -> PC 左右各留 8.9% / 手机 上下各留 4.3%

两档尺寸是给不同屏幕的像素密度用的，不是为了适配比例 ——
比例现在由 contain 自己处理，两张图内容完全一样。

用法
----
    .venv/bin/python tools/make-slides.py

改图：把原图放回 static/，改下面 JOBS 里的文件名，重跑即可。
原图进仓库会很大（P3.png 有 8 MB），加工完的两份 WebP 加起来才几百 KB。
"""

import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
OUT = os.path.join(STATIC, "promo")

# (原图, 输出的基准名)
JOBS = [
    ("P2.png", "slide-route"),   # 第 2 屏：地图轨迹
    ("P3.png", "slide-wall"),    # 第 3 屏：照片墙
    ("P4.png", "slide-share"),   # 第 4 屏：分享页
]

# (后缀, 宽, 高)。跟 landing.html 里 .stage 的断点严格对应，改一边要改两边。
# (后缀, 缩放后长边像素)。不是容器尺寸 —— 比例交给页面上的 contain 处理，
# 这里只管给不同像素密度的屏幕备好够用的分辨率。
VARIANTS = [
    ("pc", 1920),   # 桌面宽屏
    ("m", 1000),    # 手机（2x/3x 屏也够）
]

QUALITY = 82


def fit(im, long_edge):
    """等比缩放到长边 = long_edge，**不裁切**。

    这里刻意不裁：原图的比例和轮播容器的比例对不上，裁一刀必然丢内容
    （P3 在手机上左右各切 15%，照片墙两头整列没了）。页面上用 object-fit:contain
    完整显示，留边交给容器底色 —— 所以这边只需要把像素缩到位。
    """
    w, h = im.size
    scale = long_edge / max(w, h)
    if scale >= 1:                        # 本来就比目标小，别放大
        return im
    return im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    missing = False
    for src, base in JOBS:
        path = os.path.join(STATIC, src)
        if not os.path.exists(path):
            print(f"缺图：{path}", file=sys.stderr)
            missing = True
            continue
        with Image.open(path) as raw:
            im = raw.convert("RGB")
        for tag, long_edge in VARIANTS:
            out = fit(im, long_edge)
            dst = os.path.join(OUT, f"{base}-{tag}.webp")
            out.save(dst, "WEBP", quality=QUALITY, method=6)
            print(f"{dst}  {os.path.getsize(dst) // 1024} KB  "
                  f"({im.size[0]}x{im.size[1]} -> {out.size[0]}x{out.size[1]})"
                  f"  比例 {out.size[0] / out.size[1]:.3f}（不裁切）")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
