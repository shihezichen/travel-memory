"""媒体：上传（含缩略图/EXIF）、排序、caption、删除、检索。

所有文件写入都落在 tmp_path（见 tests/conftest.py 的 isolated_env），
不会碰生产 uploads/。
"""
from pathlib import Path

import pytest

import app as app_mod
import media as media_mod

pytestmark = pytest.mark.api


def _upload(client, pid, name="a.jpg", content=None, ctype="image/jpeg"):
    return client.post(f"/api/points/{pid}/media",
                       files={"file": (name, content, ctype)})


def test_upload_photo_writes_file_and_thumb(auth_client, seeded_trip, jpeg_bytes,
                                            isolated_env):
    pid = seeded_trip["point_ids"][0]
    r = _upload(auth_client, pid, content=jpeg_bytes())
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "photo"
    assert body["filename"].endswith(".jpg") and len(body["filename"]) == 36  # uuid4hex+.jpg

    src = Path(app_mod.UPLOAD_DIR) / body["filename"]
    thumb = Path(media_mod.THUMB_DIR) / body["thumb"]
    assert src.exists() and src.stat().st_size > 0
    assert thumb.exists() and thumb.stat().st_size > 0
    # 落在临时目录，不是生产 uploads/
    assert str(isolated_env) in str(src)


def test_uploaded_filename_is_randomised_not_user_controlled(auth_client, seeded_trip,
                                                             jpeg_bytes):
    """防目录穿越：原始文件名不参与落盘路径。"""
    pid = seeded_trip["point_ids"][0]
    r = _upload(auth_client, pid, name="../../evil.jpg", content=jpeg_bytes())
    assert r.status_code == 200
    assert "evil" not in r.json()["filename"]
    assert ".." not in r.json()["filename"]


def test_thumbnail_is_downscaled(auth_client, seeded_trip, jpeg_bytes):
    from PIL import Image
    pid = seeded_trip["point_ids"][0]
    r = _upload(auth_client, pid, content=jpeg_bytes(size=(2000, 1500)))
    with Image.open(Path(media_mod.THUMB_DIR) / r.json()["thumb"]) as im:
        assert max(im.size) <= 512


