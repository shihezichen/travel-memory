"""测试环境隔离与公共 fixture。

隔离策略（关键）
----------------
`db.get_conn()` 每次调用都现读模块级 `db.DB_PATH`，`app.py` / `media.py` 里的
`UPLOAD_DIR`、`THUMB_DIR`、`_TILE_DIR` 也都是「调用时查模块全局」，所以用
monkeypatch 重绑这些名字就能把整套读写重定向到 tmp_path，无需改动任何生产代码。

唯一的例外是 `import app` 那一刻的副作用：
  - `db.init_db()` 会建库（所以必须在 import 之前先把 DB_PATH 改掉，见下方）
  - `UPLOAD_DIR.mkdir(exist_ok=True)` 与 `app.mount("/uploads", StaticFiles(...))`
    绑定的是**生产** uploads/ 目录（mkdir 对已存在目录是 no-op，不写数据；
    但 /uploads/<file> 这条静态路由没法重定向 —— 见报告「可测试性改动建议」）。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# 最硬的一道保险丝：整场测试期间，任何人（测试代码、被测代码、第三方库）
# 只要试图 sqlite3.connect 到生产 data.db，直接抛异常。
# 上面 monkeypatch DB_PATH 是「让它不去」，这一条是「就算去了也进不去」。
# ---------------------------------------------------------------------------
_REAL_SQLITE_CONNECT = sqlite3.connect
_PROD_DB_STR = str((REPO_ROOT / "data.db").resolve())


def _guarded_connect(database, *args, **kwargs):
    try:
        resolved = str(Path(str(database)).resolve())
    except Exception:
        resolved = str(database)
    if resolved == _PROD_DB_STR:
        raise RuntimeError(
            f"测试试图打开生产数据库 {resolved} —— 已阻止。"
            "请检查 db.DB_PATH 是否被正确重定向到 tmp_path。"
        )
    return _REAL_SQLITE_CONNECT(database, *args, **kwargs)


# 立刻生效 —— 连本文件下面 `import app` 触发的 db.init_db() 也在保护范围内。
sqlite3.connect = _guarded_connect


def pytest_unconfigure(config):
    sqlite3.connect = _REAL_SQLITE_CONNECT


# ---------------------------------------------------------------------------
# 必须在 import app 之前：把 DB / 上传目录挪到临时位置，
# 否则 app.py 顶层的 db.init_db() 会打到生产 data.db 上。
# ---------------------------------------------------------------------------
_BOOT_TMP = Path(tempfile.mkdtemp(prefix="tm-tests-boot-"))

import db  # noqa: E402
db.DB_PATH = _BOOT_TMP / "boot.db"

import media as media_mod  # noqa: E402
media_mod.UPLOAD_DIR = _BOOT_TMP / "uploads"
media_mod.THUMB_DIR = media_mod.UPLOAD_DIR / "thumbs"
media_mod.THUMB_DIR.mkdir(parents=True, exist_ok=True)

import app as app_mod  # noqa: E402

# 拆分出来的 router 模块。它们各自持有 UPLOAD_DIR 模块级全局（路由体保持原样未改），
# 所以隔离和保险丝都要覆盖到；新增 router 时往这里加一行。
from routers import agent_api as _r_agent_api  # noqa: E402
from routers import share as _r_share  # noqa: E402

# transcode 不是 router，但同样持有 UPLOAD_DIR / VARIANT_DIR 模块级全局，
# 漏了它，转码测试就会把档位文件写进生产 uploads/variants/。
import transcode as _transcode  # noqa: E402

# oss_backfill 同理：它有自己的 UPLOAD_DIR，回源时往那里写文件。
# 漏了它，回源测试就会把对象写进生产 uploads/。
import oss_backfill as _oss_backfill  # noqa: E402

# aiindex 也有一份 UPLOAD_DIR（算向量时从那里读图）。漏了它，装了 CLIP 的机器上
# 每次上传都会去**生产** uploads/ 里找那个 uuid 文件名，找不到就把索引记成 failed
# —— 测试于是在一个「索引永远失败」的世界里跑，而这事一声不吭。
import aiindex as _aiindex  # noqa: E402
import jevrerank as _jevrerank  # noqa: E402

_ROUTER_MODULES = [_r_agent_api, _r_share, _transcode, _oss_backfill, _aiindex]
# 每个模块里需要被重定向到 tmp_path 的目录型全局。
# 不是每个模块都全有，所以按 hasattr 挑。
_ROUTER_DIR_ATTRS = ("UPLOAD_DIR", "_TILE_DIR", "VARIANT_DIR")


def _router_dir_targets():
    return [(m, a) for m in _ROUTER_MODULES for a in _ROUTER_DIR_ATTRS if hasattr(m, a)]
from domain import rate_limit  # noqa: E402

PROD_DB = REPO_ROOT / "data.db"
PROD_UPLOADS = REPO_ROOT / "uploads"


def _assert_not_production():
    """生产数据保险丝：任何一条写路径落到生产就直接失败。"""
    assert Path(db.DB_PATH).resolve() != PROD_DB.resolve(), \
        "测试试图使用生产数据库 data.db —— 已中止"
    prod_up = PROD_UPLOADS.resolve()
    targets = [(app_mod, "UPLOAD_DIR"),
               (media_mod, "UPLOAD_DIR"), (media_mod, "THUMB_DIR")]
    # 路由拆进 routers/* 之后，UPLOAD_DIR / _TILE_DIR 变成了各 router 模块自己的全局，
    # 必须一并纳入保险丝 —— 漏了哪个，测试就会静默写进生产 uploads/。
    targets += _router_dir_targets()
    for mod, attr in targets:
        q = Path(getattr(mod, attr)).resolve()
        assert prod_up not in (q, *q.parents), \
            f"{mod.__name__}.{attr} 指向生产 uploads/（{q}）—— 已中止"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_BOOT_TMP, ignore_errors=True)


# ---------------------------------------------------------------------------
# 每个用例一套全新的 DB + 上传目录
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """把 DB、uploads、thumbs、tiles 全部重定向到 tmp_path，并重置全局限流器。"""
    uploads = tmp_path / "uploads"
    thumbs = uploads / "thumbs"
    tiles = uploads / "tiles"
    variants = uploads / "variants"
    for d in (uploads, thumbs, tiles, variants):
        d.mkdir(parents=True, exist_ok=True)

    _dir_for = {"_TILE_DIR": tiles, "VARIANT_DIR": variants}
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", uploads)
    for _m, _a in _router_dir_targets():
        monkeypatch.setattr(_m, _a, _dir_for.get(_a, uploads))

    # 转码：测试里**绝不**真的跑 ffmpeg（慢、且依赖机器上的编码器）。
    # 默认把入队函数换成记录器；要断言「上传有没有触发转码」的用例
    # 直接读 transcode._TEST_CALLS，要测真转码逻辑的用例自己再 monkeypatch 回去。
    _tc_calls = []
    monkeypatch.setattr(_transcode, "_TEST_CALLS", _tc_calls, raising=False)

    def _fake_enqueue(media_id, src_path, force=False):
        _tc_calls.append({"media_id": media_id, "path": str(src_path), "force": force})
        return True

    monkeypatch.setattr(_transcode, "enqueue", _fake_enqueue)

    # 语义索引同理：**绝不**在测试里真的跑 CLIP。装了模型的机器上，每一次上传都会
    # 起一个后台线程算 512 维向量 —— 慢，而且会让「这台机器装没装模型」变成测试
    # 结果的一部分。要测索引本身的用例自己把 available/enqueue 换回去。
    monkeypatch.setattr(_aiindex, "enqueue", lambda media_id, filename: False)
    # 检索这条链路上的两张加速缓存按用例清空。它们都是进程级的，不清的话
    # 上一个用例喂给假 Jev 的回答会原样飘进下一个用例，而下一个用例换了
    # 另一个假 _post —— 结果是测试互相污染，还极难查。
    _aiindex.clear_query_cache()
    _jevrerank.clear_cache()
    monkeypatch.setattr(media_mod, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(media_mod, "THUMB_DIR", thumbs)

    # 门禁默认关闭（生产上有没有 gate_hash.txt 都不影响测试结果）
    monkeypatch.setattr(app_mod, "GATE_HASH", "")
    # 固定管理员令牌，避免依赖仓库里的 token.txt
    monkeypatch.setattr(app_mod, "ADMIN_TOKEN", "test-admin-token-xyz")

    # 临时库不需要崩溃持久性：关掉 fsync + 日志走内存。
    # 建库/提交是每个用例的固定开销，这一步把 fast 套件的耗时砍掉一大半。
    _real_get_conn = db.get_conn

    def _fast_get_conn():
        conn = _real_get_conn()
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY")
        return conn

    monkeypatch.setattr(db, "get_conn", _fast_get_conn)

    _assert_not_production()

    db.init_db()
    rate_limit.limiter.reset()
    yield tmp_path
    rate_limit.limiter.reset()


@pytest.fixture(autouse=True)
def no_outbound_network(request, monkeypatch):
    """离线保证：任何走 urllib 的外部调用（OSRM 路线、高德 REST、OpenTopoData、
    栅格瓦片）在测试里一律立刻抛错，而不是等超时。

    这样「外部依赖不可用时应当优雅降级」就变成一条可断言的行为，
    而不是碰运气。需要真网的用例打 @pytest.mark.net。
    """
    if request.node.get_closest_marker("net") or \
            request.node.get_closest_marker("e2e"):
        return
    import urllib.request

    def _blocked(*a, **kw):
        raise OSError("outbound network blocked by tests (no_outbound_network fixture)")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


# ---------------------------------------------------------------------------
# HTTP 客户端 / 账号
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def make_user():
    """直接建库级用户（跳过 HTTP，快）。返回 dict。"""
    def _make(username=None, password="pw123456", is_admin=0):
        username = username or f"u{uuid.uuid4().hex[:8]}"
        uid = db.create_user(username, password, username, is_admin)
        u = db.get_user_by_id(uid)
        u["_password"] = password
        return u
    return _make


@pytest.fixture
def auth_client(client, make_user):
    """已登录的 TestClient（普通用户）。附带 .user 属性。"""
    u = make_user()
    r = client.post("/api/login", data={"username": u["username"],
                                        "password": u["_password"]})
    assert r.status_code == 200, r.text
    client.user = u
    return client


@pytest.fixture
def other_client(make_user):
    """第二个已登录用户，用于越权断言。"""
    from fastapi.testclient import TestClient
    u = make_user()
    c = TestClient(app_mod.app)
    r = c.post("/api/login", data={"username": u["username"], "password": u["_password"]})
    assert r.status_code == 200, r.text
    c.user = u
    return c


# ---------------------------------------------------------------------------
# 测试素材
# ---------------------------------------------------------------------------
@pytest.fixture
def jpeg_bytes():
    """生成一张最小的真实 JPEG（PIL 生成，不依赖仓库里的任何文件）。"""
    def _make(size=(80, 60), color=(200, 30, 30)):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, "JPEG", quality=70)
        return buf.getvalue()
    return _make


@pytest.fixture
def seeded_trip(auth_client):
    """一条含 3 个点的行程（通过 HTTP 建，顺带覆盖创建链路）。"""
    r = auth_client.post("/api/trips", data={
        "name": "测试行程", "start_date": "2026-05-01", "end_date": "2026-05-03",
        "description": "pytest seed"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    pids = []
    for i, (nm, lat, lng, d) in enumerate([
        ("西湖", 30.2590, 120.1300, "2026-05-01"),
        ("灵隐寺", 30.2410, 120.1010, "2026-05-02"),
        ("外滩", 31.2400, 121.4900, "2026-05-03"),
    ]):
        rp = auth_client.post(f"/api/trips/{tid}/points", data={
            "name": nm, "lat": lat, "lng": lng,
            "start_date": d, "end_date": d, "description": f"第{i+1}站"})
        assert rp.status_code == 200, rp.text
        pids.append(rp.json()["id"])
    return {"trip_id": tid, "point_ids": pids}
