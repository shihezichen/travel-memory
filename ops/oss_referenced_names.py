#!/usr/bin/env python3
"""打印数据库里被引用的全部媒体文件名，一行一个。ops/sync-oss.sh --prune 用它判孤儿。

为什么不能按「本地有没有这个文件」来判：演示账号的媒体只存在 OSS 上，本机
uploads/ 里一个都没有（见 scripts/publish_demo_assets.py）。按本地存在与否
去 prune，第一趟就会把 39 个 demo 对象全删了。

只打印文件名、不含目录 —— 缩略图和转码档位分别落在 thumbs/ 和 variants/ 下，
但名字本身是唯一的，比对时把云端 key 的目录前缀剥掉就行。

出错一律非零退出，让调用方放弃 prune：清单不全 = 把还在用的对象当孤儿删掉。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


def main():
    names = set()
    conn = db.get_conn()
    try:
        for row in conn.execute("SELECT filename, thumb_filename FROM media"):
            if row["filename"]:
                names.add(row["filename"])
            if row["thumb_filename"]:
                names.add(row["thumb_filename"])
        for row in conn.execute("SELECT filename FROM media_variants"):
            if row["filename"]:
                names.add(row["filename"])
    finally:
        conn.close()

    if not names:
        print("引用清单为空 —— 库里一条媒体都没有？拒绝输出", file=sys.stderr)
        return 1

    sys.stdout.write("\n".join(sorted(names)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
