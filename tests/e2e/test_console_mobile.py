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


def _make_key(page, name, scope_ids, expire_days=""):
    """手机端建 Key 要先点开弹窗 —— 首屏上只有一个按钮。"""
    before = page.count("#keys-cards .item")
    page.click("#btn-open-create")
    page.wait_for("#create-modal.show")
    page.fill("#key-name", name)
    page.fill("#key-expire", str(expire_days))
    for sid in scope_ids:
        page.eval(f"document.getElementById('{sid}').checked || "
                  f"document.getElementById('{sid}').click()")
    page.click("#btn-create")
    page.wait_for("#key-modal.show")
    page.eval("closeModal()")
    page.wait_until(f"document.querySelectorAll('#keys-cards .item').length > {before}")


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


def test_key_card_and_table_show_expiry(mobile_page):
    """有效期得能看见 —— 原来列表里根本没有这一栏，Key 什么时候失效全靠猜。"""
    _make_key(mobile_page, "永久 Key", ["sc-read"])
    _make_key(mobile_page, "限期 Key", ["sc-read"], expire_days=30)

    cards = mobile_page.text("#keys-cards")
    assert "有效期 永久" in cards
    assert "剩 30 天" in cards

    # 宽屏那份表格也得有这一列（同一份数据渲染两遍，别只改一边）
    head = mobile_page.text("#page-keys thead")
    assert "有效期" in head
    assert "剩 30 天" in mobile_page.eval("document.getElementById('keys-body').innerText")


def test_expiry_within_a_week_is_highlighted(mobile_page):
    """一周内到期标黄 —— 悄悄失效的 Key 最难查。"""
    _make_key(mobile_page, "快到期 Key", ["sc-read"], expire_days=3)
    assert "剩 3 天" in mobile_page.text("#keys-cards")
    assert mobile_page.eval(
        "!!document.querySelector('#keys-cards [style*=\"amber\"]')")


def test_expired_and_today_keys_read_correctly(mobile_page):
    """纯前端文案：已过期 / 即将到期 / 还有几天，直接喂数据给渲染函数验。

    ⚠ 偏移量一律用**分钟/小时**给，不用「天」的小数：`0.2 天` 落不落在
    今天，取决于现在是几点 —— 晚上跑的时候它会跨到第二天，用例就随钟点红了。
    这是真实踩过的坑（2026-09-18 全量 e2e 只有它红，白天却是绿的）。
    """
    out = mobile_page.eval("""() => {
      const pad = n => String(n).padStart(2, '0');
      // 按**分钟**给偏移，和钟点无关
      const stamp = mins => {
        const x = new Date(Date.now() + mins * 60000);
        return `${x.getFullYear()}-${pad(x.getMonth()+1)}-${pad(x.getDate())} `
             + `${pad(x.getHours())}:${pad(x.getMinutes())}:00`;
      };
      return {
        forever: expiryText({expires_at: null}),
        past: expiryText({expires_at: stamp(-2 * 24 * 60)}),
        // 5 小时后（不到 24 小时，无论现在几点都不该说「剩 1 天」）
        today: expiryText({expires_at: stamp(5 * 60)}),
        // 30 分钟后：要细到分钟，越近越该看得见
        soon: expiryText({expires_at: stamp(30)}),
        // 3 天后：仍然是「剩 N 天」
        days: expiryText({expires_at: stamp(3 * 24 * 60)}),
        expiredStatus: keyStatus({expires_at: stamp(-2 * 24 * 60), revoked: 0}),
        liveStatus: keyStatus({expires_at: stamp(30 * 24 * 60), revoked: 0}),
      };
    }""")
    assert "永久" in out["forever"]
    assert "已过期" in out["past"]
    # 不到 24 小时：说「今天到期」，不能说「剩 1 天」
    assert "今天到期" in out["today"], out["today"]
    assert "剩 1 天" not in out["today"], out["today"]
    # 不到 1 小时：细到分钟
    assert "分钟" in out["soon"], out["soon"]
    # 3 天后仍是「剩 N 天」
    assert "剩 3 天" in out["days"], out["days"]
    assert "已过期" in out["expiredStatus"]
    assert "有效" in out["liveStatus"]


def test_first_screen_has_no_create_form_or_prompt_block(mobile_page):
    """手机首屏要清爽：创建表单和「Agent 接入」那一大块都不该占地方。"""
    assert mobile_page.visible("#btn-open-create")
    assert not mobile_page.visible("#create-host")
    assert not mobile_page.visible("#sec-prompt")
    assert not mobile_page.visible("#create-modal")


