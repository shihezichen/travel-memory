"""Chinese-CLIP 推理 —— 生产侧唯一的模型入口，只认 onnxruntime，不碰 torch。

torch 只在 scripts/export_clip_onnx.py 里出现过一次（装在独立的 .venv-ai），
主环境为此多出来的依赖只有 onnxruntime 和 tokenizers 两个。这是刻意的：
主 requirements.txt 的 25 个包全是精确 pin 的，显然在意安装体积和可复现。

两座塔分开加载：文本塔 410MB 只有搜索时才需要，图像塔 346MB 只有索引时才需要。
按需懒加载，一台只跑回填的机器不用把文本塔也读进内存。
"""
import logging
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("travel.clip")

MODEL_DIR = Path(os.environ.get(
    "TRAVEL_CLIP_DIR",
    str(Path(__file__).resolve().parent / "models/chinese-clip-vit-b16")))

DIM = 512
CONTEXT_LEN = 52
IMAGE_SIZE = 224

# ChineseCLIPImageProcessor 的常量，抄自 preprocessor_config.json。
# 写死而不是读 json：这三组数字一旦和导出时不一致，结果不会报错，只会悄悄变差。
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32).reshape(3, 1, 1)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32).reshape(3, 1, 1)

# onnxruntime 默认会把所有核都吃掉。这台机器同时在跑 ffmpeg 转码，
# 抢光 CPU 的后果是用户正在等的转码变慢 —— 后台索引没有这个优先级。
THREADS = int(os.environ.get("TRAVEL_CLIP_THREADS", "4"))

_LOCK = threading.Lock()
_VISION = None
_TEXT = None
_TOKENIZER = None


class ClipUnavailable(RuntimeError):
    """模型文件缺失或 onnxruntime 没装。调用方应当降级到关键词检索，而不是报错。"""


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return (MODEL_DIR / "vision.onnx").exists() and (MODEL_DIR / "text.onnx").exists()


def _session(name):
    import onnxruntime as ort
    path = MODEL_DIR / name
    if not path.exists():
        raise ClipUnavailable("模型文件不存在: %s" % path)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = THREADS
    opts.inter_op_num_threads = 1
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    log.info("加载 %s (threads=%d)", name, THREADS)
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def vision():
    global _VISION
    with _LOCK:
        if _VISION is None:
            _VISION = _session("vision.onnx")
        return _VISION


def text():
    global _TEXT
    with _LOCK:
        if _TEXT is None:
            _TEXT = _session("text.onnx")
        return _TEXT


def tokenizer():
    global _TOKENIZER
    with _LOCK:
        if _TOKENIZER is None:
            from tokenizers import Tokenizer
            tk = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
            tk.enable_padding(length=CONTEXT_LEN, pad_id=0, pad_token="[PAD]")
            tk.enable_truncation(max_length=CONTEXT_LEN)
            _TOKENIZER = tk
        return _TOKENIZER


def preprocess(img: Image.Image) -> np.ndarray:
    """和 ChineseCLIPImageProcessor 等价：双三次缩到 224×224（不裁剪）、归一化。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.BICUBIC)
    a = np.asarray(img, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return (a - _MEAN) / _STD


def encode_images(images) -> np.ndarray:
    """PIL 图片列表 -> (n, 512) 已 L2 归一化的 float32。归一化在 ONNX 图里做掉了。"""
    if not images:
        return np.zeros((0, DIM), dtype=np.float32)
    px = np.stack([preprocess(im) for im in images]).astype(np.float32)
    return vision().run(None, {"pixel_values": px})[0].astype(np.float32)


def encode_texts(texts) -> np.ndarray:
    """文本列表 -> (n, 512) 已 L2 归一化的 float32。"""
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    encs = tokenizer().encode_batch(list(texts))
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    types = np.array([e.type_ids for e in encs], dtype=np.int64)
    return text().run(None, {"input_ids": ids, "attention_mask": mask,
                             "token_type_ids": types})[0].astype(np.float32)


def pack(vec: np.ndarray) -> bytes:
    """向量 -> 存进 SQLite BLOB 的字节。float32 原样，512 维正好 2KB。"""
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
