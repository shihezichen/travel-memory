"""Jev 精判重排：描述拼装、分数融合、以及「出事了要降级成什么样」。

这套测试一次网络都不发 —— 全部 monkeypatch jevrerank._post。
真要打云端接口用 scripts/jev_probe.py，那是手动的冒烟，不进 CI。
"""
import pytest

import jevrerank

pytestmark = pytest.mark.unit


@pytest.fixture
def jev_on(monkeypatch):
    """把模块配成「已启用」，并且不许任何真实 HTTP 漏出去。"""
    monkeypatch.setattr(jevrerank, "API_KEY", "test-key")
    monkeypatch.setattr(jevrerank, "ENABLED", True)
    monkeypatch.setattr(jevrerank, "BACKEND", "typesafe")
    monkeypatch.setattr(jevrerank, "_post",
                        lambda *a, **k: pytest.fail("不该发真请求"))
    return monkeypatch


def _answers(rel, match, cat="自然风光"):
    """造一份形状和官方文档一致的 Jev 响应。"""
    return {
        "relevance": {"type": "score", "score": rel, "confidence": 0.9},
        "is_match": {"type": "noul", "noul": match},
        "category": {"type": "choice", "choice": cat, "confidence": 0.8},
    }


# ---------------- 候选图 → 文字描述 ----------------
def test_describe_puts_human_caption_first():
    d = jevrerank.describe({"caption": "夕阳下的海岸线", "point_name": "巴塞罗那",
                            "trip_title": "西班牙", "filename": "sunset.jpg"})
    assert d.startswith("夕阳下的海岸线")
    assert "巴塞罗那" in d and "西班牙" in d


@pytest.mark.parametrize("fn", ["IMG_2317.jpg", "DSC_0001.JPG", "20240101.png",
                                "mmexport1696123456789.jpg",
                                "0f8c1a2b3d4e5f60718293a4b5c6d7e8.jpg"])
def test_describe_drops_noise_filenames(fn):
    d = jevrerank.describe({"point_name": "西湖", "filename": fn})
    assert "文件名" not in d


def test_describe_keeps_meaningful_filename():
    d = jevrerank.describe({"point_name": "西湖", "filename": "断桥残雪.jpg"})
    assert "断桥残雪" in d


def test_describe_empty_metadata_is_empty_string():
    # 全空要返回空串，调用方据此跳过这张图 —— 拿空串去问 Jev 是在烧钱买噪声
    assert jevrerank.describe({"filename": "IMG_0001.jpg"}) == ""


# ---------------- CLIP 分归一化 ----------------
def test_norm_clip_spreads_narrow_cosine_band():
    # Chinese-CLIP 的余弦实际就挤在这个窄区间里，归一化后才撑满 0~1
    assert jevrerank._norm_clip([0.31, 0.25, 0.19]) == [1.0, 0.5, 0.0]


def test_norm_clip_all_equal_is_neutral():
    assert jevrerank._norm_clip([0.2, 0.2, 0.2]) == [0.5, 0.5, 0.5]


def test_norm_clip_empty():
    assert jevrerank._norm_clip([]) == []


# ---------------- 融合打分 ----------------
def test_fuse_without_answers_falls_back_to_clip_only():
    f = jevrerank.fuse(0.8, None)
    assert f["judged"] is False and f["final"] == 0.8


def test_fuse_half_answer_counts_as_unjudged():
    # 只回来一半的答案比没有答案更危险，不许拿来排序
    f = jevrerank.fuse(0.8, {"relevance": {"score": 4}})
    assert f["judged"] is False and f["final"] == 0.8


def test_fuse_applies_documented_weights():
    f = jevrerank.fuse(1.0, _answers(rel=4, match=1.0, cat="自然风光"), "自然风光")
    # 0.35*1 + 0.40*(4/4) + 0.20*1 + 0.05*1 = 1.0
    assert f["judged"] is True
    assert f["final"] == pytest.approx(1.0)
    assert f["category_hit"] is True


def test_fuse_category_bonus_only_when_intent_matches():
    hit = jevrerank.fuse(0.0, _answers(0, 0.0, "自然风光"), "自然风光")
    miss = jevrerank.fuse(0.0, _answers(0, 0.0, "城市街景"), "自然风光")
    assert hit["final"] - miss["final"] == pytest.approx(jevrerank.W_CATEGORY)


def test_fuse_score_is_normalised_by_level_count():
    # 5 档 → score 值域 [0,4]，中间档应当归一化成 0.5
    f = jevrerank.fuse(0.0, _answers(rel=2, match=0.0))
    assert f["relevance"] == pytest.approx(0.5)


# ---------------- 重排主流程 ----------------
HITS = [{"media_id": 1, "filename": "a.jpg", "score": 0.30},
        {"media_id": 2, "filename": "b.jpg", "score": 0.28},
        {"media_id": 3, "filename": "c.jpg", "score": 0.26}]
