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

1. 打开平台注册页：`https://top001.xin/` → 注册一个账号（或让平台管理员开通）
2. 登录后进入 **开发者控制台**：`https://top001.xin/console`
3. 点击「创建 API Key」，选择权限范围：
   - `read` — 只读：查询行程/记忆/导出/统计
   - `write` — 读写：在 read 基础上可创建/修改/删除行程与地点
   - `share` — 分享：在 write 基础上可创建/删除分享链接
   - `admin` — 管理（一般不需要）
4. 复制生成的 Key（格式：`tm_` 开头的一串字符），**只显示一次，请妥善保存**

### 第 2 步：把 Key 交给你的 Agent

在你的 Agent 配置中保存 Key（例如环境变量 `TRAVEL_MEMORY_API_KEY=tm_xxx`），
并告诉它「你可以通过 MCP 访问旅行记忆平台，端点是 https://top001.xin/mcp」。

### 第 3 步：配置 MCP 客户端

标准 MCP 客户端（Claude Desktop / Cursor / 支持远程 MCP 的框架）配置示例：

```json
{
  "mcpServers": {
    "travel-memory": {
      "type": "http",
      "url": "https://top001.xin/mcp",
      "headers": {
        "Authorization": "Bearer tm_你的Key"
      }
    }
  }
}
```

如果你的客户端不支持自定义 Header，也可以在 URL 中直接带 Key：
`https://top001.xin/mcp?api_key=tm_你的Key`（两种方式等价，推荐 Header 方式）。

---

## 3. 端点与认证

| 项 | 值 |
|---|---|
| 端点 | `POST https://top001.xin/mcp` ✅ Let's Encrypt 域名证书（完整校验通过，无需 -k） |
| 旧端点（已下线） | ~~IP 直连端点~~（2026-09-10 备案通过后已切回域名）、~~`https://travel-memory.47.107.66.14.nip.io/mcp`~~、~~`http://47.107.66.14:8084/mcp`~~ |
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
| `tools/list` | 列出全部可用工具（36 个），返回的是**实时** schema | 是 |
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

## 5. 工具清单（36 个）

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
| `update_trip` | write | 修改行程的名称/起止日期/描述/状态，只改传了的字段（参数：trip_id, name, start_date, end_date, description, status） |
| `list_points` | read | 列出行程下所有地点，**带 point_id**（含 index/sort_order/坐标/日期/描述/媒体数）。要改删地点或传照片，先用它拿 id（参数：trip_id） |
| `geocode` | read | 按地名查经纬度（WGS-84）（参数：address） |
| `add_point` | write | 给某次行程添加一个地点；`sort_order` 可指定当天内的插入位次（参数：trip_id, name, lat, lng, start_date, end_date, description, route_mode, is_waypoint, sort_order, client_request_id） |
| `update_point` | write | 修改地点，**只改传了的字段**；不传坐标 = 坐标原样保留（不重新地理编码），要重查坐标传 `regeocode=1`（参数：point_id, name, lat, lng, start_date, end_date, description, route_mode, is_waypoint, sort_order, regeocode） |
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

### 5.0 改一个已有地点的正确姿势

写操作（`update_point` / `delete_point` / `upload_media`）都要 `point_id`，而
`get_memory` / `export_trip` / `get_stats` 都**不**返回 id。拿 id 用 `list_points`：

```
list_points(trip_id=133)
  → {"trip_id":133, "count":9, "points":[
       {"point_id":871, "index":0, "sort_order":0, "name":"香格里拉",
        "lat":27.8231, "lng":99.7065, "start_date":"2026-10-01", ...,
        "media_n":4}, ...]}
```

拿到 id 之后，每个字段可以单独改，**不用把其他字段抄一遍**：

```
update_point(point_id=871, start_date="2026-10-05")     # 只改日期，坐标/名称/描述不动
update_point(point_id=871, description="null")          # 清空描述（字面量 "null"）
update_point(point_id=871, name="香格里拉古城", regeocode="1")   # 改名并按新地名重查坐标
```

**排序**：跨天顺序由 `start_date` 决定；同一天内的先后由 `sort_order` 决定（0 起）。
重排一天内的几个点，就逐个把它们的 `sort_order` 改成 0,1,2…；想插队建点，
`add_point(..., sort_order=1)` 会把原先站在第 1 位及之后的点整体顺延。

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

### 5.2 上传媒体（upload_media，base64）

