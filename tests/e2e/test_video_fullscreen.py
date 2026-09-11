"""视频在灯箱里播放时，原生控制条不许被说明栏盖住。

这条用例对应一个真实反馈的缺陷：视频传完点播放，再点播放器上的全屏按钮，
弹出来的却是「添加描述」—— 因为 `.lb-caption` 是 `position:absolute; bottom:0`
铺满整格底部的一条渐变，正好压在 `<video>` 的原生控制条上；点全屏按钮，
点到的其实是说明栏，而说明栏空着的时候整条都是「开始写描述」的入口。

所以这里不测「点了全屏有没有全屏」（无头 Chrome 的全屏行为不可靠），
而是钉住**几何关系**：说明栏的顶边不得低于视频的底边，且控制条右下角那一块
用 elementFromPoint 打下去必须落在视频上。这两条成立，那个误触就不可能发生。
"""
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _tiny_mp4() -> bytes:
    """现造一段 1 秒的**竖屏** mp4。

    两个要求缺一不可：
      · 真能解码 —— 只有浏览器真的把它当视频渲染，`<video controls>` 才会
        摆出原生控制条，这条用例才有意义；
      · 足够高（360×1280）—— `.lightbox-media` 是 `max-height:100%` 而不是
        `height:100%`，小视频按原始尺寸居中显示，底边离说明栏远着呢，
        那样就算把修复整个撤掉用例也照样绿。竖屏片会被缩放到贴满整格高度，
        底边正好落在说明栏原来的位置上，这才是缺陷真正发生的形状。
    """
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # 必须写真文件而不是 pipe：mp4 的 muxer 要能回头改写 moov，不支持不可 seek 的输出
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "e2e.mp4"
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=360x1280:d=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               "-t", "1", str(out)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            pytest.skip("这台机器的 ffmpeg 造不出测试用的 mp4")
        return out.read_bytes()


@pytest.fixture
def video_media(live_server, e2e_user):
    """给第一个点补传一段视频，返回它在 detailMedia 里的 media id。"""
    import httpx
    data = _tiny_mp4()
    with httpx.Client(base_url=live_server, timeout=60, trust_env=False) as c:
        r = c.post("/api/login", data={"username": e2e_user["username"],
                                       "password": e2e_user["password"]})
        assert r.status_code == 200, r.text
        m = c.post(f"/api/points/{e2e_user['point_ids'][0]}/media",
                   files={"file": ("e2e.mp4", data, "video/mp4")})
        assert m.status_code == 200, m.text
        assert m.json()["type"] == "video"
        return m.json()["id"]


def _open_video_lightbox(page, e2e_user, video_id):
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval(f"showDetail({e2e_user['point_ids'][0]})")
    page.wait_for("#media-grid .media-add")
    page.wait_until("Array.isArray(detailMedia) && detailMedia.length >= 2",
                    timeout=15000)
    page.eval(
        "(vid) => { lightboxMedia = detailMedia;"
        " openLightbox(detailMedia.findIndex(m => Number(m.id) === Number(vid))); }",
        video_id)
    page.wait_for("#lightbox:not(.hidden)")
    page.wait_for("#lightbox .lb-slide.cur video")
    # 视频元数据到了才有真实尺寸，控制条也才摆得出来
    page.wait_until(
        "(() => { const v = document.querySelector('#lightbox .lb-slide.cur video');"
        " return v && v.readyState >= 1 && v.getBoundingClientRect().height > 40; })()",
        timeout=15000)
    page.wait_ms(300)          # 让 tmReserveCaptionSpace 的补量跑完
    return page


def _geometry(page):
    return page.eval("""(() => {
      const slide = document.querySelector('#lightbox .lb-slide.cur');
      const v = slide && slide.querySelector('video');
      const cap = slide && slide.querySelector('.lb-caption');
      if (!v || !cap) return null;
      const vr = v.getBoundingClientRect(), cr = cap.getBoundingClientRect();
      // 控制条右下角那一块 —— 全屏按钮就在这儿
      const hit = document.elementFromPoint(vr.right - 18, vr.bottom - 18);
      return {
        hasVideoClass: slide.classList.contains('has-video'),
        videoBottom: vr.bottom, videoRight: vr.right, videoHeight: vr.height,
        capTop: cr.top, capHeight: cr.height,
        hitTag: hit ? hit.tagName.toLowerCase() : null,
        hitInCaption: !!(hit && hit.closest('.lb-caption')),
      };
    })()""")


