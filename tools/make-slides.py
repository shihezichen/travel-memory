#!/usr/bin/env python3
"""首页轮播的图片加工：一张原图 -> PC(16:9) + 手机(4:3) 两个版本。

为什么非要出两个版本
--------------------
首页轮播的容器比例是**跟着断点变的**：
  - 桌面 / 平板宽屏（>=641px）：.stage 是 16/9
  - 手机（<=640px）：.stage 被压成 4/3

同一个 img 用 object-fit:cover 填这两种容器，比例不符时必然要裁掉一边：
一张 16:9 的图进 4:3 的容器，左右各切掉 25%（图上的人、二维码、文字标签直接没）；
反过来 4:3 进 16:9，上下各切 25%（标题、导航栏被切掉）。
以前手机上"只能看到图片上半部分"就是这么来的。

所以每屏都出两份，HTML 里用 .only-pc / .only-m 按断点各显示一张。

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

# (原图, 输出的基准名, 裁切时纵向锚点)
# 锚点 0=贴顶 0.5=居中 1=贴底。截图类的图（顶部有工具栏）居中就好；
# 纯内容的照片墙想让最上面那排完整，可以调成偏上。
JOBS = [
    ("P2.png", "slide-route", 0.5),   # 第 2 屏：地图轨迹
    ("P3.png", "slide-wall", 0.5),    # 第 3 屏：照片墙
    ("P4.png", "slide-share", 0.5),   # 第 4 屏：分享页
]

# (后缀, 宽, 高)。跟 landing.html 里 .stage 的断点严格对应，改一边要改两边。
VARIANTS = [
    ("pc", 1920, 1080),   # 16:9，桌面
    ("m", 1000, 750),     # 4:3，手机
]

QUALITY = 82


def cover(im, tw, th, ay=0.5):
    """先按目标比例做一次"封面裁剪"再缩放。

    直接 resize 会把图拉变形；只交给浏览器 object-fit 又要多下一份大图。
    在这里裁到位、缩到位，浏览器那边几乎不用再算。
    """
    w, h = im.size
    target = tw / th
    if w / h > target:                    # 比目标更宽 -> 裁左右，居中
        nw, nh = int(round(h * target)), h
        x, y = int(round((w - nw) * 0.5)), 0
    else:                                 # 比目标更高 -> 裁上下，按锚点
        nw, nh = w, int(round(w / target))
        x, y = 0, int(round((h - nh) * ay))
    return im.crop((x, y, x + nw, y + nh)).resize((tw, th), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    missing = False
    for src, base, ay in JOBS:
        path = os.path.join(STATIC, src)
        if not os.path.exists(path):
            print(f"缺图：{path}", file=sys.stderr)
            missing = True
            continue
        with Image.open(path) as raw:
            im = raw.convert("RGB")
        for tag, tw, th in VARIANTS:
            dst = os.path.join(OUT, f"{base}-{tag}.webp")
            cover(im, tw, th, ay).save(dst, "WEBP", quality=QUALITY, method=6)
            print(f"{dst}  {os.path.getsize(dst) // 1024} KB  "
                  f"({im.size[0]}x{im.size[1]} -> {tw}x{th})")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
