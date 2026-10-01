"""检索 + 用户隔离的实测。隔离这条必须验：漏一层就是别人家的照片被搜出来。"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import aiindex                                            # noqa: E402
import db                                                 # noqa: E402

conn = db.get_conn()
users = conn.execute(
    "SELECT t.user_id AS uid, COUNT(DISTINCT m.id) AS n "
    "  FROM media m JOIN points p ON p.id=m.point_id "
    "  JOIN trips t ON t.id=p.trip_id WHERE m.type='photo' "
    " GROUP BY t.user_id").fetchall()
conn.close()
print("照片按用户分布:", [(r["uid"], r["n"]) for r in users])

uid = users[0]["uid"]
print("\n用 user_id=%s 搜索：" % uid)
for q in ["雪山", "夜景灯光", "草地上的狗", "红灯笼", "有人在走路"]:
    t0 = time.time()
    hits = aiindex.search(uid, q, limit=5)
    print("  %-8s %5.0f ms  %s" % (q, (time.time() - t0) * 1000,
                                   [(h["filename"], h["score"]) for h in hits[:3]]))

print("\n用户隔离检查：")
for r in users:
    hits = aiindex.search(r["uid"], "雪山", limit=100)
    print("  user_id=%-6s 搜到 %d 张（该用户共 %d 张照片）"
          % (r["uid"], len(hits), r["n"]))

ghost = 999999
print("  不存在的 user_id=%s 搜到 %d 张（必须是 0）"
      % (ghost, len(aiindex.search(ghost, "雪山", limit=100))))

# 去重是否生效
hits = aiindex.search(uid, "人物合影", limit=50)
names = [h["filename"] for h in hits]
print("\n去重检查: 返回 %d 条，唯一文件名 %d 个 —— %s"
      % (len(names), len(set(names)), "一致" if len(names) == len(set(names)) else "有重复"))
