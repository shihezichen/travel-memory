#!/usr/bin/env python3
"""首页轮播的图片加工：一张原图 -> PC(16:9) + 手机(1:1) 两个版本。

原则：**铺满容器，不留边；纵向从顶端对齐。**
--------------------
轮播容器的比例是跟着断点变的（桌面 16/9、手机 1/1），原图的比例跟它俩都对不上，
所以必然要裁掉一些。裁在哪边是有讲究的：

  · 原图比容器**高** -> 裁下边，顶端对齐。截图类的图顶上是工具栏/导航，
    照片墙顶上是最完整的那一排，都是最不该丢的；丢下缘观感上像"镜头往上看了一点"。
  · 原图比容器**宽** -> 只能居中裁左右。横向没有"上对齐"这回事，
    而主体通常居中，居中切最公平。

页面上是 object-fit:cover + object-position:center top，同一套规则，
改一边就要改另一边。

各图被裁掉多少（顶端对齐的纵向损失全在下边）：
    P2 1.610 -> 桌面切下 9.4% / 手机切左右各 8.6%
    P3 1.907 -> 桌面切左右各 3.4% / 手机切左右各 15.0%
    P4 1.461 -> 桌面切下 17.8%

**第 4 屏是个例外**：PC 用横版 P4.png，手机改用竖版 P4.1.png（0.715）。
横版 P4 是手机截图横向拼起来的，放进 1/1 的框里 cover 只能两头切；
竖版原图 + contain 才能把整张分享卡片完整放进容器。容器也跟着从 4/3
改成 1/1（见 landing.html 的 [data-slide="3"]），不然竖图会被压成一条细缝。

用法
----
    .venv/bin/python tools/make-slides.py

改图：把原图放进 assets/landing-src/，改下面 JOBS 里的文件名，重跑即可。
原图在**仓库里**（7 张共 17 MB）：没有它们，clone 下来的人只能拿到加工好的
WebP，任何一屏要重裁都得回头找原始截图。它们**不放在 static/** —— static 是
直接对外托管目录，原图放那儿等于把 8 MB 的 P3.png 公开挂到网上，白耗带宽。
"""

import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
# 原图在 assets/landing-src/，加工产物才落 static/promo/。两者的区别是
# **会不会被 web 服务器公开托管**：产物要给人下载所以放 static，原图只是源材料。
SRC = os.path.join(ROOT, "assets", "landing-src")
OUT = os.path.join(STATIC, "promo")

# (原图, 输出的基准名, 每个变体的裁剪对齐方式)
#
# 第 4 屏（分享页）是唯一一个 **PC 和手机用两张不同的原图** 的：
#   · PC 端沿用横版 P4.png，比例和裁切规则都没变；
#   · 手机端换成竖版 P4.1.png（721x1008，比例 0.715）。分享页在手机上本来就是
#     竖屏布局（设置项在上、二维码卡片在下），横版 P4 正是从手机截屏后横向拼的，
#     cover 进 4:3 容器必然左右各切掉一大块。竖版原图 + contain 才能把
#     整张卡片完整放进容器（见 html 里 .fit-contain）。
# align='top'  = 顶端对齐裁下边（截图类，顶上是最不该丢的工具栏/导航）
# align='center' = 居中裁（竖版图两端都是完整内容，居中给的留白最对称）
JOBS = [
    # (后缀, 原图, 输出的基准名, 裁剪对齐)。后缀跟 VARIANTS 的键对应。
    ("pc", "P2.png", "slide-route", "top"),        # 第 2 屏：地图轨迹
    ("m",  "P2.png", "slide-route", "top"),
    ("pc", "P3.png", "slide-wall", "top"),         # 第 3 屏：照片墙
    ("m",  "P3.png", "slide-wall", "top"),
    ("pc", "P4.png", "slide-share", "top"),        # 第 4 屏 PC：分享页（**横版**）
    # 手机版原图是 1:1 的 P4.1.png，跟 4:3 容器**同向**（图比容器高），
    # 走的是纵向分支 —— 所以 align 必须给 top，跟页面的 center top 对齐。
    # 之前这里是 center，是照当年竖版原图（0.715）配的；原图换成方图后
    # 这条就成了"上下各切一半"，顶部被吃掉一截（刚哥反馈的现象）。
    ("m",  "P4.1.png", "slide-share", "top"),      # 第 4 屏手机：分享页（**方图**）
]

# 必须跟 landing.html 里 .stage 的断点严格对应。改一边就要改两边，
# 否则又会出现"图被裁掉一部分"。
#   pc 16/9 —— 桌面
#   m   1/1 —— 手机。**只对第 4 屏的 `m` 成立**：那一屏手机原图是 1:1 的
#              P4.1.png，1/1 容器下 cover 不裁不切，整张图原样落地。
#   其余四屏容器都是 4/3（landing.html 的 `.stage[data-slide]{aspect-ratio:4/3}`），
#   `m` 输出的方图放进 4:3 容器由 CSS 的 cover 裁掉下边（顶端对齐）。
VARIANTS = {
    "pc": (1920, 1080),   # 16:9
    "m": (1000, 1000),    # 1:1
}

QUALITY = 82


def cover(im, tw, th, align="top"):
    """裁成目标比例再缩放：铺满容器、不留边。

    **横向裁剪**（原图比目标宽）只能居中 —— 左右没有"上对齐"这回事，
    而图片的主体通常在中间，居中切最公平。
    **纵向裁剪**（原图比目标高）按 align 走：
      · 'top'    只从底部切（顶端对齐）。截图类的图顶上是工具栏/导航，
                 照片墙顶上是最完整的那一排，都是最不该丢的。
                 对应页面上 object-position:center top。
      · 'center' 上下各切一半。竖版图两端都是完整内容时用这个，
                 留白最对称。对应页面上 object-position:center。
    页面的 object-position 要跟这里选的对齐方式一致，两边一起改。
    """
    w, h = im.size
    target = tw / th
    if w / h > target:                    # 比目标更宽 -> 裁左右，居中
        nw, nh = int(round(h * target)), h
        box = (int(round((w - nw) * 0.5)), 0, int(round((w - nw) * 0.5)) + nw, nh)
    else:                                 # 比目标更高 -> 裁上下，按 align
        nw, nh = w, int(round(w / target))
        top = 0 if align == "top" else int(round((h - nh) * 0.5))
        box = (0, top, nw, top + nh)
    return im.crop(box).resize((tw, th), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    missing = False
    for tag, src, base, align in JOBS:
        path = os.path.join(SRC, src)
        if not os.path.exists(path):
            print(f"缺图：{path}", file=sys.stderr)
            missing = True
            continue
        tw, th = VARIANTS[tag]
        with Image.open(path) as raw:
            im = raw.convert("RGB")
        out = cover(im, tw, th, align)
        dst = os.path.join(OUT, f"{base}-{tag}.webp")
        out.save(dst, "WEBP", quality=QUALITY, method=6)
        print(f"{dst}  {os.path.getsize(dst) // 1024} KB  "
              f"({im.size[0]}x{im.size[1]} -> {out.size[0]}x{out.size[1]})"
              f"  比例 {out.size[0] / out.size[1]:.3f}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
