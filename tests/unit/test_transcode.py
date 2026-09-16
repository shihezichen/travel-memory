"""transcode.py —— 档位计算、faststart 探测、任务闸门、档位列表拼装。

**这里一次 ffmpeg 都不会真跑。** 所有会 fork 子进程的地方（transcode_one /
ensure_faststart / probe_video）都被 monkeypatch 掉了：真跑转码要几秒、还依赖
机器上有没有 libx264，那属于端到端验证，不该拖慢单元层。
"""
import struct
from pathlib import Path

import pytest

import db
import transcode

pytestmark = pytest.mark.unit

# tests/conftest.py 的 isolated_env 默认把 transcode.enqueue 换成了「只记录不执行」的
# 假货（免得任何用例不小心真起后台转码）。少数几个用例要测的正是入队本身，
# 所以在被替换之前先把真函数抓在手里 —— 模块 import 发生在 fixture 之前。
_REAL_ENQUEUE = transcode.enqueue


# ---------------------------------------------------------------- 档位计算
def test_scaled_size_portrait_uses_short_side():
    """竖屏 1080x1438：短边是宽(1080)，720 档应该是 720x958，不是 1280x720。

    这条是整个功能最容易写错的地方 —— 写死 scale=-2:720 会把竖屏算成
    「高 720」，得到 541x720，比源还小一大截且比例档位全乱。
    """
    assert transcode.scaled_size(1080, 1438, 720) == (720, 958)
    assert transcode.scaled_size(1080, 1438, 480) == (480, 638)


def test_scaled_size_landscape_uses_short_side():
    """横屏 1920x1080：短边是高(1080)，720 档是 1280x720。"""
    assert transcode.scaled_size(1920, 1080, 720) == (1280, 720)
    assert transcode.scaled_size(1920, 1080, 480) == (852, 480)


def test_scaled_size_square():
    assert transcode.scaled_size(1000, 1000, 720) == (720, 720)


def test_scaled_size_always_even():
    """H.264 yuv420p 要求宽高都是偶数，奇数会让 ffmpeg 直接报错。"""
    for w, h in [(1079, 1437), (1001, 1333), (777, 1234), (1920, 803)]:
        nw, nh = transcode.scaled_size(w, h, 480)
        assert nw % 2 == 0 and nh % 2 == 0, (w, h, nw, nh)


def test_scaled_size_rejects_garbage():
    assert transcode.scaled_size(0, 100, 720) is None
    assert transcode.scaled_size(None, None, 720) is None
    assert transcode.scaled_size(-10, 100, 720) is None


def test_plan_variants_never_upscales():
    """只降不升：源短边不大于目标档位时不生成该档。"""
    # 竖屏 1080 短边：1080p 档跳过（等于不升采样），只出 720p / 480p
    plan = transcode.plan_variants(1080, 1438)
    assert [p[0] for p in plan] == ["720p", "480p"]
    assert plan[0][1:] == (720, 958)

    # 4K 横屏：三档全出
    assert [p[0] for p in transcode.plan_variants(3840, 2160)] == ["1080p", "720p", "480p"]

    # 本来就只有 480 短边：一档都不出，只留原画
    assert transcode.plan_variants(854, 480) == []
    # 比最低档还小
    assert transcode.plan_variants(320, 240) == []


def test_plan_variants_high_to_low_order():
    plan = transcode.plan_variants(3840, 2160)
    shorts = [min(w, h) for _, w, h in plan]
    assert shorts == sorted(shorts, reverse=True)


def test_plan_variants_probe_failed_returns_empty():
    """探测不到分辨率时宁可只留原画，也不要瞎猜着转。"""
    assert transcode.plan_variants(None, None) == []
    assert transcode.plan_variants(0, 0) == []


def test_variant_filename():
    assert transcode.variant_filename("abc123.mp4", "720p") == "abc123_720p.mp4"
    # 统一成 mp4 容器，源是 mov 也一样
    assert transcode.variant_filename("abc123.MOV", "480p") == "abc123_480p.mp4"


