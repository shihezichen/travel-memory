"""这一批用例守的是「界面上看得见的行为」，每一条都对应一个真实反馈过的缺陷。

- 新建行程之后没进去（还停在旧行程）
- 传/删媒体之后，地图上的数量角标不跟着变
- 手机上点地图的照片角标没反应（高德吃掉了触摸，click 根本不合成）
- 上传中的视频没法取消 / 暂停 / 先看看本地那份
- 进度到 100% 之后长时间没有任何说明
- 灯箱控件一直压在画面上，看视频碍事
- 手机上顶部两条（顶栏 + 账号信息）占地方，没法收起来
"""
import hashlib
import json

import pytest

pytestmark = pytest.mark.e2e

REAL = ("#media-grid .media-item:not(.media-add):not(.media-uploading)"
        ":not(.media-resume)")
UPLOADING = "#media-grid .media-uploading"


def _login(page, e2e_user):
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)


def _open_detail(page, e2e_user, idx):
    _login(page, e2e_user)
    pid = e2e_user["point_ids"][idx]
    page.eval(f"showDetail({pid})")
    page.wait_for("#media-grid .media-add")
    return pid


def _fake_video(path, seed, size=220_000):
    """一个**故意解不了码**的假 mp4：有像样的 ftyp 头 + 一段随机字节。

    别拿它当「能播的视频」用。这一批里只有「解码失败要给人话」那条该用它；
    需要真的播起来的地方（比如上传 cell 里点▶看本地那份）用 `_playable_video`，
    否则浏览器拿到它就是一个 readyState=0 的死块，黑屏不算，还得看运气。
    """
    head = b"\x00\x00\x00\x18ftypmp42"
    blob = hashlib.sha256(f"tm-ui-{seed}".encode()).digest()
    body = (blob * (size // len(blob) + 1))[:size - len(head)]
    path.write_bytes(head + body)
    return str(path)


def _playable_video(path, seconds=1):
    """现造一小段**真能解码**的 mp4（用 ffmpeg，和 test_video_fullscreen 同源）。

    上传 cell 里「点▶看本地那份」的用例必须喂能解码的片子：那个按钮走的是
    `<video>` 的原生解码，喂假文件的话它永远 readyState=0 / `paused===true`，
    用例就只能靠「错误分支恰好闪一下」蒙混过关（实测 20 轮错 6 轮）。
    """
    import subprocess
    import tempfile
    from pathlib import Path as _P

    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as d:
        out = _P(d) / "e2e.mp4"
        # 必须写真文件而不是 pipe：mp4 的 muxer 要回头改写 moov，不支持不可 seek 的输出
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
               f"color=c=navy:s=320x240:d={seconds}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               "-t", str(seconds), str(out)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            pytest.skip("这台机器的 ffmpeg 造不出测试用的 mp4")
        path.write_bytes(out.read_bytes())
    return str(path)


def _jpeg(path, color=(200, 60, 30)):
    from PIL import Image
    Image.new("RGB", (320, 240), color).save(str(path), "JPEG")
    return str(path)


def _throttle(page, up_bps=90_000):
    cdp = page.p.context.new_cdp_session(page.p)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 20,
        "downloadThroughput": 8_000_000, "uploadThroughput": up_bps})
    return cdp


# ==========================================================================
# 新建行程之后要进到新行程里
# ==========================================================================
def test_creating_a_trip_switches_into_it(page, e2e_user):
    _login(page, e2e_user)
    before = page.eval("currentTripId")
    page.click("#btn-new-trip")
    page.wait_for("#modal-fields input[name='name']")
    page.fill("#modal-fields input[name='name']", "刚建的行程")
    page.click("#modal-form button[type='submit']")
    page.wait_until("document.getElementById('modal').classList.contains('hidden')")
    # 新行程是空的，点列表应该清空，且当前行程 id 变成新那个
    page.wait_until(f"currentTripId !== {before}", timeout=8000)
    assert page.eval("currentTripId") != before
    names = page.eval(
        "Array.from(document.querySelectorAll('#trip-select option'))"
        ".find(o => o.value === String(currentTripId)).textContent")
    assert names == "刚建的行程"
    # currentTripId 是 selectTrip 一进门就赋的，点列表要等 loadPoints 回来才空
    page.wait_until("document.querySelectorAll('#point-list li').length === 0",
                    timeout=8000)


