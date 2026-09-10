#!/usr/bin/env python3
"""回收过期的分片上传会话（断点续传的临时文件）。

大文件走分片上传时，还没合并的分片落在 `uploads/.chunks/<session_id>/`。
正常传完会立刻清掉；但用户中途关掉浏览器再也不回来的话，这些分片就留在盘上了。
这个脚本把「超过 N 小时没有任何动静」的会话连同临时分片一起删掉，默认 24 小时。

做的事：
  1. 删 `upload_sessions` / `upload_chunks` 里过期的行
  2. 删对应的 `uploads/.chunks/<session_id>/` 目录
  3. 顺手清掉 `.chunks/` 下的孤儿目录（库里已经没有对应会话了）

不碰任何已经合并完成的媒体文件，也不碰 media 表。

用法：
    .venv/bin/python scripts/prune_upload_sessions.py             # 清 24 小时前的
    .venv/bin/python scripts/prune_upload_sessions.py --hours 6
    .venv/bin/python scripts/prune_upload_sessions.py --dry-run   # 只看会删什么

服务进程自己也会在「新建上传会话」时顺手清一次（每小时最多一次），
所以这个定时器是可选的 —— 装上只是让长期没人上传的机器也能及时回收。
"""
import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%F %T"), msg), flush=True)


def main():
    ap = argparse.ArgumentParser(description="清理过期的分片上传会话")
    ap.add_argument("--hours", type=int, default=24,
                    help="多久没动静算过期（默认 24 小时）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不删")
    args = ap.parse_args()

    import db      # 延迟 import：--help 不该去开数据库
    import app as app_mod

    stale = db.list_stale_upload_sessions(args.hours)
    if args.dry_run:
        for s in stale:
            log(f"[dry-run] 会将清理 {s['session_id']} "
                f"（{s['filename']}，最后活动 {s['updated_at']}，状态 {s['status']}）")
        log(f"[dry-run] 共 {len(stale)} 个过期会话")
        return 0

    stat = app_mod.prune_upload_sessions(hours=args.hours)
    log(f"已清理过期会话 {stat['sessions']} 个，孤儿分片目录 {stat['orphan_dirs']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
