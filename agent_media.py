"""Agent 链路的媒体链接签名。

**只给开放平台（/api/ai/*、/mcp）用**，网页展示链路（config.media_url）不动。

为什么需要这一层：API Key 保护的是「调平台 API」这道门，但过去平台返回给
Agent 的是一个 OSS 裸链接 —— Agent 拿到链接后绕过平台直接访问 OSS，Key 就
管不着了。而且 OSS 上的对象目前是 public-read（app.py:138 为了让分享页匿名
看图，把整个 /uploads/ 放行了），所以那条裸链其实谁拿到谁能用，且永久有效、
无法吊销。

这一层把 Agent 拿到的链接换成**带签名 + 有效期**的临时链接：

- 链接里签了 Expires，到点自动失效，不需要吊销动作
- 只对调用了 API 的人签发；网页照旧走裸链（体验不变）
- **不改对象 ACL**，所以不会影响任何现存媒体、也不碰同步定时器

有效期默认 24 小时 —— 比 oss.URL_TTL（1 小时，给上传分片用的）长得多，
因为 Agent 拿到链接后往往要隔一会儿才去取，1 小时太容易过期中途失败。
"""
import logging
import os
from urllib.parse import quote

import oss
from config import MEDIA_BASE_URL

log = logging.getLogger("travel.agent_media")

# Agent 链接的有效期。取 24 小时：Agent 会先列媒体、再挑几张下载，
# 中间可能隔很久；太短会半路 403，体验很差。
AGENT_URL_TTL = int(os.environ.get("TRAVEL_AGENT_URL_TTL", "86400"))


def _key_for(local_path: str):
    """站内 /uploads/ 路径 -> OSS 对象 key。只认 /uploads/ 前缀。

    本地没同步到 OSS 的文件（上传后最长 2 分钟空窗）在 OSS 上还不存在，
    这时签名没意义（签了也是 404），返回 None 让调用方回落到本机路径。
    """
    if not local_path or not local_path.startswith("/uploads/"):
        return None
    return local_path.lstrip("/")


def is_available() -> bool:
    """签名链路是否可用（OSS 配好了 + bucket 认得出）。"""
    return bool(MEDIA_BASE_URL) and oss.conf().ok


def signed_url(local_path: str, download: bool = False,
               filename: str = "", ttl: int = None):
    """把一个站内 /uploads/ 路径签成带有效期的临时 URL。

    签不出来（OSS 没配 / 文件还没同步上去）时返回 None，**绝不抛异常** ——
    这个函数是在读接口的热路径上调用的，为一个链接把整个列表接口打挂不值得。
    download=True 时签「另存为」链（带 Content-Disposition，浏览器点开就下载，
    而不是内联预览）。
    """
    key = _key_for(local_path)
    if not key or not is_available():
        return None
    try:
        if download:
            disp = _disposition(filename or local_path.rsplit("/", 1)[-1])
            return oss.sign_download_url(key, disp,
                                         expires_in=ttl or AGENT_URL_TTL)
        return oss.sign_url("GET", key, expires_in=ttl or AGENT_URL_TTL)
    except Exception as e:                                    # noqa: BLE001
        # 签不出来是「降级」不是「故障」：调用方会回落到裸链或本机路径。
        log.warning("签名失败 %s: %s", key, e)
        return None


def _disposition(filename: str) -> str:
    """构造 Content-Disposition，中文名用 RFC 5987 编码。

    直接塞中文进 filename= 会被 OSS 拒绝或变乱码，所以要 filename* 那套。
    同时留一个 ASCII 回退名，给老浏览器兜底。
    """
    safe = quote(filename, safe="")
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{safe}"


def _fallback(local_path: str):
    """签不出来时的回落：本机路径（跟网页链路一致，保证不是死链）。"""
    return local_path or None


def urls_for(local_path: str, download: bool = False, filename: str = ""):
    """对外主入口：优先签名，签不出来回落裸链。

    返回的链接保证可用（除非文件本身不存在），Agent 不会拿到 None 当 URL。
    """
    if not local_path:
        return None
    signed = signed_url(local_path, download=download, filename=filename)
    if signed:
        return signed
    # 回落到网页同款裸链（可能是本机路径）。功能可用，只是没有过期语义。
    from config import media_url
    return media_url(local_path)


def enrich_media(m: dict, thumb: bool = True) -> dict:
    """给一条 media 记录补上 Agent 用的链接字段。

    **只加字段、不改已有字段**（兼容铁律：老 Agent 可能还在读 filename）。
    新增：
      - url            原图/原视频的签名链接（24h）
      - download_url   另存为链接（带文件名，浏览器直接下载）
      - thumb_url      缩略图签名链接（视频/大图友好）
    """
    out = dict(m)
    fn = m.get("filename")
    if fn:
        out["url"] = urls_for(f"/uploads/{fn}")
        out["download_url"] = urls_for(f"/uploads/{fn}", download=True,
                                       filename=fn)
    thumb_fn = m.get("thumb_filename")
    if thumb and thumb_fn:
        # 缩略图可能跟原图同名（小图场景，db 里 thumb_filename == filename），
        # 但路径前缀不同，各签各的没问题。
        out["thumb_url"] = urls_for(f"/uploads/thumbs/{thumb_fn}")
    return out