# ==========================================================================
# 媒体增删 → 地图角标数量跟着变
# ==========================================================================
def _badge_text(page):
    return page.eval(
        "(document.querySelector('.photo-marker-badge') || {}).textContent || ''")


def test_map_badge_count_follows_media_changes(page, e2e_user, tmp_path):
    """e2e_user 给第 1 个点预置了 1 张照片，所以初始角标是 1。"""
    pid = _open_detail(page, e2e_user, idx=0)
    page.wait_until("document.querySelectorAll('.photo-marker-badge').length === 1",
                    timeout=10000)
    assert _badge_text(page) == "1"

    page.p.set_input_files("#file-input", [_jpeg(tmp_path / "a.jpg")])
    page.wait_until(f"document.querySelectorAll('{REAL}').length === 2", timeout=30000)
    page.wait_until("(document.querySelector('.photo-marker-badge')||{}).textContent === '2'",
                    timeout=10000)

    # 删掉刚传的那张，角标要退回 1
    page.eval("window.__origConfirm = window.tmConfirm; window.tmConfirm = () => Promise.resolve(true)")
    page.eval(f"document.querySelectorAll('{REAL} .del')[1].click()")
    page.wait_until("(document.querySelector('.photo-marker-badge')||{}).textContent === '1'",
                    timeout=10000)
    assert len(page.eval(f"[...document.querySelectorAll('{REAL}')].map(x=>x.dataset.mid)")) == 1
    assert pid


# ==========================================================================
# 手机上点照片角标：高德把 click 吃了，只能靠 touch 兜底
# ==========================================================================
def test_photo_badge_opens_lightbox_from_raw_touch(browser, live_server, e2e_user):
    """只派发 touchstart/touchend、**不**派发 click —— 这正是真机上的情形：
    高德接管触摸并 preventDefault，浏览器于是不再合成 click。
    修之前这一下点下去毫无反应。
    """
    pg = browser.new_page(live_server, mobile=True)
    try:
        _login(pg, e2e_user)
        pg.wait_until("document.querySelectorAll('.photo-marker').length === 1",
                      timeout=10000)
        assert not pg.eval("!document.getElementById('lightbox').classList.contains('hidden')")
        pg.eval("""
          (() => {
            const el = document.querySelector('.photo-marker');
            const r = el.getBoundingClientRect();
            const map = document.getElementById('map');
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            const mk = (id) => new Touch({ identifier: id, target: map,
                                           clientX: x, clientY: y,
                                           pageX: x, pageY: y });
            const t = mk(1);
            map.dispatchEvent(new TouchEvent('touchstart',
              { bubbles: true, cancelable: true, touches: [t], targetTouches: [t],
                changedTouches: [t] }));
            map.dispatchEvent(new TouchEvent('touchend',
              { bubbles: true, cancelable: true, touches: [], targetTouches: [],
                changedTouches: [t] }));
          })()
        """)
        pg.wait_until(
            "!document.getElementById('lightbox').classList.contains('hidden')",
            timeout=8000)
        assert pg.eval("lightboxMedia.length") >= 1
    finally:
        pg.close()


# ==========================================================================
# 上传中：取消 / 暂停 / 看本地那份 / 100% 之后的说明
# ==========================================================================
def test_uploading_cell_can_be_cancelled(page, e2e_user, tmp_path):
    pid = _open_detail(page, e2e_user, idx=1)
    _throttle(page)
    page.p.set_input_files("#file-input", [_fake_video(tmp_path / "c.mp4", 1)])
    page.wait_for(f"{UPLOADING} .up-act-cancel")
    page.eval(f"document.querySelector('{UPLOADING} .up-act-cancel').click()")
    page.wait_until(f"document.querySelectorAll('{UPLOADING}').length === 0", timeout=8000)

    # 服务端不该多出东西来（点 1 本来就是空的）
    page.wait_ms(1500)
    import httpx
    with httpx.Client(base_url=page.base_url, timeout=20, trust_env=False) as c:
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        assert c.get(f"/api/points/{pid}/media").json() == []


E2E_CHUNK = 256 * 1024      # 服务端允许的最小分片


@pytest.fixture
def chunky_page(browser, live_server):
    """把分片阈值调到 256KB —— 暂停/续传只在分片路径上有意义，
    真实阈值是 8MB，E2E 里不值得为它真传一个 8MB 文件。"""
    pg = browser.new_page(live_server)
    pg.p.add_init_script(
        f"window.TM_CHUNK_THRESHOLD = {E2E_CHUNK}; window.TM_CHUNK_SIZE = {E2E_CHUNK};")
    yield pg
    pg.close()


def test_uploading_cell_pause_and_resume(chunky_page, e2e_user, tmp_path):
    """暂停要真的停下来，继续之后要能传完。"""
    page = chunky_page
    pid = _open_detail(page, e2e_user, idx=2)
    _throttle(page, up_bps=60_000)
    page.p.set_input_files("#file-input",
                           [_fake_video(tmp_path / "p.mp4", 2, E2E_CHUNK * 4)])
    page.wait_for(f"{UPLOADING} .up-act-pause")
    page.eval(f"document.querySelector('{UPLOADING} .up-act-pause').click()")
    page.wait_until(f"document.querySelector('{UPLOADING}').classList.contains('paused')",
                    timeout=5000)
    assert "已暂停" in page.text(f"{UPLOADING} .up-stage")
    # 暂停之后「继续上传」不能长得跟旁边的「本地播放」一样 —— 以前两颗都是 ▶
    icons = page.eval(
        "JSON.stringify(["
        f"document.querySelector('{UPLOADING} .up-act-play').innerHTML,"
        f"document.querySelector('{UPLOADING} .up-act-pause').innerHTML])")
    play_icon, resume_icon = json.loads(icons)
    assert play_icon and resume_icon
    assert play_icon != resume_icon, "继续上传和本地播放用了同一个图标"
    assert "继续上传" in page.eval(
        f"document.querySelector('{UPLOADING} .up-act-pause').title")

    # 暂停之后进度不许再涨（在飞的分片被 abort，也不会有新的发出去）。
    # 先等 300ms 让「点下去那一刻正好传完的那一片」落定，再取基准值。
    page.wait_ms(300)
    pct = lambda: page.text(f"{UPLOADING} .up-pct")
    frozen = pct()
    page.wait_ms(1500)
    assert pct() == frozen, f"暂停之后进度还在涨：{frozen} → {pct()}"

    page.eval(f"document.querySelector('{UPLOADING} .up-act-pause').click()")
    page.wait_until(f"document.querySelectorAll('{REAL}').length === 1", timeout=60000)
    import httpx
    with httpx.Client(base_url=page.base_url, timeout=20, trust_env=False) as c:
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        assert len(c.get(f"/api/points/{pid}/media").json()) == 1


def test_uploading_cell_plays_the_local_file(page, e2e_user, tmp_path):
    _open_detail(page, e2e_user, idx=1)
    _throttle(page)
    page.p.set_input_files("#file-input", [_playable_video(tmp_path / "l.mp4")])
    page.wait_for(f"{UPLOADING} .up-act-play")
    page.eval(f"document.querySelector('{UPLOADING} .up-act-play').click()")
    page.wait_for(".local-preview video")
    # 放的是本地那份，不是服务端的地址
    assert page.eval("document.querySelector('.local-preview video').src")\
        .startswith("blob:")
    # 播起来了才算数：autoplay 属性会被自动播放策略拦下，必须是手动 play()。
    # 只等「不再 paused」不够 —— 解不了码的片子也会停在 paused=true 上，
    # 那样用例就变成在赌错误分支有没有及时把提示弹出来，纯看运气。
    page.wait_until("(() => { const v = document.querySelector('.local-preview video');"
                    "  return v.readyState >= 2 && v.currentTime > 0 && !v.paused; })()",
                    timeout=8000)
    page.eval("document.querySelector('.local-preview-close').click()")
    page.wait_until("document.querySelectorAll('.local-preview').length === 0")


