"""P2-1 周年提醒定时化 — 每日召回脚本

每天由 cron 调用：扫描所有用户的周年纪念，有命中则生成唤醒语。
无命中时输出 recalls 为空（正常，不是错误）。

输出 JSON：
{
  "today": "YYYY-MM-DD",
  "recalls": [
    {"user_id": 3, "username": "arthur", "display_name": "边城量子",
     "message": "...唤醒语...", "anniversary_count": 6}
  ]
}

cron 端根据 recalls 是否为空决定是否推送。
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from domain.recall import compose_recall


def main() -> int:
    conn = db.get_conn()
    try:
        # 只扫真实旅行者（非 admin）。admin 的 list_searchable_points 返回全库
        # （管理视野），若纳入会与各旅行者本人重复计算周年。
        users = conn.execute(
            "SELECT id, username, display_name FROM users "
            "WHERE is_admin = 0 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    recalls = []
    for u in users:
        r = compose_recall(u["id"])
        if not r.get("empty"):
            recalls.append({
                "user_id": u["id"],
                "username": u["username"],
                "display_name": u["display_name"],
                "message": r["message"],
                "anniversary_count": len(r["anniversaries"]),
            })

    out = {"today": date.today().isoformat(), "recalls": recalls}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
