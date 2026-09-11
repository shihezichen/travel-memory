"""旅行记忆系统 — 媒体处理（缩略图 + EXIF 读取）"""
import subprocess
from pathlib import Path

from PIL import Image
import exifread

# 注册 HEIC/HEIF 支持（iPhone 照片），使 PIL 能直接打开 .heic
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
THUMB_DIR = UPLOAD_DIR / "thumbs"
THUMB_SIZE = (512, 512)


def _dms_to_decimal(dms, ref):
    """EXIF 度分秒 -> 十进制经纬度"""
    try:
        d, m, s = dms.values
        val = float(d.num) / d.den + float(m.num) / m.den / 60 + float(s.num) / s.den / 3600
    except Exception:
        return None
    if ref in ("S", "W"):
        val = -val
    return round(val, 6)


def read_exif(path: Path):
    """读取照片 EXIF，返回 (拍摄时间, lat, lng)"""
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return None, None, None

    # 拍摄时间
    dt = None
    for key in ("EXIF DateTimeOriginal", "Image DateTimeOriginal", "Image DateTime"):
        if key in tags:
            raw = str(tags[key])
            dt = raw.replace(":", "-", 2).replace(" ", "T")
            break

    # GPS
    lat = lng = None
    try:
        if "GPS GPSLatitude" in tags and "GPS GPSLatitudeRef" in tags:
            lat = _dms_to_decimal(tags["GPS GPSLatitude"], str(tags["GPS GPSLatitudeRef"]))
        if "GPS GPSLongitude" in tags and "GPS GPSLongitudeRef" in tags:
            lng = _dms_to_decimal(tags["GPS GPSLongitude"], str(tags["GPS GPSLongitudeRef"]))
    except Exception:
        lat = lng = None

    return dt, lat, lng


def make_thumb(src_path: Path, mtype: str) -> str:
    """生成缩略图，返回缩略图文件名（不含目录）"""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    name = src_path.stem + ".jpg"
    dest = THUMB_DIR / name

    if mtype == "photo":
        img = Image.open(src_path)
        img.thumbnail(THUMB_SIZE)
        # 处理 EXIF 旋转
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img.convert("RGB").save(dest, "JPEG", quality=82)
    elif mtype == "video":
        _video_thumb(src_path, dest)

    return name


def _ffmpeg_path():
    """获取 ffmpeg 可执行文件路径，优先用 imageio_ffmpeg 内置静态二进制"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # 回退到 PATH


def _video_thumb(src_path: Path, dest: Path):
    """用 ffmpeg 截视频首帧"""
    cmd = [_ffmpeg_path(), "-y", "-i", str(src_path), "-ss", "0",
           "-vframes", "1", "-vf", "scale=512:-2", str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except Exception:
        # ffmpeg 不可用或失败时，生成占位图
        Image.new("RGB", THUMB_SIZE, (40, 40, 40)).save(dest, "JPEG", quality=80)


def probe_video_duration(path) -> float | None:
    """ffprobe 探测视频时长（秒）。失败返回 None。"""
    import json
    ffprobe = str(Path(_ffmpeg_path()).parent / "ffprobe")
    if not Path(ffprobe).exists():
        # imageio_ffmpeg 只带 ffmpeg，用 ffmpeg -i 解析 stderr 兜底
        try:
            r = subprocess.run([_ffmpeg_path(), "-i", str(path)],
                               capture_output=True, text=True, timeout=30)
            import re
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
            if m:
                h, mi, s = m.groups()
                return int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception:
            return None
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                            str(path)], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def make_subtitle_png(text: str, out_path, width: int = 1920, font_size: int = 54,
                      bottom_pad: int = 0, alpha: int = 200):
    """PIL 生成半透明黑底白字字幕图（PNG，含 alpha），供 ffmpeg overlay 使用。
    静态 ffmpeg 无 drawtext/中文字体，故用图片字幕方案（P1-4）。"""
    try:
        from PIL import ImageDraw, ImageFont
    except Exception:
        return False
    # 找可用中文字体：Noto CJK > 思源 > DejaVu 兜底
    font_path = None
    for cand in (
        "/usr/share/fonts/google-noto-vf/NotoSansCJKsc-VF.ttf",
        "/usr/share/fonts/google-noto-vf/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(cand).exists():
            font_path = cand
            break
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # 测量文字宽度（默认字体加载失败时逐字符兜底）
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    try:
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = len(text) * font_size, font_size + 10

    pad_x, pad_y = 36, 20
    img = Image.new("RGBA", (tw + pad_x * 2, th + pad_y * 2), (0, 0, 0, 0))
    dd = ImageDraw.Draw(img)
    # 半透明黑底圆角矩形
    dd.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=16,
                         fill=(0, 0, 0, alpha))
    try:
        dd.text((pad_x - bbox[0], pad_y - bbox[1]), text, font=font, fill=(255, 255, 255, 255))
    except Exception:
        dd.text((pad_x, pad_y), text, font=font, fill=(255, 255, 255, 255))
    # 缩放到目标宽度
    if img.width > width:
        ratio = width / img.width
        img = img.resize((width, int(img.height * ratio)), Image.LANCZOS)
    if bottom_pad:
        canvas = Image.new("RGBA", (width, img.height + bottom_pad), (0, 0, 0, 0))
        canvas.paste(img, (0, bottom_pad), img)
        img = canvas
    img.save(out_path, "PNG")
    return True


def attach_urls(media_list):
    """给一批媒体行挂上 `url` / `thumb_url`（服务端算好的对外地址）。

    为什么必须由服务端算：媒体先落本地磁盘、再每 2 分钟同步到 OSS，
    「这个文件到底传上去没有」只有服务端知道（config.media_url 查同步水位）。
    前端那个 mediaUrl() 是无脑拼前缀，拼不出这个判断，于是刚上传的照片
    在同步之前一律裂图。

    就地修改并返回同一个 list，跟 transcode.attach_qualities 一个用法。
    """
    from config import media_url
    for m in media_list or []:
        if m.get("filename"):
            m["url"] = media_url("/uploads/" + m["filename"])
        if m.get("thumb_filename"):
            m["thumb_url"] = media_url("/uploads/thumbs/" + m["thumb_filename"])
    return media_list