# ---------------------------------------------------------------- faststart 探测
def _atom(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _mp4(*atoms: bytes) -> bytes:
    return b"".join(atoms)


def test_is_faststart_true_when_moov_before_mdat(tmp_path):
    p = tmp_path / "good.mp4"
    p.write_bytes(_mp4(_atom(b"ftyp", b"isom" * 4),
                       _atom(b"moov", b"\x00" * 40),
                       _atom(b"mdat", b"\x11" * 200)))
    assert transcode.is_faststart(p) is True


def test_is_faststart_false_when_moov_at_tail(tmp_path):
    """手机直出的典型布局：ftyp → free → mdat → moov。这就是卡顿的元凶。"""
    p = tmp_path / "phone.mp4"
    p.write_bytes(_mp4(_atom(b"ftyp", b"isom" * 4),
                       _atom(b"free"),
                       _atom(b"mdat", b"\x11" * 500),
                       _atom(b"moov", b"\x00" * 40)))
    assert transcode.is_faststart(p) is False


def test_is_faststart_handles_64bit_atom(tmp_path):
    """size==1 表示用 64 位长度（大文件的 mdat 常这样）。"""
    big_mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + 100) + b"\x11" * 100
    p = tmp_path / "big.mp4"
    p.write_bytes(_mp4(_atom(b"ftyp", b"isom"), big_mdat, _atom(b"moov", b"\x00" * 8)))
    assert transcode.is_faststart(p) is False


def test_is_faststart_garbage_is_false(tmp_path):
    """读不懂的文件一律 False —— 宁可多做一次无害的重封装，也别漏掉卡顿文件。"""
    p = tmp_path / "junk.mp4"
    p.write_bytes(b"not an mp4 at all")
    assert transcode.is_faststart(p) is False
    assert transcode.is_faststart(tmp_path / "does_not_exist.mp4") is False
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert transcode.is_faststart(empty) is False


# ---------------------------------------------------------------- 重封装的数据安全
def _fake_run(returncode=0, produce=None, stderr=""):
    """造一个假的 subprocess.run：按需在输出路径上写点东西。"""
    class R:
        pass

    def _run(cmd, *a, **kw):
        r = R()
        r.returncode = returncode
        r.stdout = ""
        r.stderr = stderr
        if produce is not None:
            out = cmd[-1]                      # 约定：命令最后一个参数是输出文件
            produce(out)
        return r
    return _run


def test_ensure_faststart_keeps_original_when_ffmpeg_fails(tmp_path, monkeypatch):
    """ffmpeg 失败时**原文件必须原封不动** —— 这是用户唯一的原始素材。"""
    p = tmp_path / "v.mp4"
    original = _mp4(_atom(b"ftyp", b"isom"), _atom(b"mdat", b"\x11" * 300),
                    _atom(b"moov", b"\x00" * 20))
    p.write_bytes(original)
    monkeypatch.setattr(transcode.subprocess, "run", _fake_run(returncode=1))

    assert transcode.ensure_faststart(p) is False
    assert p.read_bytes() == original           # 一个字节都没动
    assert not list(tmp_path.glob("*.tmp*"))    # 临时文件也清干净了


def test_ensure_faststart_rejects_truncated_output(tmp_path, monkeypatch):
    """ffmpeg 返回 0 但产物被截断（体积暴跌）时也不能覆盖原文件。"""
    p = tmp_path / "v.mp4"
    original = _mp4(_atom(b"ftyp", b"isom"), _atom(b"mdat", b"\x11" * 5000),
                    _atom(b"moov", b"\x00" * 20))
    p.write_bytes(original)

    def produce(out):
        # 是 faststart，但只有原文件的一丁点大 —— 典型的中途断掉
        open(out, "wb").write(_mp4(_atom(b"ftyp", b"isom"), _atom(b"moov", b"\x00" * 20),
                                   _atom(b"mdat", b"\x11" * 10)))
    monkeypatch.setattr(transcode.subprocess, "run", _fake_run(produce=produce))

    assert transcode.ensure_faststart(p) is False
    assert p.read_bytes() == original