META = {1: {"caption": "半夜的海边霓虹灯"},
        2: {"caption": "夕阳沉入海平面"},
        3: {"caption": "酒店房间"}}


def test_rerank_promotes_the_candidate_jev_likes(jev_on):
    """CLIP 把霓虹海岸排第一，Jev 认得出日落 —— 第二名要被顶上来。"""
    def fake_post(state, questions):
        if "image_description" not in state:              # 意图分类那次
            return {"category": {"choice": "自然风光"}}
        d = state["image_description"]
        if "夕阳" in d:
            return _answers(4, 0.95)
        if "霓虹" in d:
            return _answers(1, 0.60)
        return _answers(0, 0.35)
    jev_on.setattr(jevrerank, "_post", fake_post)

    r = jevrerank.rerank("海边日落", HITS, META, limit=10)
    assert r["reranked"] is True
    assert [x["media_id"] for x in r["results"]][0] == 2
    assert r["results"][0]["clip_score"] == 0.28          # 原始 CLIP 分要留着


def test_rerank_drops_candidates_below_match_threshold(jev_on):
    def fake_post(state, questions):
        if "image_description" not in state:
            return {"category": {"choice": "自然风光"}}
        if "酒店" in state["image_description"]:
            return _answers(0, 0.05)                      # < 0.30，明确说不该返回
        return _answers(3, 0.9)
    jev_on.setattr(jevrerank, "_post", fake_post)

    ids = [x["media_id"] for x in jevrerank.rerank("海边日落", HITS, META)["results"]]
    assert 3 not in ids and len(ids) == 2


def test_rerank_survives_total_api_failure(jev_on):
    """Jev 全挂 → 原样返回 CLIP 顺序，一条都不能少，更不能抛异常。"""
    def boom(*a, **k):
        raise RuntimeError("connection reset")
    jev_on.setattr(jevrerank, "_post", boom)

    r = jevrerank.rerank("海边日落", HITS, META, limit=10)
    assert r["reranked"] is False and r["reason"] == "jev_unavailable"
    assert [x["media_id"] for x in r["results"]] == [1, 2, 3]


def test_rerank_partial_failure_does_not_punish_unjudged(jev_on):
    """只有一张判失败：它不该因此掉到所有判过的图后面。"""
    def flaky(state, questions):
        if "image_description" not in state:
            return {"category": {"choice": "自然风光"}}
        if "夕阳" in state["image_description"]:
            raise RuntimeError("timeout")
        return _answers(0, 0.5)                           # 其余的都判得很低
    jev_on.setattr(jevrerank, "_post", flaky)

    r = jevrerank.rerank("海边日落", HITS, META, limit=10)
    assert 2 in [x["media_id"] for x in r["results"]]
    judged = {x["media_id"]: x["jev"]["judged"] for x in r["results"]}
    assert judged[2] is False


def test_rerank_skips_when_no_metadata_at_all(jev_on):
    """图库还没写 caption：这不是故障，别白花钱去问一堆 0.5。"""
    bare = {1: {"filename": "IMG_1.jpg"}, 2: {}, 3: {}}
    r = jevrerank.rerank("海边日落", HITS, bare)
    assert r["reranked"] is False and r["reason"] == "no_descriptions"
    assert len(r["results"]) == 3


def test_rerank_noop_when_not_configured(monkeypatch):
    monkeypatch.setattr(jevrerank, "API_KEY", "")
    r = jevrerank.rerank("海边日落", HITS, META)
    assert r["reranked"] is False and r["reason"] == "jev_disabled"
    assert [x["media_id"] for x in r["results"]] == [1, 2, 3]


def test_rerank_empty_hits():
    assert jevrerank.rerank("x", [], {})["results"] == []


def test_intent_failure_is_not_fatal(jev_on):
    """意图那一次挂了只是没有 category 加分，主流程照跑。"""
    calls = []

    def fake_post(state, questions):
        calls.append(state)
        if "image_description" not in state:
            raise RuntimeError("500")
        return _answers(3, 0.9)
    jev_on.setattr(jevrerank, "_post", fake_post)

    r = jevrerank.rerank("海边日落", HITS, META)
    assert r["reranked"] is True and r["intent"] is None
    assert all(x["jev"]["category_hit"] is False for x in r["results"])


# ---------------- 请求体形状（对着官方文档钉死） ----------------
def test_candidate_questions_match_official_schema():
    qs = jevrerank._candidate_questions("海边日落")
    assert qs["relevance"]["type"] == "score"
    # score 的 criteria 必须是有序数组，返回值域 = len-1
    assert isinstance(qs["relevance"]["criteria"], list)
    assert 2 <= len(qs["relevance"]["criteria"]) <= 10
    assert qs["is_match"]["type"] == "noul"
    assert set(qs["is_match"]["criteria"]) == {"true", "false"}
    assert qs["category"]["type"] == "choice"
    # choice 的 criteria 是 option → 描述 的 map，最多 255 项
    assert isinstance(qs["category"]["criteria"], dict)
    assert len(qs["category"]["criteria"]) <= 255
