"""上传链路的端到端回归：并发上传 + 切 tab + 断点续传。

这一组用例存在的理由是一条真实缺陷：**同时传多个视频，最后只剩一个**。
根因不在服务端（服务端 5 个并发全都存下来了，见 tests/api/test_media_concurrency.py），
而在前端 —— `renderMedia()` 无条件 `grid.innerHTML=''`，把正在上传的占位格
连同 DOM 引用一起清掉；`runUpload()` 完成时看到 `!cell.isConnected` 就直接 return，
于是「服务端已经存好、前端却再也不显示」。而 `loadDetail()` 会被 tab 点击、
tab 横滑、删除媒体等一大堆日常操作触发。

所以这里的关键动作是：上传途中切一次 tab 再切回来。
"""
import hashlib

import pytest

pytestmark = pytest.mark.e2e

# 每个「视频」的大小。配合下面的上行限速，单个约 2 秒传完 —— 够长到能在
# 传输途中插入一次切 tab，又不至于让整条用例跑成分钟级。
VID_BYTES = 220_000
UP_BPS = 120_000        # 上行 120KB/s


def _fake_video(path, seed: int, size: int = VID_BYTES):
    """造一个「扩展名是 mp4 的文件」。

    服务端按扩展名判类型，缩略图那步 ffmpeg 会失败并回退到占位图 —— 这正是
    我们要的：不依赖真视频素材，也不花时间真的解码。内容用 seed 派生，
    保证每个文件字节不同，合并后的 sha256 才有意义。
    """
    head = b"\x00\x00\x00\x18ftypmp42"
    blob = hashlib.sha256(f"tm-e2e-{seed}".encode()).digest()
    body = (blob * (size // len(blob) + 1))[:size - len(head)]
    path.write_bytes(head + body)
    assert path.stat().st_size == size
    return path


@pytest.fixture
def videos(tmp_path):
    return [str(_fake_video(tmp_path / f"v{i}.mp4", i)) for i in range(3)]


def _throttle(page, up_bps=UP_BPS):
    cdp = page.p.context.new_cdp_session(page.p)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 20,
        "downloadThroughput": 8_000_000, "uploadThroughput": up_bps})
    return cdp


def _open_detail(page, e2e_user, idx=1):
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    pid = e2e_user["point_ids"][idx]
    page.eval(f"showDetail({pid})")
    page.wait_for("#media-grid .media-add")
    return pid


def _server_media(page, e2e_user, pid):
    import httpx
    with httpx.Client(base_url=page.base_url, timeout=20, trust_env=False) as c:
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        return c.get(f"/api/points/{pid}/media").json()


def _wait_server_media(page, e2e_user, pid, n, timeout=90):
    """等服务端真的收到 n 条。

    不能用「网格里没有上传中的格子了」来判断上传结束 —— 那正是本用例要抓的
    缺陷现场（格子被 loadDetail 清掉了，看起来「传完了」，其实还在传）。
    """
    import time
    deadline = time.time() + timeout
    srv = []
    while time.time() < deadline:
        srv = _server_media(page, e2e_user, pid)
        if len(srv) >= n:
            return srv
        page.wait_ms(300)
    return srv


REAL = ("#media-grid .media-item:not(.media-add):not(.media-uploading)"
        ":not(.media-resume)")
UPLOADING = "#media-grid .media-uploading"


