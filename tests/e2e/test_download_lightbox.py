"""灯箱「保存到本地」的端到端回归：真 Chrome、真下载事件。

这里要钉住的不是接口（那在 tests/api/test_media_download.py），而是**浏览器
真的把它当成下载**：Chrome 只有在收到 Content-Disposition: attachment（或
<a download> 生效）时才会派发 download 事件。所以 `expect_download()` 拿不到
东西，就说明我们又退回到「点了在标签页里打开」的老毛病了。

另外覆盖微信 webview 的降级：那个环境里下载是死路，我们要求代码**明说**，
而不是发一个注定没结果的请求。
"""
import pytest

pytestmark = pytest.mark.e2e

ALLOWED_CONSOLE = (
    "AMap", "amap", "Failed to load resource", "net::ERR_FAILED", "favicon",
    "未登录或令牌无效",
)

WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
             "MicroMessenger/8.0.49(0x18003128) NetType/WIFI Language/zh_CN")


def _open_app_lightbox(page, e2e_user):
    """登录 → 打开第一个点的详情 → 用第一张照片打开灯箱。"""
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval(f"showDetail({e2e_user['point_ids'][0]})")
    page.wait_for("#media-grid .media-add")
    page.wait_until("Array.isArray(detailMedia) && detailMedia.length > 0")
    page.eval("lightboxMedia = detailMedia; openLightbox(0)")
    page.wait_for("#lightbox:not(.hidden)")
    return page


def _open_share_lightbox(page, e2e_user):
    page.goto(f"/share/{e2e_user['share_token']}")
    page.wait_until("shareData && shareData.points && shareData.points.length === 3",
                    timeout=15000)
    page.eval("lightboxMedia = shareData.points.find(p=>p.media && p.media.length).media;"
              "shareOpenLightbox(0)")
    page.wait_for("#lightbox:not(.hidden)")
    return page


# ---------------------------------------------------------------- 主应用
def test_download_button_visible_in_lightbox(page, e2e_user):
    _open_app_lightbox(page, e2e_user)
    assert page.visible("#lightbox-download")
    # 移动端触摸下限：按钮实际渲染尺寸不能小于 44px
    box = page.p.locator("#lightbox-download").bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44, box
    page.assert_no_page_errors(allow=ALLOWED_CONSOLE)


def test_click_download_triggers_real_download(page, e2e_user):
    """Chrome 真的派发了 download 事件 = 浏览器认这是「下载」而不是「打开」。"""
    _open_app_lightbox(page, e2e_user)
    with page.p.expect_download(timeout=15000) as dl:
        page.p.click("#lightbox-download")
    d = dl.value
    name = d.suggested_filename
    # 服务端给的中文名（Content-Disposition 的 filename*）应当被 Chrome 采纳
    assert name.startswith("旅行记忆-"), name
    assert "西湖" in name, name
    assert name.endswith(".jpg"), name
    # 文件确实有内容（下到的是原图，不是错误页）
    path = d.path()
    assert path and path.stat().st_size > 0


def test_plain_navigation_downloads_without_download_attr(page, e2e_user):
    """**这条是 iOS Safari 能不能用的代理指标。**

    iOS Safari 从来不认 <a download>，它只认 Content-Disposition: attachment。
    所以这里绕开我们的 <a download>，直接把地址丢给浏览器导航：仍然触发
    download 事件，就说明「下载」这件事是服务端头促成的，不依赖那个属性。
    """
    _open_app_lightbox(page, e2e_user)
    url = page.eval("tmDownloadUrl(lightboxMedia[0], 'original')")
    with page.p.expect_download(timeout=15000) as dl:
        # location.assign 而不是 <a download> —— 没有任何前端「下载」提示
        page.eval(f"location.assign({url!r})")
    assert dl.value.suggested_filename.startswith("旅行记忆-")


def test_download_url_points_at_owner_endpoint(page, e2e_user):
    """主应用走要登录的那条路；分享页那条不能在这里冒出来。"""
    _open_app_lightbox(page, e2e_user)
    url = page.eval("tmDownloadUrl(lightboxMedia[0], 'original')")
    assert url == f"/api/media/{e2e_user['media_id']}/download"
    assert page.eval("tmDownloadUrl(lightboxMedia[0], '720p')").endswith("?quality=720p")


