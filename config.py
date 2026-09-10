"""跨模块共享的配置常量。

从 app.py 搬出来的，读取逻辑一字未改。搬出来的唯一原因是：路由拆进 routers/* 之后
那些模块也要用 AMAP_KEY / OSRM_URL 这些值，而它们不能 `import app`（app.py 自己要
import routers.*，会绕成循环）。

这里只放**静态配置**。会被测试 monkeypatch 的可变全局（UPLOAD_DIR、_TILE_DIR、
GATE_HASH、ADMIN_TOKEN）故意不放进来 —— 它们必须留在各自的使用方模块里，
测试才能通过重绑模块全局做隔离。
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 高德地图 key（从 amap_key.txt 读取，若未配置则为空）
AMAP_KEY = ""
_key_file = BASE / "amap_key.txt"
if _key_file.exists():
    AMAP_KEY = _key_file.read_text(encoding="utf-8").strip()

# 高德安全密钥（JS API 2.0 securityJsCode，从 amap_security.txt 读取）
AMAP_SECURITY = ""
_sec_file = BASE / "amap_security.txt"
if _sec_file.exists():
    AMAP_SECURITY = _sec_file.read_text(encoding="utf-8").strip()

# OSRM 全球驾车路线规划服务（高德只支持国内，海外路线用此兜底）
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
# OSRM 超时。原来是 30 秒 —— 而实测这台机器打 OSRM 公共服务是 663/665/675 ms
# （巴黎→布鲁塞尔，连打 3 次）。30 秒等于给了 45 倍的余量，代价是缓存未命中时
# 每个请求最长占住一个线程池槽位 30 秒；前端 drawRoute() 还会用 Promise.all
# 对一条行程的所有路段并发发请求（库里最长的行程是 16 段）。
# 收到 8 秒：相对实测值仍有 12 倍余量，而最坏情况的槽位占用降到 3.75 分之一。
# 超时的后果是返回空 routes，前端已有直线兜底，不会白屏。
OSRM_TIMEOUT = float(os.environ.get("TRAVEL_OSRM_TIMEOUT", "8"))
