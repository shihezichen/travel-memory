"""相册墙自动播放：铺满层必须按**面板可见区**定尺寸，不能按照片墙。

用户 2026-09-18 报的缺陷：铺满高度是按照片墙算的，而墙高由照片数量决定 ——
  · 照片少（只有一行）时墙很矮，「放大铺满」就只铺满一行那么高；
  · 照片很多、墙高过面板时，铺满又会顺着墙铺到面板外面去。
但用户看到的那块地方是**面板**，不是墙。正确行为：铺满层的高度恒等于面板
留给照片的可见高度，与照片数量无关。

判据（两条一起才说明问题真修好了）：
  1. 铺满层高度 == 面板可视高度（误差 ≤2px）
  2. 铺满层完整落在面板可视段内（上不越顶、下不越底）
另外测「照片少」和「照片多」两种极端，确保不是碰巧对上一组。
"""
import io

import pytest
from PIL import Image

pytestmark = pytest.mark.e2e


def _seed_photos(e2e_user, count, base_url):
    """往第一个点灌 count 张照片，返回实际张数。

    相册墙抽的是 cellWallByCell 里登记过的格子，所以真照片才作数。
    """
    import httpx
    made = 0
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as c:
        # 复用 e2e_user 已经建好的登录态：直接再登录一次拿 cookie
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        pid = e2e_user["point_ids"][0]
        for i in range(count):
            buf = io.BytesIO()
            Image.new("RGB", (400, 300), (40 + (i * 7) % 180, 90, 200 - i % 150)).save(buf, "JPEG")
            r = c.post(f"/api/points/{pid}/media",
                       files={"file": (f"fill{i}.jpg", buf.getvalue(), "image/jpeg")})
            if r.status_code == 200:
                made += 1
    return made


def _open_albums(page):
    """进相册 tab，等网格铺好。"""
    page.eval("""() => { const t=[...document.querySelectorAll('.tab')]
                          .find(x=>/相册/.test(x.textContent)); if(t) t.click(); }""")
    page.wait_until("document.querySelectorAll('#photo-grid .photo-cell').length > 0",
                    timeout=20000)


def _measure_fill(page, ms=9000):
    """起播并采样铺满层几何。**采样必须在页面里跑** —— Python 往返几十毫秒，
    而放大动画只有 .76s，往返采样会整拍漏掉铺满态。

    返回 (墙高, 采样列表)。采样只含 data-on 的铺满态。
    """
    wall_h = page.eval("() => +document.getElementById('photo-grid')"
                       ".getBoundingClientRect().height.toFixed(1)")
    page.eval("""
      () => { window.__L=[];
        window.__i=setInterval(()=>{
          const v=document.getElementById('albums-view');
          if(!v) return;
          // 铺满层**故意不限父节点**：挂错地方（比如挂在网格上）正是要抓的
          // 缺陷之一，写成 `#albums-view > .pw-fill` 会让那种情况变成
          // 「一个采样都没有」，断言虽然还是会红，但报出来的原因是错的。
          const f=document.querySelector('.pw-fill[data-on]');
          if(!f) return;
          const fr=f.getBoundingClientRect(), vr=v.getBoundingClientRect();
          window.__L.push({h:+fr.height.toFixed(1), vh:+vr.height.toFixed(1),
            top:+fr.top.toFixed(1), bottom:+fr.bottom.toFixed(1),
            vtop:+vr.top.toFixed(1), vbot:+vr.bottom.toFixed(1)});
        }, 50); }
    """)
    page.eval("() => pwToggle('albums')")
    page.wait_ms(ms)
    samples = page.eval("() => { clearInterval(window.__i); return window.__L; }")
    page.eval("() => pwReset()")
    return wall_h, samples


