"""删媒体时把字节真正清掉 —— 本地磁盘和 OSS 两边。

为什么单独成一个模块：删除入口有六个（app.py 的删行程/删点位/删媒体，
agent_api.py 的同样三个），原来每个都自己写一遍 unlink。写六遍的结果就是六份
各不相同的实现 —— 2026-09-15 清点时发现 ai_delete_point 压根不删文件、
ai_delete_trip 和 ai_delete_media 漏删 variants/，OSS 上因此攒了一批
「media 表里查无此行、对象却还占着空间」的孤儿。收敛成一处之后，
补一个洞就是所有入口一起补上。

本地删同步做（unlink 是微秒级），OSS 删异步做：一次删行程可能牵扯上百个对象，
每个都是一趟 HTTP，串在请求里能拖十几秒。删不掉也不让它影响响应 ——
最坏结果只是留个孤儿，跟改之前一样，不会更糟。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import oss

log = logging.getLogger("travel.purge")

# OSS 删除的并发度。这是后台清理，不是用户在等的东西，别跟正在播放的人抢。
MAX_CONCURRENT = 4

_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT,
                                       thread_name_prefix="oss-purge")
        return _POOL


def _oss_ready() -> bool:
    """有没有配 AK。没配就只删本地，静默跳过 —— 本机开发环境是常态，不该刷日志。"""
    try:
        return bool(oss.conf().ok)
    except Exception:
        return False


def oss_keys(originals=(), thumbs=(), variants=()):
    """这批文件在 OSS 上对应的 key。目录前缀跟 ops/sync-oss.sh 传上去时一致。"""
    keys = ["uploads/" + n for n in originals if n]
    keys += ["uploads/thumbs/" + n for n in thumbs if n]
    keys += ["uploads/variants/" + n for n in variants if n]
    return keys


def _delete_from_oss(keys):
    # 全项目没配 logging，root logger 停在 WARNING —— info 是看不见的。
    # 所以顺利时一声不吭，只有删不掉（＝留下孤儿）才值得占一行日志。
    failed = []
    for k in keys:
        try:
            oss.delete_object(k)      # 它自己吞掉 OSSError 并 log.warning
        except Exception as e:        # noqa: BLE001 — 后台线程，绝不能往上抛
            failed.append(k)
            log.warning("OSS 删除异常 key=%s: %s", k, e)
    if failed:
        log.warning("OSS 清理有 %d/%d 个对象没删掉，会变成孤儿：%s",
                    len(failed), len(keys), ", ".join(failed[:5]))


def purge(upload_dir: Path, thumb_dir: Path,
          originals=(), thumbs=(), variants=(), sync_oss: bool = False):
    """删掉这批文件的本地副本，并把 OSS 上的同名对象排进后台删除。

    originals / thumbs / variants 都是**不含目录的文件名**，跟 media 表里存的一样。
    sync_oss=True 时同步等 OSS 删完（测试用；生产走后台，别在请求里等）。
    """
    originals = [n for n in originals if n]
    thumbs = [n for n in thumbs if n]
    variants = [n for n in variants if n]

    for n in originals:
        (upload_dir / n).unlink(missing_ok=True)
    for n in thumbs:
        (thumb_dir / n).unlink(missing_ok=True)
    for n in variants:
        (upload_dir / "variants" / n).unlink(missing_ok=True)

    keys = oss_keys(originals, thumbs, variants)
    if not keys or not _oss_ready():
        return 0

    if sync_oss:
        _delete_from_oss(keys)
    else:
        _pool().submit(_delete_from_oss, keys)
    return len(keys)


def purge_media_row(upload_dir: Path, thumb_dir: Path, m, variant_names=()):
    """删单条 media 的所有字节。m 是 media 表的一行（dict 或 sqlite3.Row）。"""
    return purge(upload_dir, thumb_dir,
                 originals=[m["filename"]],
                 thumbs=[m["thumb_filename"]] if m["thumb_filename"] else [],
                 variants=variant_names)


def purge_file_pairs(upload_dir: Path, thumb_dir: Path, pairs, variant_names=()):
    """删一批媒体。pairs 是 [(filename, thumb_filename), ...]，
    也就是 db.get_trip_media_files() / get_point_media_files() 的返回形状。"""
    return purge(upload_dir, thumb_dir,
                 originals=[f for f, _t in pairs],
                 thumbs=[t for _f, t in pairs if t],
                 variants=variant_names)