def test_说明栏不压在视频的原生控制条上(page, e2e_user, video_media):
    _open_video_lightbox(page, e2e_user, video_media)
    g = _geometry(page)
    assert g, "灯箱里没渲染出 <video> 或说明栏"
    assert g["hasVideoClass"], "视频格没打上 has-video，让位逻辑不会生效"
    assert g["videoHeight"] > 40, "视频没量到真实高度，这条断言会变成空跑"
    # 核心：说明栏顶边不得越过视频底边（留 1px 容差给亚像素布局）
    assert g["capTop"] >= g["videoBottom"] - 1, (
        f"说明栏压在视频上了：视频底 {g['videoBottom']}，说明栏顶 {g['capTop']}")


def test_点控制条右下角落在视频上而不是说明栏(page, e2e_user, video_media):
    """全屏按钮就在这个位置。这里落到说明栏，用户点全屏就会弹出「添加描述」。"""
    _open_video_lightbox(page, e2e_user, video_media)
    g = _geometry(page)
    assert g, "灯箱里没渲染出 <video> 或说明栏"
    assert not g["hitInCaption"], f"控制条右下角被说明栏接管了（命中 {g['hitTag']}）"
    assert g["hitTag"] == "video", f"控制条右下角命中的是 {g['hitTag']}，应该是 video"


def test_照片格不受影响说明栏照旧铺在底部(page, e2e_user, video_media):
    """让位只对视频格生效 —— 照片上那条「点整条加描述」的交互不能被顺手改掉。"""
    _open_video_lightbox(page, e2e_user, video_media)
    g = page.eval("""(() => {
      const idx = lightboxMedia.findIndex(m => m.type === 'photo');
      if (idx < 0) return null;
      lightboxIndex = idx; renderLightbox();
      const slide = document.querySelector('#lightbox .lb-slide.cur');
      const cap = slide.querySelector('.lb-caption');
      const stage = document.querySelector('#lightbox-stage').getBoundingClientRect();
      return {
        hasVideoClass: slide.classList.contains('has-video'),
        padBottom: getComputedStyle(slide).paddingBottom,
        capBottom: cap.getBoundingClientRect().bottom, stageBottom: stage.bottom,
      };
    })()""")
    assert g, "相册里没有照片，这条用例失去意义"
    assert g["hasVideoClass"] is False
    assert abs(g["capBottom"] - g["stageBottom"]) < 2, "照片格的说明栏应当仍然贴着底"


# ---------------------------------------------------------------- 三枚控件同一行
# 用户反馈：「左上角的 480p 图标和 下载、关闭 图标不在同一行里」。
# 根因是 .lb-video-wrap 设了 position:relative，于是它成了清晰度按钮的包含块 ——
# 而 wrap 只有视频画面那么大、还被 .lb-slide 的 4vh/4vw 内边距推在正中间，
# 「左上角 9px」量的是视频的左上角，跟灯箱右上角的下载/关闭差出一整个 4vh。
# 钉住的是**几何**而不是某条 CSS：三枚控件的顶边和高度必须一致。

def _ctl_rects(page):
    return page.eval("""(() => {
      const pick = (sel) => {
        const e = document.querySelector('#lightbox ' + sel);
        if (!e) return null;
        const r = e.getBoundingClientRect();
        return { top: r.top, height: r.height, left: r.left, right: r.right,
                 vis: getComputedStyle(e).visibility !== 'hidden' && r.height > 0 };
      };
      return { quality: pick('.lb-slide.cur .lb-quality-btn'),
               download: pick('.lightbox-download'),
               close: pick('.lightbox-close') };
    })()""")


def test_清晰度下载关闭三枚控件在同一行(page, e2e_user, video_media):
    _open_video_lightbox(page, e2e_user, video_media)
    r = _ctl_rects(page)
    for k in ("quality", "download", "close"):
        assert r[k] and r[k]["vis"], f"{k} 控件没渲染出来，这条断言会变成空跑"
    # 1px 容差给亚像素布局；4vh 的偏差是几十 px，挡不住这条
    assert abs(r["quality"]["top"] - r["close"]["top"]) <= 1, (
        f"清晰度按钮和关闭不在同一行：{r['quality']['top']} vs {r['close']['top']}")
    assert abs(r["download"]["top"] - r["close"]["top"]) <= 1, (
        f"下载和关闭不在同一行：{r['download']['top']} vs {r['close']['top']}")
    assert abs(r["quality"]["height"] - r["close"]["height"]) <= 1, (
        "三枚控件的命中高度应该一致（都是 --lb-tap）")
    # 清晰度在左、下载和关闭在右，别叠在一起
    assert r["quality"]["right"] < r["download"]["left"]
