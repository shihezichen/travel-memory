"""媒体下载：把 /uploads/ 里的原图 / 原片 / 转码档位，以「另存为」的方式吐给浏览器。

为什么不能直接给 /uploads/<uuid>.mp4 的链接
------------------------------------------
那个链接的 Content-Type 是 video/mp4，浏览器一律**内联播放**；`<a download>`
在 iOS Safari / 微信 webview 里又基本失效（见 static/common.js 的 tmTriggerDownload）。
只有服务端打上 `Content-Disposition: attachment` 才是所有端都认的「这是下载」。

这里只做三件事，鉴权**不在**本模块
--------------------------------
  1) 把 quality 参数解析成盘上的一个真实文件（白名单，杜绝 ../）；
  2) 拼一个人能看懂的文件名（库里没存原始文件名，见下）；
  3) 组装 FileResponse。

**鉴权是调用方的责任**，本模块刻意不碰 request：
  - app.py  的 /api/media/{id}/download        → require_owner（本人/管理员）
  - share.py 的 /api/share/{token}/media/{id}/download → 校验 token + 密码 + 归属
这样「能不能下」就和「能不能看」一一对应，不会因为多了个下载口而放宽一档。

文件名：为什么是拼出来的
----------------------
media 表里**没有**原始文件名这一列（只有 uuid 的 filename），为了一个下载功能去改表
不划算。所以按「拍摄时间 / 地点 / 序号」拼一个：`旅行记忆-20260910-京都-01.mp4`。
中文名必须走 RFC 5987 的 `filename*=UTF-8''…`，同时留一个纯 ASCII 的 `filename=`
兜底 —— 只给 `filename*` 的话，老浏览器（以及一些 webview）会拿到乱码文件名。
"""
import mimetypes
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

import db
import transcode

# 下载文件名的前缀。ASCII 兜底名用的是它的拼音/英文对应物。
NAME_PREFIX = "旅行记忆"
ASCII_PREFIX = "travel-memory"

# quality 白名单。**只认这几个字面量** —— 参数会参与选文件，
# 放开了就等于把 `../../data.db` 这种路径拼接的口子留给外面。
ALLOWED_QUALITIES = ("original", *[label for label, _ in transcode.LADDER])

# 文件名里不能出现的字符：路径分隔符、Windows 保留字符、控制字符。
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WS = re.compile(r"\s+")


def _safe_part(text: str, limit: int = 40) -> str:
    """把「地点名」这类用户输入洗成能进文件名的一段。

    只做删减不做替换（除了空白折成连字符）：宁可名字短一点，也不要造出
    带路径分隔符或前导点的名字。
    """
    s = _BAD_NAME_CHARS.sub("", text or "")
    s = _WS.sub("-", s).strip("-. ")
    # 连续的连字符压成一个，纯连字符则视为空
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:limit]


def _ascii_part(text: str, limit: int = 40) -> str:
    """尽力把一段文字压成 ASCII（去掉重音符号，丢掉汉字等无法转写的字符）。

    转不出东西来是正常结果（纯中文地名就会转成空），调用方自行降级 —— 这里
    刻意不引入拼音库，一个下载文件名不值得多一个依赖。
    """
    norm = unicodedata.normalize("NFKD", text or "")
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    return _safe_part(ascii_only, limit)


def _safe_ext(ext: str) -> str:
    """扩展名单独洗：不能走 _safe_part，那个会把开头的点当垃圾字符剥掉。

    扩展名来自盘上文件的 suffix（uuid 生成的名字，本来就是 ASCII），
    这里只是防脏数据；洗完仍是空就返回空串，让文件名干脆没有后缀。
    """
    e = _BAD_NAME_CHARS.sub("", ext or "")
    e = _WS.sub("", e).lstrip(".")
    e = e.encode("ascii", "ignore").decode("ascii")
    return ("." + e[:11]) if e else ""


def _date_tag(media_row) -> str:
    """拍摄日期 → YYYYMMDD。优先 EXIF 拍摄时间，没有就退到入库时间。

    库里这两列的格式并不统一（EXIF 是 `2026:09:10 12:00:00`，created_at 是
    `2026-09-10 12:00:00`），所以不解析格式，直接抽数字取前 8 位 —— 两种都对。
    """
    for key in ("exif_time", "created_at"):
        raw = media_row.get(key)
        if not raw:
            continue
        digits = re.sub(r"\D", "", str(raw))
        if len(digits) >= 8:
            return digits[:8]
    return ""


