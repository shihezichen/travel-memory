"""Chinese-CLIP 召回质量验证 —— 铺开工程之前的唯一一道闸。

只做一件事：把 media 表里的照片全部编码，跑一组中文 query，把 top-k 打出来。
不写库、不改 app 的任何行为。验证不过，后面的 aiindex.py 一行都不该写。

取图策略（这是给 3000+ 张准备的，不是给现在 72 张）：
  本地 uploads 有就读本地；没有就走 oss.sign_url 带 x-oss-process=image/resize
  让 OSS 服务端缩到 224 再传回来。实测单张原图 1.07MB、缩到 224 只有 5.8KB，
  183 倍差距 —— 全量回填 3000 张时这是 3.2GB 和 17MB 的区别。
"""
import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import AutoTokenizer, ChineseCLIPImageProcessor, ChineseCLIPModel

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

MODEL = BASE / "models/models/AI-ModelScope--chinese-clip-vit-base-patch16/snapshots/master"
UPLOADS = BASE / "uploads"
CACHE = BASE / "models/.probe_vecs.npz"

# CLIP 输入是 224。取 448 留一倍余量给 resize 的插值质量，代价仍只有十几 KB。
OSS_PROCESS = "image/resize,l_448"

QUERIES = [
    "海边日落", "草地上的狗", "红色汽车", "教堂", "夜景灯光",
    "美食", "雪山", "人物合影", "街道建筑", "蓝天白云",
]


def _features(model, kind, **kw):
    """transformers 5.x 的 get_*_features 返回的是 ModelOutput 而非 tensor，
    不同小版本还不一样。统一走底层 model + projection，行为和官方实现一致。"""
    if kind == "image":
        out = model.vision_model(**kw)
        pooled = out[1] if not isinstance(out, dict) else out["pooler_output"]
        return model.visual_projection(pooled)
    out = model.text_model(**kw)
    seq = out[0] if not isinstance(out, dict) else out["last_hidden_state"]
    return model.text_projection(seq[:, 0, :])


def load_model():
    t0 = time.time()
    model = ChineseCLIPModel.from_pretrained(MODEL, dtype=torch.float32).eval()
    proc = ChineseCLIPImageProcessor.from_pretrained(MODEL)
    # 必须走 AutoTokenizer.from_pretrained(目录)：transformers 5.x 下
    # BertTokenizer(vocab_file=...) 和它的 Fast 版都会把中文整串切成 [UNK]，
    # 不报错、不告警，只是检索结果每个 query 都长一个样。这个坑踩一次就够了。
    tok = AutoTokenizer.from_pretrained(MODEL)
    print("[load] %.1fs" % (time.time() - t0), flush=True)
    return model, proc, tok


def list_photos():
    """media 表全部 photo。本地有就标 local，没有就标 oss —— 两边都要能索引。"""
    con = sqlite3.connect(BASE / "data.db")
    rows = con.execute(
        "select id, filename from media where type = ? order by id", ("photo",)
    ).fetchall()
    con.close()
    out = []
    for mid, fn in rows:
        p = UPLOADS / fn
        out.append((mid, fn, p if p.exists() else None))
    return out


def open_image(fn, local):
    """本地优先；回源 OSS 时让服务端先缩图，别把原图拖回来。"""
    if local is not None:
        return Image.open(local).convert("RGB"), 0
    import httpx
    import oss
    url = oss.sign_url("GET", "uploads/" + fn,
                       params={"x-oss-process": OSS_PROCESS}, expires_in=600)
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB"), len(r.content)


def encode_images(model, proc, photos, batch=16):
    vecs, ok, miss, oss_bytes = [], [], 0, 0
    t0 = time.time()
    for i in range(0, len(photos), batch):
        imgs = []
        for mid, fn, local in photos[i:i + batch]:
            try:
                im, nb = open_image(fn, local)
            except Exception as e:                       # noqa: BLE001
                print("  [skip] %s: %s" % (fn, e), flush=True)
                miss += 1
                continue
            imgs.append(im)
            ok.append((mid, fn, "local" if local else "oss"))
            oss_bytes += nb
        if not imgs:
            continue
        px = proc(images=imgs, return_tensors="pt")["pixel_values"]
        with torch.no_grad():
            f = _features(model, "image", pixel_values=px)
        f = f / f.norm(dim=-1, keepdim=True)
        vecs.append(f.numpy().astype("float32"))
        print("  [img] %d/%d" % (min(i + batch, len(photos)), len(photos)), flush=True)

    V = np.concatenate(vecs, 0)
    dt = time.time() - t0
    n_oss = sum(1 for _, _, s in ok if s == "oss")
    print("[encode] %d 张成功 / %d 失败  %.1fs = %.0fms/张"
          % (len(ok), miss, dt, dt / max(len(ok), 1) * 1000), flush=True)
    print("[oss] 回源 %d 张，共 %.1f KB（均 %.1f KB/张）"
          % (n_oss, oss_bytes / 1024, oss_bytes / 1024 / max(n_oss, 1)), flush=True)
    return V, ok


def encode_texts(model, tok, texts):
    enc = tok(texts, padding=True, truncation=True, max_length=52, return_tensors="pt")
    with torch.no_grad():
        f = _features(model, "text", **enc)
    return (f / f.norm(dim=-1, keepdim=True)).numpy().astype("float32")


def main():
    model, proc, tok = load_model()
    photos = list_photos()
    n_local = sum(1 for _, _, p in photos if p)
    print("[data] media 表 photo %d 张（本地 %d / 需回源 %d）"
          % (len(photos), n_local, len(photos) - n_local), flush=True)
    if not photos:
        sys.exit("没有可用照片")

    V, ok = encode_images(model, proc, photos)
    np.savez(CACHE, V=V, names=np.array([fn for _, fn, _ in ok]))

    T = encode_texts(model, tok, QUERIES)
    sim = T @ V.T

    report = {}
    for qi, q in enumerate(QUERIES):
        order = np.argsort(-sim[qi])[:5]
        report[q] = [{"file": ok[j][1], "score": round(float(sim[qi][j]), 4)} for j in order]
        print("\n### %s" % q)
        for t in report[q]:
            print("   %.4f  %s" % (t["score"], t["file"]))

    (BASE / "models/.probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[done] 报告写入 models/.probe_report.json")


if __name__ == "__main__":
    main()
