"""Jev 精判重排 —— CLIP 召回之后的第二道闸。

为什么要有这一层：Chinese-CLIP 的余弦只回答「像不像」，回答不了「该不该返回」。
搜「海边日落」时，半夜的霓虹海岸线在 CLIP 眼里和日落海岸线咬得很死，
靠调 min_score 分不开 —— 阈值是一维的，而「像海岸」和「是日落」是两件事。

Jev 是 System One 那一类模型：吃一个 state + 一组**有类型**的问题
（Noul 真假概率 / Score 分档 / Choice 选项），一次调用并行回答，返回校准概率，
不 decode 文本。所以它快、输出形状固定、代码可以直接拿来做 if。

默认打的是**自建的 KEV 部署**，不是 TypeSafe 官方云。两边是同一套
/v1/systemone 协议：请求体 {model, state, questions}、响应
{"answers": {问题名: {type, ...}}}，score/noul/choice 三种题型都实测过，
形状一字不差。所以换后端只换 URL 和 key，下面的问题构造、解析、融合
一个字都不用动。选自建的理由是出网和延迟：实测 120~350ms 的直连。

但它看不见图 —— 官方写死 text only，不收 image/audio/video。所以这里喂给它的是
候选图的**文字描述**（见 describe()），不是图本身。这是整套方案的天花板：
描述有多准，精判就有多准。本仓库目前没有 VLM，描述只能由 caption + 地点 +
行程 + 文件名拼出来，caption 多半是用户自己写的，常常是空 —— 这一层的收益
因此高度依赖图库的元数据质量，别期待它能凭空看出「日落」。

关闭方式：不配 key（环境变量 TM_JEV_API_KEY，或仓库根下的 jev_key.txt）
就是关的，/api/search/photos 的行为和没有这个模块时完全一样。任何一次调用超时、报错、返回格式不对，都降级回纯 CLIP 排序 ——
搜索是用户正在等的，精判是锦上添花，不能反过来把搜索搞挂。
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("travel.jev")

# ---------------------------------------------------------------- 配置

# 三种后端：kev = 自建部署（默认）、typesafe = 官方云、cloudflare = Workers AI
# 转发。前两个的请求体和响应完全一样，只差 URL 和 key；cloudflare 那条请求体
# 不一样、响应还多包一层 result，见 _post()。
BACKEND = os.environ.get("TM_JEV_BACKEND", "kev").strip().lower()

# key 先看环境变量，再看 jev_key.txt —— 跟 amap_key.txt 一个套路：文件在
# .gitignore 里，公开仓库不带凭据，生产机上放一份就行，不用动 systemd unit。
API_KEY = os.environ.get("TM_JEV_API_KEY", "").strip()
if not API_KEY:
    _key_file = Path(__file__).resolve().parent / "jev_key.txt"
    if _key_file.exists():
        API_KEY = _key_file.read_text(encoding="utf-8").strip()

API_URL = os.environ.get("TM_JEV_API_URL", "").strip()
CF_ACCOUNT = os.environ.get("TM_JEV_CF_ACCOUNT_ID", "").strip()

# 自建那台不看 model 字段（固定回 kev-0），但还是按后端填对，免得哪天切回
# 官方云时带着一个对面不认识的模型名出门。
DEFAULT_MODELS = {"kev": "kev-0", "typesafe": "jev-latest"}
MODEL = (os.environ.get("TM_JEV_MODEL", "").strip()
         or DEFAULT_MODELS.get(BACKEND, "jev-latest"))

ENABLED = os.environ.get("TM_JEV_ENABLED", "1") != "0"

# 召回多少条送去精判。规格书给的是 200；200 次调用 × 70~500ms，20 路并发
# 下总延迟 2~5 秒，对一个搜索框来说是偏慢的。调到 50 能把延迟压进 1 秒内，
# 代价是排在 50 名以后的「CLIP 看走眼、Jev 本来能救回来」的图救不回来了。
RECALL = int(os.environ.get("TM_JEV_RECALL", "200"))
CONCURRENCY = int(os.environ.get("TM_JEV_CONCURRENCY", "20"))
TIMEOUT = float(os.environ.get("TM_JEV_TIMEOUT", "3.0"))
# 整轮精判的总预算。单次 3 秒 × 10 批最坏能拖到 30 秒，没有这道闸，
# 一次网络抽风就是用户干等半分钟。超时的候选按「没判过」处理，不丢。
BUDGET = float(os.environ.get("TM_JEV_BUDGET", "8.0"))

W_CLIP = float(os.environ.get("TM_JEV_W_CLIP", "0.35"))
W_RELEVANCE = float(os.environ.get("TM_JEV_W_RELEVANCE", "0.40"))
W_MATCH = float(os.environ.get("TM_JEV_W_MATCH", "0.20"))
W_CATEGORY = float(os.environ.get("TM_JEV_W_CATEGORY", "0.05"))
MATCH_THRESHOLD = float(os.environ.get("TM_JEV_MATCH_THRESHOLD", "0.30"))

# Score 的档位。criteria 数组长度决定返回值域：5 档 → score ∈ [0, 4]。
RELEVANCE_LEVELS = ["完全无关", "弱相关", "一般", "较相关", "非常匹配"]

# Choice 的场景分类。旅行相册的实际构成，不是规格书里那套
# 沙漠/海边/雪山草原 —— 那是按某一次 query 临时编的，换个搜索词就不适用了。
CATEGORIES = [
    "自然风光", "城市街景", "建筑古迹", "人物合影", "美食饮品",
    "动物植物", "交通工具", "室内场景", "文字截图", "其他",
]

DEFAULT_URLS = {
    # 自建部署。换机器/换端口用 TM_JEV_API_URL 覆盖，不用改代码。
    "kev": "http://kev.zzss.fun:10025/v1/systemone",
    "typesafe": "https://api.typesafe.ai/v1/systemone",
    "cloudflare": "https://api.cloudflare.com/client/v4/accounts/%s/ai/run",
}


def api_url() -> str:
    if API_URL:
        return API_URL
    if BACKEND == "cloudflare":
        return DEFAULT_URLS["cloudflare"] % CF_ACCOUNT
    return DEFAULT_URLS.get(BACKEND, DEFAULT_URLS["kev"])


def available() -> bool:
    """配齐了没。调用方据此决定是走精判还是直接返回 CLIP 的结果。"""
    if not ENABLED or not API_KEY:
        return False
    if BACKEND == "cloudflare" and not (CF_ACCOUNT or API_URL):
        return False
    return True


# ---------------------------------------------------------------- 候选图 → 文字

def describe(meta: dict) -> str:
    """把一条媒体的元数据拼成一句能让 Jev 读的中文描述。

    顺序是有讲究的：caption 是人写的，信息密度最高，放最前面；文件名放最后，
    因为 IMG_2317.jpg 这种纯噪声不该污染前面的语义。全空的时候返回空串，
    调用方据此跳过这张图的精判（而不是拿空字符串去问 Jev，那是在烧钱买噪声）。

    注意这里拼出来的是「关于照片的已知信息」，不是「照片的内容」。
    没有 VLM caption 的图，Jev 只能基于地点和行程名推断，判不出画面细节。
    """
    parts = []
    for key in ("caption", "point_name", "trip_title"):
        v = (meta.get(key) or "").strip()
        if v and v not in parts:
            parts.append(v)
    t = (meta.get("exif_time") or "").strip()
    if t:
        parts.append("拍摄于 %s" % t)
    fn = (meta.get("filename") or "").strip()
    # 文件名只在它看起来像人起的名字时才有信息量。IMG_0001/DSC_0001/
    # 纯数字/纯 uuid 这些一律丢掉 —— 它们只会让 Jev 以为图里有数字。
    if fn and not _is_noise_filename(fn):
        parts.append("文件名 %s" % fn)
    return "，".join(parts)


def _is_noise_filename(fn: str) -> bool:
    stem = fn.rsplit(".", 1)[0]
    low = stem.lower()
    if len(stem) >= 24:                       # uuid / 哈希名
        return True
    for p in ("img_", "dsc_", "dscf", "p", "image", "photo", "wx", "mmexport"):
        if low.startswith(p) and low[len(p):].strip("_-").isdigit():
            return True
    return low.isdigit()


# ---------------------------------------------------------------- 问题构造

def _candidate_questions(query: str) -> dict:
    """一张候选图问三件事，一次调用并行回答（不是三次调用）。"""
    return {
        "relevance": {
            "type": "score",
            "instructions": {
                "query": query,
                "question": "照片描述与用户搜索词 `query` 的相关程度有多高？",
            },
            "criteria": RELEVANCE_LEVELS,
        },
        "is_match": {
            "type": "noul",
            "instructions": {
                "query": query,
                "question": "这张照片是否应该作为 `query` 的搜索结果返回给用户？",
            },
            "criteria": {
                "true": "用户看到这张照片会认为「对，这就是我要找的」",
                "false": "用户会认为这张照片搜错了、跟我要找的不是一回事",
            },
        },
        "category": {
            "type": "choice",
            "instructions": "这张照片最可能属于哪一类？",
            "criteria": {c: None for c in CATEGORIES},
        },
    }


def _intent_questions(query: str) -> dict:
    """对 query 本身问一次，拿到用户想要的类别。

    规格书里 `cat_bonus` 比的是 `parsed_query_intent`，但那个变量从头到尾
    没有定义过 —— 照抄过来就是 NameError。这里把它补上：每次搜索额外一次
    Jev 调用（N+1 而不是 2N），把搜索词自己也分到同一套 CATEGORIES 里，
    候选图的 category 和它一致才给加分。
    """
    return {
        "category": {
            "type": "choice",
            "instructions": {
                "query": query,
                "question": "用户用 `query` 搜照片时，最可能想找哪一类照片？",
            },
            "criteria": {c: None for c in CATEGORIES},
        },
    }


# ---------------------------------------------------------------- HTTP

_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            import httpx
            # 连接池上限跟并发对齐，否则 20 路并发会在默认的 10 条连接上排队，
            # 看起来像「Jev 变慢了」，其实是自己把自己堵住了。
            _CLIENT = httpx.Client(
                timeout=TIMEOUT,
                limits=httpx.Limits(max_connections=max(CONCURRENCY, 10),
                                    max_keepalive_connections=max(CONCURRENCY, 10)),
            )
        return _CLIENT


def _post(state, questions: dict) -> dict:
    """一次 Jev 调用，返回 answers 字典。失败一律抛异常，由调用方降级。"""
    if BACKEND == "cloudflare":
        body = {"model": "typesafe/jev", "input": {"state": state, "questions": questions}}
    else:
        body = {"model": MODEL, "state": state, "questions": questions}

    r = _client().post(api_url(),
                       headers={"Authorization": "Bearer %s" % API_KEY,
                                "Content-Type": "application/json"},
                       json=body)
    r.raise_for_status()
    data = r.json()
    # Workers AI 把真正的响应包在 result 里，直连不包。
    if BACKEND == "cloudflare":
        data = data.get("result") or data
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Jev 返回里没有 answers: %s" % json.dumps(data)[:200])
    return answers


# ---------------------------------------------------------------- 融合

def _norm_clip(scores):
    """把 CLIP 余弦在候选集内部做 min-max 归一化。

    不能直接拿余弦当 0~1 用：Chinese-CLIP 的图文余弦实际落在 0.1~0.35 这个
    窄区间里，直接乘 0.35 的权重等于让 CLIP 这一路只贡献 0.03~0.12 的分差，
    还不如 category 那 0.05 的 bonus 有话语权 —— 融合权重就形同虚设了。
    候选集内归一化之后，权重表里的数字才真的是它写的意思。
    """
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [0.5] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def fuse(clip_norm: float, answers, intent_category=None) -> dict:
    """算最终分。answers 为 None（这张图没判成）时只用 CLIP 分，不惩罚。

    「不惩罚」是刻意的：一张图因为网络抽风没判上，不该因此排到判过的图后面，
    否则一次丢包就会把搜索结果洗一遍。
    """
    if not answers:
        return {"final": clip_norm, "clip_norm": clip_norm, "judged": False}

    rel = _answer_score(answers.get("relevance"), len(RELEVANCE_LEVELS))
    match = _answer_noul(answers.get("is_match"))
    cat = _answer_choice(answers.get("category"))

    if rel is None or match is None:
        # 形状不对 == 没判过。半套答案比没有答案更危险，不要拿来排序。
        return {"final": clip_norm, "clip_norm": clip_norm, "judged": False}

    cat_hit = bool(intent_category and cat == intent_category)
    final = (W_CLIP * clip_norm + W_RELEVANCE * rel +
             W_MATCH * match + W_CATEGORY * (1.0 if cat_hit else 0.0))
    return {"final": final, "clip_norm": clip_norm, "judged": True,
            "relevance": rel, "is_match": match,
            "category": cat, "category_hit": cat_hit}


def _answer_score(a, levels):
    """Score 的返回值域是 [0, len(criteria)-1]，归一化到 0~1。"""
    if not isinstance(a, dict) or not isinstance(a.get("score"), (int, float)):
        return None
    span = max(levels - 1, 1)
    return min(max(float(a["score"]) / span, 0.0), 1.0)


def _answer_noul(a):
    if not isinstance(a, dict) or not isinstance(a.get("noul"), (int, float)):
        return None
    return min(max(float(a["noul"]), 0.0), 1.0)


def _answer_choice(a):
    if not isinstance(a, dict):
        return None
    c = a.get("choice")
    return c if isinstance(c, str) else None


# ---------------------------------------------------------------- 主入口

def rerank(query: str, hits: list, meta: dict, limit: int = 20) -> dict:
    """对 CLIP 召回的 hits 做精判重排。

    hits: aiindex.search() 的返回，[{media_id, filename, score}]，已按 CLIP 降序
    meta: db.get_media_brief() 的返回，{media_id: {...}}
    返回 {"results": [...], "reranked": bool, "judged": n, "reason": str|None}

    **任何情况下都返回一个可用的列表。** 精判没跑成就原样返回 CLIP 的顺序，
    并在 reason 里说明为什么没跑成 —— 前端据此决定要不要提示，而不是看到
    一个空结果去猜。
    """
    if not hits:
        return {"results": [], "reranked": False, "judged": 0, "reason": "no_candidates"}
    if not available():
        return {"results": hits[:limit], "reranked": False, "judged": 0,
                "reason": "jev_disabled"}

    cands = hits[:RECALL]
    clip_norm = _norm_clip([h["score"] for h in cands])

    descs = [describe(meta.get(h["media_id"], {})) for h in cands]
    todo = [i for i, d in enumerate(descs) if d]
    if not todo:
        # 整批都没有任何可读的元数据。这不是故障，是这个图库还没写 caption，
        # 硬送去问 Jev 只会得到一堆 0.5。
        return {"results": hits[:limit], "reranked": False, "judged": 0,
                "reason": "no_descriptions"}

    intent = _intent(query)
    answers = _judge_all(query, [(i, descs[i]) for i in todo])

    scored = []
    for i, h in enumerate(cands):
        f = fuse(clip_norm[i], answers.get(i), intent)
        if f["judged"] and f["is_match"] < MATCH_THRESHOLD:
            continue                      # Jev 明确说「不该返回」的，丢掉
        scored.append({**h, "clip_score": h["score"],
                       "score": round(f["final"], 4), "jev": f})

    judged = sum(1 for s in scored if s["jev"]["judged"])
    if judged == 0:
        return {"results": hits[:limit], "reranked": False, "judged": 0,
                "reason": "jev_unavailable"}

    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"results": scored[:limit], "reranked": True, "judged": judged,
            "intent": intent, "reason": None}


def _intent(query: str):
    """搜索词本身的类别。失败就返回 None —— 没有 bonus 而已，不影响主排序。"""
    try:
        a = _post({"query": query}, _intent_questions(query))
        return _answer_choice(a.get("category"))
    except Exception as e:                                # noqa: BLE001
        log.info("Jev 意图分类失败，跳过 category 加分: %s", e)
        return None


def _judge_all(query: str, items: list) -> dict:
    """并发判一批。返回 {原下标: answers}，判失败的下标直接不出现在结果里。"""
    questions = _candidate_questions(query)
    out = {}
    pool = ThreadPoolExecutor(max_workers=max(1, CONCURRENCY),
                              thread_name_prefix="jev")
    try:
        futs = {pool.submit(_post,
                            {"image_description": d, "query": query}, questions): i
                for i, d in items}
        fails = 0
        for fut in as_completed(futs, timeout=BUDGET):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception as e:                        # noqa: BLE001
                fails += 1
                if fails <= 3:                            # 200 张全挂时别刷 200 行日志
                    log.warning("Jev 精判失败 idx=%s: %s", i, e)
        if fails:
            log.warning("Jev 精判 %s/%s 失败，这些候选退回 CLIP 分", fails, len(futs))
    except TimeoutError:
        log.warning("Jev 精判超出总预算 %.1fs，已判 %s/%s，其余退回 CLIP 分",
                    BUDGET, len(out), len(items))
    except Exception as e:                                # noqa: BLE001
        log.warning("Jev 精判整体失败，全部退回 CLIP 分: %s", e)
    finally:
        # 不等还在飞的请求 —— 预算到了就是到了，用户在等。
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def stats() -> dict:
    return {"enabled": available(), "backend": BACKEND, "model": MODEL,
            "recall": RECALL, "concurrency": CONCURRENCY,
            "timeout": TIMEOUT, "budget": BUDGET,
            "weights": {"clip": W_CLIP, "relevance": W_RELEVANCE,
                        "is_match": W_MATCH, "category": W_CATEGORY},
            "match_threshold": MATCH_THRESHOLD}
