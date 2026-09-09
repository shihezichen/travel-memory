"""LLM 供应商抽象层 — 按部署机器自动二选一。

- 本机（lenserver）：走本地已登录的 Claude Code CLI（claude -p），不需要额外 key。
- 远程（Top001 等）：保持原有 DeepSeek API 模式。
- 第三档 anthropic-api：直连 Anthropic API，无 CLI 框架开销，需 ANTHROPIC_API_KEY。

用 TRAVEL_LLM_PROVIDER 环境变量强制指定：auto(默认) / claude-cli / anthropic-api / deepseek
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

# ---------- 并发闸门 ----------
# app.py 里 129 个路由中有 129-7=122 个是同步 def，它们和这里的 LLM 调用共用
# anyio 的同一个 40 槽线程池。而一次 claude-cli 调用是 subprocess.run(timeout=180)，
# 也就是说 40 个并发生成请求就能把线程池占满整整三分钟 —— 期间登录、看行程、
# 看照片全部排队，等于整站挂掉。所以在这里把 LLM 的并发压到一个小常数。
#
# 注意：闸门必须「快速失败」而不是「排队等」。等待本身也占着线程池的槽位，
# 无限期排队的话闸门等于没加。所以拿不到槽位时只短暂等一下就抛 LLMBusy，
# 让路由尽快把槽位还回去。
MAX_CONCURRENCY = int(os.environ.get("TRAVEL_LLM_MAX_CONCURRENCY", "4"))
# 拿槽位最多等多久（秒）。0 = 完全不等。默认 1 秒是个折中：足以吸收「几个请求
# 几乎同时到」的抖动，又不会让排队的请求长时间占着线程池。
ACQUIRE_TIMEOUT = float(os.environ.get("TRAVEL_LLM_ACQUIRE_TIMEOUT", "1.0"))

_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


class LLMBusy(RuntimeError):
    """LLM 并发槽位已满，本次调用没被执行。调用方应转成 HTTP 503 + Retry-After。

    刻意不继承 fastapi 的 HTTPException —— domain/ 这一层对 web 框架零依赖，
    状态码映射由 app.py 负责。
    """


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# claude CLI 的 -p 模式每次会带上自身系统提示与工具 schema（约 14k token 固定开销），
# 所以这里尽量裁剪：清空 MCP、禁用全部工具。
CLAUDE_MODEL = os.environ.get("TRAVEL_CLAUDE_MODEL", "sonnet")
ANTHROPIC_MODEL = os.environ.get("TRAVEL_ANTHROPIC_MODEL", "claude-sonnet-5")


def resolve_provider() -> str:
    """决定用哪个 provider。auto = 有 claude CLI 就用它，否则回落 DeepSeek。"""
    p = os.environ.get("TRAVEL_LLM_PROVIDER", "auto").strip().lower()
    if p and p != "auto":
        return p
    if shutil.which("claude"):
        return "claude-cli"
    return "deepseek"


def _deepseek_key() -> str:
    # 原逻辑：先试 vault，再回落环境变量。vault 只在 Top001 的 workspace 里存在。
    try:
        out = subprocess.run(
            ["python3", "scripts/secrets_tool.py", "get", "DEEPSEEK_API_KEY"],
            cwd=os.environ.get("SECRETS_VAULT_DIR", ""),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _call_deepseek(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    api_key = _deepseek_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(DEEPSEEK_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    text = body["choices"][0]["message"]["content"].strip()
    return text, body.get("usage", {}).get("total_tokens", 0)


def _call_anthropic(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 未配置")
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode())
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text").strip()
    u = body.get("usage", {})
    return text, u.get("input_tokens", 0) + u.get("output_tokens", 0)


def _call_claude_cli(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    """走本机已登录的 claude CLI。max_tokens 无法硬限，靠 system 提示约束长度。"""
    cmd = [
        "claude", "-p", user,
        "--model", CLAUDE_MODEL,
        "--system-prompt", system,
        "--allowedTools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--output-format", "json",
    ]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)          # 避免被父 Claude Code 会话的环境干扰
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                       cwd="/tmp", env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI 退出码 {r.returncode}: {r.stderr.strip()[:300]}")
    body = json.loads(r.stdout)
    if body.get("is_error"):
        raise RuntimeError(f"claude CLI 报错: {str(body.get('result'))[:300]}")
    text = (body.get("result") or "").strip()
    if not text:
        raise RuntimeError("claude CLI 返回空结果")
    u = body.get("usage", {})
    tokens = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
              + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
    return text, tokens


_DISPATCH = {
    "claude-cli": _call_claude_cli,
    "anthropic-api": _call_anthropic,
    "deepseek": _call_deepseek,
}


def call(system: str, user: str, max_tokens: int = 800) -> tuple[str, int]:
    """统一入口，返回 (text, tokens_used)。带 3 次重试 + 指数退避，与原实现一致。"""
    provider = resolve_provider()
    fn = _DISPATCH.get(provider)
    if fn is None:
        raise RuntimeError(f"未知 TRAVEL_LLM_PROVIDER：{provider}")
    # 闸门包住整个重试循环（含 2s/4s 退避），这样「同时在跑的 LLM 调用」
    # 硬上限就是 MAX_CONCURRENCY，不会因为重试而放大。
    if not _slots.acquire(timeout=ACQUIRE_TIMEOUT):
        raise LLMBusy(f"LLM 并发已满（上限 {MAX_CONCURRENCY}），请稍后重试")
    try:
        last_err = None
        for attempt in range(3):
            try:
                return fn(system, user, max_tokens)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt * 2)  # 2s / 4s 退避
        raise RuntimeError(f"LLM 调用失败（provider={provider}）：{last_err}")
    finally:
        _slots.release()
