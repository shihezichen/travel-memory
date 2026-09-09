import os
#!/usr/bin/env python3
# 补生成西班牙、爱尔兰（上次 max_tokens 不足导致 JSON 截断）
import json, os, re, sys, time
import urllib.request

KEY = sys.argv[1]
URL = "https://api.deepseek.com/chat/completions"

COUNTRIES = [
    ("西班牙", "ES"),
    ("爱尔兰", "IE"),
]

def call(prompt, maxtok):
    payload = {"model": "deepseek-chat",
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.2, "max_tokens": maxtok}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"]

def extract_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if m: text = m.group(1)
    else:
        m = re.search(r"\[.*\]", text, re.S)
        if m: text = m.group(0)
    return json.loads(text)

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'places-zh-en.json')
with open(out, encoding="utf-8") as f:
    existing = json.load(f)
seen = {it["zh"] + "|" + it["cc"] for it in existing}

for country, cc in COUNTRIES:
    # 分批：城市一批 + 景点一批，避免单次太长
    for part, extra in [("主要城市", "约 40-70 个城市"), ("热门旅游景点", "约 50-100 个景点")]:
        prompt = f"""你是地名翻译专家。请为「{country}」生成 {part} 的中文地名 ↔ 英文地名对照表。

要求：
1. 覆盖该国{extra}，尽量全面
2. 每个条目只含三个字段：zh（中文大陆通用译名）、en（英文/当地语言标准名，必须是地理编码服务 Open-Meteo 能搜到的英文名）、cc（固定 {cc}）
3. 英文名用标准拼写，不要拼音

只输出一个 JSON 数组，不要解释、不要 markdown 代码块。格式：[{{"zh":"龙达","en":"Ronda","cc":"ES"}}]"""
        print(f"[生成] {country} · {part} ...", flush=True)
        try:
            text = call(prompt, 16000)
            arr = extract_json(text)
            n = 0
            for it in arr:
                if isinstance(it, dict) and it.get("zh") and it.get("en") and it.get("cc"):
                    k = str(it["zh"]).strip() + "|" + str(it["cc"]).strip()
                    if k in seen: continue
                    seen.add(k)
                    existing.append({"zh": str(it["zh"]).strip(), "en": str(it["en"]).strip(), "cc": str(it["cc"]).strip()})
                    n += 1
            print(f"  -> +{n} 条", flush=True)
        except Exception as e:
            print(f"  !! 失败: {e}", flush=True)
        time.sleep(1)

with open(out, "w", encoding="utf-8") as f:
    json.dump(existing, f, ensure_ascii=False, indent=1)
print(f"\n✅ 合并后共 {len(existing)} 条")
