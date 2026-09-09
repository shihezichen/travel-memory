"""E2E fixtures：拉起一个独立的 uvicorn（临时 DB + 临时 uploads）+ 系统 Chrome。

默认不跑（pytest.ini 里 addopts = -m "not e2e"）。
显式运行：  .venv/bin/python -m pytest -m e2e
"""
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """独立进程跑的真服务。绝不碰生产 data.db / uploads / systemd 服务。"""
    import urllib.error
    import urllib.request

    workdir = tmp_path_factory.mktemp("e2e-server")
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "TM_TEST_DB": str(workdir / "e2e.db"),
        "TM_TEST_UPLOADS": str(workdir / "uploads"),
        "TM_TEST_PORT": str(port),
        "TRAVEL_LLM_PROVIDER": "none",
    })
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tests" / "e2e" / "_server_main.py")],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"E2E 服务启动失败:\n{proc.stdout.read()}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("E2E 服务 30s 内没起来")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def browser():
    from driver import Browser
    with Browser(headless=True) as b:
        yield b


@pytest.fixture
def page(browser, live_server):
    pg = browser.new_page(live_server)
    yield pg
    pg.close()


@pytest.fixture
def e2e_user(live_server):
    """通过真实 HTTP API 注册一个用户并播一条行程。返回凭据 + id。

    注意：E2E 层不直接操作 db 模块 —— 服务在另一个进程里，走 API 才是真实路径。
    """
    import httpx

    username = f"e2e{uuid.uuid4().hex[:8]}"
    password = "pw123456"
    with httpx.Client(base_url=live_server, timeout=20, trust_env=False) as c:
        r = c.post("/api/register", data={"username": username, "password": password,
                                          "display_name": "E2E 用户"})
        assert r.status_code == 200, r.text
        tid = c.post("/api/trips", data={"name": "E2E 行程", "start_date": "2026-05-01",
                                         "end_date": "2026-05-03"}).json()["id"]
        pids = []
        for nm, lat, lng, d in [("西湖", 30.2590, 120.1300, "2026-05-01"),
                                ("灵隐寺", 30.2410, 120.1010, "2026-05-02"),
                                ("外滩", 31.2400, 121.4900, "2026-05-03")]:
            pids.append(c.post(f"/api/trips/{tid}/points",
                               data={"name": nm, "lat": lat, "lng": lng,
                                     "start_date": d, "end_date": d}).json()["id"])
        # 给第一个点传一张真照片，覆盖照片图层/相册/灯箱
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (400, 300), (30, 120, 200)).save(buf, "JPEG")
        m = c.post(f"/api/points/{pids[0]}/media",
                   files={"file": ("e2e.jpg", buf.getvalue(), "image/jpeg")})
        assert m.status_code == 200, m.text
        share = c.post(f"/api/trips/{tid}/share").json()["token"]

    return {"username": username, "password": password, "trip_id": tid,
            "point_ids": pids, "share_token": share, "media_id": m.json()["id"]}
