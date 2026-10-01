"""量一下建库 caption 到底给精判带来多少增量 —— 同一批召回跑两遍 Jev。

为什么需要这个脚本：方案A 的全部价值都压在「Jev 看到的那句话」上，而这句话
变好了多少、排序因此动了多少，光看代码和日志是看不出来的。这里做的是**同输入
对照**：一次 CLIP 召回，两份描述（带 ai_caption / 砍掉 ai_caption，也就是加这
一层之前的世界），各送一次精判，比两边的 top-K。

CLIP 那一侧完全不动，所以两边的差异只可能来自描述 —— 这是这个对照唯一的意义。

用法：
    python scripts/eval_caption_gain.py                       # 默认一组查询词
    python scripts/eval_caption_gain.py -q 雪山 湖泊 -k 5     # 自己挑词
    python scripts/eval_caption_gain.py --json out.json       # 留档

要钱的地方：每个查询词跑两遍精判，每遍 N+1 次 Jev 调用（N = 候选数，见 --recall）。

**这个脚本自己给自己限速**（--rate，默认 28 次/分）。实测自建 KEV 那台是
每分钟 30 次的配额：超了就整分钟 429，retry-after 要等将近一分钟。线上搜索
碰到这个会静默退回 CLIP 排序（jevrerank 的既定降级路径），对照跑要是也那样，
量出来的就只是「两边都没判成」—— 所以这里宁可慢，也要让每一次调用真的落地。
限速是在 jevrerank._post 外面包一层做的，不改线上代码的行为。
"""
import argparse
import json
import threading
import time
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import aiindex                                            # noqa: E402
import db                                                 # noqa: E402
import jevrerank                                          # noqa: E402

# 这组词是按「只有看得见画面才答得上来」挑的：地点名和行程名答不了「下雪」
# 「夕阳」，用户自己写的 caption 也多半不会正好提到。换数据集时该一起换。
DEFAULT_QUERIES = ["雪山", "湖泊", "森林", "夕阳", "下雪", "公路"]


def throttle(per_min: int):
    """给 jevrerank._post 套一层节流，并把并发压到 1。

    为什么是「包一层」而不是加个参数：线上那条路不该为了一个离线对照脚本多出
    一个旋钮。配额是这台自建 KEV 的部署属性，不是 jevrerank 的业务逻辑。
    """
    gap = 60.0 / max(1, per_min)
    orig = jevrerank._post
    lock, last = threading.Lock(), [0.0]

    def paced(*a, **kw):
        with lock:                         # 串行化：配额是按「每分钟多少次」算的
            wait = last[0] + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.time()
        return orig(*a, **kw)

    jevrerank._post = paced
    jevrerank.CONCURRENCY = 1
    # 线上 8 秒的总预算是给「用户正在等」定的；这里是离线对照，等得起。
    jevrerank.BUDGET = 3600.0