def test_upload_rejects_unsupported_extension(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = _upload(auth_client, pid, name="x.txt", content=b"hello", ctype="text/plain")
    assert r.status_code == 400 and "不支持的文件类型" in r.json()["detail"]


def test_upload_rejects_empty_file(auth_client, seeded_trip):
    pid = seeded_trip["point_ids"][0]
    r = _upload(auth_client, pid, content=b"")
    assert r.status_code == 400 and "空文件" in r.json()["detail"]


def test_upload_to_missing_point_is_404(auth_client, jpeg_bytes):
    assert _upload(auth_client, 999999, content=jpeg_bytes()).status_code == 404


def test_other_user_cannot_upload(other_client, seeded_trip, jpeg_bytes):
    r = _upload(other_client, seeded_trip["point_ids"][0], content=jpeg_bytes())
    assert r.status_code == 404


def test_list_media_and_caption(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = _upload(auth_client, pid, content=jpeg_bytes()).json()["id"]
    assert auth_client.put(f"/api/media/{mid}/caption",
                           data={"caption": "  日出  "}).status_code == 200
    items = auth_client.get(f"/api/points/{pid}/media").json()
    assert len(items) == 1 and items[0]["caption"] == "日出"     # 两端空白被 strip


def test_caption_is_truncated_to_500(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mid = _upload(auth_client, pid, content=jpeg_bytes()).json()["id"]
    auth_client.put(f"/api/media/{mid}/caption", data={"caption": "字" * 600})
    got = auth_client.get(f"/api/points/{pid}/media").json()[0]["caption"]
    assert len(got) == 500


def test_media_reorder(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    ids = [_upload(auth_client, pid, name=f"{i}.jpg",
                   content=jpeg_bytes(color=(i * 40, 0, 0))).json()["id"]
           for i in range(3)]
    new_order = [ids[2], ids[0], ids[1]]
    r = auth_client.put(f"/api/points/{pid}/media/order",
                        data={"ids": ",".join(map(str, new_order))})
    assert r.json() == {"ok": True, "count": 3}
    assert [m["id"] for m in auth_client.get(f"/api/points/{pid}/media").json()] == new_order


def test_media_reorder_ignores_foreign_ids(auth_client, other_client, seeded_trip,
                                           jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    mine = _upload(auth_client, pid, content=jpeg_bytes()).json()["id"]
    r = auth_client.put(f"/api/points/{pid}/media/order",
                        data={"ids": f"{mine},999999,abc"})
    assert r.json()["count"] == 1


def test_delete_media_removes_files_from_disk(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    body = _upload(auth_client, pid, content=jpeg_bytes()).json()
    src = Path(app_mod.UPLOAD_DIR) / body["filename"]
    thumb = Path(media_mod.THUMB_DIR) / body["thumb"]
    assert src.exists() and thumb.exists()
    assert auth_client.delete(f"/api/media/{body['id']}").status_code == 200
    assert not src.exists() and not thumb.exists()
    assert auth_client.get(f"/api/points/{pid}/media").json() == []


def test_delete_point_removes_its_media_files(auth_client, seeded_trip, jpeg_bytes):
    pid = seeded_trip["point_ids"][0]
    body = _upload(auth_client, pid, content=jpeg_bytes()).json()
    src = Path(app_mod.UPLOAD_DIR) / body["filename"]
    auth_client.delete(f"/api/points/{pid}")
    assert not src.exists()


def test_delete_trip_removes_all_media_files(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    paths = []
    for pid in seeded_trip["point_ids"][:2]:
        b = _upload(auth_client, pid, content=jpeg_bytes()).json()
        paths.append(Path(app_mod.UPLOAD_DIR) / b["filename"])
    auth_client.delete(f"/api/trips/{tid}")
    assert not any(p.exists() for p in paths)


def test_other_user_cannot_delete_media(auth_client, other_client, seeded_trip,
                                        jpeg_bytes):
    mid = _upload(auth_client, seeded_trip["point_ids"][0],
                  content=jpeg_bytes()).json()["id"]
    assert other_client.delete(f"/api/media/{mid}").status_code == 404
    assert other_client.put(f"/api/media/{mid}/caption",
                            data={"caption": "hack"}).status_code == 404


def test_trip_media_flat_list(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    for pid in seeded_trip["point_ids"][:2]:
        _upload(auth_client, pid, content=jpeg_bytes())
    body = auth_client.get(f"/api/trips/{tid}/media").json()
    assert body["count"] == 2
    it = body["items"][0]
    assert it["url"].startswith("/uploads/")
    assert it["thumb_url"].startswith("/uploads/thumbs/")
    assert it["point_name"] in ("西湖", "灵隐寺")
    assert it["taken_at"]                    # EXIF 缺失时回落 created_at


def test_media_search_endpoint(auth_client, seeded_trip, jpeg_bytes):
    tid = seeded_trip["trip_id"]
    _upload(auth_client, seeded_trip["point_ids"][0], content=jpeg_bytes())
    r = auth_client.get(f"/api/trips/{tid}/media/search", params={"type": "photo"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    r = auth_client.get(f"/api/trips/{tid}/media/search", params={"type": "video"})
    assert r.json()["count"] == 0
    r = auth_client.get(f"/api/trips/{tid}/media/search", params={"q": "西湖"})
    assert r.json()["count"] == 1


def test_media_list_carries_server_computed_urls(auth_client, seeded_trip, jpeg_bytes):
    """媒体列表必须带服务端算好的 url / thumb_url。

    前端的 mediaUrl() 只会无脑拼 OSS 前缀，「这个文件同步上去没有」只有服务端
    知道（config.media_url 查同步水位）。少了这两个字段，刚上传的照片在同步
    之前一律裂图。
    """
    pid = seeded_trip["point_ids"][0]
    _upload(auth_client, pid, content=jpeg_bytes())
    items = auth_client.get(f"/api/points/{pid}/media").json()
    assert len(items) == 1
    m = items[0]
    assert m["url"].endswith("/uploads/" + m["filename"])
    assert m["thumb_url"].endswith("/uploads/thumbs/" + m["thumb_filename"])
