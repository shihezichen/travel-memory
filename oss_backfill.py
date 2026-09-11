"""OSS 回源存档 —— 把「浏览器直传上去的对象」慢慢拉回本机。

直传解决的是上行速度（见 oss.py），但代价是文件一开始只存在于 OSS：
本机没有原件，就做不出缩略图、读不到 EXIF、转不了清晰度档位，更谈不上备份。
所以每个直传成功的对象都会在 oss_backfill 表里排一条，由这里的后台线程
按顺序拉回 uploads/，然后补完那几件收尾的事。

**它慢，是故意的**：用户那一侧在 complete 的瞬间就已经看到媒体了（播放直接走
OSS），回源只是存档，不该和任何人的请求抢带宽。并发度默认 1。

失败不会丢：状态落在库里，重启后 reset_running() 把崩在半路的复位成 pending，
下次启动接着拉；单条最多重试 MAX_ATTEMPTS 次，之后留在 error 里等人来看。
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path

import db
import media as media_mod
import oss
import transcode

log = logging.getLogger("travel.oss.backfill")

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"

# 回源并发度。1 就够 —— 这是后台存档，不是用户在等的东西，把带宽让给正在播放的人。
MAX_CONCURRENT = int(os.environ.get("TRAVEL_OSS_BACKFILL_WORKERS", "1"))
MAX_ATTEMPTS = int(os.environ.get("TRAVEL_OSS_BACKFILL_ATTEMPTS", "5"))

_POOL = None
_POOL_LOCK = threading.Lock()
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()


def _pool():
    """懒加载线程池：import 本模块不该凭空起线程（测试里尤其别）。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT,
                                       thread_name_prefix="oss-backfill")
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


def inflight_count() -> int:
    with _INFLIGHT_LOCK:
        return len(_INFLIGHT)


def _touch_from_oss(path: Path, last_modified: str):
    """把本地文件的 mtime 对齐到 OSS 对象的 Last-Modified（且不晚于同步水位）。

    两个理由，都很实在：
      1. config._synced_to_oss() 用 mtime 和同步水位比大小来判断「这份是不是已经
         在云上了」。拉回来的这份**本来就是云上那份**，用下载时刻当 mtime 会让它
         被误判成「刚落盘还没同步」，于是页面把链接指回本机，媒体流量又挤回隧道 ——
         正好是直传要绕开的那条路。
      2. ops/sync-oss.sh 用 mtime+size 决定要不要重传。对齐之后它会正确地跳过，
         不会把刚拉下来的文件再原样推上去一遍。

    为什么还要再压到水位以下：直传的对象是**刚刚**传上去的，Last-Modified 就是
    几秒前，仍然晚于上一趟同步的水位，于是第 1 条那个误判照样发生（只是窗口从
    「下一趟同步」缩短到同一个量级）。而「这份已经在云上」是我们刚用 HEAD 核实过的
    事实，直接按定义把它记成「不晚于任何一次同步」最诚实，也不会让 sync-oss 误以为
    本地更新（它只在本地更新时才重传，更旧一律跳过）。
    """
    if not last_modified:
        return
    try:
        ts = parsedate_to_datetime(last_modified).timestamp()
    except Exception as e:
        log.debug("解析 Last-Modified 失败 %s: %s", path, e)
        return
    wm = _sync_watermark()
    if wm is not None:
        ts = min(ts, wm - 1)
    try:
        os.utime(path, (ts, ts))
    except OSError as e:
        log.debug("对齐 mtime 失败 %s: %s", path, e)


def _sync_watermark():
    """读 ops/sync-oss.sh 写的同步水位（秒）。没有就返回 None。"""
    try:
        return float((BASE / "logs" / "oss-sync-watermark")
                     .read_text(encoding="utf-8").strip())
    except Exception:
        return None