def test_ensure_faststart_rejects_output_still_not_faststart(tmp_path, monkeypatch):
    """产物 moov 还在尾部（等于没修好）→ 不替换，返回 False。"""
    p = tmp_path / "v.mp4"
    original = _mp4(_atom(b"ftyp", b"isom"), _atom(b"mdat", b"\x11" * 500),
                    _atom(b"moov", b"\x00" * 20))
    p.write_bytes(original)

    def produce(out):
        open(out, "wb").write(original)         # 原样复制，还是 moov 在尾
    monkeypatch.setattr(transcode.subprocess, "run", _fake_run(produce=produce))

    assert transcode.ensure_faststart(p) is False
    assert p.read_bytes() == original


def test_ensure_faststart_replaces_on_success(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    p.write_bytes(_mp4(_atom(b"ftyp", b"isom"), _atom(b"mdat", b"\x11" * 500),
                       _atom(b"moov", b"\x00" * 20)))
    fixed = _mp4(_atom(b"ftyp", b"isom"), _atom(b"moov", b"\x00" * 20),
                 _atom(b"mdat", b"\x11" * 500))

    def produce(out):
        open(out, "wb").write(fixed)
    monkeypatch.setattr(transcode.subprocess, "run", _fake_run(produce=produce))

    assert transcode.ensure_faststart(p) is True
    assert p.read_bytes() == fixed
    assert transcode.is_faststart(p) is True
    assert not list(tmp_path.glob("*.tmp*"))


def test_ensure_faststart_skips_already_faststart(tmp_path, monkeypatch):
    """已经是 faststart 的文件不该白跑一次 ffmpeg。"""
    p = tmp_path / "v.mp4"
    p.write_bytes(_mp4(_atom(b"ftyp", b"isom"), _atom(b"moov", b"\x00" * 20),
                       _atom(b"mdat", b"\x11" * 500)))
    called = []
    monkeypatch.setattr(transcode.subprocess, "run",
                        lambda *a, **kw: called.append(1))
    assert transcode.ensure_faststart(p) is True
    assert called == []


def test_ensure_faststart_skips_unsupported_container(tmp_path, monkeypatch):
    """webm/avi 没有 moov atom，重封装无从谈起，直接跳过而不是瞎跑 ffmpeg。"""
    p = tmp_path / "v.webm"
    p.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
    called = []
    monkeypatch.setattr(transcode.subprocess, "run", lambda *a, **kw: called.append(1))
    assert transcode.ensure_faststart(p) is False
    assert called == []


# ---------------------------------------------------------------- 转码执行
def test_transcode_one_builds_correct_ffmpeg_command(tmp_path, monkeypatch):
    seen = {}

    def _run(cmd, *a, **kw):
        seen["cmd"] = cmd
        open(cmd[-1], "wb").write(b"x" * 100)

        class R:
            returncode = 0
            stdout = stderr = ""
        return R()
    monkeypatch.setattr(transcode.subprocess, "run", _run)

    src = tmp_path / "s.mp4"
    src.write_bytes(b"src")
    out = tmp_path / "out" / "s_720p.mp4"
    r = transcode.transcode_one(src, out, 720, 958)

    assert r["ok"] is True
    assert out.exists()
    cmd = seen["cmd"]
    assert "scale=720:958" in cmd                      # 显式宽高，不是 -2:720
    assert "+faststart" in cmd                         # 档位自己也必须能边下边播
    assert "libx264" in cmd and "yuv420p" in cmd
    assert str(transcode.CRF) in cmd
    assert str(transcode.FFMPEG_THREADS) in cmd        # 线程数受控，别吃满 CPU
    # 先写 .partial 再 rename：前端不会读到写了一半的档位文件
    assert cmd[-1].endswith(".partial.mp4")


def test_transcode_one_failure_leaves_no_partial(tmp_path, monkeypatch):
    def _run(cmd, *a, **kw):
        open(cmd[-1], "wb").write(b"halfwritten")      # 写了一半就挂

        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return R()
    monkeypatch.setattr(transcode.subprocess, "run", _run)

    src = tmp_path / "s.mp4"
    src.write_bytes(b"src")
    out = tmp_path / "s_720p.mp4"
    r = transcode.transcode_one(src, out, 720, 958)

    assert r["ok"] is False and "boom" in r["error"]
    assert not out.exists()                             # 没有半截的成品
    assert not list(tmp_path.glob("*.partial.mp4"))     # 临时文件也清了


def test_transcode_one_timeout_is_reported_not_raised(tmp_path, monkeypatch):
    def _run(cmd, *a, **kw):
        raise transcode.subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(transcode.subprocess, "run", _run)
    src = tmp_path / "s.mp4"
    src.write_bytes(b"src")
    r = transcode.transcode_one(src, tmp_path / "o.mp4", 720, 958)
    assert r["ok"] is False and "超时" in r["error"]


# ---------------------------------------------------------------- 并发闸门
def test_concurrency_gate_is_a_configurable_constant():
    """这台机器上还跑着别的服务，并发必须有上限且可调。"""
    assert isinstance(transcode.MAX_CONCURRENT_TRANSCODES, int)
    assert 1 <= transcode.MAX_CONCURRENT_TRANSCODES <= 8
    assert isinstance(transcode.FFMPEG_THREADS, int)
    assert 1 <= transcode.FFMPEG_THREADS <= 16


def test_env_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("TM_FAKE_KNOB", "not-a-number")
    assert transcode._env_int("TM_FAKE_KNOB", 3) == 3
    monkeypatch.setenv("TM_FAKE_KNOB", "")
    assert transcode._env_int("TM_FAKE_KNOB", 3) == 3
    monkeypatch.setenv("TM_FAKE_KNOB", "5")
    assert transcode._env_int("TM_FAKE_KNOB", 3) == 5


def test_claim_dedupes_same_media():
    """同一个视频不该被同时提交两次（上传后又点重试之类）。"""
    try:
        assert transcode._claim(4242) is True
        assert transcode._claim(4242) is False        # 已经在跑
        assert transcode.inflight_count() >= 1
    finally:
        transcode._release(4242)
    assert transcode._claim(4242) is True             # 放了以后可以再来
    transcode._release(4242)


def test_enqueue_returns_false_when_already_inflight(monkeypatch):
    monkeypatch.setattr(transcode, "_pool", lambda: pytest.fail("不该提交第二次"))
    try:
        transcode._claim(777)
        assert _REAL_ENQUEUE(777, "/tmp/x.mp4") is False
    finally:
        transcode._release(777)


def test_enqueue_releases_claim_when_submit_fails(monkeypatch):
    """入队失败不能把坑永远占着，否则这个 media 再也没法转码。"""
    class Boom:
        def submit(self, fn):
            raise RuntimeError("pool is shut down")
    monkeypatch.setattr(transcode, "_pool", lambda: Boom())
    assert _REAL_ENQUEUE(888, "/tmp/x.mp4") is False
    assert transcode._claim(888) is True              # 坑已经释放
    transcode._release(888)


def test_on_media_uploaded_ignores_photos(monkeypatch):
    calls = []
    monkeypatch.setattr(transcode, "enqueue", lambda *a, **kw: calls.append(a) or True)
    assert transcode.on_media_uploaded(1, "/tmp/a.jpg", "photo") is False
    assert calls == []


def test_on_media_uploaded_enqueues_videos(monkeypatch):
    calls = []
    monkeypatch.setattr(transcode, "enqueue", lambda *a, **kw: calls.append(a) or True)
    assert transcode.on_media_uploaded(7, "/tmp/a.mp4", "video") is True
    assert calls == [(7, "/tmp/a.mp4")]


def test_on_media_uploaded_never_raises(monkeypatch):
    """上传已经成功了，转码只是锦上添花，绝不能反过来把上传搞成 500。"""
    def boom(*a, **kw):
        raise RuntimeError("转码模块炸了")
    monkeypatch.setattr(transcode, "enqueue", boom)
    assert transcode.on_media_uploaded(7, "/tmp/a.mp4", "video") is False


# ---------------------------------------------------------------- 主流程编排
@pytest.fixture
def video_media(make_user):
    u = make_user()
    tid = db.create_trip(u, "T", None, None)
    pid = db.create_point(tid, "P", 30.0, 120.0, None, None)
    mid = db.add_media(pid, "video", "v.mp4", "v.jpg")
    return {"trip_id": tid, "point_id": pid, "media_id": mid}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """把 process_media 依赖的三个重活全换成假的，只留编排逻辑。"""
    state = {"probe": {"width": 1080, "height": 1438, "duration": 6.0, "bitrate": 5629},
             "transcoded": [], "faststart_called": 0, "fail_labels": set(),
             "ladder_calls": []}

    def _probe(path):
        return dict(state["probe"])

    def _faststart(path):
        state["faststart_called"] += 1
        return True

    def _one(src, dest, w, h):
        label = str(dest).rsplit("_", 1)[-1].split(".")[0]
        if label in state["fail_labels"]:
            return {"ok": False, "error": "假装转码失败"}
        state["transcoded"].append((label, w, h))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake mp4 " * 10)
        return {"ok": True, "error": None}

    def _label_of(dest):
        return str(dest).rsplit("_", 1)[-1].split(".")[0]

    def _ladder(src, targets):
        """假的快路。语义必须和真的一致：**全成或全败**。

        任何一档在 fail_labels 里就整条命令失败，调用方该回落到逐档转 ——
        真实的 ffmpeg split 多输出命令就是这个行为。
        """
        items = [(_label_of(d), Path(d), w, h) for d, w, h in targets]
        state["ladder_calls"].append([lab for lab, *_ in items])
        if any(lab in state["fail_labels"] for lab, *_ in items):
            return {"ok": False, "error": "假装有一档挂了"}
        for lab, dest, w, h in items:
            state["transcoded"].append((lab, w, h))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake mp4 " * 10)
        return {"ok": True, "error": None}

    monkeypatch.setattr(transcode, "probe_video", _probe)
    monkeypatch.setattr(transcode, "ensure_faststart", _faststart)
    monkeypatch.setattr(transcode, "transcode_one", _one)
    monkeypatch.setattr(transcode, "transcode_ladder", _ladder)
    return state


def test_process_media_full_flow(video_media, fake_ffmpeg, isolated_env):
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)

    assert r["faststart"] is True
    assert fake_ffmpeg["faststart_called"] == 1
    assert [v["label"] for v in r["variants"]] == ["720p", "480p"]
    assert all(v["status"] == "done" for v in r["variants"])

    rows = {v["label"]: v for v in db.list_variants(video_media["media_id"])}
    assert set(rows) == {"720p", "480p"}
    assert rows["720p"]["width"] == 720 and rows["720p"]["height"] == 958
    assert rows["720p"]["filename"] == "v_720p.mp4"
    assert rows["720p"]["filesize"] > 0
    assert (transcode.VARIANT_DIR / "v_720p.mp4").exists()


