"""守住演示账号「媒体只在 OSS」这套约定。

背景：seed_test_user.py 不再产生任何文件，它只按 demo_dataset 里的固定文件名
往库里写行；真正的字节由 publish_demo_assets.py 一次性发布到 OSS。
两边靠「文件名完全一致」维系，而这里面有两处是**复刻**出来的，改坏了不会报错，
只会让演示账号变成一片破图：

  1. demo_dataset.thumb_name() 复刻了 media.make_thumb() 的命名规则
  2. photo_name() 的格式串对应 OSS 上已经存在的对象名

所以拿测试钉住。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import demo_dataset as ds  # noqa: E402
import media  # noqa: E402


def test_thumb_name_matches_make_thumb(tmp_path, monkeypatch):
    """thumb_name() 必须和 media.make_thumb() 真实产出的名字一致。"""
    from PIL import Image

    monkeypatch.setattr(media, "THUMB_DIR", tmp_path / "thumbs")
    src = tmp_path / "test_9_9_9.jpg"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(src, "JPEG")

    assert media.make_thumb(src, "photo") == ds.thumb_name(src.name)


def test_photo_name_format_is_frozen():
    """命名规则改了 = OSS 上那批对象全部失联。"""
    assert ds.photo_name(0, 0, 0) == "test_1_1_1.jpg"
    assert ds.photo_name(2, 1, 3) == "test_3_2_4.jpg"


def test_all_photo_names_matches_trips():
    names = [n for n, _pname, _k in ds.all_photo_names()]
    expected = sum(p[4] for t in ds.TRIPS for p in t["points"])
    assert len(names) == expected
    assert len(set(names)) == len(names), "文件名撞车会让两张照片指向同一个对象"


@pytest.mark.parametrize("pname", [p[0] for t in ds.TRIPS for p in t["points"]])
def test_every_point_has_a_pool_slug(pname):
    """点位没有 slug，publish 就找不到素材，那个点会一张图都没有。"""
    assert pname in ds.SLUG


def test_videos_point_at_real_points():
    point_names = {p[0] for t in ds.TRIPS for p in t["points"]}
    for pname, _pool, fname, _shot, _lat, _lng in ds.VIDEOS:
        assert pname in point_names, f"视频 {fname} 挂在不存在的点位 {pname} 上"


def test_seed_writes_no_files():
    """seed 不该再出现任何文件写入 —— 它的媒体全在 OSS 上。"""
    src = (ROOT / "scripts" / "seed_test_user.py").read_text(encoding="utf-8")
    for banned in ("shutil.copyfile", "make_thumb", "subprocess.run", "ImageDraw"):
        assert banned not in src, f"seed 里不该再有 {banned}"
