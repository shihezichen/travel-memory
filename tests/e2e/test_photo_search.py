"""E2E：顶栏放大镜 → 搜索页 → 照片墙。

钉的是**接线**：入口出不出现、默认勾的是不是当前行程、改范围有没有传到接口、
结果是不是铺成一面墙、点一张能不能进灯箱。模型搜得准不准不在这里验。

跑法： .venv/bin/python -m pytest -m e2e -k photo_search
"""
import pytest

pytestmark = pytest.mark.e2e

ALLOWED_CONSOLE = (
    "AMap", "amap", "Failed to load resource", "net::ERR_FAILED", "favicon",
    "未登录或令牌无效",
)


@pytest.fixture
def search_page(page, e2e_user):
    """登录 + 再建一条行程（范围多选得有的选），并等搜图入口表态。"""
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#trip-select option').length > 0")
    page.eval("""fetch('/api/trips', {method: 'POST',
        body: new URLSearchParams({name: '第二条行程'})}).then(r => r.json())""")
    page.eval("loadTrips()")
    page.wait_until("allTrips.length === 2", timeout=15000)

    # searchStats 一有值就说明 /api/search/status 回来了（开和关都会有值）
    page.wait_until("typeof searchStats !== 'undefined' && searchStats !== null",
                    timeout=15000)
    if not page.eval("searchAvailable"):
        pytest.skip("本机没装 Chinese-CLIP，搜图入口按设计就不该出现")
    return page


def test_entry_is_hidden_until_the_model_says_yes(page, e2e_user):
    """没登录时顶栏那枚放大镜不该在 —— 入口的开关由接口说了算，不是写死的。"""
    page.goto("/app")
    page.wait_for("#login-overlay:not(.hidden)")
    assert not page.visible("#btn-search")


def test_opening_search_preselects_only_the_current_trip(search_page):
    page = search_page
    page.click("#btn-search")
    page.wait_for("#search-overlay:not(.hidden)")

    assert page.count(".search-chip") == 2
    on = page.eval(
        "Array.from(document.querySelectorAll('.search-chip.on')).map(e => e.textContent)")
    cur = page.eval(
        "allTrips.find(t => Number(t.id) === Number(currentTripId)).name")
    assert len(on) == 1 and cur in on[0], (on, cur)


def test_scope_buttons_widen_and_narrow_again(search_page):
    page = search_page
    page.click("#btn-search")
    page.wait_for("#search-overlay:not(.hidden)")

    page.click("#search-scope-all")
    assert page.count(".search-chip.on") == 2
    page.click("#search-scope-cur")
    assert page.count(".search-chip.on") == 1

    # 最后一条不许取消 —— 取消了界面上看着「什么都没选」，后端却会当成「搜全部」
    page.click(".search-chip.on")
    assert page.count(".search-chip.on") == 1


def test_results_render_as_a_photo_wall_and_open_in_the_lightbox(search_page):
    page = search_page
    # 等那张 e2e.jpg 进索引。回填是后台线程 + 第一次要加载 410MB 权重，给足时间。
    page.wait_until(
        """fetch('/api/search/status').then(r => r.json()).then(s => s.indexed >= 1)""",
        timeout=60000)

    page.click("#btn-search")
    page.wait_for("#search-overlay:not(.hidden)")
    page.click("#search-scope-all")             # 照片挂在第一条行程下，放宽最稳
    page.fill("#search-input", "蓝色")
    page.click("#search-go")

    page.wait_for("#search-grid .photo-cell", timeout=20000)
    assert page.count("#search-grid .photo-cell") >= 1
    assert "张" in page.text("#search-status")

    # 墙上点一张 → 灯箱盖在搜索页上面，搜索页本身还在（关掉大图就回到结果）
    page.click("#search-grid .photo-cell")
    page.wait_for("#lightbox:not(.hidden)")
    assert page.eval("!document.querySelector('#search-overlay').classList.contains('hidden')")

    page.click("#lightbox-close")
    page.wait_until("document.querySelector('#lightbox').classList.contains('hidden')")
    page.click("#search-back")
    page.wait_until("document.querySelector('#search-overlay').classList.contains('hidden')")

    page.assert_no_page_errors(allow=ALLOWED_CONSOLE)