def test_process_media_registers_pending_before_faststart(video_media, fake_ffmpeg,
                                                          isolated_env, monkeypatch):
    """档位行必须在**慢**的 faststart 之前就登记好。

    否则重封装那几秒里接口返回「只有原画、transcoding=false」，
    和「这条视频不需要转码」长得一模一样，前端就不会轮询了。
    """
    seen = {}

    def _faststart(path):
        seen["rows"] = db.list_variants(video_media["media_id"])
        return True
    monkeypatch.setattr(transcode, "ensure_faststart", _faststart)

    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    transcode.process_media(video_media["media_id"], src)

    assert {r["label"] for r in seen["rows"]} == {"720p", "480p"}
    assert all(r["status"] == "pending" for r in seen["rows"])


def test_process_media_is_idempotent(video_media, fake_ffmpeg, isolated_env):
    """回填脚本要能反复跑：已经 done 且文件还在的档位跳过，不重复转。"""
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    transcode.process_media(video_media["media_id"], src)
    assert len(fake_ffmpeg["transcoded"]) == 2

    r2 = transcode.process_media(video_media["media_id"], src)
    assert len(fake_ffmpeg["transcoded"]) == 2          # 一次都没重转
    assert sorted(r2["skipped"]) == ["480p", "720p"]
    assert len(db.list_variants(video_media["media_id"])) == 2   # 也没多出重复行


