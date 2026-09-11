"""OSS V1 签名 + 分片规划。

签名对了没有，本地没法靠「调用成功」来验证（那要真打阿里云）。所以这里钉的是
**签名串的构造**：拿固定的 AK/SK/时间算出确定的结果，任何一处顺序、换行、
子资源的改动都会让它变红。串对了签名就对了 —— 剩下的是 hmac，不会错。
"""
import base64
import hashlib
import hmac

import pytest

import oss


@pytest.fixture(autouse=True)
def fixed_creds(monkeypatch):
    monkeypatch.setenv("TRAVEL_MEDIA_BASE_URL",
                       "https://demo-bucket.oss-cn-qingdao.aliyuncs.com")
    monkeypatch.setenv("TRAVEL_OSS_AK", "AK-TEST")
    monkeypatch.setenv("TRAVEL_OSS_SK", "SK-TEST")
    monkeypatch.setenv("TRAVEL_OSS_DIRECT", "1")


def test_桶名和_endpoint_从媒体前缀反推():
    c = oss.conf()
    assert c.bucket == "demo-bucket"
    assert c.endpoint == "oss-cn-qingdao.aliyuncs.com"
    assert c.ok is True
    assert oss.direct_upload_enabled() is True


def test_媒体前缀为空时直传自动关闭(monkeypatch):
    monkeypatch.setenv("TRAVEL_MEDIA_BASE_URL", "")
    assert oss.conf().ok is False
    assert oss.direct_upload_enabled() is False


def test_显式关闭开关能挡住直传(monkeypatch):
    monkeypatch.setenv("TRAVEL_OSS_DIRECT", "0")
    assert oss.conf().ok is True          # 配置还在
    assert oss.direct_upload_enabled() is False   # 但不许用


def test_缺凭据时直传关闭(monkeypatch):
    monkeypatch.setenv("TRAVEL_OSS_SK", "")
    # ~/.ossutilconfig 可能真的存在（开发机上就有），指到一个不存在的文件上
    monkeypatch.setenv("OSSUTIL_CONFIG_FILE", "/nonexistent/ossutilconfig")
    assert oss.direct_upload_enabled() is False


def test_只有白名单里的子资源进签名串():
    # partNumber / uploadId 要进；max-parts 这种普通查询参数不进 —— 进了就签错
    res = oss._canonical_resource("b", "uploads/x.mp4",
                                  {"partNumber": "3", "uploadId": "U1",
                                   "max-parts": "1000"})
    assert res == "/b/uploads/x.mp4?partNumber=3&uploadId=U1"


def test_子资源按字典序排序():
    a = oss._canonical_resource("b", "k", {"uploadId": "U", "partNumber": "1"})
    b = oss._canonical_resource("b", "k", {"partNumber": "1", "uploadId": "U"})
    assert a == b == "/b/k?partNumber=1&uploadId=U"


def test_空值子资源不带等号():
    # InitiateMultipartUpload 是 POST /key?uploads，签名串里就是裸的 "uploads"
    assert oss._canonical_resource("b", "k", {"uploads": ""}) == "/b/k?uploads"


def test_签名串就是五行拼起来的():
    sig = oss._signature("SK-TEST", "PUT", "1700000000",
                         "application/octet-stream", "", "/b/k?partNumber=1")
    want = base64.b64encode(hmac.new(
        b"SK-TEST",
        b"PUT\n\napplication/octet-stream\n1700000000\n/b/k?partNumber=1",
        hashlib.sha1).digest()).decode()
    assert sig == want


def test_分片_url_带齐三个签名参数():
    url = oss.sign_part_url("uploads/a.mp4", "UPID", 2)
    assert url.startswith("https://demo-bucket.oss-cn-qingdao.aliyuncs.com/uploads/a.mp4?")
    for k in ("partNumber=2", "uploadId=UPID", "OSSAccessKeyId=AK-TEST",
              "Expires=", "Signature="):
        assert k in url


def test_签名里的_content_type_必须和客户端发的一致():
    """分片一律按 application/octet-stream 签 —— 前端 putOnePartToOss 也写死这个值。

    这条断言的意义：谁改了其中一边而没改另一边，OSS 会回 403，
    而 403 在浏览器里长得就像「CORS 没配好」，极难排查。
    """
    url = oss.sign_part_url("uploads/a.mp4", "U", 1)
    expires = url.split("Expires=")[1].split("&")[0]
    want = oss._signature("SK-TEST", "PUT", expires, "application/octet-stream", "",
                          "/demo-bucket/uploads/a.mp4?partNumber=1&uploadId=U")
    from urllib.parse import quote
    assert "Signature=" + quote(want, safe="") in url


@pytest.mark.parametrize("size,part,want_total", [
    (1, None, 1),                                  # 1 字节也是一片
    (8 * 1024 * 1024, None, 1),                    # 正好一片
    (8 * 1024 * 1024 + 1, None, 2),
    (200 * 1024 * 1024, None, 25),
])
def test_分片规划(size, part, want_total):
    ps, total = oss.plan_parts(size, part)
    assert total == want_total
    assert ps >= oss.PART_SIZE_MIN


def test_分片数超上限时自动把片调大():
    # 10000 片 × 8MB = 80GB。给一个更大的，片大小必须翻倍而不是片数破表。
    ps, total = oss.plan_parts(200 * 1024 * 1024 * 1024)   # 200GB
    assert total <= oss.MAX_PARTS
    assert ps > oss.PART_SIZE


def test_对象_url_不会把斜杠转义():
    assert oss.object_url("uploads/a b.mp4").endswith("/uploads/a%20b.mp4")