def process_one(media_id: int) -> dict:
    """**同步**回源一条：下载 → 缩略图 → EXIF → 丢转码。返回一份结果摘要。"""
    row = db.get_oss_backfill(media_id)
    if not row:
        return {"ok": False, "reason": "队列里没有这条"}
    if int(row.get("attempts") or 0) > MAX_ATTEMPTS:
        db.finish_oss_backfill(media_id, ok=False, error="重试次数用尽")
        return {"ok": False, "reason": "重试次数用尽"}

    m = db.get_media(media_id)
    if not m:
        # 媒体已经被删了。外键是 ON DELETE CASCADE，正常情况下这条也没了；
        # 走到这儿说明是并发删除，直接当完成，别一直重试一个不存在的东西。
        db.finish_oss_backfill(media_id, ok=True)
        return {"ok": True, "reason": "媒体已删除"}

    dest = UPLOAD_DIR / m["filename"]
    mtype = row.get("mtype") or m["type"]
    try:
        if dest.exists() and (not row.get("filesize")
                              or dest.stat().st_size == int(row["filesize"])):
            log.info("回源跳过（本地已有）media=%s %s", media_id, dest.name)
        else:
            head = oss.head_object(row["oss_key"])
            n = oss.download_to(row["oss_key"], dest,
                                expect_size=row.get("filesize") or head["size"])
            _touch_from_oss(dest, head.get("last_modified"))
            log.info("回源完成 media=%s %s (%.1f MB)", media_id, dest.name,
                     n / (1024 * 1024))
    except Exception as e:
        db.finish_oss_backfill(media_id, ok=False, error=str(e))
        log.warning("回源失败 media=%s: %s", media_id, e)
        return {"ok": False, "reason": str(e)}

    # 拿到本地文件了，把上传链路里本来就该做的收尾补上。
    # 每一步都单独 try —— 缩略图做不出来不该拖累 EXIF，EXIF 读不到不该拖累转码。
    thumb = None
    if not m.get("thumb_filename"):
        try:
            thumb = media_mod.make_thumb(dest, mtype)
        except Exception as e:
            log.warning("回源后生成缩略图失败 media=%s: %s", media_id, e)
    exif_time = exif_lat = exif_lng = None
    if mtype == "photo":
        try:
            exif_time, exif_lat, exif_lng = media_mod.read_exif(dest)
        except Exception as e:
            log.warning("回源后读 EXIF 失败 media=%s: %s", media_id, e)
    try:
        db.update_media_after_backfill(media_id, thumb, exif_time, exif_lat, exif_lng)
    except Exception as e:
        log.warning("回源后写库失败 media=%s: %s", media_id, e)

    db.finish_oss_backfill(media_id, ok=True)
    # 转码放在最后、且在标记完成之后：它自己有队列和重试，失败不该让回源重来一遍
    # （重来 = 再下载一次几百 MB）。
    transcode.on_media_uploaded(media_id, dest, mtype)
    return {"ok": True, "thumb": thumb, "path": str(dest)}


def enqueue(media_id: int, oss_key: str, filesize: int = None, mtype: str = None):
    """排队 + 立刻丢进后台。返回 True = 已入队。绝不抛异常（上传已经成功了）。"""
    try:
        db.enqueue_oss_backfill(media_id, oss_key, filesize, mtype)
    except Exception as e:
        log.warning("回源入队写库失败 media=%s: %s", media_id, e)
        return False
    return _submit(media_id)


def _submit(media_id: int) -> bool:
    if not _claim(media_id):
        return False

    def _worker():
        try:
            if db.take_oss_backfill(media_id):
                process_one(media_id)
        except Exception as e:                     # 后台线程绝不能抛出去
            log.exception("回源任务异常 media=%s: %s", media_id, e)
        finally:
            _release(media_id)

    try:
        _pool().submit(_worker)
    except Exception as e:
        _release(media_id)
        log.warning("回源任务入队失败 media=%s: %s", media_id, e)
        return False
    return True


def resume_pending() -> int:
    """进程启动时调一次：把崩在 running 的复位，再把所有 pending 重新丢进队列。"""
    try:
        db.reset_running_oss_backfill()
        rows = db.list_oss_backfill(status="pending", limit=500)
    except Exception as e:
        log.warning("回源队列恢复失败: %s", e)
        return 0
    n = 0
    for r in rows:
        if _submit(int(r["media_id"])):
            n += 1
    if n:
        log.info("回源队列恢复：重新排入 %d 条", n)
    return n


def queue_status() -> dict:
    """给 /health 和运维脚本看的一眼摘要。"""
    out = {"pending": 0, "running": 0, "error": 0, "done": 0, "inflight": inflight_count()}
    try:
        for r in db.list_oss_backfill(limit=10000):
            k = r.get("status") or "pending"
            if k in out:
                out[k] += 1
    except Exception:
        pass
    return out
