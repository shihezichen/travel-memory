"""建库时预计算的照片描述：清洗、积压队列、以及它怎么接进精判。

一次模型都不加载 —— VLM 在 .venv-ai 里，CI 跑的是主 venv。这里测的是
caption.py 这一层的全部逻辑（它本来就不 import torch），模型本身的效果
靠 scripts/backfill_caption.py --dry-run 人工看，那是冒烟不是单测。
"""
import pytest

import caption
import db
import jevrerank

pytestmark = pytest.mark.unit


@pytest.fixture
def point(make_user):
    u = make_user()
    tid = db.create_trip(u, "西班牙", None, None)
    pid = db.create_point(tid, "巴塞罗那", 41.4, 2.2, "2026-05-01", None)
    return {"user": u, "trip_id": tid, "point_id": pid}


@pytest.fixture
def photo(point):
    return db.add_media(point["point_id"], "photo", "a.jpg", None, None, None, None)


# ---------------- 清洗 ----------------
@pytest.mark.parametrize("raw", [
    "这张图片展示了金色夕阳下的海岸线",
    "这是一张照片，图中展示了：金色夕阳下的海岸线",
    "画面中显示了金色夕阳下的海岸线",
    "  这张图片展示了\n金色夕阳下的海岸线  ",
])
def test_clean_strips_boilerplate(raw):
    # 每条描述都带同一段开场白 == 给所有候选加了同一段噪声，对排序零贡献
    assert caption.clean(raw) == "金色夕阳下的海岸线"


@pytest.mark.parametrize("raw", [
    "主体是金色夕阳下的海岸线",
    "主体是：金色夕阳下的海岸线",
    "画面主体是金色夕阳下的海岸线",
    "这张图片的主体是金色夕阳下的海岸线",
])
def test_clean_strips_prompt_echo(raw):
    # 提示词里写了「主体是什么」，模型就拿这三个字起头 —— 跟开场白一样条条都有
    assert caption.clean(raw) == "金色夕阳下的海岸线"


def test_clean_keeps_normal_sentence():
    s = "金黄的沙漠，远处有沙丘起伏，天空湛蓝无云"
    assert caption.clean(s) == s


def test_clean_collapses_newlines_into_sentence():
    assert caption.clean("海岸线\n天空很蓝") == "海岸线，天空很蓝"


def test_clean_empty():
    assert caption.clean("") == "" and caption.clean(None) == ""


def test_clean_truncates_at_punctuation(monkeypatch):
    monkeypatch.setattr(caption, "MAX_CHARS", 12)
    # 从中间剁断会让意思反过来（「天空湛蓝无云，远处乌云」），所以回退到句读
    assert caption.clean("一二三四五六七八，九十一二三四五六") == "一二三四五六七八"


def test_clean_hard_cuts_when_no_punctuation(monkeypatch):
    monkeypatch.setattr(caption, "MAX_CHARS", 5)
    assert caption.clean("一二三四五六七八九十") == "一二三四五"


# ---------------- 积压队列 ----------------
def test_backlog_lists_photos_without_caption(photo):
    assert [r["media_id"] for r in caption.backlog()] == [photo]


def test_backlog_skips_done(photo):
    caption.save(photo, "海边日落")
    assert caption.backlog() == []


def test_backlog_skips_videos(point):
    db.add_media(point["point_id"], "video", "v.mp4", None, None, None, None)
    assert caption.backlog() == []


def test_backlog_skips_failed_unless_asked(photo):
    caption.fail(photo, "取图失败")
    # 取不到图重试一百次还是取不到；watch 模式下那就是个死循环
    assert caption.backlog() == []
    assert [r["media_id"] for r in caption.backlog(retry_failed=True)] == [photo]


def test_backlog_remodel_picks_up_other_models(photo, monkeypatch):
    caption.save(photo, "旧模型写的")
    monkeypatch.setattr(caption, "MODEL_NAME", "某个新模型")
    assert caption.backlog() == []
    assert [r["media_id"] for r in caption.backlog(remodel=True)] == [photo]


