# -*- coding: utf-8 -*-
"""分镜表 —— 整个宣传片的单一事实源。

改这里就等于改片子：改 dur 改时长、改 src 换画面、改 mv 换运镜、改 tr 换转场。
时间轴不是手写的，是从配音 build/promo/vo.mp3 的静音边界测出来的（见 SEG），
所以只要不换配音，镜头就一定卡在解说的字上。

src 三种来源：
  synth:<name>   HTML 动画，scripts/promo/synth/<name>.html，playwright 录
  ui:<name>      真实界面录屏，scripts/promo/record.py 里的同名镜头
  card:<name>    片头/片尾卡，synth 的一种，单独分类只为好认
"""

TITLE_SEC = 2.60          # 片头卡
END_SEC = 3.40            # 片尾卡
VO_SEC = 52.416           # 配音总长

# 配音里的 17 个发声段（ffmpeg silencedetect -36dB/0.30s 测出来的）
SEG = [(0.000, 1.756), (2.112, 3.644), (4.019, 5.696), (6.397, 9.789),
       (10.456, 11.332), (11.698, 13.705), (14.420, 18.774), (19.091, 20.666),
       (21.347, 24.797), (25.487, 26.882), (27.198, 29.183), (29.491, 35.132),
       (35.810, 36.691), (37.077, 40.097), (40.780, 46.203), (46.881, 47.781),
       (48.478, 51.775)]

LINE = [
    "一趟旅行结束以后，", "照片散在相册里，", "路线留在导航记录里。",
    "那些当时的心情，过几个月就想不起来了。", "旅行记忆，", "把它们放回同一张地图上。",
    "落脚点一个一个标上去，系统算出真正的驾车路线，", "不是两点之间的直线。",
    "照片拍完就传，时间和坐标自己填好。", "一个月以后再回来，", "可以沿着路线重新走一遍，",
    "可以翻相册墙，也可以看足迹热力图，看看自己到底去过多少地方。", "想给谁看，",
    "发一个只读链接就行，对方不用注册。", "它还留了一个 MCP 接口，让你的智能体直接读懂这些记忆。",
    "旅行记忆。", "代码公开可查，数据随时带走。",
]

# 每句拆成几个镜头：(占本句时长的比例, src, 运镜, 转入方式)
# 运镜 mv：None 不动 / ("zoom", 起, 止, x锚, y锚) / ("pan", dx, dy) / ("crop", 倍数, x, y)
#          / ("focus", 起, 止) 对准 record.py 记下的焦点元素推近 —— 锚点不用手填，
#            界面改版也不会放偏
# 转场 tr：cut 硬切 / diss 溶解 / white 白闪 / slide 推屏 / none 与上镜连续
SHOTS = {
    1:  [(1.00, "synth:wall_mess",   ("pan", 0, 0.06),            "cut")],
    2:  [(1.00, "synth:wall_burst",  None,                        "cut")],
    3:  [(1.00, "synth:gps_line",    None,                        "cut")],
    4:  [(0.45, "synth:drift_apart", ("zoom", 1.06, 1.0, .5, .5), "diss"),
         (0.55, "synth:fade_dark",   None,                        "diss")],
    5:  [(1.00, "synth:logo_in",     ("zoom", 1.0, 1.04, .5, .5), "white")],
    6:  [(1.00, "synth:fly_to_map",  None,                        "diss")],
    7:  [(0.50, "ui:duku_points",    ("zoom", 1.0, 1.12, .45, .5), "cut"),
         (0.50, "ui:duku_route",     ("zoom", 1.12, 1.0, .45, .5), "none")],
    8:  [(1.00, "ui:duku_curve",     ("pan", 0.10, 0),            "slide")],
    9:  [(0.42, "ui:upload_drop",    ("focus", 1.15, 1.5),        "cut"),
         (0.58, "ui:upload_exif",    ("zoom", 1.15, 1.38, .14, .95), "none")],
    10: [(1.00, "ui:kansai_fit",     ("zoom", 1.25, 1.0, .5, .5), "white")],
    11: [(1.00, "ui:kansai_replay",  None,                        "none")],
    12: [(0.32, "ui:albums",         ("focus", 1.9, 2.1),         "cut"),
         (0.32, "synth:heat_on",     ("zoom", 1.0, 1.08, .5, .5), "diss"),
         (0.36, "synth:heat_out",    ("zoom", 1.18, 1.0, .5, .5), "none")],
    13: [(1.00, "ui:share_btn",      ("focus", 2.7, 2.9),         "cut")],
    14: [(0.42, "ui:share_link",     ("zoom", 1.0, 1.10, .5, .4), "none"),
         (0.58, "synth:phone_view",  None,                        "slide")],
    15: [(0.33, "synth:mcp_ask",     None,                        "white"),
         (0.33, "synth:mcp_tools",   None,                        "none"),
         (0.34, "synth:mcp_answer",  None,                        "diss")],
    16: [(1.00, "synth:logo_out",    None,                        "white")],
    17: [(0.45, "synth:github",      ("pan", 0, 0.05),            "cut"),
         (0.55, "ui:export",         ("focus", 1.3, 2.2),         "diss")],
}


