# Travel Memory — 远程 MCP 接入说明（面向所有 Agent 开发者）

> 版本：1.3 · 更新：2026-08-27 · 适用协议：MCP Streamable HTTP (2025-03-26)
> 本文档面向**任意 Agent / AI 客户端**（爱马仕总控、量子助手、Claude Desktop、Cursor、自研 Agent 等）。
> 你的 Agent 只需一个 API Key，即可通过标准 MCP 协议读写**你自己账号下**的旅行记忆数据。

---

## 1. 这是什么

Travel Memory 是一个多用户旅行记忆开放平台。每个用户有自己的账号和数据空间，
用户把自己的 API Key 交给自己的 Agent，Agent 就以「开发者」身份通过 MCP 协议远程接入，
消费以下能力：

- 📋 行程管理：创建/查询/更新/删除行程
- 📍 地点管理：添加/更新/删除行程中的地点（含坐标、路线模式）
- 🧠 记忆与生成：读取行程记忆、生成旅行内容（AI 写作）
- 🔗 关联与偏好：行程关联、用户偏好
- 📤 分享：生成分享链接（可设有效期/密码）
- 🗺️ 地理编码：地址 → 经纬度
- 📊 统计与导出：行程统计、完整导出

**关键特性：多租户数据隔离。** 每个 Key 只能读写它所属账号自己的数据，
不同用户的 Agent 之间天然隔离，互不可见。

---

## 2. 快速开始（3 步）

### 第 1 步：注册账号并创建 API Key

1. 打开平台注册页：`https://travel-memory.47.107.66.14.nip.io/` → 注册一个账号（或让平台管理员开通）
2. 登录后进入 **开发者控制台**：`https://travel-memory.47.107.66.14.nip.io/console`
3. 点击「创建 API Key」，选择权限范围：
   - `read` — 只读：查询行程/记忆/导出/统计
   - `write` — 读写：在 read 基础上可创建/修改/删除行程与地点
   - `share` — 分享：在 write 基础上可创建/删除分享链接
   - `admin` — 管理（一般不需要）
4. 复制生成的 Key（格式：`tm_` 开头的一串字符），**只显示一次，请妥善保存**

### 第 2 步：把 Key 交给你的 Agent

在你的 Agent 配置中保存 Key（例如环境变量 `TRAVEL_MEMORY_API_KEY=tm_xxx`），
并告诉它「你可以通过 MCP 访问旅行记忆平台，端点是 https://travel-memory.47.107.66.14.nip.io/mcp」。

### 第 3 步：配置 MCP 客户端

标准 MCP 客户端（Claude Desktop / Cursor / 支持远程 MCP 的框架）配置示例：

```json
{
  "mcpServers": {
    "travel-memory": {
      "type": "http",
      "url": "https://travel-memory.47.107.66.14.nip.io/mcp",
      "headers": {
        "Authorization": "Bearer tm_你的Key"
      }
    }
  }
}
```

如果你的客户端不支持自定义 Header，也可以在 URL 中直接带 Key：
`https://travel-memory.47.107.66.14.nip.io/mcp?api_key=tm_你的Key`（两种方式等价，推荐 Header 方式）。

---

## 3. 端点与认证

| 项 | 值 |
|---|---|
| 端点 | `POST https://travel-memory.47.107.66.14.nip.io/mcp` |
| 兼容端点 | `POST http://47.107.66.14:8084/mcp`（HTTP 明文，过渡用） |
| 协议 | MCP Streamable HTTP transport（JSON-RPC 2.0 over HTTP） |
| 协议版本 | `2025-03-26` |
| 认证方式 | `Authorization: Bearer tm_xxx`（HTTP Header） |
| 备用传参 | URL query：`?api_key=tm_xxx` |
| 响应格式 | `application/json`（非流式，单条 JSON 响应） |
| 服务标识 | `travel-memory` v1.1.0 |

所有请求都必须带有效 Key，否则返回 HTTP `401` + MCP 错误 `-32001 invalid_api_key`。

---

## 4. 支持的 MCP 方法

| 方法 | 说明 | 是否需要响应 |
|---|---|---|
| `initialize` | 握手，返回协议版本/能力/服务信息 | 是 |
| `notifications/initialized` | 客户端就绪通知 | 否（HTTP 202） |
| `tools/list` | 列出全部可用工具（17 个） | 是 |
| `tools/call` | 调用工具 | 是 |
| `ping` | 健康检查 | 是 |

**无状态设计**：服务端不维护会话，每个请求独立鉴权，任何顺序调用均可。

### 4.1 initialize

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"my-agent","version":"1.0.0"}}}
```

响应：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"travel-memory","version":"1.1.0"}}}
```

