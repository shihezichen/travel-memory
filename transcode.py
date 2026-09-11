"""旅行记忆系统 — 视频清晰度分层转码

为什么要有这个文件（实测结论，别凭感觉改）：

1. **moov atom 在文件尾 —— 播放卡顿的头号原因。**
   手机直出的 mp4/mov 常把索引（moov）写在文件末尾，浏览器必须先把整个文件
   下完才能拿到索引起播，也无法 seek。实测某条 18MB 的样片布局是
   `ftyp → free → mdat(18MB) → moov(尾部 20KB)`，起播要等 18MB 下完。
   所以**第一优先级不是转码，是把原画重封装成 faststart**（`-c copy`，不重编码，
   秒级完成、画质无损）。哪怕所有档位都还没转好，原画也能立刻起播。

2. **码率过高。** 同一条样片是 1080x1438 竖屏 30fps **5629 kb/s**。
   移动网络扛不住，于是边下边卡。转成 720p CRF24 后是 821 kb/s、体积 1/6.9，
   转码耗时 1.46 秒。这才是分层降级要解决的问题。

3. Range 请求是好的（/uploads 实测返回 206 + content-range），不用管。

设计要点：
- 档位按**短边**算，横竖屏分别处理。竖屏 1080x1438 的「720p」是短边 720，
  即 720x958 —— 写死 `scale=-2:720` 会把竖屏视频算错。
- **只降不升**：源短边不大于目标档位时，不生成该档（不做上采样）。
- 转码在后台线程池里跑，**全局按核数限流**（默认留 20% 的核给别的服务，
  见 CPU_BUDGET）。闸门记的是「同时几路 x264」，所以并发从哪来都算得清。
- **一个视频的多档是一遍解码编出来的**（SINGLE_PASS_LADDER）。逐档转的话源要
  重复解码 N 遍，而解码常常比编码还贵：实测 4K/60fps 三档，逐档串行 23.4s、
  逐档并行 18.0s、一遍解码多档编码 15.1s。快路全成或全败，失败回落逐档转。
- 某一档失败不影响播放：前端永远可以回退到原画档。失败可重试。
"""
import os
import re
import json
import shutil
import contextlib
import struct
import logging
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from config import media_url

import db
from media import _ffmpeg_path

log = logging.getLogger("transcode")

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
VARIANT_DIR = UPLOAD_DIR / "variants"

# ---------------------------------------------------------------- 可调参数
# 都可以用环境变量覆盖，方便在不同机器上调，不用改代码。


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _cpu_count() -> int:
    """可用核数。优先 sched_getaffinity —— 容器里 cpu_count() 会报宿主机的核数。"""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 2


_NPROC = _cpu_count()

