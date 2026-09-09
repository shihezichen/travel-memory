"""为 test 演示账号生成行程 / 轨迹点 / 照片（可重复执行：先清干净再重建）"""
import sys, os, random, datetime, subprocess
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import db, media

TEST_PW = 'test1234'
UP = Path('uploads'); TH = UP / 'thumbs'
UP.mkdir(exist_ok=True); TH.mkdir(exist_ok=True)

def font(sz):
    for p in ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']:
        if Path(p).exists():
            try: return ImageFont.truetype(p, sz)
            except Exception: pass
    try:
        import glob
        for p in glob.glob('/usr/share/fonts/**/*.ttc', recursive=True) + \
                 glob.glob('/usr/share/fonts/**/*.ttf', recursive=True):
            try: return ImageFont.truetype(p, sz)
            except Exception: continue
    except Exception: pass
    return ImageFont.load_default()

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

# 清掉这个账号原有的行程（含点、媒体记录）和它的媒体文件
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
for f in old_files:
    try: (UP / f).unlink(missing_ok=True)
    except Exception: pass
for f in old_thumbs:
    try: (TH / f).unlink(missing_ok=True)
    except Exception: pass
print('已清空旧数据：%d 个媒体文件' % len(old_files))

# ---------- 2) 行程与点 ----------
TRIPS = [
 dict(name='2026-关西赏樱', start='2026-03-28', end='2026-04-04', status='completed',
      desc='大阪进京都出，樱花季走了关西几个老城。',
      points=[
        ('大阪',   34.69374, 135.50218, '2026-03-28', 4, '道顿堀的夜、心斋桥的人潮。'),
        ('奈良',   34.68512, 135.80489, '2026-03-30', 3, '喂鹿，东大寺的木构真的震撼。'),
        ('京都',   35.01164, 135.76803, '2026-03-31', 5, '哲学之道的樱花开到刚刚好。'),
        ('宇治',   34.88427, 135.79968, '2026-04-02', 2, '平等院凤凰堂 + 抹茶。'),
        ('神户',   34.69013, 135.19556, '2026-04-03', 3, '北野异人馆，港口夜景。'),
      ]),
 dict(name='2025-独库公路', start='2025-07-12', end='2025-07-19', status='completed',
      desc='一天四季的独库公路，从戈壁开到雪山再到草原。',
      points=[
        ('乌鲁木齐', 43.82563, 87.61688, '2025-07-12', 2, '出发前的补给。'),
        ('独山子',   44.32812, 84.88596, '2025-07-13', 2, '独库公路零公里。'),
        ('那拉提',   43.31667, 84.15000, '2025-07-15', 4, '空中草原，牛羊比人多。'),
        ('库车',     41.71786, 82.96204, '2025-07-17', 3, '天山大峡谷的红色岩壁。'),
      ]),
 dict(name='2026-计划中：滇西北', start='2026-10-01', end='2026-10-08', status='planning',
      desc='还没去，先把路线排出来。',
      points=[
        ('丽江',   26.87220, 100.22999, '2026-10-01', 0, ''),
        ('香格里拉', 27.82568, 99.70646, '2026-10-03', 0, ''),
        ('德钦',   28.48604, 98.91093, '2026-10-05', 0, ''),
      ]),
]

