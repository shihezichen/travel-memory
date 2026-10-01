"""照片画面描述（VLM caption）—— 建库时算一次，搜索时直接拿来喂 Jev。

为什么要有这一层：jevrerank 判的是「这张图该不该作为搜索结果返回」，可它
看不见图，只能读文字。原来喂给它的文字是 caption + 地点 + 行程 + 文件名拼的
（jevrerank.describe()），而那个 caption 是**用户自己写的** —— 实测 72 张照片里
50 条非空，内容却是「哈哈哈哈哈哈」这种。于是精判要么判出一堆 0.5，要么整批
退回 no_descriptions，这一层等于没开。CLIP 召回错的图，Jev 一张也救不下来。

所以补上规格书「模块2 方案A」：**建库阶段**用 Qwen2-VL-2B 给每张照片生成一句
中文画面描述，落到 media_captions 表。查询时零额外延迟 —— 搜索链路只多了一次
LEFT JOIN，一次模型推理都不跑。方案C（元数据拼接）保留为兜底：没生成出描述的
图，jevrerank 的行为和现在完全一样。

分工按 clipmodel.py 的老办法切：
  - 本模块跑在**主 venv**，只碰 SQLite，不 import torch，web 进程可以放心引用；
  - 真正的模型推理在 scripts/backfill_caption.py，跑在 .venv-ai（torch + CUDA）。

队列不是「上传时插一行 pending」，而是 backlog() 现算的差集 —— 这样
「模块关着的时候传上来的图」「表被清过的图」「换模型要重算的图」都不会漏，
不用维护一个可能和 media 表对不上的队列。代价只是一次 LEFT JOIN，几千行的量级
不值得为它建索引之外的东西。

关闭方式：TM_CAPTION_ENABLED=0。关掉之后 backlog() 恒为空（worker 空转退出），
已经生成的描述仍然照常参与精判 —— 开关管的是「还生不生」，不是「还用不用」。
"""
import logging
import os
import re

import db

log = logging.getLogger("travel.caption")

# 模型标识只是写进库里的一个名字，用来区分「这条描述是谁生成的」。
# 换模型时拿它挑出要重算的行（backfill_caption.py --remodel），所以别随手改。
MODEL_NAME = os.environ.get("TM_CAPTION_MODEL", "qwen2-vl-2b-instruct")

ENABLED = os.environ.get("TM_CAPTION_ENABLED", "1") != "0"

# 描述长度上限。Jev 吃的是一句话，不是一段散文：描述越长，和 query 真正相关的
# 那几个字占的比重越小，判出来反而更糊。40 字的生成目标 + 这里 120 字的硬截断，
# 留的是「模型偶尔话多」的余量，不是给它写作文的空间。
MAX_CHARS = int(os.environ.get("TM_CAPTION_MAX_CHARS", "120"))

# VLM 的开场白。Qwen2-VL 十句里有三句以「这张图片展示了」开头，留着不是错，
# 但它在每条描述里都出现，等于给所有候选加了同一段噪声 —— 对排序毫无贡献，
# 还挤占前面说的那点长度预算。
_NOUN = r"这是?一?[张幅副]?(?:图片?|照片|图像)"
_BOILERPLATE = re.compile(
    # 「这张图片展示了…」「这是一张照片，…」「图中显示了：…」「画面是…」
    r"^(?:" + _NOUN + r"[中里上]?(?:展示|显示|描绘|呈现|是)了?[：:，,]?"
    r"|" + _NOUN + r"[：:，,]"
    r"|图[片中里](?:展示|显示)了?[：:，,]?"
    r"|画面[中里]?(?:展示|显示|是)了?[：:，,]?"
    # 「主体是一片雪原…」—— 提示词里写了「主体是什么」，模型就照着这三个字
    # 起头（实测 72 条里 19 条）。它和上面那些开场白是同一类东西：条条都有，
    # 等于条条都没有，还占着最前面那点权重。
    r"|(?:" + _NOUN + r"|画面|照片|图[片中])?的?主体(?:是|为)[：:，,]?)")


# ---------------------------------------------------------------- 清洗