# 每个 ffmpeg 进程用几个线程。
FFMPEG_THREADS = _env_int("TM_TRANSCODE_THREADS", 4)
# **真正的闸门**：转码总共可以占几个核。给系统留 20%（至少 2 核）——
# 这台机器上还有 uvicorn、飞书桥等别的服务，不能让转码把 CPU 吃干净。
# 以前是拿「同时几个任务」当闸门，但一个任务占几个核取决于 FFMPEG_THREADS，
# 两个参数一起调才说得清总占用；直接按核数记账更直观，也不会算错。
CPU_BUDGET = _env_int("TM_TRANSCODE_CPU_BUDGET", max(1, _NPROC - max(2, _NPROC // 5)))
# 由预算换算出「同时最多几个 ffmpeg 进程」。
MAX_PARALLEL_FFMPEG = max(1, CPU_BUDGET // max(1, FFMPEG_THREADS))
# 同时最多几个视频在转。比 ffmpeg 闸门略宽：某个视频的档位转完了在推 OSS
# （不吃 CPU）的时候，下一个视频能立刻顶上闸门的空位。
MAX_CONCURRENT_TRANSCODES = _env_int("TM_TRANSCODE_CONCURRENCY", MAX_PARALLEL_FFMPEG)
# 一个视频内部，几个清晰度档位同时转。以前是死板的串行：单个视频上传时
# 只跑一个 ffmpeg、占 4 核，20 核的机器闲着，而用户要等三档依次转完。
LADDER_CONCURRENCY = _env_int("TM_TRANSCODE_LADDER_CONCURRENCY", 3)
# 快路开关：一个 ffmpeg 进程把源**解码一次**、同时编出所有档位。
# 逐档转的话源要被重复解码 N 遍，而解码往往比编码还贵 —— 实测 4K/60fps 三档：
# 逐档串行 23.4s、逐档并行 18.0s、一遍解码多档编码 15.1s。
# 代价是这条命令全成或全败，所以失败要回落到逐档转（那条路单档失败不连坐）。
SINGLE_PASS_LADDER = _env_int("TM_TRANSCODE_SINGLE_PASS", 1) != 0
# x264 质量/速度。CRF 24 + veryfast 是实测的甜点：820kbps、画质够用、25 秒的片子 1.5 秒转完。
CRF = _env_int("TM_TRANSCODE_CRF", 24)
PRESET = os.environ.get("TM_TRANSCODE_PRESET", "veryfast")
AUDIO_BITRATE = os.environ.get("TM_TRANSCODE_ABR", "128k")
# 单个 ffmpeg 任务的超时（秒）。长视频转码慢，给足；卡死也不会永远占着闸门。
FFMPEG_TIMEOUT = _env_int("TM_TRANSCODE_TIMEOUT", 1800)

# 清晰度阶梯：(档位名, 目标短边像素)。从高到低。
LADDER = [
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
]
# 原画档在 DB 里不占行，由接口合成出来（它就是 media.filename 本身）。
ORIGINAL_LABEL = "original"

# 只有这些容器支持 faststart 重封装（moov 前移）。其余的跳过重封装、直接转码。
FASTSTART_SUFFIXES = {".mp4", ".mov", ".m4v"}


# ---------------------------------------------------------------- 探测
def _ffprobe_path():
    """ffprobe 路径。imageio_ffmpeg 只带 ffmpeg 不带 ffprobe，所以要找系统的。"""
    side = Path(_ffmpeg_path()).parent / "ffprobe"
    if side.exists():
        return str(side)
    found = shutil.which("ffprobe")
    return found  # 可能是 None，调用方要兜底


def probe_video(path) -> dict:
    """探测视频宽/高/时长/码率。返回 dict，字段拿不到就是 None。

    优先 ffprobe（准）；没有 ffprobe 就解析 `ffmpeg -i` 的 stderr 兜底
    —— 和 media.probe_video_duration() 一个路数。
    """
    path = str(path)
    info = {"width": None, "height": None, "duration": None, "bitrate": None}

    probe = _ffprobe_path()
    if probe:
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height:format=duration,bit_rate",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout or "{}")
            streams = data.get("streams") or []
            if streams:
                info["width"] = _int_or_none(streams[0].get("width"))
                info["height"] = _int_or_none(streams[0].get("height"))
            fmt = data.get("format") or {}
            info["duration"] = _float_or_none(fmt.get("duration"))
            br = _int_or_none(fmt.get("bit_rate"))
            if br:
                info["bitrate"] = br // 1000  # 存 kb/s
            if info["width"] and info["height"]:
                return info
        except Exception:
            pass  # 落到下面的 ffmpeg 兜底

    # 兜底：解析 ffmpeg -i 的 stderr
    try:
        r = subprocess.run([_ffmpeg_path(), "-i", path],
                           capture_output=True, text=True, timeout=60)
        err = r.stderr or ""
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
        if m:
            h, mi, s = m.groups()
            info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
        m = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
        if m:
            info["bitrate"] = int(m.group(1))
        # 分辨率：取视频流那一行里形如 1920x1080 的第一处
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
        if m:
            info["width"] = int(m.group(1))
            info["height"] = int(m.group(2))
    except Exception:
        pass
    return info


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- faststart
def is_faststart(path) -> bool:
    """检查 mp4/mov 的 moov atom 是否排在 mdat 之前（即能否边下边播）。

    纯 Python 扫顶层 atom，不依赖任何外部工具。读不懂的文件一律返回 False
    （宁可多做一次无害的重封装，也不要漏掉一个卡顿的文件）。
    """
    try:
        path = Path(path)
        total = path.stat().st_size
        with open(path, "rb") as f:
            off = 0
            while off < total:
                f.seek(off)
                head = f.read(16)
                if len(head) < 8:
                    return False
                size = struct.unpack(">I", head[0:4])[0]
                typ = head[4:8].decode("latin1", "replace")
                if size == 1:                       # 64 位长度
                    if len(head) < 16:
                        return False
                    size = struct.unpack(">Q", head[8:16])[0]
                elif size == 0:                     # 一直到文件尾
                    size = total - off
                if typ == "moov":
                    return True                     # 先遇到 moov → 已经是 faststart
                if typ == "mdat":
                    return False                    # 先遇到 mdat → moov 在后面，要修
                if size <= 0:
                    return False
                off += size
    except Exception:
        return False
    return False


def ensure_faststart(path) -> bool:
    """把 moov 挪到文件头（`-c copy`，不重编码）。

    返回 True 表示「现在是 faststart 了」（本来就是、或修好了）；
    False 表示没能修（此时**原文件原封不动**，绝不丢数据）。

    只在重封装产物校验通过后才替换原文件——ffmpeg 中途失败留下的半截文件
    绝不能覆盖用户的原始素材。
    """
    path = Path(path)
    if not path.exists():
        return False
    if path.suffix.lower() not in FASTSTART_SUFFIXES:
        return False
    if is_faststart(path):
        return True

    tmp = path.with_name(path.stem + ".faststart.tmp" + path.suffix)
    try:
        cmd = [_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(path), "-c", "copy", "-map", "0",
               "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.warning("faststart 重封装失败，保留原文件: %s (%s)", path.name,
                        (r.stderr or "")[:200])
            return False
        # 校验：产物必须真的是 faststart，且体积不能离谱地小（防截断）
        if not is_faststart(tmp) or tmp.stat().st_size < path.stat().st_size * 0.5:
            log.warning("faststart 产物校验不通过，保留原文件: %s", path.name)
            return False
        os.replace(tmp, path)      # 同目录 rename，原子替换
        log.info("faststart 重封装完成: %s", path.name)
        return True
    except Exception as e:
        log.warning("faststart 重封装异常，保留原文件: %s (%s)", path.name, e)
        return False
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 档位规划
def scaled_size(width: int, height: int, short_side: int):
    """按短边缩放，保持宽高比，宽高都取偶数（H.264 yuv420p 要求）。

    竖屏 1080x1438 → 短边是 1080（宽）；目标 720 时结果是 720x958。
    横屏 1920x1080 → 短边是 1080（高）；目标 720 时结果是 1280x720。
    """
    if not width or not height or width <= 0 or height <= 0:
        return None
    if width <= height:                      # 竖屏/方形：宽是短边
        nw = short_side
        nh = round(height * short_side / width)
    else:                                    # 横屏：高是短边
        nh = short_side
        nw = round(width * short_side / height)
    nw -= nw % 2
    nh -= nh % 2
    if nw <= 0 or nh <= 0:
        return None
    return nw, nh


def plan_variants(width: int, height: int):
    """给定源分辨率，规划要生成哪些档位。返回 [(label, w, h), ...]，从高到低。

    **只降不升**：源短边 <= 档位目标时跳过该档。
    分辨率探测不到时返回空列表（宁可只留原画，也不要瞎猜着上采样）。
    """
    if not width or not height:
        return []
    src_short = min(width, height)
    out = []
    for label, short_side in LADDER:
        if src_short <= short_side:
            continue                          # 不升采样
        size = scaled_size(width, height, short_side)
        if not size:
            continue
        out.append((label, size[0], size[1]))
    return out


def variant_filename(src_name: str, label: str) -> str:
    """档位文件名：<原文件名去扩展>_<档位>.mp4，统一转成 mp4 容器。"""
    return f"{Path(src_name).stem}_{label}.mp4"


# ---------------------------------------------------------------- CPU 预算闸门
class _CpuBudget:
    """按「同时几路 x264 编码」记账的闸门。

    所有真正跑转码的地方都得先拿号，不管并行来自「多个视频同时转」、
    「一个视频的多个档位同时转」，还是「一个进程里同时编好几档」——
    有这一道，上面几个并发度就可以放心往大了调，总 CPU 占用始终有上界。

    比 threading.Semaphore 多一件事：**一次原子地拿 n 个名额**。用普通信号量
    循环 acquire n 次的话，两个都想拿 3 个的调用方可能各拿到一半互相等死。
    """

    def __init__(self, total: int):
        self._total = max(1, total)
        self._free = self._total
        self._cv = threading.Condition()

    @contextlib.contextmanager
    def take(self, n: int = 1):
        # 要得比总量还多就按总量算 —— 否则这一路永远等不到，直接饿死。
        n = max(1, min(n, self._total))
        with self._cv:
            while self._free < n:
                self._cv.wait()
            self._free -= n
        try:
            yield
        finally:
            with self._cv:
                self._free += n
                self._cv.notify_all()


_CPU_GATE = _CpuBudget(MAX_PARALLEL_FFMPEG)


# ---------------------------------------------------------------- 转码执行
def transcode_one(src_path, dest_path, width: int, height: int) -> dict:
    """把 src 转成指定分辨率的 H.264/AAC + faststart mp4。

    同步执行（调用方负责放到线程池里），并且**自带 CPU 预算闸门**：
    拿不到号就在这儿等，所以任何调用方都不可能绕过总量限制。
    返回 {"ok": bool, "error": str|None}。
    先写临时文件再 rename，避免前端读到写了一半的档位文件。
    """
    with _CPU_GATE.take(1):
        return _transcode_one_locked(src_path, dest_path, width, height)


def _transcode_one_locked(src_path, dest_path, width: int, height: int) -> dict:
    src_path, dest_path = Path(src_path), Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_name(dest_path.stem + ".partial.mp4")
    cmd = [
        _ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_path),
        "-vf", f"scale={width}:{height}",
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-threads", str(FFMPEG_THREADS),
        str(tmp),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return {"ok": False, "error": (r.stderr or "ffmpeg 失败")[:300]}
        os.replace(tmp, dest_path)
        return {"ok": True, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"转码超时（>{FFMPEG_TIMEOUT}s）"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        tmp.unlink(missing_ok=True)


def build_ladder_cmd(src_path, jobs):
    """拼「解码一次、同时编出多档」的 ffmpeg 命令。jobs = [(tmp_path, w, h), ...]

    用 filter_complex 的 split 把解出来的帧分成 N 路，每路各自 scale 到目标档位，
    再接 N 个输出。编码参数和 transcode_one 逐字一致，产物应当完全等价。
    """
    n = len(jobs)
    chains = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n))]
    chains += [f"[s{i}]scale={w}:{h}[v{i}]" for i, (_, w, h) in enumerate(jobs)]
    cmd = [_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src_path), "-filter_complex", ";".join(chains)]
    for i, (tmp, _w, _h) in enumerate(jobs):
        cmd += [
            "-map", f"[v{i}]",
            # `0:a?` 的问号：没有音轨的视频也别整条命令失败
            "-map", "0:a?",
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            "-threads", str(FFMPEG_THREADS),
            str(tmp),
        ]
    return cmd


def transcode_ladder(src_path, targets) -> dict:
    """一个 ffmpeg 进程解码一次、同时编出所有档位。**快路**。

    targets = [(dest_path, width, height), ...]，返回 {"ok", "error"}。
    **全成或全败**：任何一档出问题整条命令都失败，调用方必须能回落到
    逐档的 transcode_one（那条路才能做到单档失败不连坐）。

    占的 CPU 名额按档位数算——这一个进程里跑着 N 路 x264。
    """
    src_path = Path(src_path)
    targets = [(Path(d), w, h) for d, w, h in targets]
    if not targets:
        return {"ok": True, "error": None}
    for dest, _w, _h in targets:
        dest.parent.mkdir(parents=True, exist_ok=True)
    # 同样先写 .partial 再整体 rename：中途失败不会在 variants 目录留下半截文件，
    # 也不会让前端读到一个正在写的档位。
    tmps = [d.with_name(d.stem + ".partial.mp4") for d, _w, _h in targets]
    cmd = build_ladder_cmd(src_path, [(t, w, h) for t, (_d, w, h) in zip(tmps, targets)])
    try:
        with _CPU_GATE.take(len(targets)):
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=FFMPEG_TIMEOUT)
        if r.returncode != 0 or any(not t.exists() or t.stat().st_size == 0
                                    for t in tmps):
            return {"ok": False, "error": (r.stderr or "ffmpeg 失败")[:300]}
        for tmp, (dest, _w, _h) in zip(tmps, targets):
            os.replace(tmp, dest)
        return {"ok": True, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"转码超时（>{FFMPEG_TIMEOUT}s）"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        for t in tmps:
            t.unlink(missing_ok=True)


# ---------------------------------------------------------------- 任务闸门
_POOL = None
_POOL_LOCK = threading.Lock()
# 正在排队/执行的 media_id，防止同一个视频被重复提交
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()


def _pool():
    """懒加载线程池 —— import 本模块不该凭空起线程（测试里尤其别）。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TRANSCODES,
                                       thread_name_prefix="transcode")
        return _POOL


def _claim(media_id: int) -> bool:
    """占坑。已经在跑就返回 False。"""
    with _INFLIGHT_LOCK:
        if media_id in _INFLIGHT:
            return False
        _INFLIGHT.add(media_id)
        return True


def _release(media_id: int):
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(media_id)


def inflight_count() -> int:
    with _INFLIGHT_LOCK:
        return len(_INFLIGHT)


# ---------------------------------------------------------------- 主流程

def _publish(path) -> bool:
    """把刚写好的文件推到 OSS。没配 OSS / 推失败都只是回到「等 sync-oss 兜底」，
    绝不能让它影响转码本身，所以整段包在 try 里、oss 也延迟到这里才 import
    （转码这条链路不该因为 OSS 模块的任何问题起不来）。"""
    try:
        import oss
        return oss.publish_local(path)
    except Exception as e:
        log.warning("推 OSS 失败（等 sync-oss 兜底）%s: %s", Path(path).name, e)
        return False


def process_media(media_id: int, src_path, force: bool = False) -> dict:
    """**同步**处理一个视频：登记档位 → faststart 重封装 → 逐档转码。

    这是回填脚本和后台线程共用的那段真正的逻辑。可重复调用、幂等：
    已经 done 且文件还在的档位会跳过（force=True 时强制重做）。

    为什么「登记 pending」排在 faststart **之前**：登记只是几条 INSERT（毫秒级），
    faststart 重封装要跑 ffmpeg（秒级，大文件更久）。如果先重封装，那段时间里
    /api/media/{id}/variants 返回的是「只有原画、transcoding=false」——
    和「这条视频本来就不需要转码」一模一样，前端据此判定不用轮询，
    于是档位转好了页面也不会自己刷出来，非得手动刷新。先登记就没这个空窗。
    """
    src_path = Path(src_path)
    result = {"media_id": media_id, "faststart": False, "variants": [], "skipped": [],
              "single_pass": False}
    if not src_path.exists():
        result["error"] = "源文件不存在"
        return result

    # 1) 探测源分辨率，规划档位
    info = probe_video(src_path)
    result["probe"] = info
    plan = plan_variants(info.get("width"), info.get("height"))

    # 2) 先把所有档位登记成 pending，前端立刻能看到「转码中」
    todo = []
    if plan:
        VARIANT_DIR.mkdir(parents=True, exist_ok=True)
        for label, w, h in plan:
            existing = db.get_variant(media_id, label)
            if (not force and existing and existing["status"] == "done"
                    and existing["filename"]
                    and (VARIANT_DIR / existing["filename"]).exists()):
                result["skipped"].append(label)
                continue
            db.upsert_variant(media_id, label,
                              filename=variant_filename(src_path.name, label),
                              width=w, height=h, status="pending", error=None)
            todo.append((label, w, h))

    # 3) 原画 faststart —— 最要紧的一步（哪怕一档都没转好，原画也能立刻起播）。
    #    失败也继续往下走：重封装不了不代表转不了码。
    before_ns = src_path.stat().st_mtime_ns
    result["faststart"] = ensure_faststart(src_path)
    # 真重写了才推。**直传上来的原片本来就在 OSS 上**，重写之后本地这份变新，
    # config._synced_to_oss() 会判成「还没同步」→ 播放链接指回本机 →
    # 流量重新挤回 4Mbps 隧道，一直到下一趟 sync-oss（最多两分半）才恢复。
    # 转完就推，那个窗口直接没有了；字节数和 sync-oss 稍后要传的一模一样。
    if src_path.stat().st_mtime_ns != before_ns:
        result["republished"] = _publish(src_path)

    if not plan:
        return result          # 源本来就小（或探测失败）→ 只有原画档，正常

    # 4) 各档并行转（todo 里已经排除了「已经转好且文件还在」的那些）。
    #    真正的 CPU 上限由 transcode_one 里的 _CPU_GATE 把着，这里的并发度只决定
    #    「最多同时往闸门前排几个档」。档位之间互不依赖，都从源文件读，可以随便并行。
    def _out_path(label):
        return VARIANT_DIR / variant_filename(src_path.name, label)

    def _mark_done(label, out_path, w, h):
        vinfo = probe_video(out_path)
        db.upsert_variant(
            media_id, label, filename=out_path.name, width=w, height=h,
            bitrate=vinfo.get("bitrate"), filesize=out_path.stat().st_size,
            status="done", error=None)
        _publish(out_path)          # 同上：刚转好的档位也别让它先走一趟隧道
        return {"label": label, "status": "done"}

    def _one(label, w, h):
        out_path = _out_path(label)
        db.set_variant_status(media_id, label, "running")
        r = transcode_one(src_path, out_path, w, h)
        if r["ok"]:
            return _mark_done(label, out_path, w, h)
        db.set_variant_status(media_id, label, "failed", error=r["error"])
        log.warning("转码失败 media=%s label=%s: %s", media_id, label, r["error"])
        return {"label": label, "status": "failed", "error": r["error"]}

    # 4a) 快路：一个 ffmpeg 解码一次、同时编出所有档位。省掉 N-1 遍重复解码。
    if SINGLE_PASS_LADDER and len(todo) > 1:
        for label, _w, _h in todo:
            db.set_variant_status(media_id, label, "running")
        r = transcode_ladder(src_path, [(_out_path(label), w, h)
                                        for label, w, h in todo])
        if r["ok"]:
            result["single_pass"] = True
            result["variants"] = [_mark_done(label, _out_path(label), w, h)
                                  for label, w, h in todo]
            return result
        # 快路是全成或全败，失败原因可能只出在某一档。回落到逐档转，
        # 那条路能把失败限制在单档，剩下的档位照样能转出来。
        log.warning("一遍解码多档编码失败，回落到逐档转 media=%s: %s",
                    media_id, r["error"])
        result["single_pass"] = False

    # 4b) 逐档转（也是快路失败后的兜底）
    if len(todo) <= 1 or LADDER_CONCURRENCY <= 1:
        result["variants"] = [_one(*t) for t in todo]
    else:
        # 每个视频用自己的小线程池，用完就收。线程创建的开销相对 ffmpeg 可以忽略，
        # 换来的是不用去操心一个全局池怎么分配才不会把某个视频饿死。
        with ThreadPoolExecutor(max_workers=min(LADDER_CONCURRENCY, len(todo)),
                                thread_name_prefix=f"ladder{media_id}") as ex:
            futures = [ex.submit(_one, *t) for t in todo]
            # 按 todo 的顺序收结果（高档在前），跟串行时代的返回顺序保持一致。
            result["variants"] = [f.result() for f in futures]
    return result


def enqueue(media_id: int, src_path, force: bool = False):
    """把一个视频丢进后台转码队列，**立刻返回**（不阻塞请求）。

    返回 True = 已入队；False = 该 media 已经在队列里了（去重）。
    """
    if not _claim(media_id):
        return False

    def _worker():
        try:
            process_media(media_id, src_path, force=force)
        except Exception as e:                   # 后台线程绝不能抛出去
            log.exception("转码任务异常 media=%s: %s", media_id, e)
        finally:
            _release(media_id)

    try:
        _pool().submit(_worker)
    except Exception as e:
        _release(media_id)
        log.warning("转码任务入队失败 media=%s: %s", media_id, e)
        return False
    return True


def on_media_uploaded(media_id: int, path, mtype: str):
    """**上传链路的对接入口** —— 上传成功、文件已落盘、media 行已写库之后调它。

    只做一件事：视频就丢进后台队列。非视频直接忽略。
    绝不抛异常（上传已经成功了，转码是锦上添花，不能反过来把上传搞失败）。

    用法（在 api_upload_media 的 db.add_media(...) 之后一行）：
        transcode.on_media_uploaded(mid, src_path, mtype)
    """
    try:
        if mtype != "video":
            return False
        return enqueue(media_id, path)
    except Exception as e:
        log.warning("on_media_uploaded 失败 media=%s: %s", media_id, e)
        return False


def retry_failed(media_id: int, src_path):
    """重试某个视频里失败的档位（把 failed 清成 pending 再重跑）。"""
    for v in db.list_variants(media_id):
        if v["status"] == "failed":
            db.set_variant_status(media_id, v["label"], "pending", error=None)
    return enqueue(media_id, src_path)


def variant_url(filename: str) -> str:
    """档位文件的访问 URL。带 v= 是为了命中 CacheHeaderStaticFiles 的
    immutable 长缓存分支 —— 档位文件内容永不改变，重复播放不该再回源。"""
    return media_url(f"/uploads/variants/{filename}?v=1")


def attach_qualities(media_list):
    """给一批媒体行挂上 `qualities` 字段（视频才有，照片原样不动）。

    一次批量查库（不是每条查一次），所以相册页有几十个视频也只多一次查询。
    就地修改并返回同一个 list，方便在接口里一行调用。
    """
    if not media_list:
        return media_list
    video_ids = [m["id"] for m in media_list
                 if m.get("type") == "video" and m.get("id") is not None]
    if not video_ids:
        return media_list
    try:
        vmap = db.get_variants_map(video_ids)
    except Exception as e:                 # 档位表出问题不能连累媒体列表
        log.warning("读取档位失败，本次不返回 qualities: %s", e)
        return media_list
    for m in media_list:
        if m.get("type") == "video":
            m["qualities"] = build_quality_list(m, vmap.get(m["id"], []))
    return media_list


def build_quality_list(media_row, variants) -> list:
    """把 DB 里的档位行拼成前端要的清晰度列表（含「原画」档）。

    原画档永远排在第一位且永远可用 —— 前端任何时候都能回退到它。
    """
    out = [{
        "label": ORIGINAL_LABEL,
        "name": "原画",
        "url": media_url(f"/uploads/{media_row['filename']}?v=1"),
        "status": "done",
        "width": None, "height": None, "bitrate": None, "filesize": None,
        "short_side": None,
    }]
    order = {label: i for i, (label, _) in enumerate(LADDER)}
    short_by_label = dict(LADDER)
    for v in sorted(variants, key=lambda x: order.get(x["label"], 99)):
        done = v["status"] == "done" and v["filename"]
        out.append({
            "label": v["label"],
            "name": v["label"],
            "url": variant_url(v["filename"]) if done else None,
            "status": v["status"],
            "width": v["width"], "height": v["height"],
            "bitrate": v["bitrate"], "filesize": v["filesize"],
            "short_side": short_by_label.get(v["label"]),
        })
    return out