def test_concurrent_uploads_survive_a_tab_switch(page, e2e_user, videos):
    """上传途中切走 tab 再切回来，三个视频最后都要在网格里。

    修之前：切回来时网格被重建，还在传的两个格子被丢掉，传完也不回插 ——
    最终网格里只有切 tab 之前就已经传完的那一个。
    """
    pid = _open_detail(page, e2e_user, idx=1)
    _throttle(page)
    page.p.set_input_files("#file-input", videos)

    # 等一个「有在传的」中间态，再切走
    page.wait_until(f"document.querySelectorAll('{UPLOADING}').length > 0", timeout=20000)
    page.click('.tab[data-tab="albums"]')
    page.wait_ms(150)
    page.click('.tab[data-tab="detail"]')

    # 等服务端三条都落库（说明上传本身都成功了），再看前端显示成什么样
    srv = _wait_server_media(page, e2e_user, pid, 3)
    assert len(srv) == 3, f"服务端只有 {len(srv)} 条，问题不在前端"
    page.wait_ms(800)      # 给前端把最后一个成功回调跑完的时间

    grid = page.count(REAL)
    assert grid == 3, f"网格里只有 {grid} 个，服务端有 {len(srv)} 个 —— 上传结果被前端丢了"
    assert page.eval("detailMedia.length") == 3
    # 缩略图各自独立，不能是同一张
    thumbs = page.eval(
        f"Array.from(document.querySelectorAll('{REAL} img')).map(i => i.getAttribute('src'))")
    assert len(set(thumbs)) == 3, f"缩略图重复了: {thumbs}"


def test_concurrent_uploads_without_tab_switch(page, e2e_user, videos):
    """老老实实等着不动的路径也要全都在（防止修复引入回归）。"""
    pid = _open_detail(page, e2e_user, idx=2)
    _throttle(page)
    page.p.set_input_files("#file-input", videos)
    page.wait_until(f"document.querySelectorAll('{REAL}').length === 3", timeout=60000)
    assert len(_server_media(page, e2e_user, pid)) == 3
    assert page.eval("detailMedia.length") == 3


# ==================== 分片上传 / 断点续传 ====================
# 真实阈值是 8MB / 单片 4MB。E2E 里通过 add_init_script 把它调小，
# 这样几百 KB 的文件就能跑出「多分片 + 断了续传」的完整路径，不用真传 100MB。
E2E_CHUNK = 256 * 1024          # 服务端允许的最小分片
BIG_BYTES = E2E_CHUNK * 4 + 4321


@pytest.fixture
def chunky_page(browser, live_server):
    pg = browser.new_page(live_server)
    pg.p.add_init_script(
        f"window.TM_CHUNK_THRESHOLD = {E2E_CHUNK}; window.TM_CHUNK_SIZE = {E2E_CHUNK};")
    yield pg
    pg.close()


@pytest.fixture
def big_video(tmp_path):
    return str(_fake_video(tmp_path / "big.mp4", 99, BIG_BYTES))


def _download_sha256(page, e2e_user, filename):
    """把服务端存下来的文件原样取回来算 sha256 —— 合并对不对，只有字节说了算。"""
    import httpx
    with httpx.Client(base_url=page.base_url, timeout=60, trust_env=False) as c:
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        r = c.get(f"/uploads/{filename}")
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest(), len(r.content)


def _sessions(page, e2e_user, pid):
    import httpx
    with httpx.Client(base_url=page.base_url, timeout=20, trust_env=False) as c:
        c.post("/api/login", data={"username": e2e_user["username"],
                                   "password": e2e_user["password"]})
        return c.get(f"/api/points/{pid}/upload-sessions").json()


def test_big_file_goes_chunked_and_merges_byte_exact(chunky_page, e2e_user, big_video):
    """大文件走分片路径，合并结果必须和本地文件 sha256 一致；
    进度环要是聚合出来的真实进度，不能从 0 直接跳 100。"""
    page = chunky_page
    pid = _open_detail(page, e2e_user, idx=1)
    _throttle(page, up_bps=400_000)

    seen = []
    page.p.on("request", lambda r: seen.append(r.method + " " + r.url))
    # 每 120ms 采一次进度环上的百分比，事后检查有没有中间态
    page.eval("""() => {
      window.__pcts = [];
      window.__pctTimer = setInterval(() => {
        const e = document.querySelector('#media-grid .media-uploading .up-pct');
        if (e) window.__pcts.push(e.textContent);
      }, 120);
    }""")
    page.p.set_input_files("#file-input", [big_video])
    page.wait_until(f"document.querySelectorAll('{REAL}').length === 1", timeout=90000)
    page.eval("clearInterval(window.__pctTimer)")

    srv = _server_media(page, e2e_user, pid)
    assert len(srv) == 1
    local = hashlib.sha256(open(big_video, "rb").read()).hexdigest()
    remote, size = _download_sha256(page, e2e_user, srv[0]["filename"])
    assert size == BIG_BYTES
    assert remote == local, "合并出来的文件和本地文件不一致"

    # 真的走了分片接口，而不是偷偷退回单请求
    assert any("/upload-sessions" in u for u in seen), seen[-8:]
    assert sum(1 for u in seen if "/chunks/" in u) == 5, \
        [u for u in seen if "/chunks/" in u]

    pcts = [int(p[:-1]) for p in page.eval("window.__pcts") if p.endswith("%")]
    mid = [p for p in pcts if 0 < p < 100]
    assert mid, f"进度是跳变的，没有中间态: {pcts}"


