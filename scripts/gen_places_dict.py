import os
#!/usr/bin/env python3
# 生成 5 国（西/土/英/爱/法）中文地名 ↔ 英文地名对照词典
import json, os, re, sys, time
import urllib.request

KEY = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEEPSEEK_API_KEY", "")
URL = "https://api.deepseek.com/chat/completions"

COUNTRIES = [
    ("西班牙", "ES"),
    ("土耳其", "TR"),
    ("英国", "GB"),
    ("爱尔兰", "IE"),
    ("法国", "FR"),
]

def call_deepseek(prompt):
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]

def extract_json(text):
    text = text.strip()
    # 去掉可能的 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            text = m.group(0)
    return json.loads(text)

def main():
    all_places = []
    for country, cc in COUNTRIES:
        prompt = f"""你是地名翻译专家。请为「{country}」生成一份中文地名 ↔ 英文地名的对照表，用于海外地点搜索联想。

要求：
1. 覆盖该国「主要城市」+「热门旅游景点」，尽量全面（城市约 30-60 个 + 景点约 30-80 个，越多越好）
2. 每个条目只包含三个字段：zh（中文常见音译名）、en（英文/当地语言标准名，必须是地理编码服务 Open-Meteo 能搜到的英文名）、cc（国家码，固定为 {cc}）
3. 英文名用标准英文拼写（如 Ronda、Barcelona、Istanbul、Edinburgh、Dublin、Paris），不要音译成拼音
4. 中文名用大陆通用译名（如「龙达」对应 Ronda、「伊斯坦布尔」对应 Istanbul）

只输出一个 JSON 数组，不要任何解释、不要 markdown 代码块、不要多余文字。格式示例：
[{{"zh":"龙达","en":"Ronda","cc":"ES"}},{{"zh":"巴塞罗那","en":"Barcelona","cc":"ES"}}]"""
        print(f"[生成中] {country} ({cc}) ...", flush=True)
        try:
            text = call_deepseek(prompt)
            arr = extract_json(text)
            # 校验字段
            clean = []
            for it in arr:
                if isinstance(it, dict) and it.get("zh") and it.get("en") and it.get("cc"):
                    clean.append({"zh": str(it["zh"]).strip(), "en": str(it["en"]).strip(), "cc": str(it["cc"]).strip()})
            print(f"  -> {len(clean)} 条", flush=True)
            all_places.extend(clean)
        except Exception as e:
            print(f"  !! {country} 失败: {e}", flush=True)
        time.sleep(1)

    # 去重（zh+cc 唯一）
    seen = {}
    for it in all_places:
        k = it["zh"] + "|" + it["cc"]
        if k not in seen:
            seen[k] = it
    final = list(seen.values())

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'places-zh-en.json')
    with open(out, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 共 {len(final)} 条，已写入 {out}")

if __name__ == "__main__":
    main()
