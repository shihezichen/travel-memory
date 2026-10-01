"""把 Chinese-CLIP 的两座塔分别导成 ONNX —— 一次性动作，之后生产侧不再需要 torch。

为什么要分开导：图像塔每张照片跑一次（上传时、回填时），文本塔每次搜索跑一次，
两者调用频率差几个数量级，合成一个图会强迫调用方每次都喂满两路输入。

导出用 .venv-ai（有 torch），运行用主 .venv（只加 onnxruntime）。
"""
from pathlib import Path

import torch
from transformers import AutoTokenizer, ChineseCLIPModel

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "models/models/AI-ModelScope--chinese-clip-vit-base-patch16/snapshots/master"
OUT = BASE / "models/chinese-clip-vit-b16"
OUT.mkdir(parents=True, exist_ok=True)

model = ChineseCLIPModel.from_pretrained(SRC, dtype=torch.float32).eval()


class VisionTower(torch.nn.Module):
    """pixel_values -> 512 维已 L2 归一化的向量。归一化放进图里，
    省得每个调用方自己记得做——忘了做就是检索结果全错且不报错。"""

    def __init__(self, m):
        super().__init__()
        self.vision_model, self.proj = m.vision_model, m.visual_projection

    def forward(self, pixel_values):
        f = self.proj(self.vision_model(pixel_values)[1])
        return f / f.norm(dim=-1, keepdim=True)


class TextTower(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.text_model, self.proj = m.text_model, m.text_projection

    def forward(self, input_ids, attention_mask, token_type_ids):
        out = self.text_model(input_ids=input_ids, attention_mask=attention_mask,
                              token_type_ids=token_type_ids)
        f = self.proj(out[0][:, 0, :])
        return f / f.norm(dim=-1, keepdim=True)


print("[1/2] 导出图像塔 ...", flush=True)
torch.onnx.export(
    VisionTower(model), (torch.randn(1, 3, 224, 224),), str(OUT / "vision.onnx"),
    input_names=["pixel_values"], output_names=["image_features"],
    dynamic_axes={"pixel_values": {0: "batch"}, "image_features": {0: "batch"}},
    opset_version=17, do_constant_folding=True)

print("[2/2] 导出文本塔 ...", flush=True)
tok = AutoTokenizer.from_pretrained(SRC)
enc = tok(["示例文本"], padding="max_length", max_length=52, return_tensors="pt")
torch.onnx.export(
    TextTower(model),
    (enc["input_ids"], enc["attention_mask"], enc["token_type_ids"]),
    str(OUT / "text.onnx"),
    input_names=["input_ids", "attention_mask", "token_type_ids"],
    output_names=["text_features"],
    dynamic_axes={k: {0: "batch"} for k in
                  ["input_ids", "attention_mask", "token_type_ids", "text_features"]},
    opset_version=17, do_constant_folding=True)

# tokenizer 和预处理参数一起带走，生产侧不再碰原始 753MB 的 bin
tok.save_pretrained(OUT)
for f in ("preprocessor_config.json",):
    (OUT / f).write_bytes((SRC / f).read_bytes())

for f in sorted(OUT.iterdir()):
    print("  %-28s %8.1f MB" % (f.name, f.stat().st_size / 1e6))
print("[done]", OUT)
