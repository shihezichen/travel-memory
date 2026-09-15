#!/usr/bin/env python3
"""把 28 个镜头按分镜表拼成成片：切片 → 运镜 → 转场 → 混音。

三件事值得说明：

1. 转场为什么要「多做一截」。xfade 是把两段重叠 d 秒叠化，拼完就比原素材短 d 秒。
   所以每段素材都按 shots.need_len() 多准备出跟下一段重叠的那一截，
   一路 xfade 下来总长正好等于分镜表上的时长和。

2. 运镜为什么不用 zoompan。zoompan 按帧号推进、对非整数时长会抖，而且它内部先
   放大再缩小，画面糊。这里用 crop+scale：按时间算出裁切窗口，再放大回 720p，
   等价于一台匀速推拉的摄影机，边缘锐利。

3. UI 镜头的速度。录屏时每个动作留的时间比镜头长，切下来再用 setpts 压到目标
   时长，压缩比限在 1.6 倍以内 —— 再快界面就开始显得毛躁。

用法：
    .venv/bin/python scripts/promo/assemble.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import shots as S                                        # noqa: E402

OUT = ROOT / "build" / "promo"
CLIPS = OUT / "clips"
CUTS = OUT / "cuts"
W, H, FPS = 1280, 720, 30
MAX_SPEED = 1.6

XF = {"cut": "fade", "none": "fade", "diss": "fade",
      "white": "fadewhite", "slide": "slideleft"}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(" ".join(str(c) for c in cmd[:16]), "...")
        print(r.stderr[-1500:])
        raise SystemExit("ffmpeg 失败")
    return r


def dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def focus_anchor(fx, fy, z):
    """焦点像素坐标 → crop 的锚点比例。

    让焦点落在裁切窗中心；窗口贴边时夹回 0~1，不然会裁到画面外。
    """
    cw, ch = W / z, H / z
    ax = (fx - cw / 2) / max(1e-6, W - cw)
    ay = (fy - ch / 2) / max(1e-6, H - ch)
    return max(0.0, min(1.0, ax)), max(0.0, min(1.0, ay))


def move_filter(mv, seconds, frames):
    """把运镜翻译成 ffmpeg 滤镜。

    为什么不是 crop：crop 的 w/h 只在初始化时求一次值，写成随 t 变化会直接报错，
    它只有 x/y 支持逐帧。连续推拉只能交给 zoompan。

    zoompan 出了名的抖 —— 它在原分辨率上取整算裁切窗，缩放比每帧只差一点点时
    窗口会来回跳一个像素。先把输入放大到 2 倍再 zoompan，取整误差被摊薄到半个
    像素以内，就看不出来了。
    """
    n = max(1, frames - 1)
    if not mv:
        return f"scale={W}:{H}"
    kind = mv[0]
    if kind == "zoom":
        z0, z1, ax, ay = mv[1], mv[2], mv[3], mv[4]
        z = f"{z0}+({z1}-{z0})*on/{n}"
        return (f"scale={W*2}:{H*2},"
                f"zoompan=z='{z}':x='(iw-iw/zoom)*{ax}':y='(ih-ih/zoom)*{ay}':"
                f"d=1:s={W}x{H}:fps={FPS}")
    if kind == "pan":
        dx, dy = mv[1], mv[2]           # 画面尺寸的比例，正数往右/往下
        z = 1.10                        # 先留出可平移的余量
        x = f"(iw-iw/zoom)*(0.5+{dx}*(on/{n}-0.5)*2)"
        y = f"(ih-ih/zoom)*(0.5+{dy}*(on/{n}-0.5)*2)"
        return (f"scale={W*2}:{H*2},"
                f"zoompan=z='{z}':x='max(0,min(iw-iw/zoom,{x}))':"
                f"y='max(0,min(ih-ih/zoom,{y}))':d=1:s={W}x{H}:fps={FPS}")
    if kind == "focus":
        # 锚点已经在 build_cut 里按焦点算好，塞回 zoom 分支走同一套
        z0, z1, ax, ay = mv[1], mv[2], mv[3], mv[4]
        z = f"{z0}+({z1}-{z0})*on/{n}"
        return (f"scale={W*2}:{H*2},"
                f"zoompan=z='{z}':x='(iw-iw/zoom)*{ax}':y='(ih-ih/zoom)*{ay}':"
                f"d=1:s={W}x{H}:fps={FPS}")
    if kind == "crop":
        # 固定倍数的局部放大，不随时间变，静态 crop 就够，画面比 zoompan 锐
        z, ax, ay = mv[1], mv[2], mv[3]
        cw, ch = f"floor(iw/{z}/2)*2", f"floor(ih/{z}/2)*2"
        return (f"crop=w='{cw}':h='{ch}':x='floor((iw-{cw})*{ax}/2)*2':"
                f"y='floor((ih-{ch})*{ay}/2)*2',scale={W}:{H}")
    return f"scale={W}:{H}"


def build_cut(i, tl, marks, dest):
    """产出单个镜头：素材 → 掐到 need_len → 运镜 → 定帧率。"""
    num, a, b, d, src, mv, tr, line = tl[i]
    need = S.need_len(tl, i)
    kind, key = src.split(":", 1)

    if kind in ("synth", "card"):
        srcfile = CLIPS / ("%s.mp4" % key)
        if not srcfile.exists():
            raise SystemExit("缺镜头 %s，先跑 synth.py" % key)
        pre = ["-i", str(srcfile)]
        speed = 1.0
    else:
        m = marks.get(key)
        if not m:
            raise SystemExit("缺录屏标记 %s，先跑 record.py" % key)
        raw = OUT / "raw" / ("%s.webm" % m["group"])
        have = m["end"] - m["start"]
        # 录的时候留得比镜头长，这里压回去；压太狠界面会显得毛躁
        speed = max(1.0, min(MAX_SPEED, have / need))
        take = min(have, need * speed)
        if take < need * speed - 0.02:
            print("    ! %s 素材短了 %.2fs，末尾会重复最后一帧" % (key, need * speed - take))
        pre = ["-ss", "%.3f" % m["start"], "-t", "%.3f" % take, "-i", str(raw)]

    if mv and mv[0] == "focus":
        f = (marks.get(key) or {}).get("focus")
        if f:
            # 取推到最近时的窗口来定锚点：推的过程中焦点始终不出画
            ax, ay = focus_anchor(f["x"], f["y"], max(mv[1], mv[2]))
            mv = ("focus", mv[1], mv[2], round(ax, 4), round(ay, 4))
        else:
            print("    ! %s 没记下焦点，退回居中推近" % key)
            mv = ("zoom", mv[1], mv[2], 0.5, 0.5)

    vf = []
    if speed > 1.001:
        vf.append("setpts=PTS/%.4f" % speed)
    vf.append(f"fps={FPS}")
    vf.append(move_filter(mv, need, int(round(need * FPS))))
    # tpad 兜底：素材万一差几帧，用最后一帧补齐，不然 xfade 会算错偏移
    vf.append(f"tpad=stop_mode=clone:stop_duration=1")
    vf.append(f"trim=duration={need:.3f},setpts=PTS-STARTPTS")
    run(["ffmpeg", "-y", "-v", "error"] + pre +
        ["-vf", ",".join(vf), "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS), str(dest)])
    return need, speed


def main():
    CUTS.mkdir(parents=True, exist_ok=True)
    marks = json.loads((OUT / "ui_marks.json").read_text(encoding="utf-8"))
    tl = S.timeline()

    print("[1/4] 切 %d 个镜头" % len(tl))
    files = []
    for i, t in enumerate(tl):
        dest = CUTS / ("%02d_%s.mp4" % (i, t[4].split(":", 1)[1]))
        need, sp = build_cut(i, tl, marks, dest)
        files.append(dest)
        flag = "  %.2fx" % sp if sp > 1.001 else ""
        print("  %02d %-16s %.2fs%s" % (i, t[4], need, flag))

    print("[2/4] 转场拼接")
    # xfade 只能两两拼，链式串起来；offset 是「前面所有镜头的时长和」
    inputs = []
    for f in files:
        inputs += ["-i", str(f)]
    parts, cur, off = [], "0:v", 0.0
    for i in range(1, len(files)):
        tr = tl[i][6]
        d = S.TRANS.get(tr, 0.10)
        off += tl[i - 1][3]
        lbl = "v%d" % i
        parts.append("[%s][%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f[%s]"
                     % (cur, i, XF.get(tr, "fade"), d, off, lbl))
        cur = lbl
    silent = OUT / "v2_silent.mp4"
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(parts), "-map", "[%s]" % cur,
         "-c:v", "libx264", "-preset", "medium", "-crf", "21",
         "-pix_fmt", "yuv420p", "-r", str(FPS), str(silent)])
    print("  画面 %.2fs（分镜表 %.2fs）" % (dur(silent), S.TOTAL))

    print("[3/4] 混音")
    vo, bgm = OUT / "vo.mp3", OUT / "bgm_src.flac"
    total = dur(silent)
    vo_delay = int(S.TITLE_SEC * 1000)
    mix = (
        # asplit：解说既要混进去，又要当侧链的检测源，一个输出喂不了两处
        f"[1:a]adelay={vo_delay}|{vo_delay},apad,asplit=2[vo_mix][vo_key];"
        f"[2:a]aloop=loop=-1:size=2e9,atrim=duration={total},"
        f"volume=0.22,afade=t=in:st=0:d=1.5,afade=t=out:st={total - 2.2}:d=2.2[bg];"
        # 解说一出声音乐自动让位，比固定音量干净
        f"[bg][vo_key]sidechaincompress=threshold=0.04:ratio=7:attack=8:release=380[duck];"
        # normalize=0：amix 默认按路数均分电平，两路就等于整体 -6dB，
        # 不关掉的话解说只有 -30dB，网页上要把音量拧到最大才听得清
        f"[duck][vo_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]"
    )
    final = OUT / "travel-memory-promo.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(vo),
         "-i", str(bgm), "-filter_complex", mix, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", "-shortest", str(final)])

    print("[4/4] 封面")
    poster = OUT / "travel-memory-promo.jpg"
    # 挑「照片飞回地图」落定那一刻，一帧就说清产品是干什么的
    run(["ffmpeg", "-y", "-v", "error", "-ss", "16.6", "-i", str(final),
         "-frames:v", "1", "-q:v", "3", str(poster)])

    print("\n成片：%s  %.2fs  %.1f MB" % (final, dur(final),
                                        final.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
