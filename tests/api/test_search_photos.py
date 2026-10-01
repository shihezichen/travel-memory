"""/api/search/photos：Jev 精判接上去之后，这条接口的行为和降级。

CLIP 那一侧在这里是替身（monkeypatch aiindex.search）—— 这组用例要钉的是
**接线**：召回条数有没有随精判放大、精判结果有没有真的改变顺序、Jev 没配
或者挂掉的时候接口是不是和从前逐字节一致。模型本身的效果不在这里验。
"""
import pytest

import aiindex
import jevrerank

pytestmark = pytest.mark.api


def _upload(client, pid, name, content):
    return client.post(f"/api/points/{pid}/media",
                       files={"file": (name, content, "image/jpeg")})


@pytest.fixture
def two_photos(auth_client, seeded_trip, jpeg_bytes):
    """两张挂在「西湖」下的照片，各写一句 caption。

    caption 是必须的：上传后入库的 filename 是 uuid，point_name 两张又一样，
    不写 caption 的话 describe() 对两张图给出的描述一字不差 —— Jev 再准也
    分不开。这恰好就是这套方案在真实图库里的软肋，见 jevrerank 模块注释。
    """
    pid = seeded_trip["point_ids"][0]
    ids = []
    for cap in ("冬天的断桥，桥上积雪", "夕阳西下，雷峰塔的剪影"):
        mid = _upload(auth_client, pid, "x.jpg", jpeg_bytes()).json()["id"]
        r = auth_client.put(f"/api/media/{mid}/caption", data={"caption": cap})
        assert r.status_code == 200, r.text
        ids.append(mid)
    return ids


@pytest.fixture
def fake_recall(monkeypatch, two_photos):
    """把 CLIP 召回换成固定结果，并记录它被要了多少条。"""
    a, b = two_photos
    seen = {}

    def _search(user_id, query, limit=30, min_score=0.0):
        seen["limit"] = limit
        return [{"media_id": a, "filename": "duanqiao.jpg", "score": 0.31},
                {"media_id": b, "filename": "leifeng.jpg", "score": 0.22}]

    monkeypatch.setattr(aiindex, "available", lambda: True)
    monkeypatch.setattr(aiindex, "search", _search)
    return seen


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setattr(jevrerank, "API_KEY", "test-key")
    monkeypatch.setattr(jevrerank, "ENABLED", True)
    monkeypatch.setattr(jevrerank, "BACKEND", "kev")
    return monkeypatch


def _answers(rel, match, cat="自然风光"):
    return {"relevance": {"score": rel}, "is_match": {"noul": match},
            "category": {"choice": cat}}


# ---------------- 没配 Jev：和从前一模一样 ----------------
def test_without_jev_the_endpoint_behaves_exactly_as_before(
        auth_client, fake_recall, monkeypatch):
    monkeypatch.setattr(jevrerank, "API_KEY", "")
    body = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()

    assert body["enabled"] is True and body["count"] == 2
    assert body["reranked"] is False and body["rerank_skipped"] == "jev_disabled"
    assert [r["filename"] for r in body["results"]] == ["duanqiao.jpg", "leifeng.jpg"]
    assert fake_recall["limit"] == 30          # 没精判就不多召回


def test_disabled_jev_still_returns_model_off_shape(auth_client, monkeypatch):
    monkeypatch.setattr(aiindex, "available", lambda: False)
    body = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()
    assert body == {"enabled": False, "results": [], "count": 0}


# ---------------- 配了 Jev：精判生效 ----------------
def test_jev_rerank_changes_the_order(auth_client, fake_recall, jev_on):
    def fake_post(state, questions):
        if "image_description" not in state:
            return {"category": {"choice": "自然风光"}}
        if "夕阳" in state["image_description"]:
            return _answers(4, 0.95)
        return _answers(1, 0.50)
    jev_on.setattr(jevrerank, "_post", fake_post)

    body = auth_client.get("/api/search/photos", params={"q": "夕阳"}).json()
    assert body["reranked"] is True and body["judged"] == 2
    # CLIP 把「断桥残雪」排第一（0.31 > 0.22），Jev 认得出「夕照」才是答案
    assert body["results"][0]["filename"] == "leifeng.jpg"
    assert body["results"][0]["clip_score"] == 0.22      # 原始 CLIP 分留着可查
    assert fake_recall["limit"] == jevrerank.RECALL      # 精判时召回放大


def test_rerank_false_skips_jev_entirely(auth_client, fake_recall, jev_on):
    jev_on.setattr(jevrerank, "_post",
                   lambda *a, **k: pytest.fail("rerank=false 时不该调 Jev"))
    body = auth_client.get("/api/search/photos",
                           params={"q": "夕阳", "rerank": "false"}).json()
    assert body["reranked"] is False
    assert fake_recall["limit"] == 30


def test_jev_outage_degrades_to_clip_order(auth_client, fake_recall, jev_on):
    """云端全挂：接口照常 200，顺序退回 CLIP，并说清楚为什么没精判。"""
    def boom(*a, **k):
        raise RuntimeError("connection reset by peer")
    jev_on.setattr(jevrerank, "_post", boom)

    r = auth_client.get("/api/search/photos", params={"q": "夕阳"})
    assert r.status_code == 200
    body = r.json()
    assert body["reranked"] is False and body["rerank_skipped"] == "jev_unavailable"
    assert [x["filename"] for x in body["results"]] == ["duanqiao.jpg", "leifeng.jpg"]


def test_search_status_exposes_jev_config(auth_client, jev_on):
    body = auth_client.get("/api/search/status").json()
    assert body["jev"]["enabled"] is True
    assert body["jev"]["weights"]["relevance"] == jevrerank.W_RELEVANCE


def test_search_requires_login(client):
    assert client.get("/api/search/photos", params={"q": "西湖"}).status_code == 401
