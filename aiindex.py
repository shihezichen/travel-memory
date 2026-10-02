"""照片语义索引 —— 上传完算一次向量，搜索时拿向量比。

骨架照抄 transcode.py：同样的 ThreadPoolExecutor + _claim 防重入 + on_media_uploaded
一行挂钩。相同的问题用相同的形状解决，省得以后读代码的人要学两套。

和转码的区别只有一处，但很关键：**取图可以不落盘**。
照片归档到 OSS 之后本地 uploads 就空了（实测 media 表 72 张、本地只剩 17 张），
索引要的又只是 224px 的像素，所以回源时让 OSS 服务端先缩图再传回来 ——
实测原图 1.07MB、缩到 448 只有 44KB。3000 张全量回填因此是 130MB 而不是 3.2GB。

关闭方式：TM_AI_ENABLED=0，或者干脆不放模型文件。两种情况下 app 的行为
和没有这个模块时完全一样，搜索自动退回原来的关键词匹配。
"""
import io
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import db

log = logging.getLogger("travel.aiindex")

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"

MODEL_NAME = "chinese-clip-vit-b16"

# 回源时让 OSS 缩到多大。CLIP 吃 224，取 448 给 resize 留一倍插值余量，
# 代价是每张多二十几 KB —— 比起拉原图的 1MB 仍然可以忽略。
OSS_PROCESS = os.environ.get("TM_AI_OSS_PROCESS", "image/resize,l_448")

# 索引并发。onnxruntime 内部已经开了 TRAVEL_CLIP_THREADS 个线程（默认 4），
# 这里再并发就是乘法。默认 2 条意味着最多占 8 核，给 ffmpeg 留足余量 ——
# 转码是用户正在等的，索引不是。
MAX_CONCURRENT = int(os.environ.get("TM_AI_CONCURRENCY", "2"))

ENABLED = os.environ.get("TM_AI_ENABLED", "1") != "0"

_POOL = None
_POOL_LOCK = threading.Lock()
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT,
                                       thread_name_prefix="aiindex")
        return _POOL


def _claim(media_id: int) -> bool:
    with _INFLIGHT_LOCK:
        if media_id in _INFLIGHT:
            return False
        _INFLIGHT.add(media_id)
        return True


def _release(media_id: int):
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(media_id)


def available() -> bool:
    """模型齐不齐、开关开没开。调用方据此决定走语义检索还是退回关键词。"""
    if not ENABLED:
        return False
    try:
        import clipmodel
        return clipmodel.available()
    except Exception:                                     # noqa: BLE001
        return False


# ---------------------------------------------------------------- 取图

def load_image(filename: str, process: str = None):
    """本地有就读本地，没有就让 OSS 缩好了再传回来。两条路都失败才抛。

    process 是回源时的 x-oss-process。默认走 CLIP 要的 448；喂 VLM 生成描述时
    调用方会要一张更大的（见 scripts/backfill_caption.py）—— 224 的图只够
    CLIP 算向量，说不出画面细节。
    """
    from PIL import Image

    local = UPLOAD_DIR / filename
    if local.exists():
        return Image.open(local).convert("RGB"), 0

    import httpx
    import oss
    if not oss.conf().ok:
        raise FileNotFoundError("本地无此文件且 OSS 未配置: %s" % filename)

    url = oss.sign_url("GET", "uploads/" + filename,
                       params={"x-oss-process": process or OSS_PROCESS},
                       expires_in=600)
    if not url:
        raise FileNotFoundError("签不出 OSS URL: %s" % filename)
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB"), len(r.content)


# ---------------------------------------------------------------- 写库

def _mark(media_id: int, status: str, vec=None, error: str = None):
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO media_embeddings (media_id, model, dim, vec, status, error)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET
                 model=excluded.model, dim=excluded.dim, vec=excluded.vec,
                 status=excluded.status, error=excluded.error,
                 updated_at=datetime('now','localtime')""",
            (media_id, MODEL_NAME, 0 if vec is None else len(vec) // 4,
             vec, status, error))
        conn.commit()
    finally:
        conn.close()


def index_one(media_id: int, filename: str) -> bool:
    """算一张照片的向量并落库。任何失败都写成 failed，不往上抛。"""
    if not _claim(media_id):
        return False
    try:
        import clipmodel
        _mark(media_id, "running")
        img, nbytes = load_image(filename)
        vec = clipmodel.encode_images([img])[0]
        _mark(media_id, "done", vec=clipmodel.pack(vec))
        log.info("索引完成 media=%s %s (回源 %d B)", media_id, filename, nbytes)
        return True
    except Exception as e:                                # noqa: BLE001
        log.warning("索引失败 media=%s %s: %s", media_id, filename, e)
        try:
            _mark(media_id, "failed", error=str(e)[:500])
        except Exception:                                 # noqa: BLE001
            pass
        return False
    finally:
        _release(media_id)
        _invalidate_cache()


def enqueue(media_id: int, filename: str) -> bool:
    if not available():
        return False
    _pool().submit(index_one, media_id, filename)
    return True


def on_media_uploaded(media_id: int, filename: str, mtype: str) -> bool:
    """**上传链路的对接入口** —— 和 transcode.on_media_uploaded 并排放一行即可。

    只处理照片；视频抽帧是二期的事。绝不抛异常：上传已经成功了，
    索引是锦上添花，不能反过来把上传搞失败。
    """
    try:
        if mtype != "photo":
            return False
        return enqueue(media_id, filename)
    except Exception as e:                                # noqa: BLE001
        log.warning("on_media_uploaded 失败 media=%s: %s", media_id, e)
        return False


# ---------------------------------------------------------------- 检索

# 向量缓存。3000 张只有 6MB，全读进来不心疼；真正省掉的是每次搜索
# 从 SQLite 拉几 MB BLOB 的开销。任何一次索引写入都把它作废，简单且不会读到陈旧数据。
_CACHE = None
_CACHE_LOCK = threading.Lock()


def _invalidate_cache():
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def _load_vectors():
    """(media_ids, user_ids, trip_ids, filenames, 矩阵)。user_id 一起带出来，
    是为了在矩阵里直接按用户过滤 —— 每个路由都 require_owner，这里漏一层就是越权。
    trip_id 则是给「只搜这几条行程」用的：按行程过滤发生在矩阵这一层（布尔掩码），
    不是先全局 topk 再把不要的行程扔掉 —— 后者会让「只搜北欧那条」拿到不足 limit 条。"""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE

    import numpy as np
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT e.media_id AS media_id, e.vec AS vec, m.filename AS filename,
                      t.user_id AS user_id, t.id AS trip_id
                 FROM media_embeddings e
                 JOIN media  m ON m.id = e.media_id
                 JOIN points p ON p.id = m.point_id
                 JOIN trips  t ON t.id = p.trip_id
                WHERE e.status = 'done' AND e.vec IS NOT NULL
                ORDER BY e.media_id""").fetchall()
    finally:
        conn.close()

    if not rows:
        out = ([], [], [], [], np.zeros((0, 512), dtype="float32"))
    else:
        M = np.frombuffer(b"".join(r["vec"] for r in rows),
                          dtype="float32").reshape(len(rows), -1)
        out = ([r["media_id"] for r in rows],
               [r["user_id"] for r in rows],
               [r["trip_id"] for r in rows],
               [r["filename"] for r in rows],
               M)

    with _CACHE_LOCK:
        _CACHE = out
    return out


