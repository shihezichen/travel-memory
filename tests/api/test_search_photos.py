"""/api/search/photos：Jev 精判接上去之后，这条接口的行为和降级。

CLIP 那一侧在这里是替身（monkeypatch aiindex.search）—— 这组用例要钉的是
**接线**：召回条数有没有随精判放大、精判结果有没有真的改变顺序、Jev 没配
或者挂掉的时候接口是不是和从前逐字节一致。模型本身的效果不在这里验。
"""
import pytest

import aiindex
import caption
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

    def _search(user_id, query, limit=30, min_score=0.0, trip_ids=None):
        seen["limit"] = limit
        seen["trip_ids"] = trip_ids
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


# ---------------- 建库时预生成的画面描述 ----------------
def test_ai_caption_rides_along_in_results(auth_client, fake_recall, two_photos):
    """搜索结果带出模型写的那句话 —— 排查「这张为什么被判成相关」的唯一证据。"""
    caption.save(two_photos[0], "金色夕阳下的海岸线，远处有礁石")
    body = auth_client.get("/api/search/photos",
                           params={"q": "海边日落", "rerank": "false"}).json()
    got = {x["media_id"]: x["ai_caption"] for x in body["results"]}
    assert got[two_photos[0]] == "金色夕阳下的海岸线，远处有礁石"
    assert got[two_photos[1]] is None


def test_ai_caption_is_what_jev_actually_reads(auth_client, fake_recall, jev_on,
                                               two_photos, monkeypatch):
    """这条是整件事的目的：精判读到的得是画面描述，不是用户写的那句心情。

    两张图的用户 caption 都没有判别力（fixture 里写的是地点感想），真正能回答
    「是不是海边日落」的只有模型看图写的那句。
    """
    caption.save(two_photos[0], "金色夕阳下的海岸线，远处有礁石")
    seen = []

    def _post(state, questions):
        if "image_description" not in state:         # 意图分类那一次，不是候选图
            return {"category": {"choice": "自然风光"}}
        seen.append(state["image_description"])
        hit = "海岸线" in state["image_description"]
        return _answers(4 if hit else 0, 0.95 if hit else 0.05)

    monkeypatch.setattr(jevrerank, "_post", _post)
    body = auth_client.get("/api/search/photos", params={"q": "海边日落"}).json()

    assert any(d.startswith("金色夕阳下的海岸线") for d in seen), seen
    assert body["reranked"] is True
    # 没有画面描述的那张被 Jev 判成「不该返回」，直接出局
    assert [x["media_id"] for x in body["results"]] == [two_photos[0]]


def test_without_any_ai_caption_nothing_changes(auth_client, fake_recall, jev_on,
                                                monkeypatch):
    """一张都没生成过描述时，这条接口必须和加这一层之前逐字节一致（方案C 兜底）。"""
    monkeypatch.setattr(jevrerank, "_post",
                        lambda state, q: {"category": {"choice": "自然风光"}}
                        if "image_description" not in state else _answers(2, 0.6))
    body = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()
    assert body["reranked"] is True and body["judged"] == 2
    assert all(x["ai_caption"] is None for x in body["results"])


def test_search_status_exposes_caption_progress(auth_client, two_photos):
    caption.save(two_photos[0], "金色夕阳下的海岸线")
    body = auth_client.get("/api/search/status").json()["caption"]
    assert body["photos"] == 2 and body["captioned"] == 1 and body["backlog"] == 1


def test_search_requires_login(client):
    assert client.get("/api/search/photos", params={"q": "西湖"}).status_code == 401

# ---------------- 搜索范围：trips= 这个参数 ----------------
# 界面上默认只勾当前行程（见 static/app.js 的 openPhotoSearch），所以这个参数
# 是常态而不是边角料 —— 它拼错一次，用户看到的就是「我这次旅行一张都没搜到」。
def test_trips_param_reaches_the_recall_layer(auth_client, fake_recall):
    body = auth_client.get("/api/search/photos",
                           params={"q": "海边日落", "trips": "3,7"}).json()
    assert fake_recall["trip_ids"] == [3, 7]
    assert body["trips"] == [3, 7]          # 回显出来，前端/排查都不用猜


