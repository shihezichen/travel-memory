"""为 test 演示账号生成行程 / 轨迹点 / 照片（可重复执行：先清干净再重建）

**这个脚本不碰任何文件**，只写数据库。媒体的家在 OSS 上，由
scripts/publish_demo_assets.py 一次性发布；这里只是按 demo_dataset 里那套
固定文件名往 media 表里写行。

为什么这么拆：reset_demo 每晚 3 点要跑一遍，而素材是死的、从来不变。
原来每晚都要拷 39 个文件、算 39 张缩略图、给视频转码，第二天再被 sync-oss
传一次，全是白干的活。改成只写库之后，这一趟就剩几十条 INSERT。

页面为什么能显示：config.media_url() 判断本地没有这个文件时会直接指向 OSS
（见 _synced_to_oss 末尾的 except OSError），所以本机 uploads/ 里没有
test_* 这些文件完全没关系。

前置条件：先跑过一次 scripts/publish_demo_assets.py。没跑的话库里的行会指向
OSS 上不存在的对象，演示账号点开就是一片破图。
"""
import sys, os, random, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import db
import demo_dataset as ds

TEST_PW = 'test1234'
UP = Path('uploads'); TH = UP / 'thumbs'

# ---------- 1) 用户 ----------
u = db.get_user_by_username('test')
if u:
    print('test 用户已存在 id=%s，重置密码并清空旧数据' % u['id'])
    salt, ph = db.hash_password(TEST_PW)
    c = db.get_conn()
    c.execute('UPDATE users SET password_hash=?, salt=?, display_name=? WHERE id=?',
              (ph, salt, '体验账号', u['id']))
    c.commit(); c.close()
else:
    uid = db.create_user('test', TEST_PW, '体验账号')
    print('新建 test 用户 id=%s' % uid)
u = db.get_user_by_username('test')

# 清掉这个账号原有的行程（含点、媒体记录）
c = db.get_conn()
old_files = [r['filename'] for r in c.execute(
    "SELECT m.filename FROM media m JOIN points p ON m.point_id=p.id "
    "JOIN trips t ON p.trip_id=t.id WHERE t.user_id=?", (u['id'],))]
old_thumbs = [r['thumb_filename'] for r in c.execute(
    "SELECT m.thumb_filename FROM media m JOIN points p ON m.point_id=p.id "
    "JOIN trips t ON p.trip_id=t.id WHERE t.user_id=? AND m.thumb_filename IS NOT NULL", (u['id'],))]
c.execute("DELETE FROM media WHERE point_id IN (SELECT p.id FROM points p JOIN trips t ON p.trip_id=t.id WHERE t.user_id=?)", (u['id'],))
c.execute("DELETE FROM points WHERE trip_id IN (SELECT id FROM trips WHERE user_id=?)", (u['id'],))
c.execute("DELETE FROM shares WHERE trip_id IN (SELECT id FROM trips WHERE user_id=?)", (u['id'],))
c.execute("DELETE FROM trips WHERE user_id=?", (u['id'],))
c.commit(); c.close()

# 迁移前留在本机的那份副本，顺手清掉。现在的 seed 不产生本地文件，
# 所以这段只在第一次跑到新版时有活干，之后永远是 0 个。
_purged = 0
for f in old_files:
    try:
        if (UP / f).exists():
            (UP / f).unlink(); _purged += 1
    except Exception: pass
for f in old_thumbs:
    try:
        if (TH / f).exists():
            (TH / f).unlink(); _purged += 1
    except Exception: pass
print('已清空旧数据：%d 条媒体记录%s'
      % (len(old_files), ('，并删除 %d 个遗留本地文件' % _purged) if _purged else ''))

# ---------- 2) 行程、点、照片 ----------
# 只写库。文件名从 demo_dataset 取，跟 publish_demo_assets.py 用的是同一套。
total_photos = 0
for ti, t in enumerate(ds.TRIPS):
    tid = db.create_trip(u, t['name'], t['start'], t['end'], t['desc'], t['status'])
    for pi, (pname, lat, lng, pdate, nphoto, pdesc) in enumerate(t['points']):
        pid = db.create_point(tid, pname, lat, lng, pdate, pdate, pdesc)
        for k in range(nphoto):
            fname = ds.photo_name(ti, pi, k)
            # 拍摄时间是合成的：当天上午八点起、每张隔两小时。原来也是这么算的
            # （从来没读过照片自带的 EXIF —— 素材是 Wikimedia 上的图，
            # 里面的时间地点跟这趟行程没关系）。
            shot = datetime.datetime.strptime(pdate, '%Y-%m-%d') + datetime.timedelta(
                hours=8 + k * 2, minutes=random.randint(0, 59))
            mid = db.add_media(pid, 'photo', fname, ds.thumb_name(fname),
                               shot.strftime('%Y-%m-%d %H:%M:%S'), lat, lng)
            # 写上文字描述，演示「照片说明」+ Markdown 渲染。
            # 有几条就写几张，多出来的照片留空，正好也演示「＋ 添加描述」的空态。
            caps = ds.CAPS.get(pname) or []
            if k < len(caps):
                db.set_media_caption(mid, caps[k])
            total_photos += 1
    print('行程「%s」 %d 个点' % (t['name'], len(t['points'])))

# ---------- 3) 视频 ----------
for pname, poolname, fname, shot, lat, lng in ds.VIDEOS:
    c = db.get_conn()
    row = c.execute("SELECT p.id FROM points p JOIN trips t ON p.trip_id=t.id "
                    "WHERE t.user_id=? AND p.name=?", (u['id'], pname)).fetchone()
    c.close()
    if not row:
        print('找不到点位「%s」，跳过视频 %s' % (pname, fname))
        continue
    db.add_media(row['id'], 'video', fname, ds.thumb_name(fname), shot, lat, lng)
    print('已放入视频 %s（%s）' % (fname, pname))

# ---------- 4) 汇总 ----------
c = db.get_conn()
print('\n=== test 账号数据 ===')
for r in c.execute("SELECT t.id,t.name,t.status,count(DISTINCT p.id) pts, count(m.id) med "
                   "FROM trips t LEFT JOIN points p ON p.trip_id=t.id LEFT JOIN media m ON m.point_id=p.id "
                   "WHERE t.user_id=? GROUP BY t.id ORDER BY t.id", (u['id'],)):
    print('  #%d %-22s %-9s %d 个点 / %d 个媒体' % (r['id'], r['name'], r['status'], r['pts'], r['med']))
print('  照片 %d 张' % total_photos)

# 默认落在「媒体最多」的那个行程：写死 id 只在造它的那台机器上成立，
# 别人全新安装时 id 从 1 开始，会指向一个不存在的行程。
_land = c.execute("SELECT t.id, t.name, COUNT(m.id) n FROM trips t "
                  "LEFT JOIN points p ON p.trip_id=t.id "
                  "LEFT JOIN media m ON m.point_id=p.id "
                  "WHERE t.user_id=? GROUP BY t.id ORDER BY n DESC, t.id ASC LIMIT 1",
                  (u['id'],)).fetchone()
c.close()
if _land:
    db.set_last_trip(u['id'], _land['id'])
    print('  默认打开：%s（%d 个媒体）' % (_land['name'], _land['n']))
