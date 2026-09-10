#!/usr/bin/env python
"""E2E 专用的服务启动器 —— 只在测试里用，生产不加载。

存在的唯一理由：`db.DB_PATH` 和 `app.UPLOAD_DIR` 是模块级常量，没有环境变量入口，
所以没法用 `uvicorn app:app` 直接把服务指到临时目录上。这里在 import app 之前
把这些名字改掉，等价于「带环境变量的 app.py」。

一旦 app.py / db.py 支持 TRAVEL_DB_PATH / TRAVEL_UPLOAD_DIR 环境变量
（见测试报告的「可测试性改动建议」），这个文件就可以删掉。

用法：
    TM_TEST_DB=/tmp/x/e2e.db TM_TEST_UPLOADS=/tmp/x/uploads TM_TEST_PORT=8199 \
        .venv/bin/python tests/e2e/_server_main.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DB_FILE = Path(os.environ["TM_TEST_DB"]).resolve()
UPLOADS = Path(os.environ["TM_TEST_UPLOADS"]).resolve()
PORT = int(os.environ.get("TM_TEST_PORT", "8199"))

assert DB_FILE != (REPO_ROOT / "data.db"), "E2E 服务不得使用生产 data.db"
assert UPLOADS != (REPO_ROOT / "uploads"), "E2E 服务不得使用生产 uploads/"

for d in (UPLOADS, UPLOADS / "thumbs", UPLOADS / "tiles", UPLOADS / "variants"):
    d.mkdir(parents=True, exist_ok=True)

import db                       # noqa: E402
db.DB_PATH = DB_FILE

import media as media_mod       # noqa: E402
media_mod.UPLOAD_DIR = UPLOADS
media_mod.THUMB_DIR = UPLOADS / "thumbs"

# 转码模块也持有 UPLOAD_DIR / VARIANT_DIR 两个模块级常量。漏了它，E2E 里传一个
# 视频就会把档位文件写进**生产** uploads/variants/ —— 这个进程是 subprocess，
# tests/conftest.py 的 monkeypatch 保险丝完全够不着它。
import transcode as _transcode  # noqa: E402
_transcode.UPLOAD_DIR = UPLOADS
_transcode.VARIANT_DIR = UPLOADS / "variants"

import app as app_mod           # noqa: E402
app_mod.UPLOAD_DIR = UPLOADS
# _TILE_DIR 随分享缩略图一起搬到了 routers/share.py
from routers import share as _share_mod  # noqa: E402
_share_mod.UPLOAD_DIR = UPLOADS
_share_mod._TILE_DIR = UPLOADS / "tiles"
app_mod.GATE_HASH = ""          # E2E 不测门禁，走用户名密码

# /uploads 的 StaticFiles 在 import 时就绑到生产目录了，这里重新挂一遍指向临时目录。
# （生产代码不需要这么做 —— 这正是「可测试性改动建议」里那条的由来。）
# 用 app_mod.UploadStaticFiles 而不是裸 StaticFiles：缓存头分档和 .chunks/ 屏蔽
# 都在那个子类里，换成裸的等于 E2E 测的不是生产那套行为。
for route in list(app_mod.app.router.routes):
    if getattr(route, "path", None) == "/uploads":
        route.app = app_mod.UploadStaticFiles(directory=UPLOADS)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app_mod.app, host="127.0.0.1", port=PORT, log_level="warning")
