#!/usr/bin/env python3
"""把演示账号用的真实风景照片/视频抓到本地素材池。

为什么要有这一步：seed_test_user.py 原来是用 PIL 现画渐变色块当照片，
演示账号点开一看全是"TEST DATA"的假图，说服力为零。但 seed 每天凌晨 3 点
要被 reset_demo 重跑一次，不能每次都去外网拉一遍 —— 所以拆成两段：
这个脚本**一次性**把素材抓进 assets/demo-media/，seed 每天只是从池子里拷。

素材来源是 Wikimedia Commons，并且**只收 PD / CC0**。CC BY / CC BY-SA
要求显著署名，而这些图是当作"用户自己拍的照片"展示在演示账号里的，
没有地方能自然地挂署名 —— 与其违规，不如把范围收窄到不需要署名的那部分。
代价是照片未必和地名严格对应（比如宇治的平等院就没有 PD 的图），
所以每个点配了一串候选词，从"地点专属"一路退到"区域风景"，取到够数为止。

用法：
    python3 scripts/fetch_demo_assets.py           # 缺什么补什么
    python3 scripts/fetch_demo_assets.py --force   # 全部重抓
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "assets" / "demo-media"

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia 要求 UA 里带得上联系方式，否则会直接限流
UA = {"User-Agent": "travel-memory-demo-assets/1.0 (https://top001.xin; jeff@luckyluke.org)"}
# 连续打搜索接口很容易吃 429，每次请求之间强制隔一下
THROTTLE = 1.2

# 只认这几种免署名授权。写成前缀匹配是因为 Commons 上同一种授权有好几种写法
# （"CC0"、"CC0 1.0"、"Public domain"、"PD-self"…）。
FREE_LICENSE = re.compile(r"^\s*(cc0|cc-zero|public\s*domain|pd[-\s])", re.I)
MIN_WIDTH = 1200

# 每个点：目标张数 + 候选搜索词（从专属退到通用，前面的取不够就往后走）。
#
# 词里几乎都带 "Unsplash"：Commons 收录了大批 Unsplash 的 CC0 图，
# 是这个库里少数「既免署名、质量又稳定」的来源。不带这个词直接搜地名，
# 搜回来的是航图、线路图、书封面和老明信片 —— 试过一轮，38 张里只有 15 张能看。
PLAN = [
    ("osaka", 4, ["Unsplash Dotonbori", "Unsplash Osaka castle",
                  "Unsplash Japan city night", "Unsplash Osaka"]),
    ("nara", 3, ["Unsplash Nara", "Unsplash deer", "Unsplash Japan temple",
                 "Unsplash Japanese shrine"]),
    ("kyoto", 6, ["Unsplash Arashiyama", "Unsplash torii", "Unsplash pagoda",
                  "Unsplash Kyoto temple", "Unsplash cherry blossom",
                  "Unsplash Japanese garden"]),
    ("uji", 2, ["Unsplash Japan tea", "Unsplash matcha", "Unsplash Japan river",
                "Unsplash Japanese garden pond"]),
    ("kobe", 3, ["Unsplash Kobe", "Unsplash harbor night", "Unsplash city night view",
                 "Unsplash port city"]),
    ("urumqi", 2, ["Unsplash spice market", "Unsplash bazaar", "Unsplash desert road",
                   "Unsplash silk road"]),
    ("dushanzi", 2, ["Unsplash mountain road", "Unsplash winding road",
                     "Unsplash canyon road", "Unsplash mountain pass"]),
    ("nalati", 4, ["Unsplash grassland", "Unsplash meadow horses", "Unsplash alpine meadow",
                   "Unsplash green hills", "Unsplash steppe"]),
    ("kuqa", 3, ["Unsplash red canyon", "Unsplash desert canyon", "Unsplash canyon",
                 "Unsplash rock formation desert"]),
    ("lijiang", 3, ["Unsplash chinese ancient town", "Unsplash china village",
                    "Unsplash chinese roof", "Unsplash red lantern"]),
    ("shangrila", 3, ["Unsplash tibet", "Unsplash plateau lake", "Unsplash monastery",
                      "Unsplash highland lake"]),
    ("deqin", 3, ["Unsplash snow mountain", "Unsplash himalaya", "Unsplash mountain sunrise",
                  "Unsplash glacier peak"]),
    ("yubeng", 3, ["Unsplash mountain village", "Unsplash himalaya village",
                   "Unsplash alpine hiking trail", "Unsplash mountain hut"]),
    ("benzilan", 3, ["Unsplash river bend", "Unsplash river canyon", "Unsplash mountain river",
                     "Unsplash valley river"]),
    ("hutiaoxia", 3, ["Unsplash gorge river", "Unsplash river rapids", "Unsplash deep canyon",
                      "Unsplash cliff river"]),
]

# 视频：Commons 上自由授权的片段基本是 webm/ogv，浏览器兼容性参差，
# 统一转成 H.264 mp4 —— 演示账号要在手机微信里也能直接播。
VIDEO_PLAN = [
    ("kyoto_clip", ["timelapse nature", "landscape timelapse", "forest nature",
                    "waterfall nature"]),
    ("nalati_clip", ["grassland wind", "meadow nature", "clouds timelapse",
                     "mountain landscape video"]),
    ("hutiaoxia_clip", ["river rapids", "mountain river", "river flowing",
                        "waterfall nature"]),
]

# 标题里出现这些词的基本不是照片：航图、线路图、书封面、徽章、扫描件……
TITLE_BLOCK = re.compile(
    r"(map|chart|diagram|logo|poster|cover|flag|coat of arms|seal\b|screenshot"
    r"|scan|plan\b|schema|blueprint|drawing|engraving|postcard|stamp|banknote"
    r"|\.svg|\.png|\.gif|\.tif)", re.I)


_last_call = [0.0]


def api(params):
    """带节流和 429 退避的 Commons API 调用。"""
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        wait = THROTTLE - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            time.sleep(4 * (attempt + 1))
    raise RuntimeError("unreachable")


def search(term, media_type="bitmap", limit=40):
    """返回 [(标题, 下载地址, 宽, 授权, 来源页)]，已按 PD/CC0 + 最小宽度过滤。"""
    d = api(dict(action="query", format="json", generator="search",
                 gsrsearch="filetype:%s %s" % (media_type, term),
                 gsrnamespace="6", gsrlimit=str(limit),
                 prop="imageinfo", iiprop="url|size|extmetadata",
                 iiurlwidth="2000"))
    out = []
    for pg in ((d.get("query") or {}).get("pages") or {}).values():
        ii = (pg.get("imageinfo") or [None])[0]
        if not ii:
            continue
        em = ii.get("extmetadata") or {}
        lic = ((em.get("LicenseShortName") or {}).get("value") or "").strip()
        if not FREE_LICENSE.match(lic):
            continue
        if TITLE_BLOCK.search(pg["title"]):
            continue
        if media_type == "bitmap":
            if (ii.get("width") or 0) < MIN_WIDTH:
                continue
            # thumburl 是服务端缩好的 2000px，比原图小得多，够用且省流量
            src = ii.get("thumburl") or ii.get("url")
        else:
            src = ii.get("url")
        out.append((pg["title"], src, ii.get("width"), lic, ii.get("descriptionurl")))
    return out


def acceptable_photo(path):
    """下载完再验一道：搜索结果里混进来的扫描件、纯色图、黑白老照片，
    光看标题和授权是筛不掉的，得真的打开看一眼。"""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return True                                  # 没装 PIL 就不拦
    try:
        im = Image.open(path)
        im.load()
    except Exception:
        return False
    w, h = im.size
    if w < MIN_WIDTH:
        return False
    if not 0.45 <= w / h <= 2.6:                     # 过于狭长的多半是全景条或书脊
        return False
    rgb = im.convert("RGB")
    small = rgb.resize((160, 160))
    st = ImageStat.Stat(small)
    if sum(st.stddev) / 3 < 18:                      # 几乎没有明暗变化 = 纯色/占位
        return False
    # 近似灰度判定：黑白老照片当"旅行照片"太出戏
    r, g, b = st.mean
    if max(abs(r - g), abs(g - b), abs(r - b)) < 6:
        hsv = ImageStat.Stat(small.convert("HSV"))
        if hsv.mean[1] < 28:
            return False
    return True


def download(url, dest):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest.stat().st_size


def fetch_photos(force, credits, seen, only=None):
    for slug, want, terms in PLAN:
        if only and slug not in only:
            continue
        d = POOL / slug
        d.mkdir(parents=True, exist_ok=True)
        have = sorted(d.glob("*.jpg"))
        if force:
            for p in have:
                p.unlink()
            have = []
        need = want - len(have)
        if need <= 0:
            print("  %-10s 已有 %d 张，跳过" % (slug, len(have)))
            continue
        got = len(have)
        for term in terms:
            if got >= want:
                break
            try:
                hits = search(term)
            except Exception as e:
                print("  %-10s 搜索「%s」失败：%s" % (slug, term, e))
                continue
            for title, url, w, lic, page in hits:
                if got >= want:
                    break
                if title in seen:          # 同一张图不要在两个地点重复出现
                    continue
                dest = d / ("%02d.jpg" % (got + 1))
                try:
                    size = download(url, dest)
                except Exception as e:
                    print("  %-10s 下载失败 %s：%s" % (slug, title[:40], e))
                    dest.unlink(missing_ok=True)
                    continue
                if size < 40_000 or not acceptable_photo(dest):
                    dest.unlink(missing_ok=True)
                    continue
                seen.add(title)
                credits.append(dict(slug=slug, file=dest.name, title=title,
                                    license=lic, source=page, term=term))
                got += 1
                print("  %-10s %s  <- %s [%s]" % (slug, dest.name, title[:46], lic))
        if got < want:
            print("  !! %s 只凑到 %d/%d 张" % (slug, got, want))


def fetch_videos(force, credits, seen):
    for slug, terms in VIDEO_PLAN:
        dest = POOL / ("%s.mp4" % slug)
        if dest.exists() and not force:
            print("  %-10s 已存在，跳过" % slug)
            continue
        done = False
        for term in terms:
            if done:
                break
            try:
                hits = search(term, media_type="video", limit=30)
            except Exception as e:
                print("  %-10s 搜索「%s」失败：%s" % (slug, term, e))
                continue
            for title, url, _w, lic, page in hits:
                if title in seen:
                    continue
                raw = POOL / ("_raw_" + re.sub(r"[^\w.]+", "_", title)[-60:])
                try:
                    download(url, raw)
                except Exception as e:
                    print("  %-10s 下载失败 %s：%s" % (slug, title[:40], e))
                    raw.unlink(missing_ok=True)
                    continue
                # 截前 8 秒转 H.264/AAC，720p 以内 —— 演示片段不需要更大
                cmd = ["ffmpeg", "-y", "-i", str(raw), "-t", "8",
                       "-vf", "scale='min(1280,iw)':-2",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                       "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
                       str(dest)]
                r = subprocess.run(cmd, capture_output=True)
                raw.unlink(missing_ok=True)
                if r.returncode == 0 and dest.exists() and dest.stat().st_size > 50_000:
                    seen.add(title)
                    credits.append(dict(slug=slug, file=dest.name, title=title,
                                        license=lic, source=page, term=term))
                    print("  %-10s %s  <- %s [%s]" % (slug, dest.name, title[:46], lic))
                    done = True
                    break
                dest.unlink(missing_ok=True)
        if not done:
            print("  !! %s 没找到可用视频" % slug)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已有的也重抓")
    ap.add_argument("--only", default="", help="只处理这些 slug（逗号分隔）")
    args = ap.parse_args()

    POOL.mkdir(parents=True, exist_ok=True)
    credits, seen = [], set()

    # 去重集合要从上次的清单预载，否则定向重抓某个点时会把别处已经用过的图再拉一遍
    man = POOL / "CREDITS.json"
    if man.exists():
        try:
            for c in json.loads(man.read_text(encoding="utf-8")):
                seen.add(c.get("title", ""))
        except Exception:
            pass

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    print("照片：")
    fetch_photos(args.force, credits, seen, only)
    if not only:
        print("视频：")
        fetch_videos(args.force, credits, seen)

    # PD/CC0 不强制署名，但来源该留档还是要留 —— 将来有人问这些图哪来的，
    # 或者要换授权策略，这个清单就是唯一的依据。
    if credits:
        man = POOL / "CREDITS.json"
        old = []
        if man.exists() and not args.force:
            try:
                old = json.loads(man.read_text(encoding="utf-8"))
            except Exception:
                old = []
        keep = [c for c in old if not any(
            n["slug"] == c["slug"] and n["file"] == c["file"] for n in credits)]
        man.write_text(json.dumps(keep + credits, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("\n来源清单已写入 %s（本次新增 %d 条）" % (man, len(credits)))

    total = sum(len(list((POOL / s).glob("*.jpg"))) for s, _w, _t in PLAN)
    vids = len(list(POOL.glob("*.mp4")))
    print("素材池合计：照片 %d 张，视频 %d 个" % (total, vids))
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