def test_create_form_moves_into_modal_and_back(mobile_page):
    """表单 DOM 只有一份，开弹窗搬进去、取消搬回来 —— 别复制出两套同名 id。"""
    mobile_page.click("#btn-open-create")
    mobile_page.wait_for("#create-modal.show")
    for fid in ("key-name", "sc-read", "sc-write", "key-expire", "btn-create"):
        assert mobile_page.eval(
            f"document.getElementById('create-modal').contains("
            f"document.getElementById('{fid}'))"), fid
    assert mobile_page.eval(
        "document.querySelectorAll('#key-name').length") == 1

    mobile_page.eval("closeCreateModal()")
    mobile_page.wait_until("!document.getElementById('create-modal').classList.contains('show')")
    assert mobile_page.eval(
        "document.getElementById('create-host').contains(document.getElementById('key-name'))")


def test_create_from_modal_then_shows_key_modal(mobile_page):
    """生成之后：创建弹窗收起，换成「已生成」弹窗（含完整 Key + 提示词 + 复制）。"""
    _make_key(mobile_page, "弹窗建的 Key", ["sc-read"])
    assert not mobile_page.visible("#create-modal")
    assert mobile_page.count("#keys-cards .item") == 1
    # 表单已经搬回去，且名称/有效期都清空了，下次开弹窗不会带着上次的残留
    assert mobile_page.eval(
        "document.getElementById('create-host').contains(document.getElementById('key-name'))")
    assert mobile_page.eval("document.getElementById('key-name').value") == ""
    assert mobile_page.eval("document.getElementById('key-expire').value") == ""


def _swipe_card_left(page, idx=0, dx=-110):
    """在第 idx 张 Key 卡片上从右往左划一把（真 touch 事件，不是改 class）。"""
    box = page.eval(
        f"(() => {{ const r = document.querySelectorAll('#keys-cards .item')[{idx}]"
        f"  .getBoundingClientRect(); return {{x: r.x, y: r.y, w: r.width, h: r.height}}; }})()")
    y = box["y"] + box["h"] / 2
    x1 = box["x"] + box["w"] - 20
    page.swipe(x1, y, x1 + dx, y, steps=12)
    page.wait_ms(350)            # 等吸附动画走完


def test_swipe_left_reveals_delete(mobile_page):
    """iOS 那套：手指往左划，卡片跟着走，右边露出删除。"""
    _make_key(mobile_page, "左滑删除 Key", ["sc-read"])
    assert not mobile_page.eval(
        "document.querySelector('#keys-cards .item').classList.contains('open')")

    _swipe_card_left(mobile_page)
    assert mobile_page.eval(
        "document.querySelector('#keys-cards .item').classList.contains('open')")
    # 删除按钮真的露出来了（不是只加了个 class）
    assert mobile_page.eval("""(() => {
      const el = document.querySelector('#keys-cards .item');
      const btn = el.querySelector('.swipe-del button').getBoundingClientRect();
      const card = el.getBoundingClientRect();
      return btn.width > 40 && btn.right <= card.right + 1 && btn.left >= card.left;
    })()""")


def test_small_swipe_snaps_back(mobile_page):
    """没划过一半就回弹 —— 否则列表会被误触得东倒西歪。"""
    _make_key(mobile_page, "回弹 Key", ["sc-read"])
    _swipe_card_left(mobile_page, dx=-20)
    assert not mobile_page.eval(
        "document.querySelector('#keys-cards .item').classList.contains('open')")


def test_only_one_card_opens_at_a_time(mobile_page):
    _make_key(mobile_page, "A Key", ["sc-read"])
    _make_key(mobile_page, "B Key", ["sc-read"])
    _swipe_card_left(mobile_page, idx=0)
    _swipe_card_left(mobile_page, idx=1)
    assert mobile_page.eval(
        "document.querySelectorAll('#keys-cards .item.open').length") == 1


def test_swipe_delete_button_removes_the_key(mobile_page):
    _make_key(mobile_page, "待删 Key", ["sc-read"])
    _make_key(mobile_page, "留着 Key", ["sc-read"])
    names = mobile_page.eval(
        "Array.from(document.querySelectorAll('#keys-cards .t')).map(e => e.textContent)")
    idx = names.index("待删 Key")

    mobile_page.p.once("dialog", lambda d: d.accept())      # deleteKey 里有 confirm
    _swipe_card_left(mobile_page, idx=idx)
    mobile_page.click(f"#keys-cards .item:nth-of-type({idx + 1}) .swipe-del button")
    mobile_page.wait_until(
        "document.querySelectorAll('#keys-cards .item').length === 1")
    left = mobile_page.text("#keys-cards")
    assert "待删 Key" not in left and "留着 Key" in left


