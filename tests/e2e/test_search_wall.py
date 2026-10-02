"""E2E：搜索结果墙的三件事 —— 轨迹标识、相关度切线、首屏优先加载。

和 test_photo_search.py 分开是因为这里要**两条都有照片的行程**：轨迹圆点和
图例只在跨行程的结果里才出现，单条行程时按设计整条收起来。

钉的仍然是接线，不是模型准不准：
  · 跨行程结果的格子上有没有带编号的轨迹圆点、图例对不对得上；
  · 相关度滑杆拖动时是不是**原地重切**（不再发一次请求）；
  · 首屏那几行是不是 eager（屏外的仍旧 lazy）。

跑法： .venv/bin/python -m pytest -m e2e -k search_wall
"""
import io
import uuid

import pytest

pytestmark = pytest.mark.e2e

ALLOWED_CONSOLE = (
    "AMap", "amap", "Failed to load resource", "net::ERR_FAILED", "favicon",
    "未登录或令牌无效",
)


@pytest.fixture
def two_trip_user(live_server):
    """两条行程，各挂一张颜色不同的真照片 —— 跨行程搜索才有东西可区分。"""
    import httpx
    from PIL import Image

    username = f"e2w{uuid.uuid4().hex[:8]}"
    password = "pw123456"
    trips = []
    with httpx.Client(base_url=live_server, timeout=30, trust_env=False) as c:
        r = c.post("/api/register", data={"username": username, "password": password,
                                          "display_name": "墙测用户"})
        assert r.status_code == 200, r.text
        for name, color, pt in (("蓝色之旅", (30, 120, 200), "西湖"),
                                ("绿色之旅", (40, 170, 90), "灵隐寺")):
            tid = c.post("/api/trips", data={"name": name, "start_date": "2026-05-01",
                                             "end_date": "2026-05-03"}).json()["id"]
            pid = c.post(f"/api/trips/{tid}/points",
                         data={"name": pt, "lat": 30.2590, "lng": 120.1300,
                               "start_date": "2026-05-01",
                               "end_date": "2026-05-01"}).json()["id"]
            buf = io.BytesIO()
            Image.new("RGB", (400, 300), color).save(buf, "JPEG")
            m = c.post(f"/api/points/{pid}/media",
                       files={"file": (f"{name}.jpg", buf.getvalue(), "image/jpeg")})
            assert m.status_code == 200, m.text
            trips.append(tid)
    return {"username": username, "password": password, "trip_ids": trips}


@pytest.fixture
def wall(page, two_trip_user):
    """登录 → 等两张图都进索引 → 打开搜索页、放宽到全部行程、搜一次。"""
    page.login(two_trip_user["username"], two_trip_user["password"])
    page.wait_until("document.querySelectorAll('#trip-select option').length > 0")
    page.wait_until("typeof searchStats !== 'undefined' && searchStats !== null",
                    timeout=15000)
    if not page.eval("searchAvailable"):
        pytest.skip("本机没装 Chinese-CLIP，搜图入口按设计就不该出现")

    # 回填是后台线程 + 第一次要加载 410MB 权重，给足时间。
    page.wait_until(
        """fetch('/api/search/status').then(r => r.json()).then(s => s.indexed >= 2)""",
        timeout=90000)

    page.click("#btn-search")
    page.wait_for("#search-overlay:not(.hidden)")
    page.click("#search-scope-all")
    # 切线先按到最低档，这里验的是接线不是模型 —— 两张纯色图的 CLIP 分
    # 落在哪一档不该决定这几条用例的死活。
    page.eval("""(() => {
        const sl = document.querySelector('#search-cut');
        sl.value = sl.min;
        sl.dispatchEvent(new Event('input', {bubbles: true}));
    })()""")
    page.fill("#search-input", "颜色")
    page.click("#search-go")
    page.wait_for("#search-grid .photo-cell", timeout=30000)
    return page


