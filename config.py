"""跨模块共享的配置常量。

从 app.py 搬出来的，读取逻辑一字未改。搬出来的唯一原因是：路由拆进 routers/* 之后
那些模块也要用 AMAP_KEY / OSRM_URL 这些值，而它们不能 `import app`（app.py 自己要
import routers.*，会绕成循环）。

这里只放**静态配置**。会被测试 monkeypatch 的可变全局（UPLOAD_DIR、_TILE_DIR、
GATE_HASH、ADMIN_TOKEN）故意不放进来 —— 它们必须留在各自的使用方模块里，
测试才能通过重绑模块全局做隔离。
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 高德地图 key（从 amap_key.txt 读取，若未配置则为空）
AMAP_KEY = ""
_key_file = BASE / "amap_key.txt"
if _key_file.exists():
    AMAP_KEY = _key_file.read_text(encoding="utf-8").strip()

# 高德安全密钥（JS API 2.0 securityJsCode，从 amap_security.txt 读取）
AMAP_SECURITY = ""
_sec_file = BASE / "amap_security.txt"
if _sec_file.exists():
    AMAP_SECURITY = _sec_file.read_text(encoding="utf-8").strip()

# OSRM 全球驾车路线规划服务（高德只支持国内，海外路线用此兜底）
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
# OSRM 超时。原来是 30 秒 —— 而实测这台机器打 OSRM 公共服务是 663/665/675 ms
# （巴黎→布鲁塞尔，连打 3 次）。30 秒等于给了 45 倍的余量，代价是缓存未命中时
# 每个请求最长占住一个线程池槽位 30 秒；前端 drawRoute() 还会用 Promise.all
# 对一条行程的所有路段并发发请求（库里最长的行程是 16 段）。
# 收到 8 秒：相对实测值仍有 12 倍余量，而最坏情况的槽位占用降到 3.75 分之一。
# 超时的后果是返回空 routes，前端已有直线兜底，不会白屏。
OSRM_TIMEOUT = float(os.environ.get("TRAVEL_OSRM_TIMEOUT", "8"))

# 媒体分发前缀。空 = 媒体走本机 /uploads/（frp 隧道，4Mbps 硬顶）；
# 填成 OSS/CDN 的 origin（如 https://xxx.oss-cn-qingdao.aliyuncs.com）后，
# 所有 /uploads/ 链接改指对象存储，媒体流量彻底绕开隧道。
#
# 这是**一个开关**：置空即完整回退到本机分发，不需要改任何其它代码。
# 从 media_base.txt 读（跟 amap_key.txt 一个套路），环境变量可覆盖（测试用）。
MEDIA_BASE_URL = ""
_media_base_file = BASE / "media_base.txt"
if _media_base_file.exists():
    MEDIA_BASE_URL = _media_base_file.read_text(encoding="utf-8").strip()
MEDIA_BASE_URL = os.environ.get("TRAVEL_MEDIA_BASE_URL", MEDIA_BASE_URL).rstrip("/")

# 网页媒体签名 URL 的有效期。
#
# 取 30 分钟，比 Agent 链的 24 小时短得多 —— 两者场景不同：
#   - Agent 拿到链接后要**自己去下载**，中间可能搁很久，短了会半路 403；
#   - 网页只是**渲染时当场加载**，30 分钟对一次浏览绰绰有余。
# 短一点的好处是泄露窗口小：分享页的 URL 是给匿名访客的，签得太久等于
# 把裸链的毛病原样搬过来。
#
# **改这里的值必须同时改 static/share.js 的 MEDIA_SIG_TTL_MS** —— 前端靠它
# 判断「手上这批签名是不是该换了」，对不上就会在过期后一直用旧链接。
#
# 过期不破坏播放/浏览：两处都做了自愈，不需要用户刷新页面 ——
#   - 视频（static/common.js 的 tmKeepFresh）：过期前 5 分钟主动续签，
#     并保持播放进度；万一没赶上，播放器报错时再兜底续一次。
#   - 图片/分享页（static/share.js 的 refreshSignatures）：签名可能过期时
#     重新拉一次数据换新签名，只换链接不动渲染。
# 分享链接本身不会过期（share token 在 db.get_share 里 expires_at 可为 NULL），
# 所以「发群里过两周还能点」这个场景不受影响。
MEDIA_URL_TTL = int(os.environ.get("TRAVEL_MEDIA_URL_TTL", "1800"))


# 同步水位：ops/sync-oss.sh 每跑完一趟，就把「本趟开始的时刻」写进这个文件。
# 含义是「mtime 早于这个时刻的本地文件，都已经在 OSS 上了」——
# 取开始而不是结束，是因为同步途中新落盘的文件不保证被那一趟扫到。
OSS_WATERMARK_FILE = BASE / "logs" / "oss-sync-watermark"
UPLOAD_ROOT = BASE / "uploads"

_wm_cache = {"at": 0.0, "value": None}


def _oss_watermark() -> float | None:
    """读同步水位，带 2 秒缓存。

    没有水位文件 = 没在跑同步（或刚部署），这时**不做判断**，返回 None 让
    media_url 保持老行为（一律指 OSS）—— 宁可沿用既有行为，也不要因为
    缺一个文件就把所有媒体流量全导回 4Mbps 的隧道。
    """
    import time
    now = time.monotonic()
    if now - _wm_cache["at"] < 2.0:
        return _wm_cache["value"]
    try:
        val = float(OSS_WATERMARK_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        val = None
    _wm_cache["at"] = now
    _wm_cache["value"] = val
    return val


def _synced_to_oss(path: str) -> bool:
    """这个 /uploads/ 路径对应的文件，是否已经同步到 OSS 了。"""
    wm = _oss_watermark()
    if wm is None:
        return True                       # 没水位就别拦，见 _oss_watermark
    rel = path[len("/uploads/"):].split("?", 1)[0]
    if not rel or ".." in rel:
        return True
    try:
        return (UPLOAD_ROOT / rel).stat().st_mtime <= wm
    except OSError:
        # 本地没有这个文件：可能是历史数据、可能本地被清理过，云端反而有。
        # 指回本机只会拿到 404，指 OSS 至少还有机会命中。
        return True


# 签名 URL 的过期时刻对齐到这么大的格子（秒）。
#
# 为什么要对齐：Expires 是签名的一部分，`now + ttl` 里的 now 每次调用都不同，
# 于是同一张图每刷新一次就换一个 URL，浏览器缓存键跟着变 —— OSS 那边
# Cache-Control 写着 max-age=86400，实际一次都没命中过。搜图尤其吃亏：
# 每搜一次就把整面墙重新下载一遍。
#
# 只往**后**取整，不往前：remaining ∈ (ttl, ttl+STEP]，保证签出来的链接
# 至少还有一个完整 ttl 可用。static/common.js 的 tmKeepFresh 和 share.js 的
# refreshSignatures 都是按「刚签的还有一个 ttl」来算续签时机的，
# 要是取整把寿命砍短了，那两处会在链接已经 403 之后才去续。
_EXPIRES_STEP = int(os.environ.get("TRAVEL_MEDIA_URL_STEP", "600"))


def _stable_expires(ttl: int) -> int:
    import time
    step = max(1, _EXPIRES_STEP)
    return ((int(time.time()) + int(ttl)) // step + 1) * step


def media_url(path: str, ttl: int | None = None, process: str = None) -> str:
    """把站内 /uploads/ 路径转成对外 URL。

    只认 /uploads/ 前缀 —— /static/ 是应用自己的 JS/CSS，必须跟着代码版本走，
    不能进对象存储（否则发版后前端和后端会不同步）。

    **刚上传还没同步到 OSS 的文件，返回本机路径**。媒体是「先落本地磁盘，
    再由 ops/sync-oss.sh 每 2 分钟增量推上去」，中间有最长 2 分多钟的空窗；
    原来这里无条件拼 OSS 前缀，于是刚传完的照片/视频封面全是裂图，
    要等下一趟同步才自己好。走本机那一下虽然占隧道带宽，但只是这几分钟内
    刚传的那几个文件，同步完就自动切回 OSS。

    ---

    **签名模式（2026-09-16 起）**：Bucket 关掉公共读之后，上面那条
    `MEDIA_BASE_URL + path` 拼出来的裸链会 403。这时改签带 Expires 的临时 URL
    （见 oss.sign_url）。签不出来（OSS 没配 / 还没同步上去）就退回本机路径 ——
    和「没同步」那条分支同一个出口，前端本来就能吃本机路径。

    为什么不在这里直接 `import oss`：config 是被所有人 import 的最底层模块
    （oss.py 自己也 import config 拿 MEDIA_BASE_URL），在模块顶层互相 import
    会成环。改成函数内 import，且 oss 只在真正要签名时才被拉进来。

    签不出来**不当故障处理**：Bucket 还是公共读的部署（以及本地开发）走的就是
    裸链那条分支，不能因为签名不可用就让整个媒体链挂掉。

    process 是 OSS 的 x-oss-process（缩放/转码），会**一起进签名**，所以不能
    由前端自己往 URL 上拼 —— 拼了签名就对不上。照片墙要的那几档见 media_srcset。
    裸链分支下 process 被忽略：本机分发没有服务端图片处理这回事。
    """
    if not (MEDIA_BASE_URL and path.startswith("/uploads/")):
        return path
    if not _synced_to_oss(path):
        return path

    # 签名可用就用签名（这是关掉公共读之后的唯一通路）
    try:
        import oss
        if oss.conf().ok:
            key = path.lstrip("/")
            life = ttl or MEDIA_URL_TTL
            signed = oss.sign_url(
                "GET", key,
                params={"x-oss-process": process} if process else None,
                expires_at=_stable_expires(life))
            if signed:
                return signed
    except Exception:                                     # noqa: BLE001
        # 降级不是故障：签不出来就回裸链，跟公共读时代行为一致。
        pass

    return MEDIA_BASE_URL + path


# 墙上一格要的几档宽度。浏览器拿 srcset 自己挑，挑中哪一档由 CSS 的 sizes
# （= 格子实际宽度 × DPR）决定，所以手机上永远只会去取 240 那一档。
#
# 为什么要分档：缩略图是 512 长边、40~60KB 的 JPEG，而墙上一格在手机上只有
# 95 CSS px。原样铺 60 张 = 2.6MB 下载 + 60 次 512² 解码，滚动当场掉帧。
# 240 那档实测平均 8.9KB（m_fill 方形裁切 + webp），字节少 4.8 倍、
# 解码面积少 4.5 倍。
#
# 240/480 从**缩略图**再缩（源才 512，OSS 不用去解那张 1.6MB 的原图）；
# 960 只能从原图出 —— 缩到 2 列时一格有 600+ CSS px，拿 512 的缩略图顶上去是糊的。
WALL_WIDTHS = ((240, "thumb"), (480, "thumb"), (960, "orig"))


def _wall_process(w: int) -> str:
    """墙上格子的 x-oss-process：方形裁切 + webp。

    m_fill 而不是 resize —— 格子本来就是 `aspect-ratio:1` + `object-fit:cover`，
    浏览器端裁掉的那部分根本不该下载。方形出图之后两边字节都省，显示效果一字不差。
    """
    return "image/resize,m_fill,w_%d,h_%d/format,webp/quality,q_80" % (w, w)


def media_srcset(filename: str, thumb_filename: str = None,
                 ttl: int | None = None) -> list[dict]:
    """给照片墙的一格算出 srcset 档位：[{"w":240,"url":...}, ...]。

    OSS 没配（本地开发、测试）时返回空列表 —— 调用方照旧用 thumb_url，
    行为和没有这个函数时完全一样。图片处理是 OSS 服务端的能力，本机分发
    这条路上没有等价物，硬造一个只会多一份代码两种行为。
    """
    try:
        import oss
        if not (MEDIA_BASE_URL and oss.conf().ok):
            return []
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for w, src in WALL_WIDTHS:
        if src == "thumb":
            if not thumb_filename:
                continue
            path = "/uploads/thumbs/" + thumb_filename
        else:
            path = "/uploads/" + filename
        u = media_url(path, ttl=ttl, process=_wall_process(w))
        # 没同步上去的那些 media_url 会回本机路径，带不了图片处理，
        # 混进 srcset 只会让浏览器挑到一张没缩过的原图。
        if u.startswith("http"):
            out.append({"w": w, "url": u})
    return out