def test_card_has_no_standing_delete_button(mobile_page):
    """删除只走左滑，卡片上不再摆一个常驻红按钮。"""
    _make_key(mobile_page, "无按钮 Key", ["sc-read"])
    assert mobile_page.eval(
        "document.querySelectorAll('#keys-cards .acts .btn.danger').length") == 0
    assert mobile_page.eval(
        "document.querySelectorAll('#keys-cards .acts .btn').length") == 1


def test_key_value_not_shown_in_prompt_modal_or_audit(mobile_page):
    """key 值在窄屏上截得只剩前几位，认不出是哪把 —— 这两处不显示它。"""
    _make_key(mobile_page, "隐藏 Key 值", ["sc-read"])
    prefix = mobile_page.eval(
        "keysCache[0].key_prefix")

    mobile_page.click("#keys-cards .item .acts .btn")
    mobile_page.wait_for("#prompt-modal.show")
    sub = mobile_page.text("#prompt-modal-sub")
    assert "隐藏 Key 值" in sub and "read" in sub
    assert prefix not in sub
    mobile_page.eval("closePromptModal()")

    mobile_page.click(".tabbar button[data-page=audit]")
    mobile_page.wait_until("document.querySelectorAll('#audit-cards .item').length > 0")
    assert prefix not in mobile_page.text("#audit-cards")
    assert prefix not in mobile_page.text("#audit-key")


def test_duplicate_name_keeps_the_dialog_open(mobile_page):
    """重名要挡下来，而且弹窗别关 —— 关了的话用户得从头再点一遍。"""
    _make_key(mobile_page, "重名 Key", ["sc-read"])

    mobile_page.click("#btn-open-create")
    mobile_page.wait_for("#create-modal.show")
    mobile_page.fill("#key-name", "重名 Key")
    mobile_page.click("#btn-create")
    mobile_page.wait_for("#toast.show")

    assert "换个名字" in mobile_page.text("#toast")
    assert mobile_page.visible("#create-modal")            # 弹窗还开着
    assert not mobile_page.visible("#key-modal")           # 没有真的建出来
    assert mobile_page.count("#keys-cards .item") == 1
    # 光标回到名称框，直接改就行
    assert mobile_page.eval("document.activeElement.id") == "key-name"

    mobile_page.fill("#key-name", "换个名字的 Key")
    mobile_page.click("#btn-create")
    mobile_page.wait_for("#key-modal.show")
    mobile_page.eval("closeModal()")
    mobile_page.wait_until("document.querySelectorAll('#keys-cards .item').length === 2")


def _open_prompt(page, name):
    """点开某个 Key 的「Agent 接入」。"""
    cards = page.eval(
        "Array.from(document.querySelectorAll('#keys-cards .item'))"
        ".map(c => c.querySelector('.t').textContent)")
    idx = cards.index(name)
    page.click(f"#keys-cards .item:nth-of-type({idx + 1}) .acts .btn")
    page.wait_for("#prompt-modal.show")


def test_prompt_is_a_template_and_says_so(mobile_page):
    """Key 只存 sha256，事后给不出明文 —— 那就别装作给得出。
    模板里留 <api key> 占位，并且把「这是模板、要替换」说在最前面。"""
    _make_key(mobile_page, "模板 Key", ["sc-read"])
    _open_prompt(mobile_page, "模板 Key")

    body = mobile_page.text("#prompt-modal-box")
    assert body.count("<api key>") == 3          # API Key 行 + Bearer + ?api_key=
    assert not mobile_page.eval(                 # 里面绝不能出现真 Key
        "/tm_[A-Za-z0-9_-]{20,}/.test("
        "document.getElementById('prompt-modal-box').textContent)")

    warn = mobile_page.text("#prompt-modal-warn")
    assert "模板" in warn and "<api key>" in warn and "生成那一次" in warn
    assert mobile_page.visible("#prompt-modal-warn")

    # 按钮也别叫「复制提示词」，免得让人以为拷了就能用
    assert "复制模板" in mobile_page.text("#prompt-modal .actions")


def test_freshly_created_key_prompt_is_ready_to_use(mobile_page):
    """刚生成那一刻 raw key 还在手上，这一份必须是填好的、能直接用的。"""
    mobile_page.click("#btn-open-create")
    mobile_page.wait_for("#create-modal.show")
    mobile_page.fill("#key-name", "刚建的 Key")
    mobile_page.click("#btn-create")
    mobile_page.wait_for("#key-modal.show")

    body = mobile_page.text("#key-modal-prompt")
    assert "<api key>" not in body
    assert mobile_page.eval(
        "/tm_[A-Za-z0-9_-]{40,}/.test("
        "document.getElementById('key-modal-prompt').textContent)")


