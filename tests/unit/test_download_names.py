"""download.py 的纯函数层：文件名清洗、日期抽取、Content-Disposition 编码。

HTTP 面在 tests/api/test_media_download.py。这里只钉住那些「一改就会
在某个浏览器上悄悄变成乱码 / 变成路径」的字符串细节。
"""
import pytest

import download

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 片段清洗
@pytest.mark.parametrize("raw, want", [
    ("西湖", "西湖"),
    ("  西湖  ", "西湖"),
    ("West Lake", "West-Lake"),
    ("a/b", "ab"),                      # 路径分隔符直接删掉，不替换
    ("a\\b", "ab"),
    ("../../etc", "etc"),               # 前导点被剥掉，拼不出上跳
    ('say "hi"', "say-hi"),
    ("a:b*c?d<e>f|g", "abcdefg"),       # Windows 保留字符
    ("a\x00b\nc", "abc"),               # 控制字符
    ("...", ""),
    ("---", ""),
    ("", ""),
    (None, ""),
])
def test_safe_part(raw, want):
    assert download._safe_part(raw) == want


def test_safe_part_truncates():
    assert len(download._safe_part("字" * 200)) == 40
    assert len(download._safe_part("字" * 200, 8)) == 8


@pytest.mark.parametrize("raw, want", [
    (".jpg", ".jpg"),
    ("jpg", ".jpg"),                    # 忘了带点也补上
    (".JPG", ".JPG"),                   # 大小写不动（调用方已经 lower 过）
    (".mp4", ".mp4"),
    ("../evil", ".evil"),               # 点和斜杠都清掉后只剩名字
    (".", ""),
    ("", ""),
    (None, ""),
])
def test_safe_ext(raw, want):
    assert download._safe_ext(raw) == want


def test_ascii_part_drops_cjk():
    """没有拼音库，中文转不出来是预期结果 —— 兜底名靠日期+序号可读。"""
    assert download._ascii_part("西湖") == ""
    assert download._ascii_part("Kyoto") == "Kyoto"
    assert download._ascii_part("Café") == "Cafe"        # 重音去掉，字母留下
    assert download._ascii_part("京都Kyoto") == "Kyoto"


# ---------------------------------------------------------------- 日期
@pytest.mark.parametrize("row, want", [
    ({"exif_time": "2026:09:10 12:34:56", "created_at": "2020-01-01"}, "20260910"),
    ({"exif_time": "2026-09-10T12:34:56", "created_at": "2020-01-01"}, "20260910"),
    ({"exif_time": None, "created_at": "2020-01-02 03:04:05"}, "20200102"),
    ({"exif_time": "", "created_at": "2020-01-02"}, "20200102"),
    ({"exif_time": "坏数据", "created_at": "2020-01-02"}, "20200102"),
    ({"exif_time": None, "created_at": None}, ""),
    ({}, ""),
])
def test_date_tag(row, want):
    assert download._date_tag(row) == want


# ---------------------------------------------------------------- 响应头
def test_content_disposition_has_both_forms():
    """两种写法都要给：现代浏览器认 filename*，老的只认 filename。"""
    cd = download.content_disposition("旅行记忆-20260910-京都-01.mp4",
                                      "travel-memory-20260910-01.mp4")
    assert cd.startswith("attachment; ")
    assert 'filename="travel-memory-20260910-01.mp4"' in cd
    assert "filename*=UTF-8''%E6%97%85%E8%A1%8C%E8%AE%B0%E5%BF%86" in cd
    # 整个头必须能按 latin-1 发出去，否则 ASGI 层直接抛
    cd.encode("latin-1")


def test_content_disposition_escapes_quotes_in_ascii_name():
    """兜底名里混进引号会把 header 提前闭合 —— 必须先剥掉。"""
    cd = download.content_disposition("x.jpg", 'a"b\\c.jpg')
    assert 'filename="abc.jpg"' in cd


def test_content_disposition_percent_encodes_specials():
    cd = download.content_disposition("a b;c.jpg", "a-b-c.jpg")
    assert "%20" in cd and "%3B" in cd      # 空格和分号都编码，不会拆参数


# ---------------------------------------------------------------- 白名单
def test_allowed_qualities_is_literal_whitelist():
    """新增档位要同步进 LADDER；白名单本身必须只有字面量，不能是模式匹配。"""
    import transcode
    assert download.ALLOWED_QUALITIES[0] == "original"
    assert set(download.ALLOWED_QUALITIES) == {"original"} | {l for l, _ in transcode.LADDER}
    for q in download.ALLOWED_QUALITIES:
        assert "/" not in q and "." not in q


# ---------------------------------------------------------------- 组合
def test_build_filenames_with_real_rows(monkeypatch):
    """拼装顺序：前缀-日期-地点-序号[-档位].扩展名"""
    monkeypatch.setattr(download.db, "get_point", lambda pid: {"name": "京都"})
    monkeypatch.setattr(download.db, "list_media",
                        lambda pid: [{"id": 7}, {"id": 8}, {"id": 9}])
    row = {"id": 9, "point_id": 1, "exif_time": "2026:09:10 08:00:00",
           "created_at": "2026-09-11", "type": "video"}
    assert download.build_filenames(row, ".mp4") == (
        "旅行记忆-20260910-京都-03.mp4", "travel-memory-20260910-03.mp4")
    assert download.build_filenames(row, ".mp4", "720p")[0] == \
        "旅行记忆-20260910-京都-03-720p.mp4"


def test_build_filenames_without_place_or_date(monkeypatch):
    """地点名为空、时间也没有时，名字要塌缩成合法的短名，而不是一串连字符。"""
    monkeypatch.setattr(download.db, "get_point", lambda pid: {"name": "   "})
    monkeypatch.setattr(download.db, "list_media", lambda pid: [{"id": 5}])
    row = {"id": 5, "point_id": 1, "exif_time": None, "created_at": None}
    utf8_name, ascii_name = download.build_filenames(row, ".jpg")
    assert utf8_name == "旅行记忆-01.jpg"
    assert ascii_name == "travel-memory-01.jpg"


def test_sequence_no_falls_back_to_id(monkeypatch):
    """并发删除时自己不在列表里 —— 退回 id，别抛异常把下载整挂。"""
    monkeypatch.setattr(download.db, "list_media", lambda pid: [{"id": 1}])
    assert download._sequence_no({"id": 42, "point_id": 1}) == "42"
