#!/usr/bin/env python3
"""把素材合成宣传片：片头卡 + 界面录屏 + 片尾卡，配解说和背景音乐。

素材来源，三样都是脚本产出的、可重跑：
  - build/promo/footage.webm  scripts/record_promo_footage.py 录的真实界面
  - build/promo/vo.mp3        解说，本脚本用 edge-tts 现合成（文案在
                              scripts/promo_narration.txt，那个才是入库的源）
  - build/promo/bgm_src.flac  Wikimedia Commons 上的 CC0 环境音乐

背景音乐只收 CC0/PD —— 跟 demo 照片同一个标准（见 fetch_demo_assets.py 顶部
那段关于署名的说明）。宣传片会挂在公开首页上，用要署名的素材等于埋雷。

时长对齐：录屏比解说长，所以掐掉开头的登录过程，剩下的整体轻微加速去贴解说。
加速比控制在 1.3 倍以内 —— 再快界面就开始显得毛躁了。

用法：
    .venv/bin/python scripts/build_promo_video.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
OUT = ROOT / "build" / "promo"

W, H = 1280, 720
FPS = 30
TITLE_SEC = 2.6
END_SEC = 3.4
# 录屏开头是登录和首屏加载，没什么好看的，直接掐掉。
TRIM_HEAD = 6.0
MAX_SPEED = 1.30

BG = "0x0B1220"          # 深蓝底，跟宣传页的暗色调一致
FG = "white"
ACCENT = "0x4C9AFF"

FONT = None
for c in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
          "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
          "/usr/share/fonts/google-noto-vf/NotoSansCJK-VF.ttc",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
    if Path(c).exists():
        FONT = c
        break


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(" ".join(str(c) for c in cmd[:14]), "...")
        print(r.stderr[-1500:])
        raise SystemExit("ffmpeg 失败")
    return r


def dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def card(text, sub, seconds, out):
    """一张纯色标题卡。文字淡入淡出，避免硬切。"""
    esc = lambda s: s.replace(":", r"\:").replace("'", r"\'")
    fade = min(0.6, seconds / 3)
    draw = (
        f"drawtext=fontfile={FONT}:text='{esc(text)}':fontcolor={FG}:fontsize=62:"
        f"x=(w-tw)/2:y=(h-th)/2-38:alpha='if(lt(t,{fade}),t/{fade},"
        f"if(lt(t,{seconds - fade}),1,({seconds}-t)/{fade}))',"
        f"drawtext=fontfile={FONT}:text='{esc(sub)}':fontcolor=0xA8C0E0:fontsize=27:"
        f"x=(w-tw)/2:y=(h-th)/2+46:alpha='if(lt(t,{fade}),t/{fade},"
        f"if(lt(t,{seconds - fade}),1,({seconds}-t)/{fade}))'"
    )
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c={BG}:s={W}x{H}:r={FPS}:d={seconds}",
         "-vf", draw, "-pix_fmt", "yuv420p", str(out)])


NARRATION = ROOT / "scripts" / "promo_narration.txt"
# 女声、语速放慢 4%：默认速度念产品文案偏赶，听着像广告读秒。
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "-4%"


def synth_narration(dest):
    """文案 -> 配音。改了 promo_narration.txt 重跑就行，不用手动跑 edge-tts。"""
    run([sys.executable, "-m", "edge_tts", "--voice", VOICE, "--rate=" + RATE,
         "--file", str(NARRATION), "--write-media", str(dest)])


def main():
    footage = OUT / "footage.webm"
    vo = OUT / "vo.mp3"
    bgm = OUT / "bgm_src.flac"

    # 文案比配音新 = 改过词了，重合成一遍。
    if NARRATION.exists() and (not vo.exists()
                               or NARRATION.stat().st_mtime > vo.stat().st_mtime):
        print("[0/5] 合成解说（%s）" % VOICE)
        synth_narration(vo)

    for f in (footage, vo, bgm):
        if not f.exists():
            raise SystemExit(f"缺素材：{f}")
    if not FONT:
        raise SystemExit("找不到中文字体，标题卡会变成方块")

    vo_sec = dur(vo)
    body_sec = vo_sec                       # 正片长度 = 解说长度
    raw_sec = dur(footage) - TRIM_HEAD
    speed = max(1.0, min(MAX_SPEED, raw_sec / body_sec))
    print("解说 %.1fs / 可用录屏 %.1fs -> 加速 %.2fx" % (vo_sec, raw_sec, speed))

    print("[1/5] 片头卡")
    card("旅行记忆", "把一趟旅行，变成能重新走一遍的记忆", TITLE_SEC, OUT / "c_title.mp4")
    print("[2/5] 片尾卡")
    card("top001.xin", "一键体验，无需注册  ·  代码公开  ·  数据随时带走", END_SEC, OUT / "c_end.mp4")

    print("[3/5] 正片（掐头 + 调速 + 首尾淡入淡出）")
    body = OUT / "body.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(TRIM_HEAD), "-i", str(footage),
         "-vf", (f"setpts=PTS/{speed},fps={FPS},scale={W}:{H},"
                 f"trim=duration={body_sec},setpts=PTS-STARTPTS,"
                 f"fade=t=in:st=0:d=0.5,fade=t=out:st={body_sec - 0.5}:d=0.5"),
         "-an", "-pix_fmt", "yuv420p", str(body)])

    print("[4/5] 拼接画面")
    concat = OUT / "concat.txt"
    concat.write_text("".join("file '%s'\n" % (OUT / n)
                              for n in ("c_title.mp4", "body.mp4", "c_end.mp4")),
                      encoding="utf-8")
    silent = OUT / "silent.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat), "-c:v", "libx264", "-preset", "medium", "-crf", "23",
         "-pix_fmt", "yuv420p", "-r", str(FPS), str(silent)])

    total = dur(silent)
    print("[5/5] 混音（解说在前，音乐压到背景）+ 封装")
    # 解说从片头卡结束时开始；音乐整条铺满，解说段落自动侧链压低。
    vo_delay_ms = int(TITLE_SEC * 1000)
    mix = (
        # asplit 是必须的：解说要用两次（一次当侧链检测源、一次真的混进去），
        # 而 ffmpeg 里一个滤镜输出只能被消费一次。
        f"[1:a]adelay={vo_delay_ms}|{vo_delay_ms},apad,asplit=2[vo_mix][vo_key];"
        f"[2:a]aloop=loop=-1:size=2e9,atrim=duration={total},"
        f"volume=0.22,afade=t=in:st=0:d=1.5,afade=t=out:st={total - 2.2}:d=2.2[bg];"
        # sidechaincompress：解说一出声，音乐自动让位，比固定音量干净
        f"[bg][vo_key]sidechaincompress=threshold=0.04:ratio=7:attack=8:release=380[duck];"
        # normalize=0 很关键：amix 默认按输入路数均分电平，两路就等于整体 -6dB，
        # 混出来解说只有 -30dB，放网页上要把音量拧到最大才听得清。
        f"[duck][vo_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        # loudnorm 统一到 -16 LUFS（网页视频的常用响度），免得跟站上其他声音忽大忽小
        f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]"
    )
    final = OUT / "travel-memory-promo.mp4"
    run(["ffmpeg", "-y", "-v", "error",
         "-i", str(silent), "-i", str(vo), "-i", str(bgm),
         "-filter_complex", mix, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", "-shortest", str(final)])

    # 封面：正片里挑一帧（片头卡之后 6 秒，那时地图和路线都在）
    poster = OUT / "travel-memory-promo.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(TITLE_SEC + 6), "-i", str(final),
         "-frames:v", "1", "-q:v", "3", str(poster)])

    credit = json.loads((OUT / "bgm_credit.json").read_text(encoding="utf-8"))
    print("\n成片：%s  %.1fs  %.1f MB" % (final, dur(final), final.stat().st_size / 1048576))
    print("封面：%s" % poster)
    print("音乐：%s（%s）" % (credit["title"], credit["license"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
