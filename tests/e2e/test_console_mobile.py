"""开发者控制台的手机端形态：底部常驻 Tab + 卡片式列表（表格在窄屏上不可读）。

跑法： .venv/bin/python -m pytest -m e2e tests/e2e/test_console_mobile.py
"""
import pytest

pytestmark = pytest.mark.e2e

ALLOWED_CONSOLE = ("Failed to load resource", "net::ERR_FAILED", "favicon")


@pytest.fixture
def mobile_page(browser, live_server, e2e_user):
    """iPhone 尺寸 + 已登录的控制台页。

    不能用 page.login()：手机 UA 打开 / 拿到的是落地页，没有 #login-overlay。
    控制台本来就是「知道地址的人直接进」，所以这里直接打登录接口拿 cookie。
    """
    pg = browser.new_page(live_server, mobile=True)
    pg.goto("/")
    status = pg.eval("""async ([u, p]) => {
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({username: u, password: p}),
      });
      return r.status;
    }""", [e2e_user["username"], e2e_user["password"]])
    assert status == 200, f"登录失败 HTTP {status}"
    pg.goto("/console")
    pg.wait_until("document.getElementById('who').textContent !== '…'")
    yield pg
    pg.close()


def _make_key(page, name, scope_ids):
    page.fill("#key-name", name)
    for sid in scope_ids:
        page.eval(f"document.getElementById('{sid}').checked || "
                  f"document.getElementById('{sid}').click()")
    page.click("#btn-create")
    page.wait_for("#key-modal.show")
    page.eval("closeModal()")
    page.wait_until("document.querySelectorAll('#keys-cards .item').length > 0")


def test_bottom_tabbar_is_visible_and_tables_are_not(mobile_page):
    """手机上：底部三个 Tab 常驻，顶部 Tab 和表格都让位给卡片。"""
    assert mobile_page.visible(".tabbar")
    assert mobile_page.eval(
        "document.querySelectorAll('.tabbar button').length") == 3
    labels = mobile_page.eval(
        "Array.from(document.querySelectorAll('.tabbar button'))"
        ".map(b => b.textContent.replace(/[^\\u4e00-\\u9fa5A-Za-z ]/g, '').trim())")
    assert labels == ["API Keys", "统计", "日志"]
    assert not mobile_page.visible("nav.tabs")
    assert mobile_page.eval(
        "getComputedStyle(document.querySelector('#page-keys table')).display") == "none"
    assert mobile_page.eval(
        "getComputedStyle(document.getElementById('keys-cards')).display") == "block"


def test_bottom_tabs_switch_pages(mobile_page):
    for name in ("usage", "audit", "keys"):
        mobile_page.click(f".tabbar button[data-page={name}]")
        mobile_page.wait_until(
            f"document.getElementById('page-{name}').classList.contains('active')")
        assert mobile_page.eval(
            f"document.querySelector('.tabbar button[data-page={name}]')"
            ".classList.contains('active')")


def test_keys_render_as_cards_not_table(mobile_page):
    _make_key(mobile_page, "手机端 Key", ["sc-read"])
    assert mobile_page.count("#keys-cards .item") == 1
    card = mobile_page.text("#keys-cards .item")
    assert "手机端 Key" in card and "有效" in card and "创建" in card


def test_write_checkbox_forces_and_locks_read(mobile_page):
    """write 勾上 → read 自动勾中且不可取消（否则建出个自己读不了的 Key）。"""
    mobile_page.eval("document.getElementById('sc-write').click()")
    assert mobile_page.eval("document.getElementById('sc-read').checked") is True
    assert mobile_page.eval("document.getElementById('sc-read').disabled") is True
    mobile_page.eval("document.getElementById('sc-write').click()")
    assert mobile_page.eval("document.getElementById('sc-read').disabled") is False


def test_prompt_text_follows_key_scope(mobile_page):
    """只读 Key 的提示词里不能出现「创建/修改/删除」那套能力描述。"""
    read_only = mobile_page.eval("PROMPT_TEMPLATE('tm_x', 'read', 'k')")
    assert "创建 / 修改 / 删除" not in read_only
    assert "没有 write 权限" in read_only

    writable = mobile_page.eval("PROMPT_TEMPLATE('tm_x', 'read,write', 'k')")
    assert "创建 / 修改 / 删除" in writable
    assert "没有 write 权限" not in writable
    assert "没有 share 权限" in writable        # share 没给，也要说清楚


def test_usage_cards_have_no_token_column(mobile_page):
    _make_key(mobile_page, "统计 Key", ["sc-read"])
    mobile_page.click(".tabbar button[data-page=usage]")
    mobile_page.wait_until("document.querySelectorAll('#usage-cards .item').length > 0")
    card = mobile_page.text("#usage-cards .item")
    assert "调用" in card and "成功" in card
    assert "Token" not in card and "token" not in card
    head = mobile_page.text("#page-usage thead")
    assert "Token" not in head


def test_audit_filters_by_key_and_renders_cards(mobile_page, live_server, e2e_user):
    _make_key(mobile_page, "日志 Key", ["sc-read"])
    mobile_page.click(".tabbar button[data-page=audit]")
    mobile_page.wait_until("document.querySelectorAll('#audit-key option').length >= 2")
    # 默认「全部」，至少能看到刚才建 Key 这条控制台操作
    mobile_page.wait_until("document.querySelectorAll('#audit-cards .item').length > 0")
    assert "key.create" in mobile_page.text("#audit-cards")

    # 切到具体 Key：这个 Key 还没发过调用，应当是空态而不是别人的记录
    mobile_page.eval("const s=document.getElementById('audit-key');"
                     "s.selectedIndex=1; s.dispatchEvent(new Event('change'))")
    mobile_page.wait_until(
        "document.querySelector('#audit-cards').textContent.includes('还没有调用记录')")


def test_console_has_no_page_errors_on_mobile(mobile_page):
    mobile_page.click(".tabbar button[data-page=usage]")
    mobile_page.click(".tabbar button[data-page=audit]")
    mobile_page.wait_ms(300)
    mobile_page.assert_no_page_errors(allow=ALLOWED_CONSOLE)
