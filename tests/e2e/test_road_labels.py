"""公路编号标签的取舍规则。

跑在真浏览器里是因为这套逻辑（segRoadRuns / roadRunsCoverage / roadDistLooksSame）
住在 static/common.js，没有 Node 侧的测试环境；直接在页面里调它们最省事，
也顺带保证了它们确实是全局可见的 —— ROAD_COVERAGE_MIN 一度只定义在 app.js 里，
分享页加载不到，那种错在这儿一跑就露。

不用登录：这几个都是纯函数，页面加载出 common.js 就能调。

钉住的是三条经验规则，都是被具体的路坑出来的：
  · 一段路经过好几条公路时，每条都该标出来（独库那段：先 G218 再 G217）
  · 里程门槛在长段上不能无限涨（8% 的门槛让 328 公里里的 12.7 公里 G218 落选）
  · 借道几百米的匝道不标
"""
import pytest

pytestmark = pytest.mark.e2e


def _steps(page, items):
    """[(road, 米)] → segRoadRuns 的入参。路径给两个点就够算中点。"""
    js = "[" + ",".join(
        "{road:%r,distance:%d,path:[[%f,40],[%f,40]]}" % (r, m, 80 + i, 80 + i + 1)
        for i, (r, m) in enumerate(items)) + "]"
    return js.replace("'", '"')


def _runs(page, items):
    total = sum(m for _, m in items)
    return page.eval("segRoadRuns(%s, %d).map(r => r.code)"
                     % (_steps(page, items), total))


def test_multiple_roads_in_one_segment_all_labeled(page):
    """那拉提→库车：先走 12.7 公里 G218，再走 293 公里 G217，两个都要标。

    以前只标得出 G217 —— 门槛是「不少于本段 8%」，328 公里的 8% 是 26 公里，
    G218 那 12.7 公里被当成「蹭一下」滤掉了。
    """
    page.goto("/app")
    codes = _runs(page, [("218国道", 4290), ("218国道", 8453),
                         ("G217", 293049), ("G217", 9040)])
    assert codes == ["G218", "G217"]


def test_short_detour_still_filtered(page):
    """借道 800 米的匝道不标 —— 放宽门槛不等于什么都标。"""
    page.goto("/app")
    codes = _runs(page, [("G30", 240000), ("S201", 800)])
    assert codes == ["G30"]


def test_road_threshold_capped_on_long_segments(page):
    """门槛封顶 10 公里：再长的段，走满 10 公里的路也该标出来。"""
    page.goto("/app")
    # 1000 公里的段，8% 是 80 公里；G6 这 12 公里要是没有封顶就会落选
    codes = _runs(page, [("G109", 988000), ("G6", 12000)])
    assert set(codes) == {"G109", "G6"}


def test_prefers_the_source_that_explains_more(page):
    """覆盖率和里程一致性：两条规则决定要不要换用 OSRM 那份编号。"""
    page.goto("/app")
    # 只解释了 4% 的路 → 覆盖率远低于门槛，该去问问别人
    cov = page.eval("roadRunsCoverage([{code:'G218',meters:12743}], 328130)")
    assert cov < 0.6
    # 328 公里 vs 330 公里：同一条路，换得
    assert page.eval("roadDistLooksSame(328130, 330699)") is True
    # 328 公里 vs 1361 公里：高德绕远了，换过去标签会飘到没有线的地方
    assert page.eval("roadDistLooksSame(328130, 1360765)") is False