def test_backlog_media_ids_ignores_filters(photo):
    caption.save(photo, "已经有描述了")
    # 指名道姓那批绕开全部过滤：调 prompt 时就是要反复重算同一张
    assert [r["media_id"] for r in caption.backlog(media_ids=[photo])] == [photo]


def test_backlog_empty_when_disabled(photo, monkeypatch):
    monkeypatch.setattr(caption, "ENABLED", False)
    assert caption.backlog() == []
    # 但指名道姓的仍然算 —— 开关管的是自动排空，不是手动重跑
    assert len(caption.backlog(media_ids=[photo])) == 1


def test_backlog_limit(point):
    ids = [db.add_media(point["point_id"], "photo", "p%d.jpg" % i,
                        None, None, None, None) for i in range(5)]
    assert len(caption.backlog(limit=2)) == 2
    assert [r["media_id"] for r in caption.backlog(limit=2)] == ids[:2]


# ---------------- 落库 ----------------
def test_save_cleans_before_storing(photo):
    assert caption.save(photo, "这张图片展示了一只猫") == "一只猫"
    assert caption.get_many([photo]) == {photo: "一只猫"}


def test_save_empty_output_counts_as_failure(photo):
    # 「模型回了个空」和「还没生成」必须分得开，否则每轮都会再捡起它
    assert caption.save(photo, "这张图片展示了") == ""
    assert caption.get_many([photo]) == {}
    assert caption.backlog() == []
    assert caption.stats()["failed"] == 1


def test_save_is_idempotent_upsert(photo):
    caption.save(photo, "第一版")
    caption.save(photo, "第二版")
    assert caption.get_many([photo]) == {photo: "第二版"}
    assert caption.stats()["captioned"] == 1


def test_get_many_empty_input():
    assert caption.get_many([]) == {}


def test_stats_counts(point):
    a = db.add_media(point["point_id"], "photo", "a.jpg", None, None, None, None)
    b = db.add_media(point["point_id"], "photo", "b.jpg", None, None, None, None)
    db.add_media(point["point_id"], "photo", "c.jpg", None, None, None, None)
    caption.save(a, "有描述")
    caption.fail(b, "取图失败")
    s = caption.stats()
    assert (s["photos"], s["captioned"], s["failed"], s["backlog"]) == (3, 1, 1, 1)


# ---------------- 接进精判 ----------------
def test_media_brief_carries_ai_caption(photo):
    caption.save(photo, "金色夕阳下的海岸线")
    assert db.get_media_brief([photo])[photo]["ai_caption"] == "金色夕阳下的海岸线"


def test_media_brief_hides_failed_caption(photo):
    caption.fail(photo, "取图失败")
    assert db.get_media_brief([photo])[photo]["ai_caption"] is None


def test_describe_puts_ai_caption_before_everything():
    # Jev 读的是一整句话，越靠前权重越高：唯一能回答「是不是日落」的是模型描述，
    # 用户写的 caption 可能只是「哈哈哈哈哈哈」
    d = jevrerank.describe({"ai_caption": "金色夕阳下的海岸线",
                            "caption": "哈哈哈哈哈哈", "point_name": "巴塞罗那"})
    assert d.startswith("金色夕阳下的海岸线")
    assert "哈哈哈哈哈哈" in d and "巴塞罗那" in d


def test_describe_falls_back_to_metadata_without_ai_caption():
    # 方案A 没跑过的图，行为必须和加这一层之前一模一样（方案C 兜底）
    assert jevrerank.describe({"ai_caption": None, "point_name": "西湖"}) == "西湖"


def test_describe_still_empty_when_nothing_known():
    assert jevrerank.describe({"ai_caption": "", "filename": "IMG_0001.jpg"}) == ""