def test_process_media_force_redoes_everything(video_media, fake_ffmpeg, isolated_env):
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    transcode.process_media(video_media["media_id"], src)
    transcode.process_media(video_media["media_id"], src, force=True)
    assert len(fake_ffmpeg["transcoded"]) == 4


def test_process_media_redoes_when_variant_file_vanished(video_media, fake_ffmpeg,
                                                         isolated_env):
    """DB 说 done 但盘上文件没了（手工删过/盘坏过）→ 应该重做，不能死信。"""
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    transcode.process_media(video_media["media_id"], src)
    (transcode.VARIANT_DIR / "v_720p.mp4").unlink()

    r = transcode.process_media(video_media["media_id"], src)
    assert r["skipped"] == ["480p"]
    assert [v["label"] for v in r["variants"]] == ["720p"]
    assert (transcode.VARIANT_DIR / "v_720p.mp4").exists()


def test_process_media_one_label_failing_does_not_block_others(video_media, fake_ffmpeg,
                                                               isolated_env):
    """某一档失败不影响别的档，也不影响原画播放。"""
    fake_ffmpeg["fail_labels"] = {"720p"}
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)

    got = {v["label"]: v["status"] for v in r["variants"]}
    assert got == {"720p": "failed", "480p": "done"}
    rows = {v["label"]: v for v in db.list_variants(video_media["media_id"])}
    assert rows["720p"]["status"] == "failed" and rows["720p"]["error"]
    assert rows["480p"]["status"] == "done"


