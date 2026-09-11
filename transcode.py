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
- 转码在后台线程池里跑，**全局限流**（默认 2 并发 × 每任务 4 线程），
  因为这台机器上还跑着别的服务，不能把 CPU 吃满。
- 某一档失败不影响播放：前端永远可以回退到原画档。失败可重试。
"""
import os
import re
import json
import shutil
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


# 同时最多几个转码任务。这台机器上还有别的服务，别把 CPU 吃满。
MAX_CONCURRENT_TRANSCODES = _env_int("TM_TRANSCODE_CONCURRENCY", 2)
# 每个 ffmpeg 进程用几个线程。2 × 4 = 最多占 8 核（本机 20 核）。
FFMPEG_THREADS = _env_int("TM_TRANSCODE_THREADS", 4)
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


# ---------------------------------------------------------------- 转码执行
def transcode_one(src_path, dest_path, width: int, height: int) -> dict:
    """把 src 转成指定分辨率的 H.264/AAC + faststart mp4。

    同步执行（调用方负责放到线程池里）。返回 {"ok": bool, "error": str|None}。
    先写临时文件再 rename，避免前端读到写了一半的档位文件。
    """
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
    result = {"media_id": media_id, "faststart": False, "variants": [], "skipped": []}
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

    # 4) 逐档转码（todo 里已经排除了「已经转好且文件还在」的那些）
    for label, w, h in todo:
        out_name = variant_filename(src_path.name, label)
        out_path = VARIANT_DIR / out_name
        db.set_variant_status(media_id, label, "running")
        r = transcode_one(src_path, out_path, w, h)
        if r["ok"]:
            vinfo = probe_video(out_path)
            db.upsert_variant(
                media_id, label, filename=out_name, width=w, height=h,
                bitrate=vinfo.get("bitrate"), filesize=out_path.stat().st_size,
                status="done", error=None)
            _publish(out_path)      # 同上：刚转好的档位也别让它先走一趟隧道
            result["variants"].append({"label": label, "status": "done"})
        else:
            db.set_variant_status(media_id, label, "failed", error=r["error"])
            result["variants"].append({"label": label, "status": "failed",
                                       "error": r["error"]})
            log.warning("转码失败 media=%s label=%s: %s", media_id, label, r["error"])
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
