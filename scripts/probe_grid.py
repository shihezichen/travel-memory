"""把几个 query 的 top-3 拼成一张图，用来人眼核验召回质量。"""
import io, json, sys
from pathlib import Path
from PIL import Image
import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import oss

rep = json.loads((BASE / "models/.probe_report.json").read_text(encoding="utf-8"))
QS = ["雪山", "夜景灯光", "美食", "人物合影", "街道建筑", "海边日落", "红色汽车", "草地上的狗"]
CELL, K = 300, 3

canvas = Image.new("RGB", (CELL * K, CELL * len(QS)), "white")
for r, q in enumerate(QS):
    for c, item in enumerate(rep[q][:K]):
        fn = item["file"]
        p = BASE / "uploads" / fn
        if p.exists():
            im = Image.open(p).convert("RGB")
        else:
            u = oss.sign_url("GET", "uploads/" + fn,
                             params={"x-oss-process": "image/resize,l_600"}, expires_in=600)
            im = Image.open(io.BytesIO(httpx.get(u, timeout=30).content)).convert("RGB")
        im = im.resize((CELL, CELL))
        canvas.paste(im, (c * CELL, r * CELL))
    print("row %d %s: %s" % (r, q, [i["file"] for i in rep[q][:K]]))

out = BASE / "models/.probe_grid.jpg"
canvas.save(out, quality=82)
print("saved", out, out.stat().st_size)
