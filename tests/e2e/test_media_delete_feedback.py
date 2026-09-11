"""删除媒体必须**立刻**从界面上消失。

对应一条真实反馈：「删除图片或视频时，特别是删除视频，要等一会儿图片才会从界面
消失，估计走 0.5 秒（当时有一个视频在上传）。从使用体验，这个等待有点太长。」

原来的做法是 `await DELETE` → `loadDetail()`，也就是**两个**隧道往返之后格子才
消失；同时有视频在传的时候隧道是满的（实测 4Mbps / 3.8% 重传），半秒以上很正常，
而这期间界面没有任何反馈。现在改成乐观删除：先摘掉，请求在后台发，失败再放回去。

所以这里不测「删完之后格子没了」（老代码也能过），而是**把 DELETE 响应按住**，
在请求还没回来的时候就断言格子已经不见了。这一条只有乐观删除能过。
"""
import pytest
from PIL import Image

pytestmark = pytest.mark.e2e

REAL = ("#media-grid .media-item:not(.media-add):not(.media-uploading)"
        ":not(.media-resume)")


def _jpg(tmp_path, name, color):
    p = tmp_path / name
    Image.new("RGB", (320, 240), color).save(str(p), "JPEG")
    return str(p)


def _open_detail(page, e2e_user):
    page.login(e2e_user["username"], e2e_user["password"])
    page.wait_until("document.querySelectorAll('#point-list li').length === 3",
                    timeout=15000)
    page.eval(f"showDetail({e2e_user['point_ids'][0]})")
    page.wait_for("#media-grid .media-add")
    page.wait_until("Array.isArray(detailMedia) && detailMedia.length >= 1",
                    timeout=15000)


def _count(page):
    return page.eval(f"document.querySelectorAll('{REAL}').length")


def _click_delete(page, idx=0):
    page.eval(f"document.querySelectorAll('{REAL}')[{idx}].querySelector('.del').click()")
    page.wait_ms(250)
    # 确认弹窗：只点当前可见的那颗「删除」
    page.eval("Array.from(document.querySelectorAll('button'))"
              ".filter(b => b.offsetParent && b.textContent.trim() === '删除').pop().click()")


def test_删除时格子立刻消失不等服务端(page, e2e_user, tmp_path):
    _open_detail(page, e2e_user)
    before = _count(page)
    assert before >= 1

    # 把 DELETE 按住 2 秒。用 Playwright 的路由拦截，不动服务端。
    held = {"n": 0}

    def _hold(route):
        held["n"] += 1
        import time
        time.sleep(2.0)
        route.continue_()

    page.p.route("**/api/media/*", lambda route: (
        _hold(route) if route.request.method == "DELETE" else route.continue_()))

    _click_delete(page)
    page.wait_ms(400)                    # 远小于 2 秒：请求这会儿还在路上
    assert held["n"] == 1, "DELETE 没发出去，这条用例就不是在测乐观删除了"
    assert _count(page) == before - 1, "请求还没回来，格子就该已经不见了"
    # detailMedia 也要同步摘掉，否则灯箱翻页会翻到一条已删的
    assert page.eval("detailMedia.length") == before - 1

    # 请求回来之后仍然是删掉的状态（别又被 loadDetail 补回来）
    page.wait_ms(2200)
    assert _count(page) == before - 1
    page.p.unroute("**/api/media/*")


def test_删除失败会把格子放回去并提示(page, e2e_user, tmp_path):
    _open_detail(page, e2e_user)
    before = _count(page)

    page.p.route("**/api/media/*", lambda route: (
        route.fulfill(status=500, body="boom")
        if route.request.method == "DELETE" else route.continue_()))
    _click_delete(page)
    page.wait_until(f"document.querySelectorAll('{REAL}').length === {before}",
                    timeout=8000)
    assert page.eval("detailMedia.length") == before
    assert page.eval("!!document.querySelector('.tm-toast, .toast')"), "失败了得有提示"
    page.p.unroute("**/api/media/*")
