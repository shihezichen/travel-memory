#!/usr/bin/env python3
"""Jev 冒烟：打一次真实云端，确认 key、出网、请求体形状都对。

    TM_JEV_API_KEY=xxx python scripts/jev_probe.py "海边日落"

单测里一次网络都不发（全是 monkeypatch），所以凭据和 wire format 这两件事
只有这个脚本能验。排查顺序就是它的输出顺序：配置 → 意图 → 一次候选精判。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jevrerank  # noqa: E402


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "海边日落"
    desc = sys.argv[2] if len(sys.argv) > 2 else "夕阳沉入海平面，橘色的天空，沙滩上有两个人的剪影"

    print("配置:", json.dumps(jevrerank.stats(), ensure_ascii=False, indent=2))
    print("端点:", jevrerank.api_url())
    if not jevrerank.available():
        print("\n未配置。至少要有 TM_JEV_API_KEY；走 Cloudflare 还要 TM_JEV_CF_ACCOUNT_ID。")
        return 2

    t = time.time()
    try:
        a = jevrerank._post({"query": query}, jevrerank._intent_questions(query))
    except Exception as e:                                # noqa: BLE001
        print("\n意图分类失败:", type(e).__name__, e)
        return 1
    print("\n[%.0fms] 意图:" % ((time.time() - t) * 1000),
          json.dumps(a, ensure_ascii=False))

    t = time.time()
    try:
        a = jevrerank._post({"image_description": desc, "query": query},
                            jevrerank._candidate_questions(query))
    except Exception as e:                                # noqa: BLE001
        print("\n候选精判失败:", type(e).__name__, e)
        return 1
    print("\n[%.0fms] 精判:" % ((time.time() - t) * 1000),
          json.dumps(a, ensure_ascii=False, indent=2))

    f = jevrerank.fuse(1.0, a, jevrerank._answer_choice(
        {"choice": os.environ.get("TM_JEV_PROBE_INTENT", "自然风光")}))
    print("\n融合分（假设 CLIP 归一化后为 1.0）:",
          json.dumps(f, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
