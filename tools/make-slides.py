#!/usr/bin/env python3
"""首页轮播的图片加工：一张原图 -> PC(16:9) + 手机(4:3) 两个版本。

原则：**铺满容器，不留边；纵向从顶端对齐。**
--------------------
轮播容器的比例是跟着断点变的（桌面 16/9、手机 4/3），原图的比例跟它俩都对不上，
所以必然要裁掉一些。裁在哪边是有讲究的：

  · 原图比容器**高** -> 裁下边，顶端对齐。截图类的图顶上是工具栏/导航，
    照片墙顶上是最完整的那一排，都是最不该丢的；丢下缘观感上像"镜头往上看了一点"。
  · 原图比容器**宽** -> 只能居中裁左右。横向没有"上对齐"这回事，
    而主体通常居中，居中切最公平。

页面上是 object-fit:cover + object-position:center top，同一套规则，
改一边就要改另一边。

各图被裁掉多少（顶端对齐，纵向损失全在下边）：
    P2 1.610 -> 桌面切下 9.4% / 手机切左右各 8.6%
    P3 1.907 -> 桌面切左右各 3.4% / 手机切左右各 15.0%
    P4 1.461 -> 桌面切下 17.8% / 手机切左右各 4.4%

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
# (后缀, 宽, 高)。必须跟 landing.html 里 .stage 的断点严格对应：
# 桌面 16/9、手机 4/3。改一边就要改两边，否则又会出现"图被裁掉一部分"。
VARIANTS = [
    ("pc", 1920, 1080),   # 16:9，桌面
    ("m", 1000, 750),     # 4:3，手机
]

QUALITY = 82


def cover(im, tw, th):
    """裁成目标比例再缩放：铺满容器、不留边，**纵向从顶端对齐**。

    横向裁剪（原图比目标宽）只能居中 —— 左右没有"上对齐"这回事，
    而图片的主体通常在中间，居中切最公平。
    纵向裁剪（原图比目标高）按顶端对齐，只从底部切：截图类的图顶上是
    工具栏/导航，照片墙顶上是最完整的那一排，都是最不该丢的。
    这跟页面上 object-position:center top 是同一套规则，两边要一起改。
    """
    w, h = im.size
    target = tw / th
    if w / h > target:                    # 比目标更宽 -> 裁左右，居中
        nw, nh = int(round(h * target)), h
        box = (int(round((w - nw) * 0.5)), 0, int(round((w - nw) * 0.5)) + nw, nh)
    else:                                 # 比目标更高 -> 裁下边，顶端对齐
        nw, nh = w, int(round(w / target))
        box = (0, 0, nw, nh)
    return im.crop(box).resize((tw, th), Image.LANCZOS)


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
        for tag, tw, th in VARIANTS:
            out = cover(im, tw, th)
            dst = os.path.join(OUT, f"{base}-{tag}.webp")
            out.save(dst, "WEBP", quality=QUALITY, method=6)
            print(f"{dst}  {os.path.getsize(dst) // 1024} KB  "
                  f"({im.size[0]}x{im.size[1]} -> {out.size[0]}x{out.size[1]})"
                  f"  比例 {out.size[0] / out.size[1]:.3f}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