def test_no_trips_param_means_every_trip(auth_client, fake_recall):
    """不传 = 全部行程。这里要的是 None 而不是空列表：空列表在召回层会被当成
    「一条行程都不要」，那是另一个意思。"""
    body = auth_client.get("/api/search/photos", params={"q": "海边日落"}).json()
    assert fake_recall["trip_ids"] is None
    assert body["trips"] == []


def test_junk_in_trips_is_dropped_rather_than_rejected(auth_client, fake_recall):
    """参数是前端拼串来的，多个逗号、带空格、夹个空值都很常见。
    为此把整次搜索打回 422 不值当 —— 认得出的留下，认不出的丢掉。"""
    r = auth_client.get("/api/search/photos",
                        params={"q": "海边日落", "trips": " 8 ,,abc,9,"})
    assert r.status_code == 200
    assert fake_recall["trip_ids"] == [8, 9]


# ---------------- 召回层：过滤真的发生在矩阵那一层 ----------------
@pytest.fixture
def cross_trip_vectors(auth_client, seeded_trip, jpeg_bytes, monkeypatch):
    """两条行程各一张照片，向量手工写进 media_embeddings。

    CLIP 换成固定向量的替身：这组用例要钉的是**范围过滤本身** —— 收窄到一条
    行程之后，另一条里分数更高的那张必须一张都不出现，而不是「排在后面」。
    """
    np = pytest.importorskip("numpy")
    import sys
    import types

    import db

    # 第二条行程 + 一个点
    t2 = auth_client.post("/api/trips", data={"name": "第二条行程"}).json()["id"]
    p2 = auth_client.post(f"/api/trips/{t2}/points", data={
        "name": "海边", "lat": 36.06, "lng": 120.38}).json()["id"]

    m1 = _upload(auth_client, seeded_trip["point_ids"][0], "a.jpg",
                 jpeg_bytes()).json()["id"]
    m2 = _upload(auth_client, p2, "b.jpg", jpeg_bytes()).json()["id"]

    # 第二条行程那张跟查询更贴（0.96 对 0.60）。不这么摆的话，
    # 「收窄之后它没出现」可能只是因为它本来就排不进前几名。
    vecs = {m1: [1.0, 0.0, 0.0, 0.0], m2: [0.8, 0.6, 0.0, 0.0]}
    conn = db.get_conn()
    try:
        for mid, v in vecs.items():
            conn.execute(
                # 用 upsert 而不是裸 INSERT：上传链路如果哪天又开始自己写这张表，
                # 这里撞的是主键冲突，报出来的却是一句和本用例毫不相干的 IntegrityError。
                "INSERT INTO media_embeddings (media_id, model, dim, vec, status) "
                "VALUES (?, ?, ?, ?, 'done') "
                "ON CONFLICT(media_id) DO UPDATE SET "
                "  model=excluded.model, dim=excluded.dim, vec=excluded.vec, "
                "  status=excluded.status",
                (mid, "test", 4, np.array(v, dtype="float32").tobytes()))
        conn.commit()
    finally:
        conn.close()
    aiindex._invalidate_cache()

    fake = types.ModuleType("clipmodel")
    fake.available = lambda: True
    fake.encode_texts = lambda qs: [np.array([0.6, 0.8, 0.0, 0.0], dtype="float32")]
    monkeypatch.setitem(sys.modules, "clipmodel", fake)
    monkeypatch.setattr(aiindex, "available", lambda: True)

    return {"uid": auth_client.user["id"], "t1": seeded_trip["trip_id"], "t2": t2,
            "m1": m1, "m2": m2}


def test_scoped_search_excludes_other_trips(auth_client, cross_trip_vectors):
    c = cross_trip_vectors
    hits = aiindex.search(c["uid"], "海边日落", trip_ids=[c["t1"]])
    assert [h["media_id"] for h in hits] == [c["m1"]]