def test_process_media_missing_source_is_reported(video_media, fake_ffmpeg, isolated_env):
    r = transcode.process_media(video_media["media_id"], transcode.UPLOAD_DIR / "nope.mp4")
    assert r["error"] == "源文件不存在"
    assert db.list_variants(video_media["media_id"]) == []


def test_process_media_small_source_gets_no_variants(video_media, fake_ffmpeg,
                                                     isolated_env):
    """源本来就小 → 只有原画档，一档都不转（也不该留下 pending 行）。"""
    fake_ffmpeg["probe"] = {"width": 640, "height": 480, "duration": 3.0, "bitrate": 500}
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)

    assert r["variants"] == []
    assert db.list_variants(video_media["media_id"]) == []
    assert fake_ffmpeg["faststart_called"] == 1      # 但 faststart 照做不误


def test_process_media_still_faststarts_when_probe_fails(video_media, fake_ffmpeg,
                                                         isolated_env):
    """探测不到分辨率也要把原画重封装好 —— 这才是起播快慢的关键。"""
    fake_ffmpeg["probe"] = {"width": None, "height": None,
                            "duration": None, "bitrate": None}
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)
    assert r["faststart"] is True
    assert r["variants"] == []


def test_retry_failed_resets_only_failed_rows(video_media, fake_ffmpeg, isolated_env,
                                              monkeypatch):
    mid = video_media["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", status="failed", error="boom")
    db.upsert_variant(mid, "480p", filename="v_480p.mp4", status="done")
    monkeypatch.setattr(transcode, "enqueue", lambda *a, **kw: True)

    transcode.retry_failed(mid, transcode.UPLOAD_DIR / "v.mp4")
    rows = {v["label"]: v for v in db.list_variants(mid)}
    assert rows["720p"]["status"] == "pending" and rows["720p"]["error"] is None
    assert rows["480p"]["status"] == "done"          # 成功的那档不动


# ---------------------------------------------------------------- 档位列表拼装
def test_build_quality_list_original_always_first_and_playable():
    """原画档永远在第一位、永远 done —— 转码全挂了也能播。"""
    out = transcode.build_quality_list({"filename": "v.mp4"}, [])
    assert len(out) == 1
    assert out[0]["label"] == "original" and out[0]["status"] == "done"
    assert out[0]["url"] == "/uploads/v.mp4"      # 不带 ?v=1，见 variant_url 注释