def _sequence_no(media_row) -> str:
    """这张图在所在轨迹点里的序号（两位，从 01 起）。

    用的是 db.list_media 的展示顺序，也就是用户在灯箱里看到的顺序 ——
    下载下来的 `-03` 就对应他翻到的第 3 张，这是这个序号唯一的意义。
    """
    try:
        ids = [m["id"] for m in db.list_media(media_row["point_id"])]
        return f"{ids.index(media_row['id']) + 1:02d}"
    except (ValueError, KeyError, TypeError):
        # 排序里找不到自己（并发删除等）：退回用 id，至少保证唯一
        return str(media_row.get("id") or 0)


def build_filenames(media_row, ext: str, quality: str = "original"):
    """拼出 (UTF-8 显示名, ASCII 兜底名)。

    形如 `旅行记忆-20260910-京都-01.mp4`；非原画档位再缀上档位名
    （`…-01-720p.mp4`），否则同一个视频下两档会重名互相覆盖。
    """
    point = db.get_point(media_row["point_id"]) or {}
    place = _safe_part(point.get("name") or "")
    date = _date_tag(media_row)
    seq = _sequence_no(media_row)
    tail = "" if quality == "original" else f"-{_safe_part(quality, 12)}"
    suffix = _safe_ext(ext)

    parts = [NAME_PREFIX, date, place, seq]
    utf8_name = "-".join(p for p in parts if p) + tail + suffix

    # ASCII 兜底名：地名转写不出来就直接省略，只留「前缀-日期-序号」。
    # 它是给老浏览器看的，可读即可，不追求和 UTF-8 名一一对应。
    a_parts = [ASCII_PREFIX, date, _ascii_part(point.get("name") or ""), seq]
    ascii_name = "-".join(p for p in a_parts if p) + tail + suffix
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = f"{ASCII_PREFIX}-{media_row['id']}{suffix}"
    return utf8_name, ascii_name


def content_disposition(utf8_name: str, ascii_name: str) -> str:
    """RFC 6266 + RFC 5987 的双写法。

    `filename=` 里只能出现 ASCII，且要防着引号把 header 拆开；
    `filename*=` 用 UTF-8 百分号编码，现代浏览器优先认它。两个都给，才两头都对。
    """
    safe_ascii = ascii_name.replace('"', "").replace("\\", "")
    encoded = quote(utf8_name, safe="")
    return f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"


def resolve_file(media_row, quality: str, upload_dir: Path):
    """(quality, media) → 盘上的真实路径。越权/不存在一律 HTTPException。

    路径安全有两道：quality 走字面量白名单（根本拼不出 `../`），拿到路径后
    再断言它确实落在 upload_dir 里面（防的是库里被写进脏 filename 的情况）。
    """
    quality = (quality or "original").strip() or "original"
    if quality not in ALLOWED_QUALITIES:
        raise HTTPException(400, "不支持的清晰度")

    upload_dir = Path(upload_dir)
    if quality == "original":
        rel = media_row["filename"]
        path = upload_dir / rel
    else:
        if media_row["type"] != "video":
            raise HTTPException(404, "该清晰度不存在")
        v = db.get_variant(media_row["id"], quality)
        if not v or v["status"] != "done" or not v["filename"]:
            raise HTTPException(404, "该清晰度还没转好")
        path = upload_dir / "variants" / v["filename"]

    # 兜底越界检查：resolve() 之后必须还在 upload_dir 下面
    root = upload_dir.resolve()
    try:
        real = path.resolve()
        real.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(404, "文件不存在")
    if not real.is_file():
        raise HTTPException(404, "文件不存在")
    return real, quality


def _oss_rel_path(media_row, quality: str):
    """(media, quality) → 站内相对路径 `/uploads/...`。拿不到就 None。

    和 resolve_file() 的映射规则一致，但**不碰磁盘** —— 本地有没有这个文件，
    正是调用方要判断的事。
    """
    if quality == "original":
        return "/uploads/" + media_row["filename"]
    if media_row["type"] != "video":
        return None
    v = db.get_variant(media_row["id"], quality)
    if not v or v["status"] != "done" or not v["filename"]:
        return None
    return "/uploads/variants/" + v["filename"]