def clean(text: str) -> str:
    """模型原始输出 -> 入库的一句话。空白归一、去开场白、硬截断。

    截断按标点回退到最近的句读，而不是从中间剁断 —— 半句话会让 Jev 读出
    和原意相反的东西（「天空湛蓝无云，远处乌云」截在逗号前后意思正相反）。
    """
    if not text:
        return ""
    s = re.sub(r"\s+", " ", str(text).replace("\n", "，")).strip()
    s = s.strip("「」\"'` *#")
    prev = None
    while prev != s:                       # 偶尔会套两层：「这是一张图片，图中展示了…」
        prev = s
        s = _BOILERPLATE.sub("", s).lstrip("：:，, ")
    s = s.strip()
    if len(s) <= MAX_CHARS:
        return s
    cut = s[:MAX_CHARS]
    for p in ("。", "；", "，", ";", ",", " "):
        i = cut.rfind(p)
        if i >= MAX_CHARS // 2:            # 回退得太狠就不如硬截
            return cut[:i].strip()
    return cut.strip()


# ---------------------------------------------------------------- 读

def backlog(limit: int = None, retry_failed: bool = False,
            remodel: bool = False, media_ids=None) -> list:
    """还没生成描述的照片，[{media_id, filename}]，按 id 升序。

    failed 默认不重试：取图失败（文件被删、OSS 没配）重试一百次还是失败，
    watch 模式会把它变成一个永不停歇的死循环。要重试就显式 --retry-failed。

    media_ids 是「就要这几张」的手动口子（调 prompt 时反复重算同一张用），
    绕开全部过滤条件，也绕开 ENABLED —— 人都指名道姓了就别再替他判断。
    """
    args = []
    if media_ids:
        ids = [int(i) for i in media_ids]
        if not ids:
            return []
        where = ["m.type = 'photo'", "m.id IN (%s)" % ",".join("?" * len(ids))]
        args += ids
    else:
        if not ENABLED:
            return []
        cond = ["c.media_id IS NULL", "c.status = 'pending'"]
        if retry_failed:
            cond.append("c.status = 'failed'")
        if remodel:
            cond.append("c.model <> ?")
            args.append(MODEL_NAME)
        where = ["m.type = 'photo'", "(%s)" % " OR ".join(cond)]

    sql = ("SELECT m.id AS media_id, m.filename AS filename "
           "  FROM media m LEFT JOIN media_captions c ON c.media_id = m.id "
           " WHERE %s ORDER BY m.id" % " AND ".join(where))
    if limit:
        sql += " LIMIT %d" % int(limit)
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_many(media_ids) -> dict:
    """{media_id: caption}，只给生成成功的。没有的键直接不出现。"""
    ids = [int(i) for i in media_ids]
    if not ids:
        return {}
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT media_id, caption FROM media_captions "
            " WHERE status = 'done' AND caption IS NOT NULL AND caption <> '' "
            "   AND media_id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    finally:
        conn.close()
    return {r["media_id"]: r["caption"] for r in rows}


# ---------------------------------------------------------------- 写

def _upsert(media_id: int, status: str, caption=None, error=None):
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO media_captions (media_id, model, caption, status, error)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET
                 model=excluded.model, caption=excluded.caption,
                 status=excluded.status, error=excluded.error,
                 updated_at=datetime('now','localtime')""",
            (int(media_id), MODEL_NAME, caption, status, error))
        conn.commit()
    finally:
        conn.close()


def save(media_id: int, text: str) -> str:
    """清洗后落库，返回真正入库的那句话。洗完是空串就记 failed ——
    「模型回了个空」和「还没生成」必须分得开，否则 worker 每轮都会再捡起它。"""
    c = clean(text)
    if not c:
        log.info("media=%s 模型输出洗完是空的，原文：%r", media_id, str(text)[:120])
        _upsert(media_id, "failed", error="模型输出清洗后为空")
        return ""
    _upsert(media_id, "done", caption=c)
    return c


def fail(media_id: int, error: str):
    _upsert(media_id, "failed", error=str(error)[:500])


# ---------------------------------------------------------------- 状态

def stats() -> dict:
    conn = db.get_conn()
    try:
        photos = conn.execute(
            "SELECT COUNT(*) AS c FROM media WHERE type='photo'").fetchone()["c"]
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM media_captions "
                            "GROUP BY status").fetchall()
    finally:
        conn.close()
    by = {r["status"]: r["c"] for r in rows}
    done = by.get("done", 0)
    return {"enabled": ENABLED, "model": MODEL_NAME, "photos": photos,
            "captioned": done, "failed": by.get("failed", 0),
            "backlog": max(photos - done - by.get("failed", 0), 0)}
