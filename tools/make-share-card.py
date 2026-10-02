# -*- coding: utf-8 -*-
"""03/分享 那一节的配图：把真实的竖版分享卡截图重做成落地页同款深色卡片。

为什么不直接贴原截图
--------------------
原图（assets/landing-src/P5-card.png，1500x2000）是 App 真实生成的分享卡，
白底、留白很大 —— 直接放进 03/分享 那张深色卡里会像贴了一块白纸，和全页
「深色卡面 + 浅色前景」的家族对不上，上下两大片空白也把有效内容压得很小。

所以这里只取原图里**信息最密的两块**（地图 + 二维码），其余（标题、统计行、
扫码提示）按页面自己的字色重排到深色卡面上：
  · 卡面 #101014 + 1px 白描边，跟 .demo-box 同一档表面；
  · 顶部烘一层品牌橙的径向光，呼应 03/分享 那张卡自己的背景；
  · 地图保持原样的亮色（产品里的地图本来就是亮的，轮播第 2 屏也是真截图），
    只做圆角裁切 —— 亮地图压在深卡面上反而最跳；
  · 二维码必须留白底：反色二维码很多扫码器识别不了。

用法
----
    python tools/make-share-card.py
产物落 static/promo/share-card.webp，landing.html 里 03/分享 直接引用。
"""
import os
from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "landing-src", "P5-card.png")
OUT = os.path.join(ROOT, "static", "promo", "share-card.webp")

# 原图上两块内容的像素框，换原图就要重新量（按「非白行/列」扫出来的）
MAP_BOX = (80, 288, 1420, 1127)
QR_BOX = (482, 1232, 1018, 1769)

W, H = 720, 1000          # 输出尺寸，比例 0.72，竖版卡
PAD = 40
F_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
F_REG = "C:/Windows/Fonts/msyh.ttc"

TITLE = "2026-川西自驾10天 · 成都进出"
META = "2026-09-19 至 09-30 · 21 个点 · 4,863.5 km · 12 天 19 城"
CAP = "扫码 / 长按识别二维码查看"
FOOT = "旅行记忆 · 只读分享"


def rounded(im, r):
    """给一块位图切圆角（返回带 alpha 的 RGBA）。

    图本身已经带 alpha（比如下面那层径向光斑）时，**只能取两者的交集**——
    直接 putalpha 会把原来的渐变 alpha 整个覆盖成实心，光斑就变成一块死橙色。
    """
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], r, fill=255)
    out = im.convert("RGBA")
    if "A" in im.getbands():
        mask = ImageChops.multiply(mask, im.getchannel("A"))
    out.putalpha(mask)
    return out


def brand_glow(size, cx, cy, rx, ry, rgb, peak):
    """径向渐变光斑。

    **必须画在 L 掩膜上、由外向内覆盖写入**，不能一层层往 RGBA 上叠半透明椭圆 ——
    叠加的 alpha 会累积，64 层下来中心直接糊成一块不透明的橙色（试过）。
    掩膜是「写入」语义，画多少层中心都只有一个值。
    """
    w, h = size
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    steps = 72
    for i in range(steps, 0, -1):
        t = i / steps
        a = int(round(peak * (1 - t) ** 1.6))
        d.ellipse([cx - rx * t, cy - ry * t, cx + rx * t, cy + ry * t], fill=a)
    layer = Image.new("RGBA", size, rgb + (0,))
    layer.putalpha(mask)
    return layer


def centered(draw, y, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    draw.text(((W - (bb[2] - bb[0])) / 2 - bb[0], y), text, font=font, fill=fill)


def build():
    src = Image.open(SRC).convert("RGB")

    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    # 卡面本体：圆角 + 1px 白描边，跟页面上的 .card / .demo-box 同一套
    d.rounded_rectangle([0, 0, W - 1, H - 1], 30, fill=(16, 16, 20, 255))

    # 顶部品牌橙光斑，只在卡面内（切圆角后再叠）
    glow = brand_glow((W, H), W / 2, 0, W * 0.78, H * 0.46, (255, 138, 0), 42)
    card.alpha_composite(rounded(glow, 30))
    d.rounded_rectangle([0, 0, W - 1, H - 1], 30, outline=(255, 255, 255, 40), width=2)

    f_title = ImageFont.truetype(F_BOLD, 36)
    f_meta = ImageFont.truetype(F_REG, 19)
    f_cap = ImageFont.truetype(F_REG, 21)
    f_foot = ImageFont.truetype(F_REG, 17)

    centered(d, 48, TITLE, f_title, (255, 255, 255, 255))
    centered(d, 106, META, f_meta, (255, 255, 255, 150))

    # 地图：等比缩到卡宽内，圆角 + 一圈极淡的描边把它和卡面分开
    mp = src.crop(MAP_BOX)
    mw = W - PAD * 2
    mh = int(round(mw * (MAP_BOX[3] - MAP_BOX[1]) / (MAP_BOX[2] - MAP_BOX[0])))
    mp = rounded(mp.resize((mw, mh), Image.LANCZOS), 16)
    map_y = 152
    card.alpha_composite(mp, (PAD, map_y))
    d.rounded_rectangle([PAD, map_y, PAD + mw - 1, map_y + mh - 1], 16,
                        outline=(255, 255, 255, 46), width=2)

    # 二维码：白底圆角托盘，码本身不反色，保证能扫
    qs, qpad = 236, 18
    tray = qs + qpad * 2
    tx, ty = (W - tray) // 2, map_y + mh + 44
    d.rounded_rectangle([tx, ty, tx + tray, ty + tray], 20, fill=(255, 255, 255, 255))
    card.paste(src.crop(QR_BOX).convert("RGB").resize((qs, qs), Image.LANCZOS),
               (tx + qpad, ty + qpad))

    y = ty + tray + 30
    centered(d, y, CAP, f_cap, (255, 255, 255, 195))
    centered(d, y + 38, FOOT, f_foot, (255, 255, 255, 92))
    return card


if __name__ == "__main__":
    img = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 只落 WebP。static/ 是直接对外托管目录，顺手存一份 PNG 等于
    # 把同一张图用两倍体积再公开挂一遍，没人会去拉它。
    img.save(OUT, "WEBP", quality=88, method=6)
    print("wrote", OUT, os.path.getsize(OUT), "bytes")