def pick_user() -> int:
    """照片最多的那个用户。对照跑在谁的库上不重要，有图就行。"""
    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT t.user_id AS uid, COUNT(*) AS c
                 FROM media m
                 JOIN points p ON p.id = m.point_id
                 JOIN trips  t ON t.id = p.trip_id
                WHERE m.type = 'photo'
                GROUP BY t.user_id ORDER BY c DESC LIMIT 1""").fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("库里一张照片都没有，没得比。")
    return row["uid"]


def strip_ai(meta: dict) -> dict:
    """把 ai_caption 抹掉 —— 这就是加这一层之前 describe() 能看到的东西。"""
    return {k: {kk: vv for kk, vv in v.items() if kk != "ai_caption"}
            for k, v in meta.items()}


def describable(hits, meta) -> int:
    return sum(1 for h in hits if jevrerank.describe(meta.get(h["media_id"], {})))


def top_ids(res, k):
    return [r["media_id"] for r in res["results"][:k]]


def mean_rel(res, k):
    """top-K 的 Jev 相关度均值。没判过的条目不计入（它们没有这个数）。"""
    vals = [r["jev"]["relevance"] for r in res.get("results", [])[:k]
            if r.get("jev", {}).get("judged")
            and r["jev"].get("relevance") is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def show(tag, res, meta, k):
    print("  [%s] reranked=%s judged=%s reason=%s 相关度均值=%s"
          % (tag, res["reranked"], res["judged"], res["reason"], mean_rel(res, k)))
    for i, r in enumerate(res["results"][:k], 1):
        m = meta.get(r["media_id"], {})
        jv = r.get("jev") or {}
        print("    %d. #%-5s %-18s rel=%-5s %s"
              % (i, r["media_id"], (m.get("filename") or "")[:18],
                 jv.get("relevance"), (m.get("ai_caption") or m.get("caption")
                                       or "")[:34]))


def main():
    ap = argparse.ArgumentParser(description="caption 给 Jev 精判带来多少增量")
    ap.add_argument("-q", "--queries", nargs="+", default=DEFAULT_QUERIES)
    ap.add_argument("-k", type=int, default=5, help="比较 top 几")
    ap.add_argument("--recall", type=int, default=12,
                    help="召回多少条候选送去精判（每条一次调用，受配额限制）")
    ap.add_argument("--rate", type=int, default=28, help="每分钟最多几次 Jev 调用")
    ap.add_argument("--user", type=int, default=None)
    ap.add_argument("--json", default=None, help="把结果写成 JSON 留档")
    args = ap.parse_args()

    if not aiindex.available():
        raise SystemExit("CLIP 索引不可用，先把 backfill_clip.py 跑完。")
    if not jevrerank.available():
        raise SystemExit("Jev 没配 key，对照无从跑起（见 jevrerank.py 顶部）。")

    uid = args.user or pick_user()
    recall = args.recall or jevrerank.RECALL
    throttle(args.rate)
    print("用户 %s｜召回 %s｜限速 %s 次/分｜后端 %s"
          % (uid, recall, args.rate, jevrerank.api_url()))
    print("预计 %d 次调用，约 %.1f 分钟"
          % (2 * (recall + 1) * len(args.queries),
             2 * (recall + 1) * len(args.queries) / args.rate))

    report = []
    for q in args.queries:
        hits = aiindex.search(uid, q, limit=recall)
        if not hits:
            print("\n== %s ==  CLIP 一条都没召回，跳过" % q)
            continue
        meta_with = db.get_media_brief([h["media_id"] for h in hits])
        meta_without = strip_ai(meta_with)

        print("\n== %s ==  候选 %d 张，可描述 %d（有 caption）/ %d（无 caption）"
              % (q, len(hits), describable(hits, meta_with),
                 describable(hits, meta_without)))

        # 先跑「没有」那一侧：两侧用的是同一个 hits，顺序不影响结果，
        # 但先跑基线能让中断时手里至少有一半数据。
        res_b = jevrerank.rerank(q, hits, meta_without, limit=args.k)
        res_a = jevrerank.rerank(q, hits, meta_with, limit=args.k)
        show("无 caption", res_b, meta_without, args.k)
        show("有 caption", res_a, meta_with, args.k)

        a, b = top_ids(res_a, args.k), top_ids(res_b, args.k)
        same = len(set(a) & set(b))
        print("  top%d 重合 %d/%d，换掉 %d 张" % (args.k, same, len(a), len(a) - same))
        report.append({"query": q, "candidates": len(hits),
                       "describable_with": describable(hits, meta_with),
                       "describable_without": describable(hits, meta_without),
                       "with": {"reason": res_a["reason"], "judged": res_a["judged"],
                                "top": a, "mean_rel": mean_rel(res_a, args.k)},
                       "without": {"reason": res_b["reason"], "judged": res_b["judged"],
                                   "top": b, "mean_rel": mean_rel(res_b, args.k)},
                       "overlap": same})

    # 汇总那两行才是结论，上面一屏屏的明细是给人核对用的。
    moved = sum(len(r["with"]["top"]) - r["overlap"] for r in report)
    total = sum(len(r["with"]["top"]) for r in report)
    rel_a = [r["with"]["mean_rel"] for r in report if r["with"]["mean_rel"] is not None]
    rel_b = [r["without"]["mean_rel"] for r in report
             if r["without"]["mean_rel"] is not None]
    print("\n==== 汇总 ====")
    print("top%d 位次被换掉 %d/%d" % (args.k, moved, total))
    if rel_a and rel_b:
        print("相关度均值：无 caption %.3f -> 有 caption %.3f"
              % (sum(rel_b) / len(rel_b), sum(rel_a) / len(rel_a)))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("明细已写入 %s" % args.json)


if __name__ == "__main__":
    main()
