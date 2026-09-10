"""并发上传：服务端这一侧到底会不会丢。

存在的理由：线上现象是「同时传多个视频，最后只显示一个」。第一件事是把
服务端摘干净 —— 这里 5 个上传真并发打进去（FastAPI 把同步 def 丢线程池，
所以是货真价实的多线程写 SQLite），断言 5 条全在、文件名/缩略图各自独立。

这条一直是绿的，正说明根因在前端（见 tests/e2e/test_upload_uploads.py）。
留着它当护栏：以后谁把 db 的连接管理改坏了，这里会先红。
"""
import concurrent.futures as cf
from pathlib import Path

import pytest

import app as app_mod
import media as media_mod

pytestmark = pytest.mark.api

N = 5


def _fake_mp4(i):
    return b"\x00\x00\x00\x18ftypmp42" + bytes([i]) * 4096


def test_concurrent_video_uploads_all_land(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]

    def up(i):
        return auth_client.post(
            f"/api/points/{pid}/media",
            files={"file": (f"v{i}.mp4", _fake_mp4(i), "video/mp4")})

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        rs = list(ex.map(up, range(N)))

    assert [r.status_code for r in rs] == [200] * N, [r.text for r in rs]
    lst = auth_client.get(f"/api/points/{pid}/media").json()
    assert len(lst) == N, lst
    # 每条各有各的文件和缩略图，不能互相覆盖
    assert len({m["filename"] for m in lst}) == N
    assert len({m["thumb_filename"] for m in lst}) == N
    for m in lst:
        assert (Path(app_mod.UPLOAD_DIR) / m["filename"]).exists()
        assert (Path(media_mod.THUMB_DIR) / m["thumb_filename"]).exists()


def test_concurrent_upload_order_is_stable(auth_client, seeded_trip):
    """exif_time 全为 NULL、sort_order 全为 0 时，排序键完全相同 ——
    此时必须靠 id 兜底给出稳定顺序，不能出现「同一份数据两次请求顺序不同」。"""
    pid = seeded_trip["point_ids"][0]

    def up(i):
        return auth_client.post(
            f"/api/points/{pid}/media",
            files={"file": (f"v{i}.mp4", _fake_mp4(i), "video/mp4")})

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        list(ex.map(up, range(N)))

    seen = [[m["id"] for m in auth_client.get(f"/api/points/{pid}/media").json()]
            for _ in range(3)]
    assert seen[0] == seen[1] == seen[2] == sorted(seen[0])


def test_concurrent_chunked_uploads_all_land(auth_client, seeded_trip):
    """分片路径同样要扛住并发：3 个文件 × 各自的分片同时打进来。"""
    chunk = app_mod.CHUNK_SIZE_MIN
    pid = seeded_trip["point_ids"][0]
    files = {i: (bytes([i]) * (chunk + 77)) for i in range(3)}

    sids = {}
    for i, data in files.items():
        r = auth_client.post(f"/api/points/{pid}/upload-sessions", data={
            "fingerprint": f"fp{i}", "filename": f"v{i}.mp4",
            "filesize": len(data), "chunk_size": chunk})
        assert r.status_code == 200, r.text
        sids[i] = r.json()["session_id"]

    jobs = [(i, k) for i in files for k in range(2)]

    def put(job):
        i, k = job
        part = files[i][k * chunk:(k + 1) * chunk]
        return auth_client.put(f"/api/upload-sessions/{sids[i]}/chunks/{k}",
                               files={"file": (f"{k}", part,
                                               "application/octet-stream")})

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        assert [r.status_code for r in ex.map(put, jobs)] == [200] * len(jobs)

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        outs = list(ex.map(
            lambda i: auth_client.post(f"/api/upload-sessions/{sids[i]}/complete"),
            files))
    assert [r.status_code for r in outs] == [200] * 3, [r.text for r in outs]

    lst = auth_client.get(f"/api/points/{pid}/media").json()
    assert len(lst) == 3
    # 完成顺序是不确定的（并发），所以按内容配对而不是按 id 顺序
    got = {(Path(app_mod.UPLOAD_DIR) / m["filename"]).read_bytes() for m in lst}
    assert got == set(files.values())