def test_build_quality_list_orders_high_to_low():
    variants = [
        {"label": "480p", "filename": "v_480p.mp4", "width": 480, "height": 638,
         "bitrate": 500, "filesize": 100, "status": "done"},
        {"label": "1080p", "filename": "v_1080p.mp4", "width": 1080, "height": 1438,
         "bitrate": 2000, "filesize": 400, "status": "done"},
        {"label": "720p", "filename": "v_720p.mp4", "width": 720, "height": 958,
         "bitrate": 900, "filesize": 200, "status": "done"},
    ]
    out = transcode.build_quality_list({"filename": "v.mp4"}, variants)
    assert [q["label"] for q in out] == ["original", "1080p", "720p", "480p"]
    assert out[2]["url"] == "/uploads/variants/v_720p.mp4"
    assert out[2]["short_side"] == 720


def test_build_quality_list_unfinished_has_no_url():
    """还没转好 / 转失败的档位不给 url，前端拿不到 url 就不会去点它。"""
    variants = [
        {"label": "720p", "filename": "v_720p.mp4", "width": 720, "height": 958,
         "bitrate": None, "filesize": None, "status": "running"},
        {"label": "480p", "filename": "v_480p.mp4", "width": 480, "height": 638,
         "bitrate": None, "filesize": None, "status": "failed"},
    ]
    out = transcode.build_quality_list({"filename": "v.mp4"}, variants)
    assert out[1]["status"] == "running" and out[1]["url"] is None
    assert out[2]["status"] == "failed" and out[2]["url"] is None
    assert out[0]["url"]                       # 原画照样能播


def test_attach_qualities_photos_untouched(video_media, isolated_env):
    mid = video_media["media_id"]
    db.upsert_variant(mid, "720p", filename="v_720p.mp4", width=720, height=958,
                      status="done")
    photo_id = db.add_media(video_media["point_id"], "photo", "p.jpg", "p.jpg")

    rows = transcode.attach_qualities(db.list_media(video_media["point_id"]))
    by_id = {r["id"]: r for r in rows}
    assert "qualities" not in by_id[photo_id]
    assert [q["label"] for q in by_id[mid]["qualities"]] == ["original", "720p"]


def test_attach_qualities_handles_empty_and_photo_only(isolated_env, make_user):
    assert transcode.attach_qualities([]) == []
    u = make_user()
    tid = db.create_trip(u, "T", None, None)
    pid = db.create_point(tid, "P", 1.0, 2.0, None, None)
    db.add_media(pid, "photo", "a.jpg", "t.jpg")
    rows = transcode.attach_qualities(db.list_media(pid))
    assert all("qualities" not in r for r in rows)


def test_attach_qualities_survives_db_failure(video_media, isolated_env, monkeypatch):
    """档位表出问题不能连累整个媒体列表 —— 大不了这次不返回 qualities。"""
    def boom(ids):
        raise RuntimeError("表没了")
    monkeypatch.setattr(db, "get_variants_map", boom)
    rows = transcode.attach_qualities(db.list_media(video_media["point_id"]))
    assert rows and all("qualities" not in r for r in rows)


def test_attach_qualities_single_query_for_many_videos(video_media, isolated_env,
                                                       monkeypatch):
    """相册里几十个视频也只查一次库（防 N+1）。"""
    pid = video_media["point_id"]
    for i in range(12):
        db.add_media(pid, "video", f"v{i}.mp4", None)
    calls = []
    real = db.get_variants_map
    monkeypatch.setattr(db, "get_variants_map",
                        lambda ids: (calls.append(list(ids)), real(ids))[1])
    transcode.attach_qualities(db.list_media(pid))
    assert len(calls) == 1
    assert len(calls[0]) == 13


def test_variant_url_has_no_query_string():
    """档位 URL **不能**带 ?v=1。

    原来带它是为了命中本机 CacheHeaderStaticFiles 的 immutable 分支，但媒体迁到
    OSS 之后那个分支根本不执行；而 media_url 改签名之后，`?v=1` 会被当成 OSS
    对象 key 的一部分编码成 %3Fv%3D1，签出来的链接指向不存在的对象 ——
    视频档位全 404。这条用例就是钉住这个回归。
    """
    assert transcode.variant_url("v_720p.mp4") == "/uploads/variants/v_720p.mp4"


