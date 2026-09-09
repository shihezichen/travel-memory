"""旅程视频生成引擎（P1-4）— FFmpeg 模板引擎

模板：
  slideshow  — 照片轮播：Ken Burns 缓慢缩放 + 交叉淡化 + 地点/日期字幕
  cinematic  — 照片+视频混合：照片 Ken Burns、视频截取片段，淡化转场

字幕方案：静态 ffmpeg 无 drawtext/中文字体 → PIL 生成字幕 PNG 再 overlay。
输出：libx264 + yuv420p + faststart（浏览器可播），无音轨（音乐留接口）。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

try:
    from . import db, media as media_mod
except ImportError:
    import db  # type: ignore
    import media as media_mod  # type: ignore

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
GEN_DIR = UPLOAD_DIR / "generated"
GEN_DIR.mkdir(parents=True, exist_ok=True)

FPS = 25
W, H = 1920, 1080
XFADE_DUR = 0.6          # 转场时长（秒）
DEFAULT_SECONDS = 3.0    # 每张照片默认时长


def _ffmpeg():
    return media_mod._ffmpeg_path()


def _cover_filter(width=W, height=H):
    """缩放+居中裁剪到目标分辨率（cover 模式）"""
    return (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"scale={width}:{height}")


def _zoompan_filter(frames: int):
    """Ken Burns 缓慢推近（1.0 → 1.15）。

    d=1：每输入帧输出 1 帧（输入靠 -loop 1 -t 提供时长），
    z 用 on 线性插值，避免 zoompan 对 loop 输入逐帧倍增导致时长爆炸。
    """
    return (f"zoompan=z='1+0.15*on/{frames}':d=1:s={W}x{H}:fps={FPS}")


def _clip_filter(idx: int, is_photo: bool, sub_stream: int | None,
                 seconds: float = DEFAULT_SECONDS) -> str:
    """单个 clip 的 filter 描述。

    照片: [idx:v]cover+zoompan[vi];[sub_stream:v]字幕处理[subv];[vi][subv]overlay[v{idx}]
    视频: [idx:v]cover[vi];(字幕同左)
    sub_stream: 字幕输入流的绝对下标（媒体输入在前、字幕在后），None=无字幕
    """
    frames = int(seconds * FPS)
    if is_photo:
        flt = f"[{idx}:v]" + _cover_filter() + "," + _zoompan_filter(frames)
    else:
        flt = f"[{idx}:v]" + _cover_filter()
    flt += f"[vi{idx}]"
    if sub_stream is not None:
        fade_out = max(seconds - 0.4, 0.1)
        flt += (f";[{sub_stream}:v]format=rgba,"
                f"fade=t=in:st=0:d=0.4:alpha=1,"
                f"fade=t=out:st={fade_out:.2f}:d=0.4:alpha=1[subv{idx}];"
                f"[vi{idx}][subv{idx}]overlay=(W-w)/2:H-h-90[v{idx}]")
    else:
        flt = flt.replace(f"[vi{idx}]", f"[v{idx}]")
    return flt


def _xfade_chain(n: int, seconds: float = DEFAULT_SECONDS):
    """n 个等长片段 [v0..v{n-1}] → xfade 链式串联 → [vout]。返回 (fc 片段, 总时长)"""
    if n == 1:
        return "[v0]null[vout]", seconds
    parts = []
    prev = "v0"
    offset = seconds - XFADE_DUR
    total = seconds + (n - 1) * (seconds - XFADE_DUR)
    for i in range(1, n):
        out = f"vx{i}" if i < n - 1 else "vout"
        parts.append(f"[{prev}][v{i}]xfade=transition=fade:duration={XFADE_DUR}:offset={offset:.3f}[{out}]")
        prev = out
        offset += seconds - XFADE_DUR
    return ";".join(parts), total


def _make_subtitles(trip_id: int, items: list[dict],
                    seconds: float = DEFAULT_SECONDS) -> list[Path | None]:
    """为每个媒体生成字幕 PNG（地点名 · 日期）。返回与 items 等长的列表。"""
    subs: list[Path | None] = []
    for it in items:
        date = (it.get("exif_time") or it.get("point_start") or "")[:10]
        text = it.get("point_name") or "旅行记忆"
        if date:
            text = f"{text} · {date}"
        p = GEN_DIR / f"sub_{trip_id}_{it['id']}.png"
        try:
            media_mod.make_subtitle_png(text, p, width=W, font_size=54)
            subs.append(p)
        except Exception:
            subs.append(None)
    return subs


def _run(cmd: list[str], timeout: int = 1800):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {proc.stderr[-800:]}")


def generate_trip_video(trip_id: int, template: str = "slideshow",
                        seconds_per_photo: float = DEFAULT_SECONDS,
                        on_progress=None) -> dict:
    """生成行程视频。返回 {output: 相对路径, duration: 秒, photos: n, videos: n}"""
    items = db.list_trip_media(trip_id)
    if not items:
        raise ValueError("行程没有任何媒体，无法生成视频")

    photos = [it for it in items if it["type"] == "photo"]
    vids = [it for it in items if it["type"] == "video"]
    if template == "cinematic":
        clips = photos + vids  # 混合：照片+视频
    else:
        clips = photos if photos else items  # slideshow 默认只用照片；无照片时降级全媒体
    if not clips:
        raise ValueError("没有可用于生成的照片/视频")
    # 按时间排序（list_trip_media 已按 exif_time 排序，这里再稳一遍）
    clips = sorted(clips, key=lambda x: (x.get("exif_time") or "", x["id"]))

    subs = _make_subtitles(trip_id, clips, seconds_per_photo)
    n = len(clips)
    total = n * seconds_per_photo - (n - 1) * XFADE_DUR if n > 1 else seconds_per_photo
    out_name = f"trip{trip_id}_{template}_{int(time.time())}.mp4"
    out_path = GEN_DIR / out_name

    # 输入：先全部媒体（照片 loop 1 / 视频截取），再全部字幕 PNG（loop 1）
    inputs: list[str] = []
    sub_stream_idx: list[int | None] = []   # 每个 clip 对应字幕输入流下标（None=无）
    n_media = len(clips)
    for it in clips:
        src = UPLOAD_DIR / it["filename"]
        if not src.exists():
            raise ValueError(f"媒体文件缺失: {it['filename']}")
        if it["type"] == "photo":
            inputs += ["-loop", "1", "-t", f"{seconds_per_photo}", "-i", str(src)]
        else:
            inputs += ["-ss", "0", "-t", f"{seconds_per_photo}", "-i", str(src)]
    for i, it in enumerate(clips):
        if subs[i] and subs[i].exists():
            inputs += ["-loop", "1", "-t", f"{seconds_per_photo}", "-i", str(subs[i])]
            sub_stream_idx.append(n_media + sum(1 for s in sub_stream_idx if s is not None))
        else:
            sub_stream_idx.append(None)

    # 每个 clip 的 filter
    filters = []
    for i, it in enumerate(clips):
        filters.append(_clip_filter(i, it["type"] == "photo",
                                    sub_stream_idx[i], seconds_per_photo))
    xfade_part, _dur = _xfade_chain(n, seconds_per_photo)
    filter_complex = ";".join(filters) + ";" + xfade_part

    cmd = [_ffmpeg(), "-y", *inputs, "-filter_complex", filter_complex,
           "-map", "[vout]", "-c:v", "libx264", "-preset", "medium",
           "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-movflags", "+faststart", str(out_path)]

    if on_progress:
        on_progress(0, n, "准备素材…")
    _run(cmd)
    if on_progress:
        on_progress(n, n, "完成")

    # 清理字幕临时图
    for p in subs:
        if p and p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    return {"output": f"uploads/generated/{out_name}", "duration": round(total, 1),
            "photos": len(photos), "videos": len(vids), "clips": n}