# 演示用的照片文字描述。说明栏支持 Markdown，所以这里刻意把各种语法都铺一遍：
# 标题 / 粗斜 / 删除线 / 行内代码 / 链接 / 有序无序列表 / 引用 / 分隔线 / 多段，
# 另外留了几条纯文本，方便对照「没写标记时的样子」。
CAPS = {
 '大阪': [
   "## 道顿堀の夜\n"
   "招牌亮到晃眼，*人挤人*也值得。\n\n"
   "- 到达：地铁**难波站** 14 号口\n"
   "- 最佳机位：戎桥中段，别站桥头\n"
   "- 排队：章鱼烧 `20 分钟` 起\n\n"
   "> 夜里十点人反而更多，想拍空景得等到打烊。",

   "### 心斋桥 · 战利品清单\n"
   "1. 药妆 `3` 袋\n"
   "2. 一双跑鞋\n"
   "3. ~~克制消费~~ 失败\n\n"
   "走到腿断，最后还是买了。",

   "**大阪城**的护城河比想象中宽。\n"
   "天守阁里面是博物馆，*别期待古建内饰*。",

   "通天阁下面那条街，昭和感很重。\n\n"
   "---\n\n"
   "回程在新世界吃了串炸，酱汁**只能蘸一次**——这是规矩。",
 ],
 '奈良': [
   "鹿会鞠躬，也会咬包。\n\n"
   "> 鹿仙贝 `200 円` 一份，拿在手里就别想再走神。",

   "## 东大寺\n"
   "大佛殿的木构，**站在下面**才知道什么叫大。\n\n"
   "- 门票 600 円\n"
   "- 柱子根部那个洞，钻过去据说保平安\n"
   "- 建议 *早上八点* 到，人能少一半",

   "若草山傍晚的光最好。爬到二重目就够了，三重目那段路又陡又没树荫。",
 ],
 '京都': [
   "## 哲学之道\n"
   "沿河一路都是**染井吉野**，*清晨*人最少。\n\n"
   "- 时段：`6:00-7:30`\n"
   "- 交通：5 路巴士到银阁寺道\n\n"
   "> 花期只有一周，出发前先看[开花情报](https://sakura.weathermap.jp/)。",

   "### 清水寺\n"
   "人太多，改成清晨再来一次。\n\n"
   "1. 五点半出门\n"
   "2. 二年坂空无一人\n"
   "3. 六点半开始涌人\n\n"
   "~~午后再去~~ 别想了。",

   "伏见稻荷的千本鸟居，越往上人越少。\n"
   "走到 **四辻** 大概 30 分钟，之后就随缘。",

   "岚山的竹林要赶在旅游大巴之前到。",

   "祇园白川的夜樱\n\n"
   "灯打得**很克制**，水面反光比树本身好看。\n\n"
   "> 不要追着艺伎拍照，这里有明文禁止。",
 ],
 '宇治': [
   "平等院的**倒影**比正面好看。\n"
   "凤凰堂内部要另买票，*限时限人*。",

   "### 抹茶三连\n"
   "- 中村藤吉：生茶果冻\n"
   "- 伊藤久右卫门：抹茶荞麦面\n"
   "- 通圆：`600` 年老店，只喝一碗浓茶\n\n"
   "> 一天喝三家，晚上没睡着。",
 ],
 '神户': [
   "北野异人馆一路上坡，回头是整个港口。\n\n"
   "> 缆车票和馆内套票分开卖，别买重了。",

   "## 六甲山夜景\n"
   "号称**千万美元**。\n\n"
   "1. 六甲缆车上山\n"
   "2. 天览台看港口\n"
   "3. 末班车 `21:10`，别错过\n\n"
   "---\n\n"
   "山上比市区低 `6℃`，*多带一件*。",

   "有马温泉的金汤是铁锈色的，第一次看到会愣一下。",
 ],
 '乌鲁木齐': [
   "### 出发前\n"
   "- 机场 T3 取车\n"
   "- 加满油再上路，独库沿途加油站很少\n"
   "- 备一箱水\n\n"
   "> 独库公路每年 **6 月才开**，10 月就封路。",

   "国际大巴扎的干果摊，*先尝后买*，别不好意思。",
 ],
 '独山子': [
   "大峡谷的沟壑，**一层一层**像被刀削过。\n"
   "站在观景台边上腿会软。",

   "这一段限速 `40`，弯多，别赶时间。",
 ],
 '那拉提': [
   "## 空中草原\n"
   "牛羊比人多。\n\n"
   "- 骑马 `1` 小时，讲好价再上\n"
   "- 日落在 21:40 前后\n\n"
   "> 草原上没信号，先约好集合时间。",
 ],
 '库车': [
   "天山大峡谷的红，*照片拍不出那个颜色*。\n\n"
   "---\n\n"
   "峡谷里温差大，**中午晒、进沟凉**。",

   "克孜尔千佛洞不让拍照，只能靠记。",
 ],
}

