"""domain/ 里的纯函数：坐标系换算、API Key scope、城市标签、限流。"""
import pytest

import db
from domain import labels, rate_limit, schemas, token_auth

pytestmark = pytest.mark.unit


# ---------------- 坐标系（GCJ-02 ↔ WGS-84） ----------------
@pytest.mark.parametrize("lng,lat", [
    (116.397428, 39.90923),    # 北京天安门
    (121.4900, 31.2400),       # 上海外滩
    (120.1300, 30.2590),       # 杭州西湖
])
def test_gcj02_wgs84_roundtrip_is_within_a_few_meters(lng, lat):
    g_lng, g_lat = schemas.wgs84_to_gcj02(lng, lat)
    b_lng, b_lat = schemas.gcj02_to_wgs84(g_lng, g_lat)
    # 1e-5 度 ≈ 1.1 米
    assert abs(b_lng - lng) < 1e-4 and abs(b_lat - lat) < 1e-4


def test_gcj02_offset_is_nonzero_inside_china():
    g_lng, g_lat = schemas.wgs84_to_gcj02(116.397428, 39.90923)
    # 国内偏移量量级在几百米（~0.003 度）
    assert 0.001 < abs(g_lng - 116.397428) < 0.02      # 经度偏移 ~0.012 度
    assert 0.0001 < abs(g_lat - 39.90923) < 0.02       # 纬度偏移 ~0.0003 度


@pytest.mark.parametrize("lng,lat", [(2.2945, 48.8584), (-74.0060, 40.7128)])
def test_no_offset_outside_china(lng, lat):
    """境外坐标不做火星偏移（埃菲尔铁塔 / 纽约）。"""
    assert schemas.wgs84_to_gcj02(lng, lat) == (lng, lat)
    assert schemas.gcj02_to_wgs84(lng, lat) == (lng, lat)


# ---------------- API Key 鉴权 ----------------
@pytest.fixture
def api_key(make_user):
    u = make_user()
    raw, prefix, kid = db.create_api_key(u["id"], "test", "read,write")
    return {"raw": raw, "prefix": prefix, "id": kid, "user": u}


def test_resolve_key_accepts_bearer(api_key):
    k = token_auth.resolve_key(f"Bearer {api_key['raw']}")
    assert k is not None and k["id"] == api_key["id"]
    assert token_auth.resolve_key(f"bearer {api_key['raw']}") is not None  # 大小写不敏感


@pytest.mark.parametrize("header", ["", None, "Token abc", "Bearer", "Bearer   ",
                                    "Bearer wrong-key", "justthekey"])
def test_resolve_key_rejects_bad_headers(header, api_key):
    assert token_auth.resolve_key(header) is None


def test_resolve_key_rejects_revoked(api_key):
    db.revoke_api_key(api_key["id"], api_key["user"]["id"])
    assert token_auth.resolve_key(f"Bearer {api_key['raw']}") is None


def test_has_scope_matrix():
    assert token_auth.has_scope({"scope": "read"}, "read")
    assert not token_auth.has_scope({"scope": "read"}, "write")
    assert token_auth.has_scope({"scope": "read,write"}, "write")
    assert token_auth.has_scope({"scope": "admin"}, "write")     # admin 万能
    assert token_auth.has_scope({"scope": "admin"}, "share")
    assert not token_auth.has_scope(None, "read")


def test_authorize_returns_error_codes(api_key):
    k, err = token_auth.authorize(f"Bearer {api_key['raw']}", "read")
    assert err is None and k["id"] == api_key["id"]
    k, err = token_auth.authorize(f"Bearer {api_key['raw']}", "admin")
    assert k is None and err == "insufficient_scope"
    k, err = token_auth.authorize("Bearer nope", "read")
    assert k is None and err == "invalid_api_key"


def test_authorize_updates_last_used(api_key):
    assert db.list_api_keys(api_key["user"]["id"])[0]["last_used_at"] is None
    token_auth.authorize(f"Bearer {api_key['raw']}", "read")
    assert db.list_api_keys(api_key["user"]["id"])[0]["last_used_at"] is not None


# ---------------- 限流 ----------------
def test_rate_limiter_blocks_after_limit():
    lim = rate_limit.RateLimiter()
    for _ in range(3):
        assert lim.check("k", 3) == (True, 0)
    allowed, retry = lim.check("k", 3)
    assert allowed is False and retry >= 1


def test_rate_limiter_is_per_key_and_resettable():
    lim = rate_limit.RateLimiter()
    lim.check("a", 1)
    assert lim.check("a", 1)[0] is False
    assert lim.check("b", 1)[0] is True      # 另一个 key 不受影响
    lim.reset()
    assert lim.check("a", 1)[0] is True


def test_check_key_uses_write_limit_for_write_scopes():
    rate_limit.limiter.reset()
    for scope in ("write", "share", "admin"):
        assert rate_limit.check_key(1, scope)[0] is True
    assert rate_limit.check_ip("")[0] is True     # 空 IP 不限流


# ---------------- 城市标签 ----------------
def test_extract_city_direct_hit():
    """城市词表来自 memory._CITY_THEMES（62 个城市），巴黎/厦门在表内。"""
    assert labels.extract_city("在厦门待了三天") == "厦门"
    assert labels.extract_city("巴黎-西班牙 混合行程") == "巴黎"


def test_extract_city_via_landmark():
    assert labels.extract_city("故宫") == "北京"
    assert labels.extract_city("埃菲尔铁塔") == "巴黎"


def test_city_word_wins_over_landmark():
    """同时命中时城市词优先（更精确）。"""
    assert labels.extract_city("北京 埃菲尔铁塔主题公园") == "北京"


def test_extract_city_none():
    assert labels.extract_city("") is None
    assert labels.extract_city(None) is None
    assert labels.extract_city("qqqqqq") is None


def test_extract_city_misses_cities_absent_from_dictionary():
    """已知局限：杭州不在 _CITY_THEMES 词表里，只能靠地标『西湖』兜底。"""
    assert labels.extract_city("在杭州待了三天") is None
    assert labels.extract_city("西湖") == "杭州"


def test_label_point_city_prefers_name_then_description():
    assert labels.label_point_city("西湖") == {"city": "杭州", "source": "name"}
    assert labels.label_point_city("某个没名字的点", "在厦门") == \
        {"city": "厦门", "source": "desc"}
    assert labels.label_point_city("xxx", "yyy") == {}