def test_cross_trip_results_carry_a_numbered_trip_dot(wall):
    """跨行程时每格左下角一枚带编号的圆点，图例给出编号→行程名的对照。"""
    page = wall
    assert page.count("#search-grid .photo-cell") >= 2

    # 每一格都有圆点（两条行程 = 两种编号两种颜色）
    assert page.count("#search-grid .search-trip-dot") == \
        page.count("#search-grid .photo-cell")
    colors = page.eval(
        """Array.from(document.querySelectorAll('#search-grid .search-trip-dot'))
             .map(e => e.style.getPropertyValue('--c'))""")
    assert len(set(colors)) == 2, colors

    # 图例：两条，编号和墙上用的是同一套
    assert page.visible("#search-legend")
    assert page.count("#search-legend .search-legend-item") == 2
    legend = page.eval(
        """Array.from(document.querySelectorAll('#search-legend .search-legend-item'))
             .map(e => e.textContent)""")
    assert any("蓝色之旅" in t for t in legend), legend
    assert any("绿色之旅" in t for t in legend), legend

    page.assert_no_page_errors(allow=ALLOWED_CONSOLE)


def test_legend_hides_when_only_one_trip_is_in_scope(wall):
    """收回到一条行程 → 没有要区分的东西，图例整条收起来。"""
    page = wall
    page.click("#search-scope-cur")
    page.click("#search-go")
    page.wait_until("searchAll.length > 0", timeout=30000)
    page.wait_until("document.querySelector('#search-legend').classList.contains('hidden')")
    assert page.count("#search-grid .search-trip-dot") == 0


def test_cut_slider_refilters_in_place_without_a_new_request(wall):
    """拖滑杆只重新切已有结果 —— 不该再发一次请求（那是 CLIP + 精判，最坏 8 秒）。"""
    page = wall
    assert page.visible("#search-filter")
    before_seq = page.eval("searchReqSeq")
    before_n = page.count("#search-grid .photo-cell")
    assert before_n >= 1

    # 推到最高档：CLIP 原始分整体只到 0.45，顶格必然一张不剩
    page.eval("""(() => {
        const sl = document.querySelector('#search-cut');
        sl.value = sl.max;
        sl.dispatchEvent(new Event('input', {bubbles: true}));
    })()""")
    assert page.eval("searchReqSeq") == before_seq        # 没有重新发请求
    assert page.count("#search-grid .photo-cell") == 0
    assert page.visible("#search-grid .empty-hint")
    assert "相关度" in page.text("#search-grid .empty-hint")
    assert "已隐藏" in page.text("#search-status")

    # 「推荐」把它拉回默认值；再推到最低档，照片全回来
    page.click("#search-cut-reset")
    page.eval("""(() => {
        const sl = document.querySelector('#search-cut');
        sl.value = sl.min;
        sl.dispatchEvent(new Event('input', {bubbles: true}));
    })()""")
    assert page.count("#search-grid .photo-cell") == before_n
    assert page.eval("searchReqSeq") == before_seq

    page.assert_no_page_errors(allow=ALLOWED_CONSOLE)


def test_cut_defaults_differ_by_whether_jev_judged(wall):
    """两档阈值必须是**两个数** —— 同一个数去切两种量纲，必然有一边是错的。"""
    page = wall
    rr = page.eval("SEARCH_CUT.reranked.def")
    clip = page.eval("SEARCH_CUT.clip.def")
    assert rr != clip
    # 这一批没精判（纯色图答不出画面描述），用的就该是 CLIP 那一档的量程
    assert page.eval("searchReranked") is False
    assert page.eval("Number(document.querySelector('#search-cut').max)") == \
        page.eval("SEARCH_CUT.clip.max")
    assert "CLIP" in page.text("#search-filter-note")


def test_first_screen_is_eager_and_the_rest_stays_lazy(wall):
    """首屏那几行直接拉，屏外的仍旧 lazy —— 否则人看到的永远是「先白一片」。"""
    page = wall
    flags = page.eval(
        """Array.from(document.querySelectorAll('#search-grid .photo-cell img'))
             .map(i => [i.loading, i.fetchPriority])""")
    assert flags, "墙上一张图都没有"
    # 结果只有两张，必然都在首屏之内
    assert all(f[0] == "eager" for f in flags), flags
    assert all(f[1] == "high" for f in flags), flags
    # 本地分发（E2E 里 MEDIA_BASE_URL 为空）没有 OSS 图片处理，srcset 该是空的；
    # 有 OSS 的那条路由单元测试钉（tests/unit/test_media_url.py）。
    assert page.eval("""(searchResults[0].srcset || []).length""") == 0