# 查询向量缓存。一次搜索的耗时里，文本编码这一趟 ONNX 实测 60~200ms，
# 而同一个搜索词被反复编码是常态 —— 改一下行程范围、拖一下相关度线之后
# 再搜一次、翻完一遍回头再搜，问的都是同一句话。词条就几十个字，
# 存 256 条也只有几百 KB。
#
# 不设 TTL，也不用随索引作废：同一段文字经同一个模型编出来的向量是定值，
# 和库里有哪些照片无关（那是 _CACHE 那一层的事）。
_TEXT_CACHE = {}
_TEXT_CACHE_MAX = int(os.environ.get("TM_AI_QUERY_CACHE", "256"))
_TEXT_CACHE_LOCK = threading.Lock()


def _encode_query(query: str):
    """把搜索词编成向量，同一句话只编一次。"""
    import clipmodel
    with _TEXT_CACHE_LOCK:
        v = _TEXT_CACHE.get(query)
    if v is not None:
        return v
    v = clipmodel.encode_texts([query])[0]
    with _TEXT_CACHE_LOCK:
        # 满了整批扔掉而不是按 LRU 淘汰：这是纯粹的加速缓存，扔错了最坏
        # 也只是多编一次。为此引进一个 OrderedDict 和一套淘汰逻辑不划算。
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.clear()
        _TEXT_CACHE[query] = v
    return v


def clear_query_cache():
    """测试用。生产上没人调 —— 向量不会过期，见 _TEXT_CACHE 的注释。"""
    with _TEXT_CACHE_LOCK:
        _TEXT_CACHE.clear()


def search(user_id: int, query: str, limit: int = 30, min_score: float = 0.0,
           trip_ids=None):
    """语义检索。返回 [{media_id, filename, score}]，已按用户隔离、已去重。

    去重是必须的：media 表里存在同 filename 的重复行（实测 bcn_a_1.jpg 两条），
    不去重的话搜索结果会成片出现同一张图，3000 张规模下更明显。

    trip_ids 给定时只在这几条行程里搜；None 或空集合 = 全部行程。
    传了一堆别人的行程 id 也无妨 —— 用户掩码和行程掩码是**与**的关系，
    越不过 user_id 那一关。
    """
    if not available() or not query.strip():
        return []

    import numpy as np

    media_ids, user_ids, row_trips, filenames, M = _load_vectors()
    if len(media_ids) == 0:
        return []

    want = set(int(t) for t in trip_ids) if trip_ids else None
    mine = np.array([u == user_id and (want is None or t in want)
                     for u, t in zip(user_ids, row_trips)])
    if not mine.any():
        return []

    q = _encode_query(query)
    sim = M @ q
    sim[~mine] = -2.0                       # 别人的照片直接排到最后，不参与 topk

    out, seen = [], set()
    for j in np.argsort(-sim):
        s = float(sim[j])
        if s <= -2.0 or s < min_score:
            break
        if filenames[j] in seen:            # 同一张图只出一次
            continue
        seen.add(filenames[j])
        out.append({"media_id": media_ids[j], "filename": filenames[j],
                    "score": round(s, 4)})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 状态

def stats() -> dict:
    conn = db.get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM media WHERE type='photo'").fetchone()["c"]
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM media_embeddings "
            "GROUP BY status").fetchall()
    finally:
        conn.close()
    by = {r["status"]: r["c"] for r in rows}
    return {"enabled": available(), "model": MODEL_NAME, "photos": total,
            "indexed": by.get("done", 0), "pending": by.get("pending", 0),
            "running": by.get("running", 0), "failed": by.get("failed", 0),
            "inflight": len(_INFLIGHT)}