PALETTE = [(232,116,97),(96,150,215),(240,180,80),(112,183,140),(150,120,200),
           (230,140,175),(90,180,190),(200,160,110),(120,140,210),(210,110,140)]

total_photos = 0
for ti, t in enumerate(TRIPS):
    tid = db.create_trip(u, t['name'], t['start'], t['end'], t['desc'], t['status'])
    for pi, (pname, lat, lng, pdate, nphoto, pdesc) in enumerate(t['points']):
        pid = db.create_point(tid, pname, lat, lng, pdate, pdate, pdesc)
        for k in range(nphoto):
            idx = total_photos
            fname = 'test_%s_%s_%d.jpg' % (ti + 1, pi + 1, k + 1)
            w, h = (1280, 853) if k % 3 else (960, 1280)      # 混排横图/竖图
            base = PALETTE[idx % len(PALETTE)]
            img = Image.new('RGB', (w, h), base)
            dr = ImageDraw.Draw(img)
            # 简单渐变 + 斜纹，纯色图看起来太假
            for y in range(0, h, 3):
                f = y / h
                dr.line([(0, y), (w, y)],
                        fill=(int(base[0]*(1-0.45*f)), int(base[1]*(1-0.45*f)), int(base[2]*(1-0.45*f))), width=3)
            for x in range(-h, w, 90):
                dr.line([(x, h), (x + h, 0)], fill=(255, 255, 255, 30), width=2)
            dr.text((60, 60), pname, font=font(int(w*0.09)), fill=(255, 255, 255))
            dr.text((60, 60 + int(w*0.11)), '%s · %02d' % (pdate, k + 1),
                    font=font(int(w*0.04)), fill=(255, 255, 255))
            dr.text((60, h - 90), 'TEST DATA · 旅行记忆', font=font(int(w*0.028)), fill=(255, 255, 255))
            img.save(UP / fname, 'JPEG', quality=86)
            thumb = media.make_thumb(UP / fname, 'photo')
            shot = datetime.datetime.strptime(pdate, '%Y-%m-%d') + datetime.timedelta(
                hours=8 + k * 2, minutes=random.randint(0, 59))
            mid = db.add_media(pid, 'photo', fname, thumb, shot.strftime('%Y-%m-%d %H:%M:%S'), lat, lng)
            # 写上文字描述，演示「照片说明」+ Markdown 渲染。
            # 有几条就写几张，多出来的照片留空，正好也演示「＋ 添加描述」的空态。
            caps = CAPS.get(pname) or []
            if k < len(caps):
                db.set_media_caption(mid, caps[k])
            total_photos += 1
    print('行程「%s」 %d 个点' % (t['name'], len(t['points'])))

# ---------- 3) 一段短视频（放在京都） ----------
c = db.get_conn()
kyoto = c.execute("SELECT p.id FROM points p JOIN trips t ON p.trip_id=t.id "
                  "WHERE t.user_id=? AND p.name='京都'", (u['id'],)).fetchone()
c.close()
if kyoto:
    vname = 'test_kyoto_clip.mp4'
    cmd = ['ffmpeg','-y','-f','lavfi','-i','color=c=0x4a90d9:s=960x540:d=4',
           '-vf',"drawtext=text='Kyoto · TEST':fontcolor=white:fontsize=48:x=(w-tw)/2:y=(h-th)/2",
           '-pix_fmt','yuv420p', str(UP / vname)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0 and (UP / vname).exists():
        vthumb = media.make_thumb(UP / vname, 'video')
        db.add_media(kyoto['id'], 'video', vname, vthumb, '2026-03-31 17:20:00', 35.01164, 135.76803)
        print('已生成视频 %s' % vname)
    else:
        print('视频生成失败：', r.stderr.decode()[-300:])

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
