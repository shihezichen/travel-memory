"""把 x-www-form-urlencoded 请求体统一成「百分号编码的纯 ASCII」再交给路由。

起因：外部 agent 通过 HTTP API 往行程里加了个「芒康」，网页上打开，名字和描述
全是乱码（`è\x8a\x92åº·`），HTTP 却是 200。

根因在 Starlette：解 urlencoded 表单时它是**按 latin-1 解字节**的
（starlette/formparsers.py，`field_value.decode("latin-1")`），再交给
unquote_plus 按 UTF-8 还原百分号转义。于是同一个「芒康」有两种下场：

    curl --data-urlencode "name=芒康"   body 是 %E8%8A%92%E5%BA%B7（纯 ASCII）
                                        latin-1 解出来原样，unquote 按 UTF-8 还原 → 对
    curl -d "name=芒康"                 body 是 UTF-8 裸字节
                                        latin-1 逐字节当 Latin-1 字符 → 「è\x8a\x92åº·」→ 错

后者恰恰是 agent 最容易写出来的形式：LLM 拼 curl 命令时基本都是 `-d`，Java/Go
的老 HTTP 客户端默认也是 ISO-8859-1。而且它坏得很安静 —— 不报错、不 4xx，
就是库里多一条乱码，等用户打开网页才发现。

所以在进路由之前先把 body 按 UTF-8 解一遍、重新百分号编码成 ASCII。
已经编好码的 body 过这一遍是幂等的（parse_qsl → urlencode 原样还原）。
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

# 表单体不该很大 —— 真正的大身体（上传）走 multipart，不经过这里。
# 超过这个尺寸就原样放行，免得中间件把内存吃掉。
MAX_FORM_BODY = 1 << 20  # 1 MiB

_FORM_CT = b"application/x-www-form-urlencoded"


def normalize_form_body(body: bytes) -> bytes:
    """裸 UTF-8 字节 → 百分号编码。已是 ASCII 的原样返回；不是 UTF-8 的不猜，原样返回。"""
    if not body or body.isascii():
        # 纯 ASCII 说明客户端已经编过码，latin-1 那步是无损的，不用动。
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        # 真 · latin-1 客户端，或者压缩过的 body。猜错比不猜更糟，交回原逻辑。
        return body
    try:
        pairs = parse_qsl(text, keep_blank_values=True)
    except ValueError:
        return body
    return urlencode(pairs).encode("ascii")


class Utf8FormMiddleware:
    """纯 ASGI 中间件：只碰 content-type 是 urlencoded 的请求，其余原样透传。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_urlencoded(scope):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        oversize = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # 断开事件不能吞：原样喂给下游，让它自己收场。
                await self.app(scope, _once(message), send)
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_FORM_BODY:
                oversize = True
            if not oversize:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break

        if oversize:
            # 已经丢了一部分 body，没法重放；这种请求本来也不该是表单。
            await self.app(scope, _fail_body(), send)
            return

        body = normalize_form_body(b"".join(chunks))
        await self.app(_with_content_length(scope, len(body)), _once(
            {"type": "http.request", "body": body, "more_body": False}), send)


def _is_urlencoded(scope) -> bool:
    for key, value in scope.get("headers") or ():
        if key.lower() == b"content-type":
            return value.lower().startswith(_FORM_CT)
    return False


def _with_content_length(scope, length: int):
    """重编码会改长度，content-length 得跟着改，否则下游按旧长度截断。"""
    headers = [(k, v) for k, v in scope.get("headers") or () if k.lower() != b"content-length"]
    headers.append((b"content-length", str(length).encode("ascii")))
    new_scope = dict(scope)
    new_scope["headers"] = headers
    return new_scope


def _once(message):
    """把一条 ASGI 消息做成 receive 可调用对象：第一次给它，之后一律 disconnect。"""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return message
        return {"type": "http.disconnect"}

    return receive


def _fail_body():
    async def receive():
        return {"type": "http.disconnect"}

    return receive