def timeline():
    """展开成扁平的镜头列表：[(编号, 起, 止, 时长, src, 运镜, 转场, 解说), ...]

    片头卡和片尾卡也在里面，编号用 0 和 99。
    镜头之间的停顿（解说的换气）归给前一个镜头 —— 画面不能断，
    否则每句之间会闪一下黑。
    """
    out = [(0, 0.0, TITLE_SEC, TITLE_SEC, "card:title", ("zoom", 1.0, 1.06, .5, .5),
            "none", "")]
    for i, ((s, e), line) in enumerate(zip(SEG, LINE), 1):
        a = s + TITLE_SEC
        # 本句画面一直持续到下一句开口，换气的空隙不切黑
        nxt = SEG[i][0] + TITLE_SEC if i < len(SEG) else VO_SEC + TITLE_SEC
        span = nxt - a
        acc = a
        parts = SHOTS[i]
        for j, (ratio, src, mv, tr) in enumerate(parts):
            d = span * ratio
            # 最后一段兜住浮点误差，保证严丝合缝接到下一句
            if j == len(parts) - 1:
                d = nxt - acc
            out.append((i, acc, acc + d, d, src, mv, tr, line if j == 0 else ""))
            acc += d
    t = TITLE_SEC + VO_SEC
    out.append((99, t, t + END_SEC, END_SEC, "card:end", ("zoom", 1.06, 1.0, .5, .5),
                "diss", ""))
    return out


# 转场要吃掉时长：xfade 把两段重叠 d 秒拼起来，成片就短 d 秒。
# 所以每段素材都得比它在成片里的时长多做出「跟下一段转场」的那一截，
# 拼完总长才正好等于 sum(时长)。TRANS 就是各类转场的重叠秒数。
TRANS = {"cut": 0.06, "none": 0.10, "diss": 0.30, "white": 0.26, "slide": 0.34}


def need_len(tl, i):
    """第 i 个镜头要准备多长的素材：自己的时长 + 跟下一镜转场的重叠。"""
    d = tl[i][3]
    if i + 1 < len(tl):
        d += TRANS.get(tl[i + 1][6], 0.10)
    return round(d, 3)


TOTAL = TITLE_SEC + VO_SEC + END_SEC

if __name__ == "__main__":
    tl = timeline()
    print("总长 %.2fs ｜ %d 个镜头" % (TOTAL, len(tl)))
    for n, a, b, d, src, mv, tr, line in tl:
        print("#%-3s %6.2f-%6.2f %5.2fs  %-18s %-6s %s"
              % (n, a, b, d, src, tr, line))
