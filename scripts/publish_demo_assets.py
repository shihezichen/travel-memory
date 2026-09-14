#!/usr/bin/env python3
"""把演示账号的媒体一次性发布到 OSS —— 之后 seed 只写库，本机一个文件都不留。

为什么要有这一步：原来 seed 每晚 3 点重跑时，会把 assets/demo-media/ 里的
39 个文件拷进 uploads/、逐个算缩略图、给视频转码，然后等 sync-oss.sh 再传一遍。
可素材根本没变过 —— 这些活天天重做一遍，纯浪费，还让本机凭空多背 30MB 素材池
和一份同样大小的 uploads/ 副本。

改成这样之后：
  - 媒体的家就是 OSS。本机 uploads/ 里没有 test_* 这些文件，也不需要有。
    config.media_url() 遇到本地不存在的文件会直接指向 OSS（见 _synced_to_oss
    里那个 except OSError），所以页面照常显示。
  - seed 每晚只剩几十条 INSERT，秒完。

文件名必须跟 seed 完全一致，所以两边都从 demo_dataset 取，谁都不许自己拼。

用法：
    python3 scripts/publish_demo_assets.py            # 缺什么传什么
    python3 scripts/publish_demo_assets.py --force    # 全部重传
    python3 scripts/publish_demo_assets.py --dry-run  # 只列要传什么
"""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media
import oss
import transcode
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_dataset as ds

# 跟 ops/sync-oss.sh 的 ONE_DAY 对齐。原片可能被 ensure_faststart 原地覆盖，
# 所以不能给 immutable。
CACHE_ONE_DAY = "public, max-age=86400"


def oss_has(key):
    try:
        oss.head_object(key)
        return True
    except oss.OSSError:
        return False


def put(key, path, dry):
    if dry:
        print("  [dry-run] 上传 %s (%.0f KB)" % (key, path.stat().st_size / 1024))
        return
    oss.put_object(key, path, cache_control=CACHE_ONE_DAY)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在的也重传")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ds.POOL.exists():
        print("素材池 %s 不存在，先跑 scripts/fetch_demo_assets.py" % ds.POOL)
        return 1
    if not oss.direct_upload_enabled():
        print("OSS 未配置（缺 ~/.ossutilconfig 或 media_base.txt）")
        return 1

    # 缩略图统一生成到临时目录：本机 uploads/thumbs 不该留 demo 的东西。
    tmp = Path(tempfile.mkdtemp(prefix="demo-publish-"))
    orig_thumb_dir = media.THUMB_DIR
    media.THUMB_DIR = tmp / "thumbs"
    media.THUMB_DIR.mkdir(parents=True, exist_ok=True)

    done = skipped = missing = 0
    try:
        # ---------- 照片 ----------
        for fname, pname, k in ds.all_photo_names():
            slug = ds.SLUG.get(pname)
            src = ds.POOL / slug / ("%02d.jpg" % (k + 1)) if slug else None
            if not src or not src.exists():
                print("  缺素材 %s (%s 第 %d 张)" % (fname, pname, k + 1))
                missing += 1
                continue

            key = "uploads/" + fname
            tkey = "uploads/thumbs/" + ds.thumb_name(fname)
            if not args.force and oss_has(key) and oss_has(tkey):
                skipped += 1
                continue

            staged = tmp / fname
            shutil.copyfile(src, staged)
            tname = media.make_thumb(staged, "photo")
            put(key, staged, args.dry_run)
            put(tkey, media.THUMB_DIR / tname, args.dry_run)
            staged.unlink(missing_ok=True)
            done += 1
            print("  ✓ %s" % fname)

        # ---------- 视频 ----------
        for pname, poolname, fname, shot, lat, lng in ds.VIDEOS:
            src = ds.POOL / poolname
            if not src.exists():
                print("  缺素材 %s" % poolname)
                missing += 1
                continue

            key = "uploads/" + fname
            tkey = "uploads/thumbs/" + ds.thumb_name(fname)
            if not args.force and oss_has(key) and oss_has(tkey):
                skipped += 1
                continue

            staged = tmp / fname
            shutil.copyfile(src, staged)
            # moov 挪到文件头，否则要等整片下完才起播。只做这一件事，
            # 不做多档位转码 —— demo 这两段一共 2MB 出头，不值得。
            transcode.ensure_faststart(staged)
            tname = media.make_thumb(staged, "video")
            put(key, staged, args.dry_run)
            put(tkey, media.THUMB_DIR / tname, args.dry_run)
            staged.unlink(missing_ok=True)
            done += 1
            print("  ✓ %s" % fname)
    finally:
        media.THUMB_DIR = orig_thumb_dir
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n上传 %d，跳过（已存在）%d，缺素材 %d" % (done, skipped, missing))
    if missing:
        print("缺的那些 seed 会跳过不写库，页面上少几张图而已，不影响其余部分。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
