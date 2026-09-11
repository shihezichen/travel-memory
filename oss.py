"""阿里云 OSS 客户端 —— 只为「浏览器直传」这一件事而存在。

为什么要直传：公网入口 139.129.24.132 的 frp 隧道实测只有 4Mbps（单条 TCP，
3.8% 重传）。视频从浏览器传到本机，全程挤在这条隧道里 —— 一段 150MB 的视频
理论最快也要 5 分钟，而且期间整站的 API 都跟着排队。
改成浏览器直接往 OSS 传之后，这段流量走的是用户自己的上行带宽，一个字节都不经过
隧道；本机再在后台不慌不忙地把对象拉回来存档（见 oss_backfill.py）。

这里**不引入 oss2 依赖**：需要的只是 V1 签名 + 六个 HTTP 调用，用已有的 httpx
就够了；多一个 SDK 就多一处要跟着升级、要在 Top001 热备上同步装好的东西。

凭据来源刻意和 ops/sync-oss.sh 一致 —— 都读 ~/.ossutilconfig，不再复制一份 AK
到别的文件里。桶名和 endpoint 从 config.MEDIA_BASE_URL 反推，于是
media_base.txt 仍然是**唯一那个开关**：置空即整套回退到本机上传 + 本机分发。
"""
import base64
import datetime
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import httpx

from config import MEDIA_BASE_URL

log = logging.getLogger("travel.oss")

# 签名里要算进 CanonicalizedResource 的子资源。OSS 的完整清单几十项，这里只列
# 我们真正会用到的三个 —— 多列不会错，少列才会签错，而用不到的列进来只是噪音。
SIGNED_SUBRESOURCES = ("partNumber", "uploadId", "uploads")

# 分片大小。OSS 要求除最后一片外每片 ≥ 100KB、片数 ≤ 10000。
# 取 8MB 而不是前端原来那个 4MB：直传没有「服务端逐片落盘」的开销，片子大一点
# 就少一轮握手；8MB × 10000 片 = 80GB，远在上传上限之上，不可能撞到片数天花板。
PART_SIZE = int(os.environ.get("TRAVEL_OSS_PART_SIZE", str(8 * 1024 * 1024)))
PART_SIZE_MIN = 100 * 1024
MAX_PARTS = 10000

# 签出去的分片 URL 活多久。够传完一片即可，短一点，泄漏的窗口就小一点。
URL_TTL = int(os.environ.get("TRAVEL_OSS_URL_TTL", "3600"))

_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=300.0, pool=10.0)


class OSSError(RuntimeError):
    """OSS 返回了非 2xx。message 里带上 OSS 自己的 Code，方便排障。"""


# ---------------------------------------------------------------- 配置

