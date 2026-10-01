"""给存量照片补语义向量 —— 套路和 scripts/backfill_transcode.py 一样。

断点续跑：只挑 media_embeddings 里没有 done 记录的。中途 Ctrl-C、机器重启、
OSS 抽风，重新跑一遍就接着干，不会重复算已经算好的。

3000 张的实测预期：编码 85ms/张 ≈ 4.2 分钟，OSS 回源约 44KB/张 ≈ 130MB。
真正的瓶颈是回源的网络往返，所以取图开了并发、推理仍然串行（onnxruntime
内部已经多线程，再并发只会互相抢核）。

    .venv/bin/python scripts/backfill_clip.py            # 跑全部缺失的
    .venv/bin/python scripts/backfill_clip.py --limit 50 # 先试 50 张
    .venv/bin/python scripts/backfill_clip.py --redo     # 连 failed 的一起重算
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import aiindex                                            # noqa: E402
import clipmodel                                          # noqa: E402
import db                                                 # noqa: E402


def pending(redo: bool, limit: int):
    conn = db.get_conn()
    try:
        cond = "e.status IS NULL OR e.status != 'done'" if redo else "e.status IS NULL"
        rows = conn.execute(
            "SELECT m.id AS id, m.filename AS filename "
            "  FROM media m LEFT JOIN media_embeddings e ON e.media_id = m.id "
            " WHERE m.type = 'photo' AND (%s) ORDER BY m.id" % cond).fetchall()
    finally:
        conn.close()
    return [(r["id"], r["filename"]) for r in rows][:limit] if limit else \
           [(r["id"], r["filename"]) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true", help="连 failed 的一起重算")
    ap.add_argument("--fetch-workers", type=int, default=6,
                    help="OSS 回源并发。推理是串行的，这里只并行取图")
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    if not aiindex.available():
        sys.exit("模型不可用：检查 models/chinese-clip-vit-b16 和 TM_AI_ENABLED")

    todo = pending(args.redo, args.limit)
    print("待索引 %d 张" % len(todo), flush=True)
    if not todo:
        return

    t0 = time.time()
    ok = fail = 0
    fetched_bytes = 0
    pool = ThreadPoolExecutor(max_workers=args.fetch_workers)

    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]

        # 取图并发（网络等待），推理串行（CPU）
        def fetch(item):
            mid, fn = item
            try:
                img, nb = aiindex.load_image(fn)
                return mid, fn, img, nb, None
            except Exception as e:                        # noqa: BLE001
                return mid, fn, None, 0, e

        got = list(pool.map(fetch, chunk))
        imgs, keep = [], []
        for mid, fn, img, nb, err in got:
            if err is not None:
                print("  [skip] %s: %s" % (fn, err), flush=True)
                aiindex._mark(mid, "failed", error=str(err)[:500])
                fail += 1
                continue
            imgs.append(img)
            keep.append((mid, fn))
            fetched_bytes += nb

        if not imgs:
            continue
        vecs = clipmodel.encode_images(imgs)
        for (mid, _fn), v in zip(keep, vecs):
            aiindex._mark(mid, "done", vec=clipmodel.pack(v))
            ok += 1

        done = i + len(chunk)
        el = time.time() - t0
        print("  %d/%d  成功 %d 失败 %d  用时 %.0fs  预计剩余 %.0fs"
              % (done, len(todo), ok, fail, el, el / done * (len(todo) - done)),
              flush=True)

    aiindex._invalidate_cache()
    el = time.time() - t0
    print("\n[done] 成功 %d / 失败 %d  用时 %.1fs (%.0f ms/张)"
          % (ok, fail, el, el / max(ok + fail, 1) * 1000))
    print("[oss] 回源共 %.1f MB" % (fetched_bytes / 1e6))
    print("[stats]", aiindex.stats())


if __name__ == "__main__":
    main()
