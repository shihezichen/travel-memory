#!/usr/bin/env python3
"""存量视频回填 —— 给 uploads/ 里已有的视频补 faststart 重封装 + 清晰度档位。

为什么需要它：转码钩子只在「上传成功」时触发，历史上传的视频既没有 faststart
（moov 在文件尾 → 起播要等整个文件下完），也没有低码率档位。这个脚本把存量补齐。

**可重复执行、幂等、能中断续跑**：
  - faststart：已经是 faststart 的文件直接跳过（纯 Python 读 atom，不调 ffmpeg）。
  - 档位：DB 里 status=done 且档位文件还在盘上的，跳过；只补缺的那些。
  - 中断（Ctrl-C / 断电）后重跑，已完成的部分不会重做，从没做完的那个继续。
  - 转码写的是 .partial.mp4 临时文件，成功才 rename，所以中断不会留下半截档位。

用法：
    # 先看看有多少活、预计多占多少盘，什么都不改
    .venv/bin/python scripts/backfill_transcode.py --dry-run

    # 拿 2 个文件小试
    .venv/bin/python scripts/backfill_transcode.py --limit 2

    # 全量回填（建议 nohup 挂后台，机器上还有别的服务，默认串行 1 个任务）
    nohup .venv/bin/python scripts/backfill_transcode.py > logs/backfill.log 2>&1 &

    # 只处理某一个
    .venv/bin/python scripts/backfill_transcode.py --media-id 42

    # 重做（含已 done 的档位）
    .venv/bin/python scripts/backfill_transcode.py --media-id 42 --force

选项 --jobs N 可以并行，但默认 1：这台机器上还跑着别的服务，别把 CPU 吃满。
"""
import os
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import transcode


def human(n):
    """字节数 -> 人类可读"""
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def work_for(media, upload_dir):
    """算出这条视频还有什么没做。返回 (源路径, 需要重封装?, 待转档位, 已完成档位)。"""
    src = upload_dir / media["filename"]
    if not src.exists():
        return src, False, [], []

    need_fs = (src.suffix.lower() in transcode.FASTSTART_SUFFIXES
               and not transcode.is_faststart(src))

    info = transcode.probe_video(src)
    plan = transcode.plan_variants(info.get("width"), info.get("height"))
    todo, done = [], []
    for label, w, h in plan:
        row = db.get_variant(media["id"], label)
        ok = (row and row["status"] == "done" and row["filename"]
              and (transcode.VARIANT_DIR / row["filename"]).exists())
        (done if ok else todo).append((label, w, h))
    return src, need_fs, todo, done


def main():
    ap = argparse.ArgumentParser(description="存量视频转码回填（幂等、可续跑）")
    ap.add_argument("--dry-run", action="store_true", help="只报告要做什么，不动文件")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条（0=不限）")
    ap.add_argument("--media-id", type=int, default=0, help="只处理指定 media id")
    ap.add_argument("--force", action="store_true", help="连已完成的档位一起重做")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行任务数（默认 1，机器上还有别的服务）")
    args = ap.parse_args()

    # 确保 media_variants 表/列存在（幂等）。脚本可能在服务没起过的机器上先跑。
    db.init_db()

    upload_dir = transcode.UPLOAD_DIR
    videos = db.list_all_videos()
    if args.media_id:
        videos = [v for v in videos if v["id"] == args.media_id]
        if not videos:
            print(f"没有 id={args.media_id} 的视频")
            return 1

    print(f"库里共 {len(videos)} 条视频，上传目录 {upload_dir}")
    print("-" * 68)

    pending, skipped, missing = [], 0, 0
    for v in videos:
        src, need_fs, todo, done = work_for(v, upload_dir)
        if not src.exists():
            missing += 1
            print(f"  ! id={v['id']:<5} 源文件不存在，跳过: {v['filename']}")
            continue
        if not args.force and not need_fs and not todo:
            skipped += 1
            continue
        pending.append((v, src, need_fs, todo, done))

    print(f"待处理 {len(pending)} 条；已完成跳过 {skipped} 条；源文件缺失 {missing} 条")

    if args.limit and len(pending) > args.limit:
        pending = pending[:args.limit]
        print(f"（--limit {args.limit}，本次只做前 {len(pending)} 条）")

    if not pending:
        print("没有要做的活。")
        return 0

    print("-" * 68)
    est = 0
    for v, src, need_fs, todo, done in pending:
        size = src.stat().st_size
        marks = []
        if need_fs:
            marks.append("faststart")
        marks += [t[0] for t in (todo if not args.force else
                                 [x for x in todo] + [x for x in done])]
        # 粗估：每档大致是源的 1/7 ~ 1/3，按 1/4 估个数量级
        est += int(size * 0.25) * max(1, len(todo))
        print(f"  id={v['id']:<5} {src.name}  {human(size):>7}  待做: {', '.join(marks) or '-'}")
    print(f"\n预计新增占盘约 {human(est)}（粗估，实测 720p 约为源的 1/7）")

    if args.dry_run:
        print("\n--dry-run，什么都没改。")
        return 0

    print("-" * 68)
    t0 = time.time()
    ok = fail = 0

    def run_one(item):
        v, src, _need_fs, _todo, _done = item
        started = time.time()
        r = transcode.process_media(v["id"], src, force=args.force)
        return v, r, time.time() - started

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = [pool.submit(run_one, it) for it in pending]
            results = [f.result() for f in as_completed(futs)]
    else:
        results = [run_one(it) for it in pending]

    for v, r, dt in results:
        bad = [x for x in r.get("variants", []) if x["status"] != "done"]
        if r.get("error") or bad:
            fail += 1
            print(f"  ✗ id={v['id']:<5} {dt:5.1f}s  "
                  f"{r.get('error') or bad[0].get('error', '')[:80]}")
        else:
            ok += 1
            labels = [x["label"] for x in r.get("variants", [])]
            print(f"  ✓ id={v['id']:<5} {dt:5.1f}s  "
                  f"faststart={'是' if r['faststart'] else '否'}  "
                  f"档位={','.join(labels) or '无（源已够小）'}")

    print("-" * 68)
    print(f"完成 {ok} 条，失败 {fail} 条，耗时 {time.time() - t0:.1f}s")
    print("失败的可以直接重跑本脚本（幂等，只会重做没成的那些）。")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