def test_agent_prompt_opens_from_each_key_card(mobile_page):
    """提示词改成每个 Key 卡片上的按钮 —— 弹出的那份必须是这个 Key 的。"""
    _make_key(mobile_page, "只读 Key", ["sc-read"])
    _make_key(mobile_page, "读写 Key", ["sc-write"])

    _open_prompt(mobile_page, "只读 Key")
    assert "只读 Key" in mobile_page.text("#prompt-modal-sub")
    body = mobile_page.text("#prompt-modal-box")
    assert "没有 write 权限" in body and "创建 / 修改 / 删除" not in body

    mobile_page.eval("closePromptModal()")
    _open_prompt(mobile_page, "读写 Key")
    assert "读写 Key" in mobile_page.text("#prompt-modal-sub")
    assert "创建 / 修改 / 删除" in mobile_page.text("#prompt-modal-box")


def test_write_checkbox_forces_and_locks_read(mobile_page):
    """write 勾上 → read 自动勾中且不可取消（否则建出个自己读不了的 Key）。"""
    mobile_page.click("#btn-open-create")
    mobile_page.wait_for("#create-modal.show")
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


def test_audit_shows_source_ip(mobile_page):
    """日志里要看得出请求来自哪儿；没采过的老记录显示「—」而不是留白。"""
    _make_key(mobile_page, "IP Key", ["sc-read"])
    mobile_page.click(".tabbar button[data-page=audit]")
    mobile_page.wait_until("document.querySelectorAll('#audit-cards .item').length > 0")
    assert "来自" in mobile_page.text("#audit-cards")
    assert "来源 IP" in mobile_page.text("#page-audit thead")
    out = mobile_page.eval("""() => ({
      empty: auditIp({}),
      local: auditIp({client_ip: '127.0.0.1'}),
      real: auditIp({client_ip: '203.0.113.7'}),
    })""")
    assert out["empty"] == '<span class="muted">—</span>'
    assert "本机" in out["local"]
    assert out["real"] == "203.0.113.7"


def test_console_has_no_page_errors_on_mobile(mobile_page):
    mobile_page.click(".tabbar button[data-page=usage]")
    mobile_page.click(".tabbar button[data-page=audit]")
    mobile_page.wait_ms(300)
    mobile_page.assert_no_page_errors(allow=ALLOWED_CONSOLE)


# ---------------- 宽屏：这些改动一点都不该影响桌面 ----------------
@pytest.fixture
def desktop_page(browser, live_server, e2e_user):
    pg = browser.new_page(live_server, mobile=False)
    pg.goto("/")
    pg.eval("""async ([u, p]) => {
      await fetch('/api/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({username: u, password: p}),
      });
    }""", [e2e_user["username"], e2e_user["password"]])
    pg.goto("/console")
    pg.wait_until("document.getElementById('who').textContent !== '…'")
    yield pg
    pg.close()


def test_desktop_prompt_goes_through_the_same_modal(desktop_page):
    """宽屏也不在页面上常驻提示词，走跟手机端同一个弹窗、同一份模板。"""
    desktop_page.fill("#key-name", "宽屏 Key")
    desktop_page.click("#btn-create")
    desktop_page.wait_for("#key-modal.show")
    desktop_page.eval("closeModal()")
    desktop_page.wait_until("document.querySelectorAll('#prompt-key option').length > 0")

    assert desktop_page.count("#prompt-box") == 0          # 常驻的提示词块没了
    desktop_page.click("#btn-copy-prompt")
    desktop_page.wait_for("#prompt-modal.show")
    assert desktop_page.text("#prompt-modal-box").count("<api key>") == 3
    assert desktop_page.visible("#prompt-modal-warn")


def test_desktop_keeps_inline_form_table_and_prompt_section(desktop_page):
    """宽屏仍然是：页面上直接摆表单、表格、底部一整块 Agent 接入。"""
    assert desktop_page.visible("#create-host")
    assert desktop_page.visible("#sec-prompt")
    assert desktop_page.visible("nav.tabs")
    assert not desktop_page.visible("#btn-open-create")
    assert not desktop_page.visible(".tabbar")
    assert desktop_page.eval(
        "getComputedStyle(document.querySelector('#page-keys table')).display") != "none"
    assert desktop_page.eval(
        "getComputedStyle(document.getElementById('keys-cards')).display") == "none"


def test_desktop_create_still_works_inline(desktop_page):
    desktop_page.fill("#key-name", "桌面 Key")
    desktop_page.click("#btn-create")
    desktop_page.wait_for("#key-modal.show")
    desktop_page.eval("closeModal()")
    desktop_page.wait_until(
        "document.getElementById('keys-body').innerText.includes('桌面 Key')")
    # 创建弹窗从头到尾没被用上，表单也没被搬走
    assert not desktop_page.visible("#create-modal")
    assert desktop_page.eval(
        "document.getElementById('create-host').contains(document.getElementById('key-name'))")
