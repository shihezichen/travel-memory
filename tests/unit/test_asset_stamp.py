"""静态资源版本号打戳（routers/deps.stamp_assets）。

回归重点：只能动 /static/ 自己的资源。2026-09-11 出过一次线上事故 —— 正则写成了
裸的 `(?<=[?&])v=...`，把高德 SDK 的 `webapi.amap.com/maps?v=2.0` 一起改掉，
SDK 加载失败、window.AMap 不存在，initMap() 抛异常后整条启动链停摆，
页面永远卡在「载入中…」。
"""
import pytest

from routers.deps import asset_version, stamp_assets

pytestmark = pytest.mark.unit


def test_static_assets_get_current_version():
    html = '<script src="/static/app.js?v=20200101"></script>'
    assert f'/static/app.js?v={asset_version()}' in stamp_assets(html)


def test_third_party_sdk_version_untouched():
    html = ('<script src="https://webapi.amap.com/maps?v=2.0'
            '&key=abc&plugin=AMap.Driving"></script>')
    assert stamp_assets(html) == html


@pytest.mark.parametrize("url", [
    "https://webapi.amap.com/maps?v=1.4.15&key=abc",
    "https://example.com/sdk.js?v=9.9.9",
    "https://cdn.example.com/a/static/x.js?v=keep-me",  # /static/ 不在路径开头，不算自家
])
def test_foreign_urls_untouched(url):
    html = f'<script src="{url}"></script>'
    assert stamp_assets(html) == html


@pytest.mark.parametrize("name", ["index.html", "share.html"])
def test_real_templates_keep_amap_version(name):
    """真模板过一遍：__AMAP_VERSION__ 占位符替换后不能再被打戳覆盖。"""
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[2] / "static" / name
    html = tpl.read_text(encoding="utf-8").replace("__AMAP_VERSION__", "2.0")
    out = stamp_assets(html)
    assert "webapi.amap.com/maps?v=2.0" in out
    assert f"/static/style.css?v={asset_version()}" in out