def _read_ossutil_config() -> dict:
    """解析 ~/.ossutilconfig 的 [Credentials] 段。文件不在就返回空 dict。"""
    path = Path(os.environ.get("OSSUTIL_CONFIG_FILE",
                               str(Path.home() / ".ossutilconfig")))
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "[")):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _split_media_base(url: str):
    """https://<bucket>.<endpoint>/ -> (bucket, endpoint)。认不出就 (None, None)。"""
    m = re.match(r"^https?://([^./]+)\.((?:oss-|[\w-]+\.)[^/]+)/?$", url or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


class _Conf:
    """配置快照。每次取都重新算 —— 测试会 monkeypatch 环境变量和文件。"""

    def __init__(self):
        base = os.environ.get("TRAVEL_MEDIA_BASE_URL", MEDIA_BASE_URL).rstrip("/")
        self.bucket, self.endpoint = _split_media_base(base)
        self.base_url = base
        cfg = _read_ossutil_config()
        self.ak = os.environ.get("TRAVEL_OSS_AK") or cfg.get("accessKeyID") or ""
        self.sk = os.environ.get("TRAVEL_OSS_SK") or cfg.get("accessKeySecret") or ""
        # 显式关掉直传（CORS 还没配好、或者想临时退回隧道）时用
        self.direct = os.environ.get("TRAVEL_OSS_DIRECT", "1") not in ("0", "false", "no")

    @property
    def ok(self) -> bool:
        return bool(self.bucket and self.endpoint and self.ak and self.sk)


def conf() -> _Conf:
    return _Conf()


def direct_upload_enabled() -> bool:
    """前端该不该走直传。配置不全 / 被显式关掉 -> False，链路完整回退到隧道上传。"""
    c = conf()
    return c.ok and c.direct


def object_url(key: str) -> str:
    return f"{conf().base_url}/{quote(key, safe='/')}"


# ---------------------------------------------------------------- 签名

def _canonical_resource(bucket: str, key: str, params: dict) -> str:
    sub = sorted((k, v) for k, v in (params or {}).items() if k in SIGNED_SUBRESOURCES)
    res = f"/{bucket}/{key}"
    if sub:
        res += "?" + "&".join(k if v in (None, "") else f"{k}={v}" for k, v in sub)
    return res


def _signature(sk: str, method: str, date_or_expires: str, content_type: str,
               content_md5: str, canonical_resource: str) -> str:
    # OSS V1：CanonicalizedOSSHeaders 这一段我们一个 x-oss-* 头都不发，所以是空串，
    # 直接把 CanonicalizedResource 接在 Date 行后面。
    to_sign = "\n".join([method, content_md5, content_type,
                         date_or_expires, canonical_resource])
    mac = hmac.new(sk.encode(), to_sign.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


def sign_url(method: str, key: str, params: dict = None, content_type: str = "",
             expires_in: int = URL_TTL) -> str:
    """签一个给**浏览器**直接用的临时 URL。

    浏览器发请求时必须原样带上这里签进去的 Content-Type，否则签名对不上（403）。
    """
    c = conf()
    if not c.ok:
        raise OSSError("OSS 未配置")
    import time
    expires = str(int(time.time()) + int(expires_in))
    sig = _signature(c.sk, method.upper(), expires, content_type, "",
                     _canonical_resource(c.bucket, key, params))
    qs = dict(params or {})
    qs.update({"OSSAccessKeyId": c.ak, "Expires": expires, "Signature": sig})
    query = "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
                     for k, v in qs.items())
    return f"{c.base_url}/{quote(key, safe='/')}?{query}"


def _request(method: str, key: str, params: dict = None, content_type: str = "",
             data=None, extra_headers: dict = None, stream: bool = False):
    """服务端自己发的 OSS 请求（Authorization 头那种签法）。"""
    c = conf()
    if not c.ok:
        raise OSSError("OSS 未配置")
    date = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    sig = _signature(c.sk, method.upper(), date, content_type, "",
                     _canonical_resource(c.bucket, key, params))
    headers = {
        "Date": date,
        "Authorization": f"OSS {c.ak}:{sig}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra_headers or {})
    url = f"{c.base_url}/{quote(key, safe='/')}"
    # trust_env=False：OSS 在国内，走开发环境里那个 SOCKS/HTTP 代理只会更慢（还可能
    # 因为缺 socksio 直接起不来）。这条链路是机器到机器的，不需要任何代理。
    client = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False,
                          trust_env=False)
    if stream:
        # 调用方负责 close：流式下载要边读边写，不能在这里把连接关掉
        return client, client.stream(method.upper(), url, params=params or None,
                                     headers=headers, content=data)
    try:
        r = client.request(method.upper(), url, params=params or None,
                           headers=headers, content=data)
    finally:
        client.close()
    if r.status_code >= 300:
        code = ""
        try:
            code = (ET.fromstring(r.text).findtext("Code") or "")
        except Exception:
            pass
        raise OSSError(f"OSS {method} {key} -> {r.status_code} {code}".strip())
    return r


# ---------------------------------------------------------------- 分片直传

def guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


def init_multipart(key: str, filename: str = "", cache_control: str = None) -> str:
    """开一个分片上传，返回 uploadId。

    Content-Type / Cache-Control 只在这一步定，之后各分片带什么都不影响成品对象。
    缓存策略跟 ops/sync-oss.sh 对齐：原片 1 天（转码可能原地覆盖它）。
    """
    headers = {}
    cc = cache_control or "public, max-age=86400"
    if cc:
        headers["Cache-Control"] = cc
    r = _request("POST", key, params={"uploads": ""},
                 content_type=guess_content_type(filename or key),
                 extra_headers=headers)
    upload_id = ET.fromstring(r.text).findtext("UploadId")
    if not upload_id:
        raise OSSError("InitiateMultipartUpload 没返回 UploadId")
    return upload_id


def sign_part_url(key: str, upload_id: str, part_number: int,
                  expires_in: int = URL_TTL) -> str:
    """签一片的直传 URL。

    分片一律按 application/octet-stream 签 —— 成品对象的 Content-Type 在 initiate
    时已经定死，分片带什么都不影响它，统一成一个值就不用为每种文件类型各签一套。
    """
    return sign_url("PUT", key,
                    params={"partNumber": str(int(part_number)), "uploadId": upload_id},
                    content_type="application/octet-stream", expires_in=expires_in)


def list_parts(key: str, upload_id: str) -> list:
    """已经传上去的分片。断点续传靠它 —— **以 OSS 的记账为准**，不信客户端。"""
    out = []
    marker = "0"
    while True:
        params = {"uploadId": upload_id, "max-parts": "1000",
                  "part-number-marker": marker}
        r = _request("GET", key, params=params)
        root = ET.fromstring(r.text)
        ns = ""
        for p in root.findall(f"{ns}Part"):
            out.append({
                "number": int(p.findtext("PartNumber") or 0),
                "etag": (p.findtext("ETag") or "").strip('"'),
                "size": int(p.findtext("Size") or 0),
            })
        if (root.findtext("IsTruncated") or "false").lower() != "true":
            break
        marker = root.findtext("NextPartNumberMarker") or ""
        if not marker:
            break
    return out


def complete_multipart(key: str, upload_id: str, parts: list) -> dict:
    """把分片拼成对象。parts = [{'number': 1, 'etag': '...'}, ...]，按序号升序。"""
    parts = sorted(parts, key=lambda p: int(p["number"]))
    body = ["<CompleteMultipartUpload>"]
    for p in parts:
        etag = str(p["etag"]).strip('"')
        body.append(f"<Part><PartNumber>{int(p['number'])}</PartNumber>"
                    f"<ETag>\"{etag}\"</ETag></Part>")
    body.append("</CompleteMultipartUpload>")
    xml = "".join(body).encode()
    r = _request("POST", key, params={"uploadId": upload_id},
                 content_type="application/xml", data=xml)
    root = ET.fromstring(r.text)
    return {"etag": (root.findtext("ETag") or "").strip('"'),
            "url": object_url(key)}


def abort_multipart(key: str, upload_id: str):
    """放弃分片上传，顺手让 OSS 把已传的碎片删掉（不然它们会一直计费）。"""
    try:
        _request("DELETE", key, params={"uploadId": upload_id})
    except OSSError as e:
        log.warning("abort_multipart 失败 key=%s: %s", key, e)


def head_object(key: str) -> dict:
    """对象在不在、多大。complete 之后拿它做一次核对，不听客户端报的大小。"""
    r = _request("HEAD", key)
    return {"size": int(r.headers.get("content-length") or 0),
            "etag": (r.headers.get("etag") or "").strip('"'),
            "last_modified": r.headers.get("last-modified") or "",
            "content_type": r.headers.get("content-type") or ""}


def delete_object(key: str):
    try:
        _request("DELETE", key)
    except OSSError as e:
        log.warning("delete_object 失败 key=%s: %s", key, e)


def download_to(key: str, dest: Path, expect_size: int = None) -> int:
    """把对象拉到本地。先写 .part 再原子改名 —— 半个文件绝不能被别的环节看见。

    返回落盘字节数。大小和 expect_size 对不上就当失败（删掉半成品并抛异常）。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    client, ctx = _request("GET", key, stream=True)
    try:
        with ctx as r:
            if r.status_code >= 300:
                r.read()
                raise OSSError(f"OSS GET {key} -> {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    written += len(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        client.close()
    if expect_size is not None and written != int(expect_size):
        tmp.unlink(missing_ok=True)
        raise OSSError(f"回源大小不符：期望 {expect_size}，实际 {written}")
    os.replace(tmp, dest)
    return written


def plan_parts(filesize: int, part_size: int = None) -> tuple:
    """(片大小, 片数)。片数超上限就把片调大，别让大文件直接传不了。"""
    ps = max(PART_SIZE_MIN, int(part_size or PART_SIZE))
    size = max(1, int(filesize))
    total = (size + ps - 1) // ps
    while total > MAX_PARTS:
        ps *= 2
        total = (size + ps - 1) // ps
    return ps, total


# ---------------------------------------------------------------- 本机文件直推

def put_object(key: str, path, content_type: str = "", cache_control: str = None) -> dict:
    """把本机一个文件整体 PUT 上去（覆盖同名对象）。返回 {'etag', 'url'}。

    单次 PUT 的上限是 5GB，而上传接口对视频的限额远在这之下，所以不用分片。
    body 直接传文件对象，httpx 会流式读，不会把几百 MB 抓进内存。
    """
    path = Path(path)
    ct = content_type or guess_content_type(path.name)
    headers = {"Content-Length": str(path.stat().st_size)}
    if cache_control:
        headers["Cache-Control"] = cache_control
    with open(path, "rb") as f:
        r = _request("PUT", key, content_type=ct, data=f, extra_headers=headers)
    return {"etag": (r.headers.get("etag") or "").strip('"'), "url": object_url(key)}


def publish_local(path, upload_root=None) -> bool:
    """把 uploads/ 下的一个文件立刻推到 OSS，并把 mtime 压到同步水位以下。

    为什么需要它（这是实打实踩过的一条路）：直传的原片本来已经在 OSS 上了，
    但 transcode.ensure_faststart 会**原地重写**它（moov 前移），于是
      1. 本地 mtime 变新 → config._synced_to_oss() 判定「还没同步」→
         media_url 把链接指回本机 → 播放流量重新挤回 4Mbps 的 frp 隧道；
      2. 要等下一趟 ops/sync-oss.sh（每 2 分多钟一趟）扫到才恢复。
    档位文件（variants/）刚转出来时同理。字节数和 sync-oss 稍后要传的完全一样，
    只是把时机从「最多两分半之后」提前到「转完就传」，那个回落窗口直接没有了。

    缓存策略和 ops/sync-oss.sh 对齐：variants/ 一年 immutable，其余 1 天。
    返回 True = 已推上去。任何一步失败都只记日志返回 False —— sync-oss 还会兜底。
    """
    import config
    path = Path(path)
    root = Path(upload_root or config.UPLOAD_ROOT)
    if not direct_upload_enabled():
        return False
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        log.debug("publish_local 跳过（不在 uploads/ 下）：%s", path)
        return False
    cc = ("public, max-age=31536000, immutable" if rel.startswith("variants/")
          else "public, max-age=86400")
    try:
        put_object("uploads/" + rel, path, cache_control=cc)
    except Exception as e:
        log.warning("直推 OSS 失败（等 sync-oss 兜底）%s: %s", rel, e)
        return False
    # 推上去了就把 mtime 压到水位以下，media_url 才会继续指向 OSS
    # （同样的理由见 oss_backfill._touch_from_oss）。没有水位文件时不用动。
    wm = config._oss_watermark()
    if wm is not None:
        try:
            ts = min(path.stat().st_mtime, wm - 1)
            os.utime(path, (ts, ts))
        except OSError as e:
            log.debug("publish_local 对齐 mtime 失败 %s: %s", rel, e)
    log.info("已直推 OSS: %s", rel)
    return True