### 4.2 通知

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

响应：HTTP `202 No Content`（空 body）。

---

## 5. 工具清单（34 个）

| 工具 | 所需 scope | 说明 |
|---|---|---|
| `list_trips` | read | 列出用户的所有旅行行程（含 id 和名称） |
| `get_memory` | read | 获取某次行程的结构化旅行记忆（时间线、地点、照片数、主题）（参数：trip_id） |
| `get_memory_overview` | read | 获取用户的旅行记忆全景（标准化 bundle）：所有行程简表、整体统计、旅行人格画像（偏好+跨行程联想）、足迹范围 |
| `get_associations` | read | 获取跨行程的联想（重访地点、主题重复、季节偏好）（参数：trip_id, limit） |
| `get_preferences` | read | 获取用户的长期旅行偏好画像 |
| `recall_memory` | read | 唤醒用户的旅行周年纪念：找出今天或未来 N 天内「几年前的今天」用户去过的地方，返回一段有温度的唤醒语（如「去年今天你在巴黎」）（参数：lookahead_days） |
| `search_memory` | read | 用自然语言检索用户的旅行记忆（如'我去过哪些海边''哪里拍过日落'）（参数：query, limit） |
| `ask_memory` | read | 用自然语言'问'用户的旅行记忆，返回一段有温度的对话式回答（如'我上次在巴黎吃的小馆子叫什么'）（参数：question, limit） |
| `generate` | read | 记忆创作引擎：根据行程生成内容（参数：kind, trip_id, style, language） |
| `create_trip` | write | 创建一次新旅行行程（参数：name, start_date, end_date, description, status, client_request_id） |
| `update_trip_status` | write | 更新行程状态机：planning 计划中 → traveling 进行中 → completed 已完成 → archived 已归档（参数：trip_id, status） |
| `geocode` | read | 按地名查经纬度（WGS-84）（参数：address） |
| `add_point` | write | 给某次行程添加一个地点（参数：trip_id, name, lat, lng, start_date, end_date, description, route_mode, client_request_id） |
| `update_point` | write | 修改行程中某个地点的信息（名称/坐标/日期/描述等）（参数：point_id, name, lat, lng, start_date, end_date, description, route_mode） |
| `delete_point` | write | 从行程中删除一个地点（参数：point_id） |
| `delete_trip` | write | 删除整个行程（含所有地点和媒体）（参数：trip_id） |
| `export_trip` | read | 导出某次行程为 JSON（坐标 WGS-84），供备份/迁移/二次加工（参数：trip_id） |
| `export_memory` | read | 全量导出用户所有旅行记忆（行程+地点+情绪元数据+媒体元数据+人格画像+联想）为一个可携带 bundle（travel-memory.full-bundle.v1） |
| `discover_patterns` | read | 跨旅程模式发现：分析用户所有行程，自动发现重访地点、季节偏好、地点类型偏好、出行节奏，产出可分享的「旅行画像」一句话总结 |
| `find_unfinished_journeys` | read | 未完成旅程识别：扫描用户记忆中的「没说出口的遗憾」——搁置超过 90 天的计划、没归档的旅程、计划过但从没去成的地方，并生成一句把遗憾变成前进动力的提示语 |
| `annual_travel_report` | read | 年度旅行报告：聚合某一年（默认当前年）的行程/地点/城市/里程/照片/情绪统计，生成一段有温度的年度叙事（参数：year） |
| `shared_memory` | read | 共同记忆：把两个用户的足迹叠在一起——共同去过的城市、同一天/同一月的时间重叠、各自独享的目的地，生成一段「你们的故事」（参数：with_username） |
| `reinterpret_trip` | read | 记忆再阐释：给过去的旅程换一个视角重新讲述（成长/遗憾/同行/幽默四种镜头），输出基于真实地点清单的纪实体重述（参数：trip_id, lens） |
| `travel_biography` | read | 旅行人生传记：把用户全部已成行旅程按时间排序写成一本分章节的书（markdown 格式），含全局统计（行程数/城市/里程/照片） |
| `time_travel` | read | 时间旅行：三种模式在记忆的时间轴穿行——rewind 回溯某天（date=YYYY-MM-DD，看那天在哪/做什么）；preview 预演未来某天的规划行程（date）；counterfactual 反事实推演（trip_id，'如果当时换一条路'，输出带【虚构推演】标注）（参数：mode, date, trip_id） |
| `recommend_destinations` | read | 行程推荐：基于用户历史足迹和偏好，推荐「还没去过但可能喜欢」的目的地，每个带可解释理由（如'因为你上次在海边城市最开心'）（参数：count, style） |
| `import_memory` | write | 导入一个 full-bundle.v1 全量记忆打包（来自 export_memory），恢复/迁移/传承到当前账号（参数：bundle） |
| `get_stats` | read | 获取某次行程的统计（地点数/照片数/视频数/起止日期）（参数：trip_id） |
| `create_share` | share | 为某次行程生成只读分享链接（token），供他人查看（参数：trip_id, expire_days, password） |
| `delete_share` | share | 撤销某次行程的全部分享链接（参数：trip_id） |
| `share_count` | read | 查询某次行程当前有效分享链接数量（参数：trip_id） |
| `list_media` | read | 列出某地点已上传的照片/视频媒体（含缩略图、EXIF 时间/坐标）（参数：point_id） |
| `upload_media` | write | 给某地点上传一张照片或一段视频（data_base64 为文件内容 base64 编码）（参数：point_id, data_base64, filename, content_type） |
| `delete_media` | write | 删除某条媒体（照片/视频）（参数：media_id） |