def _is_on_oss(local_path) -> bool:
    """这份文件云上有没有。local_path=None 表示本机根本没有。

    判据和 config.media_url 一致 —— 和同步水位比 mtime。这里刻意**自己比**而不是
    调 config._synced_to_oss()：那个函数认死了 config.UPLOAD_ROOT，而下载链路的
    uploads 目录是调用方传进来的（测试里会指到 tmp）。用传进来的真实路径比 mtime，
    两边就不会各说各话。
    """
    from config import _oss_watermark
    if local_path is None:
        return True                       # 直传刚传完、还没回源：只有云上有
    wm = _oss_watermark()
    if wm is None:
        return True                       # 没在跑同步：沿用「一律指 OSS」的老行为
    try:
        return local_path.stat().st_mtime <= wm
    except OSError:
        return True


def _oss_redirect(media_row, quality: str, local_path=None):
    """能走 OSS 就重定向过去下载，回 None 表示「只能由本机吐」。

    **这是下载链路上最贵的一段**：原来只在「本地文件不存在」时才重定向，于是
    回源存档一跑完，本地有了文件，一段 130MB 的视频就改成由本机 FileResponse
    吐出去 —— 整整 130MB 全挤在 4Mbps 的 frp 隧道里（见 ops/sync-oss.sh 顶部的
    实测），而同一份字节在 OSS 上躺着没人用。现在反过来：**只要云上有，就走云**。

    判据见 _is_on_oss()，它覆盖了三种情况：
      - 本地没有这个文件（直传刚传完、还没回源）→ True，云上才有
      - 本地有且 mtime 早于同步水位 → True，两边都有，走云
      - 本地有但比水位新（刚重封装/刚转好还没推上去）→ False，老实走本机
    档位文件也一样能走云：ops/sync-oss.sh 一直在同步 variants/，
    transcode 转完还会立刻直推（见 oss.publish_local）。

    链接必须**签名**：对象虽然是 public-read，但 OSS 不允许匿名请求覆盖响应头
    （回 400 InvalidRequest "Can not override response header for an anonymous
    user"）。所以这里走 oss.sign_download_url，而不是自己拼 query。
    签不出来（没配 AK）就返回 None，老实由本机吐 —— 宁可慢也不能给一个 400。
    """
    import oss
    from config import MEDIA_BASE_URL
    if not MEDIA_BASE_URL or not oss.conf().ok:
        return None
    rel = _oss_rel_path(media_row, quality)
    if not rel or not _is_on_oss(local_path):
        return None
    ext = Path(rel).suffix.lower()
    utf8_name, ascii_name = build_filenames(media_row, ext, quality)
    disp = content_disposition(utf8_name, ascii_name)
    try:
        url = oss.sign_download_url(rel.lstrip("/"), disp)    # /uploads/x -> uploads/x
    except oss.OSSError:
        return None
    # 307 而不是 302：语义上是「同一个请求换个地址再发一次」，别让中间层把它
    # 改写成 GET（这条本来就是 GET，但 307 的意图更明确，也不会被缓存成永久跳转）
    return RedirectResponse(url, status_code=307)


def build_response(media_id: int, quality: str, upload_dir: Path) -> FileResponse:
    """组装下载响应。调用方**必须**在此之前完成鉴权。"""
    m = db.get_media(media_id)
    if not m:
        raise HTTPException(404, "媒体不存在")
    quality = (quality or "original").strip() or "original"
    try:
        path, quality = resolve_file(m, quality, upload_dir)
    except HTTPException as e:
        # 只接「文件不存在」，别把 400/越权也顺手转成重定向
        if e.status_code != 404:
            raise
        redirect = _oss_redirect(m, quality)
        if redirect is None:
            raise
        return redirect

    # 本地有这个文件，但云上也有的话仍然走云 —— 省下的是整条文件的隧道带宽
    redirect = _oss_redirect(m, quality, local_path=path)
    if redirect is not None:
        return redirect

    utf8_name, ascii_name = build_filenames(m, path.suffix.lower(), quality)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=mime,
        headers={
            "Content-Disposition": content_disposition(utf8_name, ascii_name),
            # 下载链接带着 token / 密码，别让中间层缓存下来
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )
