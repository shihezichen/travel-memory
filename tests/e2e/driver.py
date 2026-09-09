"""可复用的浏览器驱动 —— 把以前一次性写在 /tmp 里的 CDP 脚本沉淀成一个模块。

设计要点
--------
1. **离线优先**：默认拦截所有外部域名的请求。高德 JS API 的那个 <script> 会被
   换成 tests/e2e/amap_stub.js（见该文件顶部注释），其余外部请求一律 abort。
   所以整套 E2E 不需要外网，也不会真的去打高德/OSRM。
2. **真实输入事件**：tap() 用 CDP 的 Input.dispatchTouchEvent 派发真触摸，
   click() 走 Playwright 的真实鼠标，而不是 el.click() 这种 DOM 快捷方式 ——
   移动端手势相关的 bug 只有真事件才复现得出来。
3. **默认收集控制台报错**：每个 Page 都挂 console/pageerror 监听，
   assert_no_page_errors() 一把校验，避免「功能看着对但控制台一片红」。
4. **截图 + 像素差分**：screenshot() / pixel_diff_ratio() 保留了原来那套
   截图比对手法，但变成公共工具而不是每次重写。

Playwright 用系统已装的 /usr/bin/google-chrome（channel="chrome"），
不需要 `playwright install` 下载浏览器。
"""
from __future__ import annotations

import re
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
AMAP_STUB = E2E_DIR / "amap_stub.js"

# 需要拦截替换成桩的高德 JS API
_AMAP_SCRIPT_RE = re.compile(r"https?://webapi\.amap\.com/maps")


class Browser:
    """薄封装：负责起浏览器、开页面、装离线路由。用 with 语句管理生命周期。"""

    def __init__(self, headless: bool = True, viewport=(1280, 900),
                 mobile: bool = False):
        self.headless = headless
        self.viewport = viewport
        self.mobile = mobile
        self._pw = None
        self._browser = None
        self._ctx = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel="chrome", headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self._contexts = []
        return self

    def __exit__(self, *exc):
        for ctx in list(getattr(self, "_contexts", [])):
            try:
                ctx.close()
            except Exception:
                pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def new_page(self, base_url: str, mobile: bool = None) -> "Page":
        """每个 Page 独占一个 BrowserContext —— cookie / localStorage 互不串味，
        用例之间不会因为「上一条留下的登录态」而互相影响。"""
        mobile = self.mobile if mobile is None else mobile
        kw = {"viewport": {"width": self.viewport[0], "height": self.viewport[1]}}
        if mobile:
            kw.update({"viewport": {"width": 390, "height": 844},
                       "device_scale_factor": 3, "is_mobile": True,
                       "has_touch": True,
                       "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
                                      "Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                                      "Gecko) Version/17.0 Mobile/15E148 Safari/604.1")})
        ctx = self._browser.new_context(**kw)
        self._contexts.append(ctx)
        return Page(ctx.new_page(), base_url, _owner_context=ctx)


class Page:
    """页面级原语：goto / eval / click / tap / wait / screenshot / 错误收集。"""

    def __init__(self, pw_page, base_url: str, _owner_context=None):
        self.p = pw_page
        self.ctx = _owner_context
        self.base_url = base_url.rstrip("/")
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.blocked_requests: list[str] = []

        self.p.on("console", self._on_console)
        self.p.on("pageerror", lambda e: self.page_errors.append(str(e)))
        self._install_offline_routes()

    # ---------- 内部 ----------
    def _on_console(self, msg):
        if msg.type == "error":
            self.console_errors.append(msg.text)

    def _install_offline_routes(self):
        stub = AMAP_STUB.read_text(encoding="utf-8")

        def handler(route, request):
            url = request.url
            if url.startswith(self.base_url):
                return route.continue_()
            if _AMAP_SCRIPT_RE.match(url):
                self.blocked_requests.append(url)
                return route.fulfill(status=200,
                                     content_type="application/javascript; charset=utf-8",
                                     body=stub)
            # 其余外部资源一律断掉：E2E 必须能在无网环境跑
            self.blocked_requests.append(url)
            return route.abort()

        self.p.route("**/*", handler)

    # ---------- 导航 ----------
    def goto(self, path="/", wait="domcontentloaded"):
        self.p.goto(self.base_url + path, wait_until=wait)
        return self

    def reload(self, wait="domcontentloaded"):
        self.p.reload(wait_until=wait)
        return self

    # ---------- 执行 / 断言 ----------
    def eval(self, expr, arg=None):
        """在页面里求值。expr 可以是表达式或 (arg)=>{...} 形式的函数。"""
        return self.p.evaluate(expr, arg)

    def text(self, selector: str) -> str:
        return self.p.inner_text(selector)

    def visible(self, selector: str) -> bool:
        return self.p.is_visible(selector)

    def count(self, selector: str) -> int:
        return self.p.locator(selector).count()

    def wait_for(self, selector: str, timeout=8000, state="visible"):
        self.p.wait_for_selector(selector, timeout=timeout, state=state)
        return self

    def wait_until(self, js_expr: str, timeout=8000):
        """等一个 JS 表达式变真，比 sleep 可靠。"""
        self.p.wait_for_function(js_expr, timeout=timeout)
        return self

    def wait_ms(self, ms: int):
        self.p.wait_for_timeout(ms)
        return self

    # ---------- 输入 ----------
    def click(self, selector: str, timeout=8000):
        """真实鼠标点击（会检查可见性/可点击性）。"""
        self.p.click(selector, timeout=timeout)
        return self

    def fill(self, selector: str, value: str):
        self.p.fill(selector, value)
        return self

    def tap(self, selector: str):
        """真实触摸：CDP Input.dispatchTouchEvent，复现移动端手势问题用。"""
        box = self.p.locator(selector).first.bounding_box()
        assert box, f"元素不可见，无法 tap: {selector}"
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.tap_xy(x, y)
        return self

    def tap_xy(self, x: float, y: float):
        cdp = self.p.context.new_cdp_session(self.p)
        pt = [{"x": x, "y": y, "radiusX": 8, "radiusY": 8, "force": 1}]
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
        return self

    def swipe(self, x1, y1, x2, y2, steps=10):
        """真实滑动手势（灯箱左右切换、抽屉上拉这类交互用）。"""
        cdp = self.p.context.new_cdp_session(self.p)

        def tp(x, y):
            return [{"x": x, "y": y, "radiusX": 8, "radiusY": 8, "force": 1}]

        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": tp(x1, y1)})
        for i in range(1, steps + 1):
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": tp(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
        return self

    # ---------- 登录快捷方式 ----------
    def login(self, username: str, password: str):
        """走真实登录表单（而不是直接塞 cookie）—— 登录本身也是被测行为。"""
        self.goto("/")
        self.wait_for("#login-overlay:not(.hidden)")
        self.fill("#login-username", username)
        self.fill("#login-password", password)
        self.p.click("#login-form button[type=submit]")
        self.wait_until("document.querySelector('#login-overlay')"
                        ".classList.contains('hidden')")
        return self

    def close(self):
        for obj in (self.p, self.ctx):
            try:
                obj.close()
            except Exception:
                pass

    # ---------- 截图 / 像素差分 ----------
    def screenshot(self, path=None, full_page=False, selector=None) -> bytes:
        target = self.p.locator(selector).first if selector else self.p
        kw = {"path": str(path)} if path else {}
        if not selector:
            kw["full_page"] = full_page
        return target.screenshot(**kw)

    # ---------- 健康检查 ----------
    def assert_no_page_errors(self, allow=()):
        """页面不应有未捕获异常 / console.error。allow 里的子串会被忽略。"""
        def keep(msgs):
            return [m for m in msgs if not any(a in m for a in allow)]

        errs = keep(self.page_errors)
        cons = keep(self.console_errors)
        assert not errs, f"页面抛出未捕获异常: {errs}"
        assert not cons, f"控制台报错: {cons}"


def pixel_diff_ratio(a: bytes, b: bytes) -> float:
    """两张 PNG 的像素差异比例（0=完全一致，1=完全不同）。

    以前每个 verify_*.py 都手写一遍这段，现在统一到这里。
    """
    import io

    from PIL import Image, ImageChops
    ia = Image.open(io.BytesIO(a)).convert("RGB")
    ib = Image.open(io.BytesIO(b)).convert("RGB")
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    diff = ImageChops.difference(ia, ib)
    total = ia.size[0] * ia.size[1]
    changed = sum(1 for px in diff.getdata() if px != (0, 0, 0))
    return changed / total if total else 0.0