def test_local_preview_says_so_when_the_codec_is_unsupported(page, e2e_user, tmp_path):
    """解不了码的时候要给一句人话，不能只留一块黑屏。

    ⚠ 这里**不能** `wait_for('.local-preview video')` 等它「可见」。
    Playwright 的可见性判定要求元素有非空 bounding box，而一块解不了码的
    <video> 根本没有内在尺寸 —— 能不能出现那么一帧有尺寸，取决于错误事件
    和这一步谁先到，于是这个 wait_for 就是在赌几十毫秒（实测 3~11ms 的窗口，
    不压网络时单跑也有 ~13% 的轮次赌输）。文案出来时 video 已经按设计藏掉了，
    「可见」本就不该是这条用例的验收条件。

    所以：等 overlay 挂上（挂载，不是可见），再等文案，最后查两件事 ——
    说了人话，且黑框确实藏起来了。
    """
    _open_detail(page, e2e_user, idx=1)
    _throttle(page)
    page.p.set_input_files("#file-input", [_fake_video(tmp_path / "bad.mp4", 7)])
    page.wait_for(f"{UPLOADING} .up-act-play")
    page.eval(f"document.querySelector('{UPLOADING} .up-act-play').click()")
    # state="attached"：元素进了 DOM 就算数，不管有没有渲染出尺寸
    page.p.wait_for_selector(".local-preview video", state="attached", timeout=8000)
    page.wait_until("document.querySelector('.local-preview-err')"
                    ".style.display !== 'none'", timeout=8000)
    assert "转码" in page.text(".local-preview-err")
    # 光有文案不够：解不了的这块 video 必须真的藏掉，否则黑框和提示语会一起
    # 杵在屏幕上。这条断言是补的 —— 从前只查 err 的 display，把「video 没藏」
    # 这种回归放过去了（而且失败时会伪装成上面那句 wait_for 超时，看着像偶发）。
    assert page.eval("getComputedStyle(document.querySelector('.local-preview video'))"
                     ".display") == "none", "解不了码时 video 应当隐藏"


def test_progress_shows_what_the_server_is_doing_after_100pct(page, e2e_user, tmp_path):
    """字节传完之后服务端还要合并/生成缩略图，这段时间界面必须说人话。"""
    _open_detail(page, e2e_user, idx=1)
    _throttle(page, up_bps=200_000)
    page.p.set_input_files("#file-input", [_fake_video(tmp_path / "s.mp4", 4)])
    page.wait_until(
        "(() => { const c = document.querySelector('" + UPLOADING + "');"
        "  return !!c && c.classList.contains('processing'); })()",
        timeout=40000)
    assert page.text(f"{UPLOADING} .up-stage").strip(), "100% 之后没有任何状态提示"


# ==========================================================================
# 灯箱控件自动隐藏
# ==========================================================================
def test_lightbox_controls_hide_when_idle_and_come_back_on_tap(page, e2e_user):
    _login(page, e2e_user)
    page.eval(f"openPointPhotos({e2e_user['point_ids'][0]})")
    page.wait_until("!document.getElementById('lightbox').classList.contains('hidden')")
    assert not page.eval("document.getElementById('lightbox').classList.contains('idle')")
    page.wait_until("document.getElementById('lightbox').classList.contains('idle')",
                    timeout=8000)
    # 淡出之后不能再挡着操作（opacity 0 + pointer-events none）
    assert page.eval(
        "getComputedStyle(document.getElementById('lightbox-close')).pointerEvents") == "none"
    page.eval("document.getElementById('lightbox')"
              ".dispatchEvent(new PointerEvent('pointerdown', {bubbles:true}))")
    assert not page.eval("document.getElementById('lightbox').classList.contains('idle')")


def test_lightbox_controls_are_the_same_size(page, e2e_user):
    """关闭 / 下载 / 清晰度三枚：命中区一致（44px 触摸下限），可见部分也一致。"""
    _login(page, e2e_user)
    page.eval(f"openPointPhotos({e2e_user['point_ids'][0]})")
    page.wait_for("#lightbox-download")
    boxes = page.eval("""
      ['#lightbox-close', '#lightbox-download', '#lightbox-prev', '#lightbox-next']
        .map(s => { const r = document.querySelector(s).getBoundingClientRect();
                    return [Math.round(r.width), Math.round(r.height)]; })
    """)
    for w, h in boxes:
        assert w >= 44 and h >= 44, boxes
    assert len({tuple(b) for b in boxes}) == 1, f"控件尺寸不一致：{boxes}"


# ==========================================================================
# 手机端顶部两条可收起
# ==========================================================================
def test_mobile_chrome_can_be_collapsed_and_summoned(browser, live_server, e2e_user):
    pg = browser.new_page(live_server, mobile=True)
    try:
        _login(pg, e2e_user)
        pg.wait_for("#btn-chrome-toggle")
        h0 = pg.eval("document.querySelector('.topbar').getBoundingClientRect().height")
        assert h0 > 20

        pg.tap("#btn-chrome-toggle")
        pg.wait_until("document.body.classList.contains('chrome-hidden')")
        pg.wait_until(
            "document.querySelector('.topbar').getBoundingClientRect().height < 1",
            timeout=4000)
        assert pg.eval(
            "document.querySelector('.userbar').getBoundingClientRect().height") < 1

        pg.tap("#btn-chrome-toggle")
        pg.wait_until("!document.body.classList.contains('chrome-hidden')")
        pg.wait_until(
            f"document.querySelector('.topbar').getBoundingClientRect().height > {h0 - 1}",
            timeout=4000)
    finally:
        pg.close()