**scope 说明**：
- `read`：只能查询，不能改任何数据
- `write`：read + 创建/修改/删除行程与地点、导入导出记忆
- `share`：write + 创建/删除分享链接

调用需要更高 scope 的工具而 Key 权限不足时，返回 MCP 错误 `-32002`（附所需 scope 提示）。

### 5.1 调用示例（tools/call）

创建行程：

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_trip","arguments":{"name":"十一欧洲自驾","start_date":"2026-10-01","end_date":"2026-10-10"}}}
```

响应（所有工具统一返回文本格式，成功时 `isError: false`）：

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"id\": 16, \"name\": \"十一欧洲自驾\", ...}"}],"isError":false}}
```

失败时 `isError: true`，`text` 字段含错误详情。

---

## 6. 手测示例（curl）

不依赖任何 MCP 客户端，用 curl 即可完整验证链路：

```bash
BASE=https://travel-memory.47.107.66.14.nip.io/mcp
KEY=tm_你的Key

# 1) 握手
curl -s $BASE -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl-test","version":"1.0"}}}'

# 2) 列出工具
curl -s $BASE -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# 3) 查询我的行程
curl -s $BASE -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_trips","arguments":{}}}'

# 4) 无效 Key → 401
curl -s -o /dev/null -w "%{http_code}" $BASE -H "Authorization: Bearer tm_wrong" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"ping"}'
```

---

## 7. 错误码速查

| HTTP | MCP code | 场景 | 处理建议 |
|---|---|---|---|
| 401 | -32001 | Key 无效/缺失 | 检查 Key 是否完整、是否被吊销；重新在控制台创建 |
| 403 | -32002 | 工具所需 scope 超出 Key 权限 | 在控制台重新创建更高 scope 的 Key |
| 400 | — | 请求体不是合法 JSON-RPC | 检查 JSON 格式与 Content-Type |
| 200 | -32601 | 未知方法 | 只使用上文列出的方法 |
| 200 | -32602 | 参数错误 | 对照工具清单补全参数 |
| 200 | — | `isError:true` | text 字段含领域 API 错误详情（如 404 行程不存在） |

---

## 8. 安全与限制

- 🔑 **Key 即身份**：谁拿到 Key 谁就能操作你的数据，请像保管密码一样保管
- 🚫 **无管理能力**：远程 MCP 不能注册账号、不能管理 Key（这些只能在 Web 控制台做）
- ⏱️ **超时**：单次工具调用上限 60 秒（AI 生成类可能较慢，属正常）
- 📡 **无流式**：当前返回完整 JSON，不支持 SSE 流式输出
- 🔒 **传输安全**：主端点已支持 HTTPS（Let's Encrypt 正式证书）；`http://47.107.66.14:8084` 为兼容过渡端点，建议新接入统一走 HTTPS
- 🧹 **Key 吊销**：随时可在开发者控制台吊销，吊销后立即失效

---

## 9. 常见问题

**Q: 我的 Agent 报 `invalid_api_key`？**
A: 检查 Key 是否复制完整（含 `tm_` 前缀）、是否已吊销。可在控制台重新创建。

**Q: 为什么我查不到别人的行程？**
A: 正常。平台是多租户隔离的，每个 Key 只能访问自己账号的数据。

**Q: 支持 SSE / 流式输出吗？**
A: 暂不支持，当前为单次请求-响应模式。后续版本会评估。

**Q: 在哪里管理我的 Key？**
A: 开发者控制台 `https://travel-memory.47.107.66.14.nip.io/console`（需登录）：查看、创建、吊销、用量审计。

---

_如有接入问题，请联系平台管理员。_
