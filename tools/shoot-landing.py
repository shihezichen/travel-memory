#!/usr/bin/env python3
"""抓首页轮播用的真实页面截图。

首页轮播原本是 4 张手绘 SVG 示意图，太抽象、看不出产品长什么样。
现在换成这个脚本从**演示账号**真机上抓的截图，改版式后重跑即可。
（2026-09-16 刚哥反馈「轮播现在用的照片不行啊，太抽象了」时加的。）

用法（在项目根目录）：
    .venv/bin/python tools/shoot-landing.py

前置：本机 travel-memory 服务在跑（systemd user 服务，8443 HTTPS）。
产出：static/promo/shot-{map,album,detail,lightbox}.webp（1600×900 webp）

为什么走 8443 而不是 8084：8443 是本机 systemd 实例的真实监听口，带证书，
和线上跑的是同一份代码；8084 那个是 start.sh 手动拉的开发实例。
"""
import asyncio, os, sys
from PIL import Image
from playwright.async_api import async_playwright

BASE = os.environ.get('TM_BASE', 'https://127.0.0.1:8443')
OUTDIR = '/tmp/tm-landing-shots'
FINAL = 'static/promo'
SHOTS = {                       # 截图名 -> 最终文件名
    's1_map': 'shot-map',
    's2_album': 'shot-album',
    's3_detail': 'shot-detail',
    's4_lightbox': 'shot-lightbox',
}


async def shoot():
    os.makedirs(OUTDIR, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(args=['--no-sandbox'])
        ctx = await b.new_context(viewport={'width': 1440, 'height': 810},
                                  device_scale_factor=2, ignore_https_errors=True)
        pg = await ctx.new_page()
        await pg.goto(BASE + '/app', wait_until='networkidle', timeout=45000)
        await pg.wait_for_timeout(1200)

        # 体验账号一键登录 —— 截图要的是有内容的界面，不是空状态
        await pg.get_by_text('体验账号一键登录').first.click(timeout=8000)
        await pg.wait_for_timeout(8000)

        # 1) 主界面：地图上的整条轨迹
        await pg.screenshot(path=f'{OUTDIR}/s1_map.png')

        # 2) 相册：整趟旅行的照片墙
        await pg.get_by_text('相册', exact=True).first.click(timeout=5000)
        await pg.wait_for_timeout(6000)
        await pg.screenshot(path=f'{OUTDIR}/s2_album.png')

        # 3) 地点详情 + 4) 原图灯箱
        # 地图 marker 被高德的 overlay 图层盖着，直接点会被拦截（Playwright 会
        # 报「intercepts pointer events」），所以绕开 DOM 直接调页面自己的
        # showDetail(pointId) —— 跟用户点 marker 走的是同一条渲染路径。
        pid = await pg.evaluate("""async () => {
          const t = await (await fetch('/api/trips')).json();
          const trip = t.find(x => x.name.includes('滇藏线')) || t[0];
          const pts = await (await fetch('/api/trips/' + trip.id + '/points')).json();
          const p = pts.find(x => x.name === '香格里拉') || pts[2];
          return p ? p.id : null;
        }""")
        if not pid:
            sys.exit('拿不到演示点，检查演示账号数据是否还在')
        await pg.evaluate(f'showDetail({pid})')
        await pg.wait_for_timeout(5000)
        await pg.screenshot(path=f'{OUTDIR}/s3_detail.png')

        cells = await pg.query_selector_all('#media-grid > *')
        if cells:
            await cells[0].click(force=True)       # 第一格 → 开灯箱
            await pg.wait_for_timeout(6000)
            await pg.screenshot(path=f'{OUTDIR}/s4_lightbox.png')

        await b.close()


def convert():
    os.makedirs(FINAL, exist_ok=True)
    for src, name in SHOTS.items():
        f = f'{OUTDIR}/{src}.png'
        if not os.path.exists(f):
            print('跳过（没抓到）:', src); continue
        im = Image.open(f).convert('RGB')
        # 视口就是 16:9，DPR=2 → 2880×1620，缩到 1600 宽够 1120 逻辑像素 ×2 用
        im = im.resize((1600, 900), Image.LANCZOS)
        p = f'{FINAL}/{name}.webp'
        im.save(p, 'WEBP', quality=82, method=6)
        print(f'{p}  {os.path.getsize(p) // 1024} KB')


if __name__ == '__main__':
    asyncio.run(shoot())
    convert()
    print('\n完成。换图后记得重启服务：')
    print('  systemctl --user restart travel-memory.service')