# ------------------------------------------------ 一遍解码多档编码（快路）
def test_ladder_cmd_decodes_once_and_writes_every_rung(tmp_path):
    """快路的命令：一个 -i、一个 split，N 个输出，编码参数和逐档转逐字一致。"""
    src = tmp_path / "s.mp4"
    jobs = [(tmp_path / "s_1080p.partial.mp4", 1080, 1920),
            (tmp_path / "s_720p.partial.mp4", 720, 1280),
            (tmp_path / "s_480p.partial.mp4", 480, 852)]
    cmd = transcode.build_ladder_cmd(src, jobs)

    assert cmd.count("-i") == 1                       # 源只解一遍，这就是快路的全部意义
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "split=3" in fc
    for w, h in [(1080, 1920), (720, 1280), (480, 852)]:
        assert f"scale={w}:{h}" in fc                 # 显式宽高，横竖屏都不会算错
    for tmp, _w, _h in jobs:
        assert str(tmp) in cmd                        # 每一档都有自己的输出
    assert cmd.count("libx264") == 3 and cmd.count("+faststart") == 3
    assert cmd.count("-map") == 6                     # 每档一路视频 + 一路音频
    assert "0:a?" in cmd                              # 问号：没音轨的视频也别整条挂掉


def test_ladder_is_all_or_nothing_and_leaves_no_partials(tmp_path, monkeypatch):
    """快路失败时不能留下半截档位文件——否则前端会拿到一个播不了的档。"""
    def _run(cmd, *a, **kw):
        # 只写出第一个输出就「挂了」
        outs = [c for c in cmd if str(c).endswith(".partial.mp4")]
        open(outs[0], "wb").write(b"x" * 100)

        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return R()
    monkeypatch.setattr(transcode.subprocess, "run", _run)

    src = tmp_path / "s.mp4"
    src.write_bytes(b"src")
    targets = [(tmp_path / "out" / "s_720p.mp4", 720, 958),
               (tmp_path / "out" / "s_480p.mp4", 480, 640)]
    r = transcode.transcode_ladder(src, targets)

    assert r["ok"] is False and "boom" in r["error"]
    for dest, _w, _h in targets:
        assert not dest.exists()
        assert not dest.with_name(dest.stem + ".partial.mp4").exists()


def test_process_media_uses_single_pass_when_there_are_several_rungs(
        video_media, fake_ffmpeg, isolated_env):
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)

    assert r["single_pass"] is True
    assert fake_ffmpeg["ladder_calls"] == [["720p", "480p"]]   # 一次搞定两档
    assert [v["label"] for v in r["variants"]] == ["720p", "480p"]
    rows = {v["label"]: v for v in db.list_variants(video_media["media_id"])}
    assert all(rows[lab]["status"] == "done" for lab in ("720p", "480p"))


def test_process_media_falls_back_to_per_rung_when_single_pass_fails(
        video_media, fake_ffmpeg, isolated_env, monkeypatch):
    """快路是全成或全败，所以它失败之后必须逐档重来，把损失关在单档里。"""
    fake_ffmpeg["fail_labels"] = {"720p"}
    src = transcode.UPLOAD_DIR / "v.mp4"
    src.write_bytes(b"src")
    r = transcode.process_media(video_media["media_id"], src)

    assert r["single_pass"] is False
    assert fake_ffmpeg["ladder_calls"] == [["720p", "480p"]]   # 试过快路
    got = {v["label"]: v["status"] for v in r["variants"]}
    assert got == {"720p": "failed", "480p": "done"}           # 只坏了该坏的那一档
    assert fake_ffmpeg["transcoded"] == [("480p", 480, 638)]   # 好的那档确实逐档转出来了


def test_cpu_budget_takes_several_slots_atomically():
    """一次要 n 个名额必须原子：各拿一半互相等死是这类闸门的经典死法。"""
    b = transcode._CpuBudget(4)
    with b.take(3):
        assert b._free == 1
    assert b._free == 4
    # 要得比总量多不该饿死，按总量给
    with b.take(99):
        assert b._free == 0
    assert b._free == 4