def test_album_lightbox_also_has_download(page, e2e_user):
    """相册页是灯箱的第三个入口（点详情 / 地图照片点 / 相册），别只顾着前两个。

    它的列表来自 /api/trips/{id}/media，字段和点详情那条不是同一份，
    漏了 id 的话按钮会静默隐藏 —— 所以这里连下载一次，确认拿到的是真文件。
    """
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval("switchTab('albums')")
    page.wait_until("Array.isArray(photoShown) && photoShown.length > 0", timeout=15000)
    page.eval("lightboxMedia = photoShown; openLightbox(0)")
    page.wait_for("#lightbox:not(.hidden)")
    assert page.visible("#lightbox-download")
    with page.p.expect_download(timeout=15000) as dl:
        page.p.click("#lightbox-download")
    assert dl.value.path().stat().st_size > 0


# ---------------------------------------------------------------- 视频选档下载
# 用真视频跑这条不现实（E2E 服务里 ffmpeg 对着假 mp4 只会转失败，永远没有
# done 档位）。档位菜单读的就是 m.qualities 这一个数组，所以直接把它塞进去 ——
# 这条用例要验的是「菜单从已有数据长出来、点谁就下谁」，不是转码本身。
_FAKE_QUALITIES = """
  lightboxMedia[0].type = 'video';
  lightboxMedia[0].qualities = [
    {label:'original', status:'done', url:'/uploads/x.mp4', filesize: 8388608},
    {label:'720p',     status:'done', url:'/uploads/variants/x_720p.mp4', filesize: 2097152},
    {label:'480p',     status:'pending', url:null}
  ];
"""


def test_video_download_menu_lists_ready_qualities(page, e2e_user):
    _open_app_lightbox(page, e2e_user)
    page.eval(_FAKE_QUALITIES + "tmUpdateDownloadBtn(lightboxMedia[0]);")
    page.p.click("#lightbox-download")
    page.wait_for("#lightbox-download-menu:not(.hidden)")
    rows = page.eval("Array.from(document.querySelectorAll('.lb-download-item'))"
                     ".map(e=>e.textContent)")
    # 只列已转好的两档；还在转的 480p 不该出现（点了也下不到）
    assert len(rows) == 2, rows
    assert "原画" in rows[0] and "8.0M" in rows[0]
    assert "720P" in rows[1] and "2.0M" in rows[1]


def test_video_download_menu_first_row_really_downloads(page, e2e_user):
    """从菜单里点「原画」，真的走完一次下载 —— 菜单到下载这条线是通的。

    （这一格底下其实是那张 jpg，只是被我们标成了 video；原画档下下来就是它本身。）
    """
    _open_app_lightbox(page, e2e_user)
    page.eval(_FAKE_QUALITIES + "tmUpdateDownloadBtn(lightboxMedia[0]);")
    page.p.click("#lightbox-download")
    page.wait_for("#lightbox-download-menu:not(.hidden)")
    with page.p.expect_download(timeout=15000) as dl:
        page.eval("document.querySelectorAll('.lb-download-item')[0].click()")
    assert dl.value.suggested_filename.startswith("旅行记忆-")
    # 选完就收起，别挡着视频
    assert page.eval(
        "document.getElementById('lightbox-download-menu').classList.contains('hidden')")


def test_video_download_menu_passes_the_chosen_label(page, e2e_user):
    """点 720P 那一行，交给下载器的必须是 720p 那个地址。

    这里换掉 tmTriggerDownload 来记参数，而不是等一个真的下载：E2E 服务里
    没有真转好的 720p 档（假 mp4 转不出来），真发请求只会拿到 404，
    观察不到下载事件。要验的是「菜单把哪一档传下去了」，记参数正对这一点。
    """
    _open_app_lightbox(page, e2e_user)
    page.eval(_FAKE_QUALITIES + "tmUpdateDownloadBtn(lightboxMedia[0]);")
    # 末尾那个 `0;` 不能删：page.eval 求值结果若是函数，Playwright 会当成
    # 「要执行的函数」再调一次（于是 m=null 直接炸）。让完成值不是函数即可。
    page.eval("window.__dl = [];"
              "tmTriggerDownload = (m, label) => { window.__dl.push(tmDownloadUrl(m, label)); };"
              "0;")
    page.p.click("#lightbox-download")
    page.wait_for("#lightbox-download-menu:not(.hidden)")
    page.eval("document.querySelectorAll('.lb-download-item')[1].click()")
    assert page.eval("window.__dl") == [
        f"/api/media/{e2e_user['media_id']}/download?quality=720p"]
    assert page.eval(
        "document.getElementById('lightbox-download-menu').classList.contains('hidden')")


