# 旅程搭子（Companion）— 领域 API 层最小消费者 demo

阶段2 产出：验证 `/api/ai/*` 领域 API 能被一个 agent 完整消费，并产出自然语言回答。

## 架构

```
用户提问
   │
   ▼
companion.py（DeepSeek function calling 做意图路由）
   │  工具调用（Bearer tm_xxx, read/write scope）
   ▼
领域 API /api/ai/*
   ├─ GET  /trips           列出行程
   ├─ GET  /trips/{id}/memory   结构化旅行记忆
   ├─ GET  /associations    跨行程联想（重访/主题/季节）
   ├─ GET  /preferences     长期偏好画像（惰性自动沉淀）
   ├─ GET  /geocode         按地名查坐标（WGS-84）
   ├─ POST /trips           新建行程（write）
   ├─ POST /trips/{id}/points  加地点（write，地名可自动地理编码）
   ├─ PUT  /points/{id}     改地点（write）
   └─ POST /generate        生成总结/路书/明信片/日记
```

## P2 深水区更新（2026-08-26）

新增 5 个领域能力（全部规则层零 token 聚合 + DeepSeek 叙事，LLM 失败自动降级规则模板，不抛异常）：

| 模块 | API | MCP 工具 | 要点 |
|------|-----|---------|------|
| N-11 年度报告 | `GET /api/ai/wrapped?year=` | `annual_travel_report` | 行程/城市/里程/照片/月度/季节聚合 + 叙事；12 月数据满一年自动出首份真实年报 |
| N-14 共同记忆 | `GET /api/ai/shared?with_username=` | `shared_memory` | 两人足迹合并：共同城市/同日同城重叠/互补目的地；只输出城市级+日期，不泄露行程细节 |
| N-16 记忆再阐释 | `GET /api/ai/trips/{id}/reinterpret?lens=` | `reinterpret_trip` | 成长/遗憾/同行/幽默四视角重讲一段旅程 |
| N-18 旅行传记 | `GET /api/ai/biography` | `travel_biography` | 全部 completed 行程分章节长文（规则层章节骨架 + LLM 润色） |
| N-20 时间旅行 | `GET /api/ai/timetravel?mode=&date=&trip_id=` | `time_travel` | 回溯（某日你在哪）/ 预演（未来某日规划）/ 反事实（如果没去某地） |

当前 MCP 工具总数：**34 个**。回归：`scripts/verify_wrapped_shared.py`（13 项）+ `scripts/verify_p2_deep.py`（18 项）全绿。

## Agent 接入方式（开放平台）

远程 Agent 有三种标准接入方式，全部用**自己的 API Key**（Bearer 认证）→ 数据天然多租户隔离：

| 方式 | 入口 | 适用 |
|------|------|------|
| **MCP 远程**（推荐） | `POST https://top001.xin/mcp`（Streamable HTTP transport） | Claude Desktop / Cursor / 爱马仕总控 / 任意 MCP 客户端 |
| MCP 本机 | `python3 mcp_server.py`（stdio） | 服务器本机 agent |
| HTTP 直调 | `GET/POST /api/ai/*` | 任意语言 SDK / curl |

MCP 远程示例（任何 MCP 客户端配置）：

```json
{
  "mcpServers": {
    "travel-memory": {
      "url": "https://top001.xin/mcp",
      "headers": { "Authorization": "Bearer tm_你的Key" }
    }
  }
}
```

Key 在开发者控制台 `/console` 自助创建，scope 分级：read（读）/ write（写）/ share（分享）。

## 用法

```bash
# 单次问答（飞书/脚本调用友好）
python3 companion.py "帮我回忆一下巴黎西班牙之旅"

# 交互式 REPL
python3 companion.py
```

环境变量（可选）：
- `COMPANION_API_KEY`：领域 API Key（默认读 vault `TRAVEL_COMPANION_KEY`，read+write scope）
- `TRAVEL_BASE_URL`：领域 API 地址（默认 `http://127.0.0.1:8084`）

## 验证结果（2026-08-24）

| 场景 | 工具 | 结果 |
|------|------|------|
| 回忆行程 | list_trips + get_memory | ✅ 完整召回 17 地点时间线 |
| 生成明信片 | generate(postcard) | ✅ 文案优美、串起旅程亮点 |
| 旅行偏好 | get_preferences | ✅ 自动沉淀（古城/山野/海边 + 冬）|
| 跨行程联想 | get_associations | ✅ 端点可用（异构数据暂空）|
| 只给地名记地点 | add_point（无坐标）| ✅ 自动地理编码（coord_source=geocode）|

## 完善记录（2026-08-24 晚）

本轮「继续完善系统」落地 5 项：

1. **地理编码闭环**：新增 `domain/geocode.py`（高德 geocode/geo，JS API 2.0 参数 + 30 天缓存）
   + `GET /api/ai/geocode` 端点 + `add_point`/`update_point` 的 lat/lng 改为可选
   —— 只给地名时自动地理编码，不再靠 LLM 瞎估坐标。
2. **坐标校验**：lat -90~90 / lng -180~180 越界直接 400，防离谱坐标入库。
3. **主题抽取增强**：`memory.py` 加城市/地标词典（欧洲 + 国内），纯城市名（巴黎/托莱多/龙达…）也能命中主题。
4. **偏好自动沉淀**：新增 `derive_preferences()`，`GET /preferences` 惰性幂等派生
   （theme + season，单行程也沉淀），不再依赖"先跑 associations"。
5. **修 2 个 bug**：
   - `db.upsert_pref` SQL 参数错位（5 占位符传 4 参数 → 500）
   - `association.py` 硬编码 `is_admin=0` 导致 admin/真实用户看不到行程 → 改用 `get_user_by_id`

**Key 归属修正**：companion 原用 admin(user2) key，行程数据在 arthur(user3，"边城量子")，
偏好会记错名。已新建 `companion-arthur`(read+write) key 写入 vault `TRAVEL_COMPANION_KEY`，
revoke 旧 admin write key(id7)。`_READONLY_KEY` 兜底保留（read-only 无害）。

## 已知局限（继承自领域 API 层）

1. **海外地名地理编码受限**：高德 geocode 对纯海外地名（"托莱多""孔苏埃格拉"）会误解析到国内同名 POI，`formatted` 字段会暴露实际解析地址供核对；Nominatim 本机不可达（网络墙）。海外地点建议手动点选或提供坐标。
2. **trip 无地点则无偏好**：偏好从 memory 的 timeline 派生，纯描述行程（如 trip 4）无 points 时抽不出季节/主题。
3. **季节偏好按日期字段**：需地点带 `start_date` 才能判季节。
