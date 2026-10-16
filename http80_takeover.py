import http.server
import os
import socketserver

WEBROOT = "/root/travel-memory-standby/acme/webroot"
# lego --http.webroot 把挑战文件写在 <webroot>/.well-known/acme-challenge/
ACME_PREFIX = "/.well-known/acme-challenge/"
ACME_ROOT = os.path.join(WEBROOT, ".well-known", "acme-challenge")


class R(http.server.BaseHTTPRequestHandler):
    server_version = "tm-redirect/1.0"

    def _redirect(self):
        host = (self.headers.get("Host", "") or "").split(":")[0]
        self.send_response(301)
        self.send_header("Location", "https://" + host + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # ACME HTTP-01 验证路径：有 webroot 就照常供文件，证书续期才不会被跳转挡掉。
        #
        # 2026-10-16 修：原来只取 basename 然后拼到 WEBROOT 根下，但 lego 的
        # webroot 模式会把挑战文件写在 WEBROOT/.well-known/acme-challenge/ 里。
        # 于是这个分支永远走不到 200，全部掉进 301 —— 公网验证拿不到挑战文件，
        # 证书续期必然失败。改成**完整路径映射**（并做越界防护）。
        if self.path.startswith(ACME_PREFIX):
            rel = os.path.relpath(self.path.split("?", 1)[0], ACME_PREFIX)
            if rel.startswith("..") or os.path.isabs(rel):
                self._redirect()
                return
            path = os.path.join(ACME_ROOT, rel)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self._redirect()

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 30


S(("0.0.0.0", 80), R).serve_forever()