HTTP transport 无法传 multipart，媒体通过 `data_base64` 传输（文件内容 base64 编码）：

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"upload_media","arguments":{"point_id":42,"data_base64":"<BASE64_内容>","filename":"eiffel_sunset.jpg"}}}
```

响应返回媒体元数据（含缩略图名、EXIF 提取的时间/坐标）。`filename` 的扩展名或 `content_type` 用于推断照片/视频类型。

### 5.3 幂等重试（防重复创建）

写操作支持传 `client_request_id`（UUID）。网络超时后重试时传相同值，不会重复创建：

```json
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"create_trip","arguments":{"name":"十一欧洲自驾","client_request_id":"550e8400-e29b-41d4-a716-446655440000"}}}
```

同一个 `client_request_id` 只会执行一次；后续相同调用直接返回首次的结果。

---

## 6. 手测示例（curl）

不依赖任何 MCP 客户端，用 curl 即可完整验证链路：

```bash
BASE=https://top001.xin/mcp
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
| 429 | -32029 | 触发限流（请求过于频繁） | 按 Retry-After / 错误信息提示的秒数退避后重试 |
| 409 | — | 幂等冲突（相同 client_request_id 正在处理中） | 稍后重试，或更换 client_request_id |
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
- 🚦 **限流**：按 Key 分级限流（读 120/写 60/生成 20 次/分钟）+ 按 IP 兜底（300 次/分钟）。超限返回 429，请按提示退避
- 🔁 **幂等**：写操作（create_trip / add_point / upload_media）支持传 `client_request_id`（建议 UUID）。网络重试时传相同值不会重复创建，直接返回首次结果
- 🔒 **传输安全**：全程 HTTPS，Let's Encrypt 正式证书（签给域名 `top001.xin`，90 天，服务端自动续期），
  客户端**无需**跳过证书校验（不要用 `curl -k` / `verify=False`）。HTTP 会 301 跳 HTTPS。
- ℹ️ **端点变更历史**：2026-09-10 域名 `top001.xin` 备案通过（粤ICP备2026134597号），
  端点已从此前的 IP 直连地址切回域名。续期由服务端自动完成，对接入方**透明无感**。
- 🧹 **Key 吊销**：随时可在开发者控制台吊销，吊销后立即失效

---

## 9. 版本与变更记录

### 9.1 你的 Agent 怎么知道接口变了

本服务是**无状态** MCP 实现：每个 POST 独立鉴权，不维护 session，没有 SSE 长连接。
服务端因此**无法**主动推送 `notifications/tools/list_changed`，`capabilities` 里也
不会声明 `tools.listChanged`（声明了却推不出去只会更误导）。

感知变更只有两条正路：

1. **重连时重拉 `tools/list`** —— 返回的是实时 schema，不是快照。工具名、参数、
   必填项、描述全都是当场从服务端生成的。**别把 schema 缓存过夜**。
2. **比对 `initialize` 返回的 `serverInfo.version`** —— 工具集一变这个号就变。
   长驻 Agent 建议每次 `initialize` 后比一下上次记下的版本，不同就重拉 `tools/list`。

版本号规则：`patch` 只改了描述文字；`minor` 新增工具或给已有工具加可选参数；
`major` 删了工具、改了必填参数、或改变了已有参数的语义。

### 9.2 变更记录

**v1.2.0**（2026-09-16）

新增：

- `list_points(trip_id)` —— 列出行程下所有地点，**带 `point_id`**。
- `update_trip(trip_id, ...)` —— 改行程名称/起止日期/描述/状态，只改传了的字段。
- `add_point` 和 `update_point` 新增可选参数 `sort_order`（同一天内的位次，0 起）。

行为变更（**已接入的 Agent 请注意**）：

- `update_point` 的 `name` 由必填改为可选，`required` 现在只有 `point_id`。
- ⚠️ `update_point` **不传 `lat`/`lng` 时不再重新地理编码**，坐标原样保留。
  旧行为是「不传坐标就按 `name` 重查」，导致「只改个日期」也要走一次地理编码，
  地名稍偏就整次更新 400 失败。要恢复旧行为，显式传 `regeocode=1`。
- `update_point` 不再对 `route_mode` 填默认值 `drive`。旧行为会在你「只改日期」时
  把设成 `line` 的段（飞机/火车）静默改回走路网。
- `update_trip` 传空 `name` 现在按「不改」处理，不再把行程名抹空。

修复了什么：`point_id` 此前读不到（`get_memory` / `export_trip` / `get_stats` 都不返回），
而写接口又都要它 —— 改一个已有地点的日期做不到，只能删掉整条行程重建、连带丢照片。
`list_points` 补上了这个断口。

---

## 10. 常见问题

**Q: 我的 Agent 报 `invalid_api_key`？**
A: 检查 Key 是否复制完整（含 `tm_` 前缀）、是否已吊销。可在控制台重新创建。

**Q: 为什么我查不到别人的行程？**
A: 正常。平台是多租户隔离的，每个 Key 只能访问自己账号的数据。

**Q: 支持 SSE / 流式输出吗？**
A: 暂不支持，当前为单次请求-响应模式。后续版本会评估。

**Q: 在哪里管理我的 Key？**
A: 开发者控制台 `https://top001.xin/console`（需登录）：查看、创建、吊销、用量审计。

---

_如有接入问题，请联系平台管理员。_
