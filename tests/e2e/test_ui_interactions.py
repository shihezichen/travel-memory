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
    head = b"\x00\x00\x00\x18ftypmp42"
    blob = hashlib.sha256(f"tm-ui-{seed}".encode()).digest()
    body = (blob * (size // len(blob) + 1))[:size - len(head)]
    path.write_bytes(head + body)
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
    page.p.set_input_files("#file-input", [_fake_video(tmp_path / "l.mp4", 3)])
    page.wait_for(f"{UPLOADING} .up-act-play")
    page.eval(f"document.querySelector('{UPLOADING} .up-act-play').click()")
    page.wait_for(".local-preview video")
    # 放的是本地那份，不是服务端的地址
    assert page.eval("document.querySelector('.local-preview video').src")\
        .startswith("blob:")
    page.eval("document.querySelector('.local-preview-close').click()")
    page.wait_until("document.querySelectorAll('.local-preview').length === 0")


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