# ==========================================================================
# 足迹热力图：打开图层要真能画出点来
# ==========================================================================
def test_heatmap_layer_draws_footprint_markers(page, e2e_user):
    """重构足迹图层时最容易踩的坑：数据拉回来了但没画到地图上。

    这一条守着「勾上图层 -> 真的有 CircleMarker 出现在地图上」这条链路，
    顺带确认点少的时候走的是精确坐标那条路（aggregated=false，不该重拉）。
    """
    _login(page, e2e_user)
    page.eval("toggleLayerHeatmap(true)")
    page.wait_until("Array.isArray(footprintMarkers) && footprintMarkers.length > 0",
                    timeout=15000)
    # 一个足迹点画「光晕 + 实心」两层，所以 marker 数应当是点数的两倍
    assert page.eval("footprintMarkers.length") == page.eval("footprintData.length") * 2
    assert page.eval("footprintData.length") >= 3
    # 测试数据只有几个点，远没到服务端聚合的阈值
    assert page.eval("footprintAggregated") is False
    # 关掉图层，marker 要从地图上摘下来（不是只把它们藏起来）
    page.eval("toggleLayerHeatmap(false)")
    page.wait_until(
        "map.getAllOverlays().filter(o => footprintMarkers.indexOf(o) >= 0).length === 0",
        timeout=8000)


def test_heatmap_requests_the_current_viewport(page, e2e_user):
    """视野要传给服务端 —— 不传的话放大地图时会退化成「聚合全世界」。"""
    _login(page, e2e_user)
    seen = []
    page.p.on("request", lambda r: seen.append(r.url) if "/api/footprint" in r.url else None)
    page.eval("toggleLayerHeatmap(true)")
    page.wait_until("Array.isArray(footprintMarkers) && footprintMarkers.length > 0",
                    timeout=15000)
    assert seen, "打开足迹图层没有请求 /api/footprint"
    assert "bbox=" in seen[-1], f"请求里没带视野范围：{seen[-1]}"
    assert "zoom=" in seen[-1]


# ==========================================================================
# 里程标签的图层开关
# ==========================================================================
# 用户反馈：地图上每段路线中点那个公里数是常驻的，点多了整张图全是小胶囊，
# 想看底图得先把它们弄走 —— 但没有任何地方能关。现在归到「图层控制」里，
# 和景区/卫星/路况/照片一样是个勾选框。
#
# 判定看的是胶囊上那个 .seg-label-off（`display:none`）—— 这正是把标签藏起来的
# 那个机制本身，不是我们另记的一本账。不查 AMap 的私有字段（跨版本会碎），
# 也不查 DOM 里有没有 .seg-label 元素：e2e 跑的是 amap_stub.js，
# 桩里的 Marker 根本不把 content 挂进 DOM，那样断言会永远是 0。

_VISIBLE_PILLS = ("segLabels.filter(m => m._pill && "
                  "!m._pill.classList.contains('seg-label-off')).length")


def _wait_seg_labels(page):
    """等里程标签建出来（3 个点 = 2 段）。"""
    page.wait_until("Array.isArray(segLabels) && segLabels.length >= 2", timeout=15000)


def test_里程标签默认显示(page, e2e_user):
    """加开关之前它就是常驻的，默认值不能因为多了个勾选框就变。"""
    _login(page, e2e_user)
    _wait_seg_labels(page)
    assert page.eval("document.querySelector('#lay-mileage').checked") is True
    assert page.eval(_VISIBLE_PILLS) >= 2, "默认状态下里程标签应当看得见"


def test_取消勾选里程标签就从地图上消失(page, e2e_user):
    _login(page, e2e_user)
    _wait_seg_labels(page)
    page.eval("""() => {
      const cb = document.querySelector('#lay-mileage');
      cb.checked = false;
      cb.dispatchEvent(new Event('change'));
    }""")
    page.wait_until(f"{_VISIBLE_PILLS} === 0", timeout=8000)
    # 再勾回来要能恢复 —— 不能是「关了就永久没了」
    page.eval("""() => {
      const cb = document.querySelector('#lay-mileage');
      cb.checked = true;
      cb.dispatchEvent(new Event('change'));
    }""")
    page.wait_until(f"{_VISIBLE_PILLS} >= 2", timeout=8000)