def test_download_menu_closes_on_outside_click(page, e2e_user):
    _open_app_lightbox(page, e2e_user)
    page.eval(_FAKE_QUALITIES + "tmUpdateDownloadBtn(lightboxMedia[0]);")
    page.p.click("#lightbox-download")
    page.wait_for("#lightbox-download-menu:not(.hidden)")
    page.eval("document.getElementById('lightbox-counter').click()")
    page.wait_until(
        "document.getElementById('lightbox-download-menu').classList.contains('hidden')")


def test_photo_download_has_no_menu(page, e2e_user):
    """照片只有一档，点了就直接下 —— 不该先弹一个只有一项的菜单。"""
    _open_app_lightbox(page, e2e_user)
    with page.p.expect_download(timeout=15000):
        page.p.click("#lightbox-download")
    assert page.eval(
        "document.getElementById('lightbox-download-menu').classList.contains('hidden')")


# ---------------------------------------------------------------- 分享页（匿名）
def test_share_page_anonymous_download(page, e2e_user):
    _open_share_lightbox(page, e2e_user)
    assert page.visible("#lightbox-download")
    with page.p.expect_download(timeout=15000) as dl:
        page.p.click("#lightbox-download")
    d = dl.value
    assert d.suggested_filename.startswith("旅行记忆-")
    assert d.path().stat().st_size > 0


def test_share_download_url_uses_share_endpoint(page, e2e_user):
    _open_share_lightbox(page, e2e_user)
    url = page.eval("tmDownloadUrl(lightboxMedia[0], 'original')")
    assert url == (f"/api/share/{e2e_user['share_token']}"
                   f"/media/{e2e_user['media_id']}/download")


# ---------------------------------------------------------------- 移动端真触摸
def test_mobile_tap_downloads(browser, live_server, e2e_user):
    """iPhone 视口 + 真 touch 事件。

    灯箱上铺着一整套手势逻辑（单指翻页、双指捏合、点一下进放大态），下载按钮
    必须被 touchstart 那份「不接管」名单放行，否则手指点下去会被判成「点图片」，
    合成 click 被 preventDefault 吃掉 —— 表现就是按钮在手机上按不动。
    """
    pg = browser.new_page(live_server, mobile=True)
    try:
        _open_share_lightbox(pg, e2e_user)
        box = pg.p.locator("#lightbox-download").bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44, box
        with pg.p.expect_download(timeout=15000) as dl:
            pg.tap("#lightbox-download")
        assert dl.value.suggested_filename.startswith("旅行记忆-")
        # 点了下载不该顺带进放大态
        assert pg.eval(
            "!document.getElementById('lightbox').classList.contains('zoomed')")
    finally:
        pg.close()


# ---------------------------------------------------------------- 微信降级
def test_wechat_webview_degrades_to_hint(browser, live_server, e2e_user):
    """微信里点下载不发请求，只给「长按保存」的提示 —— 不假装能下载。"""
    pg = browser.new_page(live_server)
    pg.p.add_init_script(
        "Object.defineProperty(navigator, 'userAgent', "
        f"{{get: () => {WECHAT_UA!r}}});")
    try:
        _open_share_lightbox(pg, e2e_user)
        assert pg.eval("tmIsWeChat()") is True
        pg.p.click("#lightbox-download")
        pg.wait_for(".tm-toast")
        toast = pg.text(".tm-toast")
        assert "无法直接下载" in toast and "长按" in toast, toast
    finally:
        pg.close()
