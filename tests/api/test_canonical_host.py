"""规范域名跳转：www.top001.xin -> top001.xin。

这里锁的是三件容易回归的事：
  ① www 的请求要在**鉴权之前**就跳走（否则用户看到的是 401 而不是跳转）
  ② 路径和 query 不能丢
  ③ 裸域和其它 Host（IP、localhost）一律不受影响
"""
import pytest

import app as app_mod

pytestmark = pytest.mark.api


def test_www_redirects_to_apex(client, monkeypatch):
    monkeypatch.setattr(app_mod, "CANONICAL_HOST", "top001.xin")
    r = client.get("/trips/7?tab=map", headers={"Host": "www.top001.xin"},
                   follow_redirects=False)
    assert r.status_code == 301
    # 路径和 query 都要原样带过去
    assert r.headers["location"].endswith("//top001.xin/trips/7?tab=map")


def test_www_redirect_happens_before_auth(client, monkeypatch):
    """/api/trips 是要登录的。未登录时从 www 访问应该拿到 301 而不是 401 ——
    这一条在断言中间件的注册顺序，顺序反了这里立刻红。"""
    monkeypatch.setattr(app_mod, "CANONICAL_HOST", "top001.xin")
    r = client.get("/api/trips", headers={"Host": "www.top001.xin"},
                   follow_redirects=False)
    assert r.status_code == 301


def test_www_with_port_in_host_header(client, monkeypatch):
    monkeypatch.setattr(app_mod, "CANONICAL_HOST", "top001.xin")
    r = client.get("/health", headers={"Host": "www.top001.xin:443"},
                   follow_redirects=False)
    assert r.status_code == 301


@pytest.mark.parametrize("host", [
    "top001.xin",              # 裸域：本来就是规范域名
    "top001.xin:443",
    "139.129.24.132",          # IP 直连入口
    "localhost",
    "wwwtop001.xin",           # 少个点，不是 www 子域
    "evil-www.top001.xin",     # 前缀不是恰好 "www."
])
def test_other_hosts_not_redirected(client, monkeypatch, host):
    monkeypatch.setattr(app_mod, "CANONICAL_HOST", "top001.xin")
    r = client.get("/health", headers={"Host": host}, follow_redirects=False)
    assert r.status_code == 200


def test_disabled_when_domain_unset(client, monkeypatch):
    """没配 TRAVEL_DOMAIN 时整段中间件是空操作 —— IP 模式下不能乱跳。"""
    monkeypatch.setattr(app_mod, "CANONICAL_HOST", "")
    r = client.get("/health", headers={"Host": "www.top001.xin"},
                   follow_redirects=False)
    assert r.status_code == 200