def test_关掉之后重建标签不会自己冒回来(page, e2e_user):
    """最容易漏的一条。

    里程标签会被反复重建：真实路线算完一次、切行程一次、临时分段建好一次。
    如果只在「点勾选框」那一刻 hide()，那么关掉里程之后随便触发一次重建，
    标签就全回来了 —— 用户的感受是「这个开关时灵时不灵」。
    """
    _login(page, e2e_user)
    _wait_seg_labels(page)
    page.eval("""() => {
      const cb = document.querySelector('#lay-mileage');
      cb.checked = false;
      cb.dispatchEvent(new Event('change'));
    }""")
    page.wait_until(f"{_VISIBLE_PILLS} === 0", timeout=8000)
    # 直接调重建，等价于真实路线算完那一下
    page.eval("renderSegLabels()")
    page.wait_ms(300)
    assert page.eval(_VISIBLE_PILLS) == 0, "重建之后里程标签又冒回来了，开关没守住"
    assert page.eval("segLabels.length") >= 2, "标签本身应当还在，只是不显示"


def test_里程开关刷新后还记得(page, e2e_user):
    """和其它图层一样存在 localStorage 里，刷新不该丢。"""
    _login(page, e2e_user)
    _wait_seg_labels(page)
    page.eval("""() => {
      const cb = document.querySelector('#lay-mileage');
      cb.checked = false;
      cb.dispatchEvent(new Event('change'));
    }""")
    page.wait_until(f"{_VISIBLE_PILLS} === 0", timeout=8000)
    page.reload()
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    _wait_seg_labels(page)
    assert page.eval("document.querySelector('#lay-mileage').checked") is False
    page.wait_ms(300)
    assert page.eval(_VISIBLE_PILLS) == 0, "刷新之后里程标签又回来了，偏好没存住"


def test_图层面板在矮地图上不会被裁掉(browser, live_server, e2e_user):
    """加了第 6 项之后的新风险。

    手机端 `.map` 是 `height:45~55vh` + `overflow:hidden`，面板绝对定位在里面。
    项数一多，面板底部会被直接裁掉 —— 最后一项（正好是新加的「里程标签」）
    既看不见也点不到，而且没有任何滚动条提示还有内容。
    这里把地图压到很矮，断言面板仍然收在地图里、且最后一项够得着。
    """
    pg = browser.new_page(live_server, mobile=True)
    try:
        _login(pg, e2e_user)
        # 压到 260px —— 比 iPhone SE 上 45vh（约 256px）略宽松，正是会出事的尺寸
        pg.eval("document.getElementById('map').style.height = '260px'")
        pg.click("#btn-layers")
        pg.wait_for("#layer-panel:not(.hidden)")
        g = pg.eval("""(() => {
          const p = document.getElementById('layer-panel');
          const m = document.getElementById('map');
          const last = document.querySelector('#lay-mileage');
          const pr = p.getBoundingClientRect(), mr = m.getBoundingClientRect();
          // 把最后一项滚进视野（面板限高后内部可滚）
          last.scrollIntoView({block: 'nearest'});
          const lr = last.getBoundingClientRect();
          return {
            panelBottom: pr.bottom, mapBottom: mr.bottom,
            scrollable: p.scrollHeight > p.clientHeight + 1,
            lastTop: lr.top, lastBottom: lr.bottom,
            panelTop: pr.top,
          };
        })()""")
        assert g["panelBottom"] <= g["mapBottom"] + 1, (
            f"面板超出地图下沿，会被 overflow:hidden 裁掉："
            f"面板底 {g['panelBottom']}，地图底 {g['mapBottom']}")
        # 面板确实被限高了（说明 max-height 生效），且最后一项能滚到可视区内
        assert g["scrollable"], "面板没有进入可滚动状态，限高可能没生效"
        assert g["panelTop"] - 1 <= g["lastTop"] and g["lastBottom"] <= g["panelBottom"] + 1, (
            f"「里程标签」滚不进面板可视区：项 {g['lastTop']}~{g['lastBottom']}，"
            f"面板 {g['panelTop']}~{g['panelBottom']}")
    finally:
        pg.close()
