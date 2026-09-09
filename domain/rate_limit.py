"""领域 API 层 — 内存滑动窗口限流（P0-1 开放平台安全）

按 API Key + 客户端 IP 双维度限流：
- Key 维度：防单个 Key 被滥用（读 120/min、写 60/min、生成 20/min）
- IP 维度：兜底防「注册海量 Key」绕过单 Key 限额（总 300/min）

滑动窗口：记录最近 window 秒内的请求时间戳，超限返回 429 + Retry-After。
线程安全（单进程 FastAPI + SQLite 部署，内存计数器足够；重启清零可接受，
审计日志 audit_log 仍保留完整历史供事后排查）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# 限额配置（可调）
READ_LIMIT = 120        # 读操作：次/分钟/Key
WRITE_LIMIT = 60        # 写操作：次/分钟/Key
GENERATE_LIMIT = 20     # LLM 生成（最贵）：次/分钟/Key
IP_LIMIT = 300          # 兜底：次/分钟/IP

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int = WINDOW_SECONDS):
        """检查 key 是否超限。返回 (allowed: bool, retry_after_seconds: int)。"""
        now = time.time()
        with self._lock:
            dq = self._windows[key]
            # 清理窗口外的时间戳
            while dq and now - dq[0] > window_seconds:
                dq.popleft()
            if len(dq) >= limit:
                retry_after = max(1, int(window_seconds - (now - dq[0])) + 1)
                return False, retry_after
            dq.append(now)
            return True, 0

    def reset(self):
        """清空所有窗口（测试/运维用）。"""
        with self._lock:
            self._windows.clear()


# 全局单例
limiter = RateLimiter()


def check_key(key_id, required_scope: str) -> tuple[bool, int]:
    """按 Key + scope 检查限流。返回 (allowed, retry_after)。"""
    if required_scope in ("write", "share", "admin"):
        return limiter.check(f"key:{key_id}:w", WRITE_LIMIT)
    return limiter.check(f"key:{key_id}:r", READ_LIMIT)


def check_generate(key_id) -> tuple[bool, int]:
    """generate（LLM 调用）额外加更严限额。"""
    return limiter.check(f"key:{key_id}:gen", GENERATE_LIMIT)


def check_ip(ip: str) -> tuple[bool, int]:
    """IP 维度兜底限流。"""
    if not ip:
        return True, 0
    return limiter.check(f"ip:{ip}", IP_LIMIT)