def test_unscoped_search_still_sees_everything(auth_client, cross_trip_vectors):
    """不收窄时顺序由分数说了算：第二条行程那张更贴，排第一。"""
    c = cross_trip_vectors
    hits = aiindex.search(c["uid"], "海边日落")
    assert [h["media_id"] for h in hits] == [c["m2"], c["m1"]]


def test_narrowing_does_not_eat_into_limit(auth_client, cross_trip_vectors):
    """收窄之后 limit 仍然取得满 —— 过滤在矩阵那一层，不是先全局取 topk 再筛。

    limit=1 配上「只搜第一条行程」：先取 topk 的实现会取到第二条行程那张
    （分数更高），筛完返回空；按掩码过滤的实现返回第一条行程里的那张。
    """
    c = cross_trip_vectors
    hits = aiindex.search(c["uid"], "海边日落", limit=1, trip_ids=[c["t1"]])
    assert [h["media_id"] for h in hits] == [c["m1"]]


def test_other_peoples_trip_ids_buy_you_nothing(auth_client, other_client,
                                                cross_trip_vectors):
    """拿别人的行程 id 来搜也是空的 —— 用户掩码和行程掩码是「与」的关系。"""
    c = cross_trip_vectors
    assert aiindex.search(other_client.user["id"], "海边日落",
                          trip_ids=[c["t1"], c["t2"]]) == []



# ---------------- 判过就记住：同一句话不重复问 Jev ----------------
# 精判是整条检索链路里唯一按秒计的那一段（一批几十上百个候选、并发 HTTP、
# 最坏吃满 BUDGET 的 8 秒）。而用户的真实动作是**反复问同一句话** —— 换个
# 行程范围再搜、拖完相关度线再搜、从灯箱退回来再搜。这些重来的每一次，
# query 和送进去的画面描述都一字不差，答案当然也一样。
def test_same_query_twice_asks_jev_only_once(auth_client, fake_recall, jev_on,
                                             two_photos, monkeypatch):
    calls = []

    def _post(state, questions):
        calls.append(state.get("image_description"))
        return _answers(3, 0.8)

    monkeypatch.setattr(jevrerank, "_post", _post)
    first = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()
    n = len(calls)
    assert n >= 3                      # 一次意图分类 + 两张候选

    second = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()
    assert len(calls) == n              # 第二趟一次网络都没发
    # 省掉调用不等于省掉结果：两次的排序、张数、judged 必须一模一样。
    assert second["reranked"] is True and second["judged"] == first["judged"]
    assert [x["media_id"] for x in second["results"]] == \
           [x["media_id"] for x in first["results"]]


def test_a_different_query_is_judged_again(auth_client, fake_recall, jev_on,
                                           two_photos, monkeypatch):
    """缓存的键里带着 query —— 换个说法必须重新问，不能把上一句的答案套上去。"""
    calls = []
    monkeypatch.setattr(jevrerank, "_post",
                        lambda state, q: (calls.append(state.get("query")),
                                          _answers(3, 0.8))[1])
    auth_client.get("/api/search/photos", params={"q": "西湖"})
    n = len(calls)
    auth_client.get("/api/search/photos", params={"q": "雷峰塔的剪影"})
    assert len(calls) > n


def test_a_failed_judgement_is_not_remembered(auth_client, fake_recall, jev_on,
                                              two_photos, monkeypatch):
    """判失败的不进缓存 —— 网络抖一下就把「这张判不了」记一辈子，那是把
    一次偶发故障焊成了永久状态。"""
    calls = []
    state = {"boom": True}

    def _post(st, questions):
        calls.append(1)
        if state["boom"]:
            raise RuntimeError("connection reset by peer")
        return _answers(3, 0.8)

    monkeypatch.setattr(jevrerank, "_post", _post)
    assert auth_client.get("/api/search/photos",
                           params={"q": "西湖"}).json()["reranked"] is False
    n = len(calls)
    state["boom"] = False
    body = auth_client.get("/api/search/photos", params={"q": "西湖"}).json()
    assert len(calls) > n               # 重试了，没把失败记下来
    assert body["reranked"] is True
