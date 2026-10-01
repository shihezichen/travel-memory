"""稳态吞吐 —— 决定 3000 张回填要跑多久、搜索要等多久。"""
import sys, time
from pathlib import Path
import numpy as np
from PIL import Image
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import clipmodel

imgs = [Image.open(p).convert("RGB")
        for p in sorted((BASE / "uploads").glob("*.jpg"))[:16]]
print("样本 %d 张" % len(imgs))

clipmodel.encode_images(imgs[:1])            # 预热，把加载开销摊掉
for bs in (1, 8, 16):
    batch = (imgs * 3)[:bs]
    t0 = time.time()
    for _ in range(3):
        clipmodel.encode_images(batch)
    dt = (time.time() - t0) / 3
    print("batch=%-3d %6.0f ms  =>  %5.0f ms/张  =>  3000 张需 %4.1f 分钟"
          % (bs, dt * 1000, dt / bs * 1000, dt / bs * 3000 / 60))

clipmodel.encode_texts(["预热"])
t0 = time.time()
for _ in range(10):
    clipmodel.encode_texts(["海边日落"])
print("\n文本编码（单条，搜索时的延迟）: %.0f ms" % ((time.time() - t0) / 10 * 1000))

for n in (3000, 10000, 100000):
    V = np.random.randn(n, 512).astype("float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    q = V[:1]
    t0 = time.time()
    for _ in range(20):
        np.argsort(-(q @ V.T)[0])[:30]
    print("向量检索 %6d 张: %5.1f ms/次   内存 %5.1f MB"
          % (n, (time.time() - t0) / 20 * 1000, V.nbytes / 1e6))
