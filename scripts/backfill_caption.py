"""建库时预计算照片描述 —— 规格书「模块2 方案A」的离线 worker。

一句话：把图库里还没有画面描述的照片捞出来，用 Qwen2-VL-2B 生成一句中文描述，
写进 media_captions。搜索链路一行代码都不用动就能用上 —— jevrerank.describe()
会优先读它（见 caption.py 顶部）。

跑在 .venv-ai（torch + CUDA），和 export_clip_onnx.py 同一套分工：主 venv 只认
onnxruntime，torch 只在离线脚本里出现。2B 模型 fp16 约 4.4GB 显存，A4000 够用；
没有 GPU 会自动退到 CPU，慢十几倍但能跑完（3000 张大概要一夜）。

典型用法
--------
  .venv-ai/bin/python scripts/backfill_caption.py --limit 10 --dry-run   # 先看效果
  .venv-ai/bin/python scripts/backfill_caption.py                        # 排空积压
  .venv-ai/bin/python scripts/backfill_caption.py --watch                # 常驻，接新上传
  .venv-ai/bin/python scripts/backfill_caption.py --media-id 17 --dry-run # 调 prompt

--watch 是增量的办法：上传链路不插队列、不碰模型（web 进程塞个 2B 模型既占显存
又会让上传请求等在生成上），由这里每 --interval 秒扫一次差集。新图在上传后最多
一个 interval 就有描述，而上传本身快慢完全不受影响。

取图复用 aiindex.load_image：本地有读本地，没有就让 OSS 服务端缩好了再传回来。
缩的尺寸比 CLIP 那条大 —— 224 的图喂 VLM 只能看出「有个人」，说不出「背着红包
的人站在雪山前」，而描述的细粒度正是这一层的全部价值。
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import aiindex                                            # noqa: E402
import caption                                            # noqa: E402

log = logging.getLogger("backfill_caption")

MODEL_DIR = BASE / "models/qwen2-vl-2b-instruct"

# VLM 回源尺寸。448（CLIP 那条）太小，1280 又让生成慢一倍且描述开始啰嗦，
# 896 是实测下来「看得清主体和光线」与「一张一秒」的交点。
OSS_PROCESS = "image/resize,l_896"

# 提示词是这一层的真正旋钮，比模型本身更影响最终排序效果。三条约束各有来历：
#   只描述看得见的 —— 不加约束时模型爱猜地名（「这可能是巴黎」），猜错了
#                     就是往 Jev 嘴里塞假证据，比没有描述更坏；
#   不要开场白     —— 见 caption.clean()，每条都带同一段废话等于没信息；
#   限字数         —— 描述越长，和 query 相关的那几个字占比越低，判得越糊。
PROMPT = os.environ.get("TM_CAPTION_PROMPT") or (
    "用一句中文描述这张照片的画面：主体是什么、在什么场景、什么光线或天气。"
    "严禁写出任何地名、国名、建筑名、人名或年代 —— 你并不知道这是哪里，"
    "只描述眼睛能直接看到的景物。不要加「这张图片」之类的开场白，不超过40个字。")


def load_model(device: str = None):
    """加载 Qwen2-VL-2B。返回 (processor, model, device)。"""
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    if not MODEL_DIR.exists():
        raise SystemExit(
            "模型不在 %s。先下载：\n"
            "  .venv-ai/bin/python -c 'from modelscope import snapshot_download;"
            " snapshot_download(\"Qwen/Qwen2-VL-2B-Instruct\","
            " local_dir=\"models/qwen2-vl-2b-instruct\")'" % MODEL_DIR)

    dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
    # size 这两个 edge 的单位是**像素面积**不是边长（Qwen2VL 的 processor 就这么
    # 定义的），它决定视觉 token 数，也就是显存和速度。上限 768 个 patch ≈ 60 万
    # 像素，比默认的 1280 patch 省掉将近一半时间，描述质量肉眼看不出差别。
    # 旧写法是 min_pixels/max_pixels，transformers 5.23 起移除。
    proc = AutoProcessor.from_pretrained(
        str(MODEL_DIR), size={"shortest_edge": 256 * 28 * 28,
                              "longest_edge": 768 * 28 * 28})
    # 批量生成必须左 padding：右 padding 会让短样本的 pad 夹在 prompt 和生成之间，
    # 模型从一串 <pad> 后面开始续写，输出直接变成乱码。
    proc.tokenizer.padding_side = "left"

    def build(d):
        return AutoModelForImageTextToText.from_pretrained(
            str(MODEL_DIR),
            dtype=torch.float16 if d == "cuda" else torch.float32).to(d).eval()

    try:
        model = build(dev)
    except torch.OutOfMemoryError:
        # 这块卡是和别人共用的（实测 16GB 被别的进程占掉 13.5GB）。显存不够时
        # 退到 CPU 继续跑，而不是整个回填任务直接失败 —— 它是离线任务，慢十倍
        # 也比「等显卡空出来」强，何况下次再跑还是碰运气。要强制用卡就 --device cuda。
        if device:                                        # 用户指名了卡，就别替他改主意
            raise
        log.warning("显存不足，退到 CPU 跑（慢但不阻塞）")
        torch.cuda.empty_cache()
        dev = "cpu"
        model = build(dev)
    log.info("模型就绪 %s on %s", MODEL_DIR.name, dev)
    return proc, model, dev


def describe_images(proc, model, dev, images) -> list:
    """一批 PIL 图 -> 一批原始描述文本（未清洗）。"""
    import torch

    msgs = [{"role": "user",
             "content": [{"type": "image"}, {"type": "text", "text": PROMPT}]}]
    prompt = proc.apply_chat_template(msgs, add_generation_prompt=True,
                                      tokenize=False)
    inputs = proc(text=[prompt] * len(images), images=list(images),
                  padding=True, return_tensors="pt").to(dev)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=96, do_sample=False)
    # 把 prompt 那段切掉，只留生成的部分。左 padding 下每行前缀等长，直接按长度切。
    gen = out[:, inputs["input_ids"].shape[1]:]
    return proc.batch_decode(gen, skip_special_tokens=True)


def run_once(proc, model, dev, args) -> int:
    """排空一轮 backlog，返回这轮成功写入的条数。"""
    todo = caption.backlog(limit=args.limit, retry_failed=args.retry_failed,
                           remodel=args.remodel, media_ids=args.media_id)
    if not todo:
        return 0
    log.info("本轮待处理 %d 张", len(todo))

    done = 0
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        imgs, keep = [], []
        for item in chunk:
            try:
                img, nbytes = aiindex.load_image(item["filename"],
                                                 process=OSS_PROCESS)
                imgs.append(img)
                keep.append((item, nbytes))
            except Exception as e:                        # noqa: BLE001
                # 取不到图是数据问题不是模型问题，记 failed 直接跳过 ——
                # 一张坏图不该让整批生成挂掉。
                log.warning("取图失败 media=%s %s: %s",
                            item["media_id"], item["filename"], e)
                if not args.dry_run:
                    caption.fail(item["media_id"], "取图失败: %s" % e)
        if not imgs:
            continue

        t0 = time.time()
        try:
            texts = describe_images(proc, model, dev, imgs)
        except Exception as e:                            # noqa: BLE001
            log.error("生成失败（整批退回）: %s", e)
            if not args.dry_run:
                for item, _ in keep:
                    caption.fail(item["media_id"], "生成失败: %s" % e)
            continue
        dt = time.time() - t0

        for (item, nbytes), raw in zip(keep, texts):
            text = caption.clean(raw)
            if args.dry_run:
                print("[%s] %s\n    %s" % (item["media_id"], item["filename"],
                                           text or "(空)"))
                continue
            if caption.save(item["media_id"], raw):
                done += 1
                log.info("media=%s %s (回源 %dB) -> %s",
                         item["media_id"], item["filename"], nbytes, text)
        log.info("批 %d/%d 用时 %.1fs（%.2fs/张）",
                 i // args.batch + 1, (len(todo) - 1) // args.batch + 1,
                 dt, dt / max(len(imgs), 1))
    return done


def main():
    ap = argparse.ArgumentParser(description="建库时预计算照片画面描述（方案A）")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多处理几张")
    ap.add_argument("--batch", type=int, default=4, help="一次喂几张给模型")
    ap.add_argument("--watch", action="store_true",
                    help="常驻，排空后继续扫新上传的照片")
    ap.add_argument("--interval", type=int, default=30,
                    help="--watch 下的轮询间隔（秒）")
    ap.add_argument("--retry-failed", action="store_true", help="连 failed 一起重算")
    ap.add_argument("--remodel", action="store_true",
                    help="重算由别的模型生成的描述")
    ap.add_argument("--media-id", type=int, nargs="+", default=None,
                    help="只处理这几张（忽略其它过滤条件，调 prompt 用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--device", default=None, help="cuda / cpu，默认自动")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # 没活干就别加载模型 —— 一次性跑完的场景下这能省掉 20 秒的冷启动，
    # watch 模式下则相反：先加载好，免得新图上来还要等模型。
    if not args.watch and not caption.backlog(limit=1, media_ids=args.media_id,
                                              retry_failed=args.retry_failed,
                                              remodel=args.remodel):
        print("没有待生成的照片。")
        return

    proc, model, dev = load_model(args.device)
    total = run_once(proc, model, dev, args)
    print("本轮写入 %d 条描述。" % total)

    while args.watch:
        time.sleep(args.interval)
        args.media_id = None            # 指名道姓那批只在第一轮算，否则会无限重算
        n = run_once(proc, model, dev, args)
        if n:
            print("增量写入 %d 条描述。" % n)


if __name__ == "__main__":
    main()
