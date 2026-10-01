"""ONNX 和 torch 对不对得上 —— 导出可能悄悄出错，必须逐向量比。"""
import sys, time
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import clipmodel

ref = np.load(BASE / "models/.probe_vecs.npz", allow_pickle=True)
names = [str(n) for n in ref["names"]]
V_torch = ref["V"]

# 只比本地有文件的那些，避免再回源一次 OSS
local = [(i, n) for i, n in enumerate(names) if (BASE / "uploads" / n).exists()]
print("对比样本: %d 张（本地文件）" % len(local))

imgs = [Image.open(BASE / "uploads" / n) for _, n in local]
t0 = time.time()
V_onnx = clipmodel.encode_images(imgs)
dt = time.time() - t0
print("onnx 图像编码 %.2fs = %.0f ms/张" % (dt, dt / len(imgs) * 1000))

idx = [i for i, _ in local]
cos = (V_onnx * V_torch[idx]).sum(1)
print("图像向量 与 torch 的余弦: min=%.6f mean=%.6f" % (cos.min(), cos.mean()))

QS = ["雪山", "夜景灯光", "草地上的狗", "海边日落"]
t0 = time.time()
T = clipmodel.encode_texts(QS)
print("onnx 文本编码 %d 条 %.0f ms" % (len(QS), (time.time() - t0) * 1000))
print("文本向量范数:", np.round(np.linalg.norm(T, axis=1), 4))

# 端到端：用 onnx 两塔跑检索，看 top-3 和 torch 版报告一致不
import json
rep = json.loads((BASE / "models/.probe_report.json").read_text(encoding="utf-8"))
V_all = V_torch                      # 图像侧用 torch 全量（含 OSS 回源那批）
sim = T @ V_all.T
print()
ok = True
for qi, q in enumerate(QS):
    top = [names[j] for j in np.argsort(-sim[qi])[:3]]
    want = [x["file"] for x in rep[q][:3]]
    same = top == want
    ok &= same
    print("%-10s onnx: %s" % (q, top))
    print("%-10s torch: %s   %s" % ("", want, "一致" if same else "不一致"))

print("\n结论:", "ONNX 与 torch 等价" if cos.min() > 0.999 and ok else "有偏差，需排查")
