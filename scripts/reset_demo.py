#!/usr/bin/env python3
"""把演示账号（默认 test）恢复成出厂状态 —— 当天所有改动一律不保留。

公开演示账号谁都能登、谁都能改，一天下来数据就面目全非了。
这个脚本设计成每天定时跑一次（推荐 03:00），把它整个抹掉重建。

做的事：
  1. 踢掉该账号所有登录会话（别人正开着的页面下次操作会被要求重新登录）
  2. 清掉该账号在各张附属表里的痕迹：偏好、生成记录、幂等键、审计日志、点元数据
     **但 API Key 保留不删** —— 重置的是演示内容，不是吊销对外凭证。详见下面
     main() 里那段注释。
  3. 调用 seed_test_user.py 重建行程 / 地点 / 照片 / 描述（只写库；媒体在 OSS 上，
     由 scripts/publish_demo_assets.py 一次性发布，这里不碰文件）
  4. 重置密码为出厂值，并把「上次打开的行程」指回第一个

用法：
    python3 scripts/reset_demo.py            # 重置默认演示账号 test
    DEMO_USER=其他账号名 python3 scripts/reset_demo.py   # 换个账号（默认就是 test）

想关掉这个功能，把定时器停掉即可，脚本本身不会被任何请求触发。
"""
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import db  # noqa: E402

DEMO_USER = os.environ.get("DEMO_USER", "test")


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%F %T"), msg), flush=True)


def main():
    u = db.get_user_by_username(DEMO_USER)
    if not u:
        # 账号还不存在：直接交给 seed 脚本从零建
        log("演示账号 %s 不存在，走全新创建" % DEMO_USER)
    else:
        uid = u["id"]
        c = db.get_conn()
        # 会话：不清的话，别人已经登着的页面能一直用下去
        n_sess = c.execute("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount
        # 附属表。generations / point_meta 挂在 trip/point 上，seed 会删 trip 和 point，
        # 但不一定级联，所以这里先按 user 反查着删干净，免得留下孤儿行。
        c.execute(
            "DELETE FROM point_meta WHERE point_id IN "
            "(SELECT p.id FROM points p JOIN trips t ON t.id=p.trip_id WHERE t.user_id=?)",
            (uid,))
        c.execute(
            "DELETE FROM generations WHERE trip_id IN (SELECT id FROM trips WHERE user_id=?)",
            (uid,))
        c.execute(
            "DELETE FROM idempotency_keys WHERE api_key_id IN "
            "(SELECT id FROM api_keys WHERE user_id=?)", (uid,))
        # ---- API Key：不删 ----
        # 2026-09-16 刚哥明确要求：重置演示账号时，**这个账号已经发出去的 API Key
        # 必须保留**。原来自动化的 OpenClaw / companion 之类接在这台演示账号上，
        # 每晚 3 点被重置删掉一次，第二天调用方全部 401，而调用方那边根本不知道
        # 自己手里的 key 是"因为演示账号被重置"才失效的 —— 排查起来极其费劲。
        #
        # 所以 api_keys 这张表在这里碰都不碰。它跟"行程数据"是两码事：行程是给人
        # 看的演示内容，key 是对外签出去的凭证。重置演示内容不等于吊销凭证。
        #
        # 想作废某把 key，走正规途径：页面上的吊销，或者
        # UPDATE api_keys SET revoked=1 WHERE id=?。
        n_keys = c.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=?",
                           (uid,)).fetchone()[0]
        c.execute("DELETE FROM audit_log WHERE user_id=?", (uid,))
        c.execute("DELETE FROM user_prefs WHERE user_id=?", (uid,))
        c.commit()
        c.close()
        log("已清理：%d 个会话，以及偏好/生成/审计/点元数据（保留 %d 个 API Key）"
            % (n_sess, n_keys))

    # 先确保 OSS 上素材齐全，再重建库里的行。
    #
    # 为什么这步不能省：演示账号的媒体只存在 OSS 上，而「页面上删掉一张照片」现在
    # 会连 OSS 对象一起删（见 media_purge.py）。谁在演示账号里点了删除，
    # 那个对象就真没了 —— 只重建数据库行的话，行指向一个不存在的对象，
    # 演示账号从此永久缺一张图。所以每晚顺手补齐。
    #
    # 代价很小：publish 脚本对每个对象先 HEAD 一下，齐全时一个字节都不传。
    # 素材池不在这台机器上（或者 OSS 没配）时它会自己报错退出，那就跳过 ——
    # 补不齐总比整个重置失败好，库里的行照样重建。
    pub = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "publish_demo_assets.py")],
                         capture_output=True, text=True, cwd=ROOT)
    if pub.returncode == 0:
        log("  素材检查：" + (pub.stdout.strip().splitlines() or ["（无输出）"])[-1])
    else:
        log("  素材检查跳过（%s）" % (pub.stdout.strip().splitlines() or [pub.stderr.strip()[-200:]])[-1])

    # 重建行程与照片（seed 幂等：先清行程/点/媒体/分享，再重建。只写库，不产生文件）
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "seed_test_user.py")],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        log("重建失败：\n%s\n%s" % (r.stdout[-2000:], r.stderr[-2000:]))
        return 1
    for line in r.stdout.strip().splitlines():
        log("  " + line)

    # 把「上次打开的行程」归位，保证下次登录看到的画面也是出厂状态。
    # 选媒体最多的那个行程而不是列表第一个：列表按开始日期倒序，排头的往往是
    # 「计划中」的空行程，演示账号一登进来看到空白地图，第一印象就废了。
    u = db.get_user_by_username(DEMO_USER)
    if u:
        c = db.get_conn()
        row = c.execute(
            "SELECT t.id, t.name, COUNT(m.id) AS n FROM trips t "
            "LEFT JOIN points p ON p.trip_id = t.id "
            "LEFT JOIN media m ON m.point_id = p.id "
            "WHERE t.user_id=? GROUP BY t.id ORDER BY n DESC, t.id ASC LIMIT 1",
            (u["id"],)).fetchone()
        c.close()
        if row:
            db.set_last_trip(u["id"], row["id"])
            log("last_trip_id 归位 -> %s（%s，%s 个媒体）" % (row["id"], row["name"], row["n"]))
    log("演示账号 %s 已恢复出厂状态" % DEMO_USER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