def _assert_fill_matches_panel(wall_h, samples, photos):
    assert samples, (f"{photos} 张照片：整段播放里一次铺满态都没采到 —— "
                     f"墙高 {wall_h}px，自动播放可能根本没起来")
    # 「长到位」= 高度达到本序列最大值的那几帧（前面都是放大动画的中间值）
    peak = max(s["h"] for s in samples)
    fin = [s for s in samples if s["h"] >= peak - 0.5][-1]
    assert abs(fin["h"] - fin["vh"]) <= 2, (
        f"{photos} 张照片：铺满层高 {fin['h']}px ≠ 面板可视高 {fin['vh']}px"
        f"（墙高 {wall_h}px）—— 尺寸又跟照片墙走了")
    assert fin["top"] >= fin["vtop"] - 2, (
        f"{photos} 张照片：铺满层顶 {fin['top']} 越过了面板顶 {fin['vtop']}")
    assert fin["bottom"] <= fin["vbot"] + 2, (
        f"{photos} 张照片：铺满层底 {fin['bottom']} 越过了面板底 {fin['vbot']}")


def test_fill_covers_panel_not_wall_few_photos(browser, live_server, e2e_user):
    """照片少（墙比面板矮）：铺满也必须铺满面板，而不是只有一行那么高。"""
    _seed_photos(e2e_user, 4, live_server)
    pg = browser.new_page(live_server, mobile=True)
    pg.login(e2e_user["username"], e2e_user["password"])
    _open_albums(pg)
    wall_h, samples = _measure_fill(pg)
    panel = pg.eval("() => document.getElementById('albums-view').clientHeight")
    # 前提：这一组确实「墙比面板矮」，否则这条用例就没测到该测的东西
    assert wall_h < panel, (
        f"用例前提不成立：墙高 {wall_h}px 应当小于面板 {panel}px（照片太少了？）")
    _assert_fill_matches_panel(wall_h, samples, 4)
    pg.close()


def test_fill_never_exceeds_panel_many_photos(browser, live_server, e2e_user):
    """照片多（墙远高于面板）：铺满不能超出面板，得在面板里收住。"""
    _seed_photos(e2e_user, 24, live_server)
    pg = browser.new_page(live_server, mobile=True)
    pg.login(e2e_user["username"], e2e_user["password"])
    _open_albums(pg)
    wall_h, samples = _measure_fill(pg)
    panel = pg.eval("() => document.getElementById('albums-view').clientHeight")
    # 前提：这一组确实「墙比面板高」，否则测的是另一回事
    assert wall_h > panel, (
        f"用例前提不成立：墙高 {wall_h}px 应当高于面板 {panel}px（照片不够多？）")
    _assert_fill_matches_panel(wall_h, samples, 24)
    pg.close()


def test_fill_height_same_across_photo_counts(browser, live_server, e2e_user):
    """两组的墙高天差地别，铺满高度必须**一样**（都等于面板高）。"""
    pg = browser.new_page(live_server, mobile=True)
    pg.login(e2e_user["username"], e2e_user["password"])
    _open_albums(pg)
    panel = pg.eval("() => document.getElementById('albums-view').clientHeight")

    _seed_photos(e2e_user, 4, live_server)
    pg.reload()
    _open_albums(pg)
    _, few = _measure_fill(pg)
    few_wall = pg.eval("() => +document.getElementById('photo-grid')"
                       ".getBoundingClientRect().height.toFixed(1)")

    _seed_photos(e2e_user, 20, live_server)
    pg.reload()
    _open_albums(pg)
    _, many = _measure_fill(pg)
    many_wall = pg.eval("() => +document.getElementById('photo-grid')"
                        ".getBoundingClientRect().height.toFixed(1)")

    assert few and many, "两组里至少一组没采到铺满态"
    # 墙高必须真的差很多，否则这个对比没意义
    assert many_wall > few_wall * 2, (
        f"用例前提不成立：两次墙高 {few_wall} / {many_wall} 没拉开差距")

    def settled(s):
        peak = max(x["h"] for x in s)
        return [x for x in s if x["h"] >= peak - 0.5][-1]

    f, m = settled(few), settled(many)
    assert abs(f["h"] - m["h"]) <= 2, (
        f"照片少时铺满 {f['h']}px、照片多时 {m['h']}px —— 还是跟着墙高 "
        f"({few_wall} vs {many_wall}) 走")
    for x in (f, m):
        assert abs(x["h"] - panel) <= 2, f"铺满高 {x['h']}px 不等于面板 {panel}px"
    pg.close()