def test_interrupted_upload_can_be_resumed_after_reload(chunky_page, e2e_user,
                                                        big_video):
    """传一半刷新页面 → 出现「未完成的上传」占位格 → 重新选中同一个文件继续 →
    合并结果字节级正确。

    这里也钉住浏览器限制下的真实能力边界：刷新之后 File 句柄拿不回来，
    **不会**自动续传，必须由用户重新选中同一个文件。
    """
    page = chunky_page
    pid = _open_detail(page, e2e_user, idx=2)
    _throttle(page, up_bps=150_000)
    page.p.set_input_files("#file-input", [big_video])

    # 等服务端确实收到了至少一片，再把页面刷掉（模拟用户关掉/切走导致上传中断）
    import time
    deadline = time.time() + 60
    got = []
    while time.time() < deadline:
        ss = _sessions(page, e2e_user, pid)
        if ss and ss[0]["received"]:
            got = ss[0]["received"]
            break
        page.wait_ms(200)
    assert got, "服务端一片都没收到，这条用例没测到东西"

    page.reload()
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval(f"showDetail({pid})")
    page.wait_for("#media-grid .media-resume", timeout=15000)
    assert "未完成" in page.text("#media-grid .media-resume")
    assert "重新选中同一文件" in page.text("#media-grid .media-resume")
    # 刷新之后没有任何东西在自动传 —— 能力边界就在这里，UI 也是这么写的
    assert page.count(UPLOADING) == 0

    # 用户重新选中同一个文件 → 接着传
    with page.p.expect_file_chooser() as fc:
        page.click("#media-grid .media-resume .resume-go")
    fc.value.set_files(big_video)

    page.wait_until(f"document.querySelectorAll('{REAL}').length === 1", timeout=120000)
    srv = _server_media(page, e2e_user, pid)
    assert len(srv) == 1
    local = hashlib.sha256(open(big_video, "rb").read()).hexdigest()
    remote, size = _download_sha256(page, e2e_user, srv[0]["filename"])
    assert size == BIG_BYTES
    assert remote == local, "续传后合并出来的文件字节不对（偏移算错了）"
    assert _sessions(page, e2e_user, pid) == []
    assert page.count("#media-grid .media-resume") == 0


def test_giving_up_an_unfinished_upload_clears_it(chunky_page, e2e_user, big_video):
    """「放弃」按钮要真的把会话和临时分片清掉。"""
    page = chunky_page
    pid = _open_detail(page, e2e_user, idx=1)
    _throttle(page, up_bps=150_000)
    page.p.set_input_files("#file-input", [big_video])

    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        if _sessions(page, e2e_user, pid):
            break
        page.wait_ms(200)
    page.reload()
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval(f"showDetail({pid})")
    page.wait_for("#media-grid .media-resume", timeout=15000)

    page.click("#media-grid .media-resume .resume-drop")
    page.click(".tm-dialog .tm-dialog-ok")
    page.wait_until("document.querySelectorAll('#media-grid .media-resume').length === 0",
                    timeout=15000)
    assert _sessions(page, e2e_user, pid) == []
    assert _server_media(page, e2e_user, pid) == []
