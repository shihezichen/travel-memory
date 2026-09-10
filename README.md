<div align="center">

# 🧭 Travel Memory · 旅行记忆

**把一次旅行沉淀成可以反复回看的记忆 —— 地图、照片、路线、文字，以及一套对 AI Agent 开放的记忆 API。**

*Turn a trip into a memory you can revisit — maps, photos, routes, notes, plus a memory API open to AI agents.*

[中文](#中文) · [English](#english)

</div>

---

<a name="中文"></a>

## 中文

### 这是什么

Travel Memory 是一个自托管的多用户旅行记忆系统。它有两副面孔：

- **对人**：一个移动端优先的 Web 应用。在地图上记录行程的每一个落脚点，挂上照片和视频，写下当时的想法；之后可以回放整条轨迹、按行程浏览相册、生成只读分享链接发给朋友。
- **对 Agent**：一套 **MCP (Model Context Protocol) Streamable HTTP** 端点，34 个工具，任意远程 Agent 用自己的 API Key 接入，多租户隔离。可以让 AI 读你的旅行记忆、发现模式、生成年度报告、回答"我上次去京都是什么时候"。

技术栈刻意保持简单：**FastAPI + SQLite + 原生 JavaScript**。没有前端构建步骤，没有 ORM，没有 Redis，`git clone` 之后装几个 Python 包就能跑。

### 功能

**行程与地图**

- 多行程管理，每个行程包含有序的轨迹点；支持计划中 / 进行中 / 已完成 / 已归档四种状态
- 真实驾车路线规划（高德 Driving，OSRM 兜底），逐段可点、可切换备选方案，结果缓存
- 每段路线在中点标注里程；选中某个点时，来时路（绿）和即将去（橙）高亮
- 轨迹点旁标注海拔（OpenTopoData / SRTM 90m，查一次即缓存）
- 图层：行政区划 / 景区 POI / 卫星影像 / 实时路况 / 照片视频 / 足迹热力图
- 轨迹回放：沿真实路线播放整段旅程，可调速
- 地图全屏、一键全览整条行程
- 我的位置：蓝点 + 精度圈，点按钮回到自己所在位置（走高德定位，坐标系与地图一致）

**照片与视频**

- 上传照片/视频，自动生成缩略图，读取 EXIF 时间与坐标
- 大文件（>8MB）自动走分片上传：多分片并发 + 失败指数退避重传 + 聚合进度环；
  中途断了会在该地点留一个「未完成的上传」占位格，**重新选中同一个文件**即可接着传
  （浏览器不允许网页保存本地文件句柄，所以做不到自动续传）。24 小时没动静的会话
  连同临时分片自动回收（`scripts/prune_upload_sessions.py`）
- 地图上按轨迹点聚成"照片堆"，随缩放级别自动放大缩小
- 相册：整个行程的扁平网格（类 iOS 照片），双指捏合改变每行数量，可按类型筛选、按时间排序
- 点详情：九宫格式媒体网格，长按拖拽排序，格内直接编辑描述
- 保存到本地：灯箱右上角一个下载按钮，照片直接下原图，视频可挑清晰度档位下载。
  文件名按「拍摄日期-地点-序号」拼（`旅行记忆-20260910-京都-01.mp4`）。
  分享页的匿名访客同样能下，权限和「能不能看」一致。
  微信内置浏览器拦下载，那里会退化成「长按保存」的提示，不假装能下
- 灯箱：跟手轮播翻页、点击进入全屏、双指缩放与平移、拖到边缘继续翻页（安卓图库式）
- 每张照片可写一段描述，**支持 Markdown**（粗斜体、列表、引用、链接、行内代码）

**分享**

- 生成只读分享链接，可设有效期与访问密码，可随时撤销
- 分享页与主界面交互对齐（图层、全屏、回放、相册、灯箱），但**不包含**跨行程数据（足迹热力图、全量照片图层不会出现在公开页）
- 生成竖版分享卡片（含二维码），移动端可直接保存到相册

**AI 与开放平台**

- 34 个 MCP 工具，覆盖行程读写、记忆生成、模式发现、年度报告、时光机、回忆唤醒、语义检索
- API Key 管理（scope: read / write / share / admin，可设有效期、可吊销）内置在开发者控制台
- LLM provider 可切换：Claude CLI / Anthropic API / DeepSeek（`TRAVEL_LLM_PROVIDER`）

**移动端体验**

这个项目在移动端交互上花的功夫比功能本身还多：面板拖拽调高、tab 卡片式横滑、行程切换的方向性动画、手势仲裁（长按拖拽 vs 横滑切 tab vs 双指缩放）、浏览器返回拦截（边缘右滑不会退出网站）、深色模式与卫星底图下的图标可读性等等。

### 快速开始

只需要 **Python 3.10+**。两条命令：

```bash
git clone https://github.com/<你的用户名>/travel-memory.git
cd travel-memory

./setup.sh      # 建虚拟环境、装依赖、初始化数据库
./start.sh      # 启动
```

打开 `http://localhost:8084`，注册第一个账号即可开始用。数据库 `data.db` 会自动创建并完成迁移，
不需要手工建表、不需要改配置文件。

**想先看看效果**，造一份演示数据（3 个行程、12 个地点、28 张合成照片、1 段视频、24 条 Markdown 描述）：

```bash
./.venv/bin/python scripts/seed_test_user.py
```

会创建账号 `test` / `test1234`，登录页有一键进入按钮。

**唯一需要手工配的一项：高德地图 Key。** 不配也能启动，行程、照片、分享都正常，
只是地图区域是空白 —— 页面顶部会挂一条提示告诉你去哪配。拿到 Key 之后：

```bash
echo '你的Key'      > amap_key.txt
echo '你的安全密钥'  > amap_security.txt
# 重启即可生效
```

> 可选：`ffmpeg`。装了才能给上传的视频抽封面，照片功能不受影响。
> Debian/Ubuntu `sudo apt install ffmpeg`，macOS `brew install ffmpeg`。

#### 可选的环境变量

一个都不配也能跑，按需再加：

| 变量 | 作用 | 默认 |
|---|---|---|
| `AMAP_APPNAME` | 高德 Key 的白名单 appname，用到「行政区划」图层时才需要，要和高德控制台配的一致 | `http://localhost:8084/` |
| `TRAVEL_LLM_PROVIDER` | AI 功能走哪个模型：`auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | 对应 provider 的密钥，不用 AI 功能就不用配 | 空 |
| `DEMO_USER` | 每日重置的演示账号名（见下） | `test` |

#### 演示账号每日重置（可选）

公开演示账号谁都能改，一天下来数据就乱了。仓库里带了一对 systemd user unit，
让它每天 03:00 恢复出厂状态、当天改动一律不保留：

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

手动跑一次：`python3 scripts/reset_demo.py`。不需要演示账号的话，不装这个 timer 即可。

### 关于高德地图 Key

地图依赖高德 JS API 2.0，需要在 [高德开放平台](https://console.amap.com/dev/key/app) 申请 **Web 端 (JS API)** 类型的 Key，同时拿到配对的**安全密钥 securityJsCode**。

两者都会随页面下发到浏览器（这是 JS API 2.0 的设计），所以：

> ⚠️ **务必在高德控制台给 Key 配置域名白名单**，否则任何人都能拿走你的 Key 消耗配额。

海拔数据来自 [OpenTopoData](https://www.opentopodata.org/) 公开实例（SRTM 90m），限 1000 次/天。点位很少且查过即缓存，正常用不到上限；大批量导入行程时请自建或换数据源。

### 部署

单进程 uvicorn 就够用（SQLite 单写入者）。生产环境建议：

- 前面挂 Nginx / Caddy 处理 TLS
- `systemd` 用户级 service 托管进程
- 备份只需要拿走 `data.db` + `uploads/` + 三个密钥文件这几样

### 项目结构

```
app.py                 FastAPI 应用，95 个 HTTP 端点
db.py                  SQLite 表结构、迁移与全部查询
media.py               缩略图 / EXIF / 视频抽帧
download.py            媒体「另存为」：文件名拼装 + RFC 5987 编码 + quality 白名单
mcp_http.py            MCP Streamable HTTP 端点（34 个工具）
mcp_server.py          MCP stdio 版本
companion.py           伴随进程（定时任务）
domain/                领域逻辑：记忆生成、联想、检索、地理编码、LLM provider、token 鉴权
static/
  index.html app.js    主界面（约 4900 行 JS）
  share.html share.js  只读分享页
  console.html         开发者控制台（API Key 管理）
  style.css
scripts/               种子数据、演示账号重置、密钥扫描等工具脚本
deploy/                可选的 systemd unit 示例（演示账号每日重置）
tests/                 自动化测试（unit / api / e2e 三层）
.githooks/             提交前检查（密钥扫描 + 快速测试）
test.sh setup.sh start.sh   测试 / 一键安装 / 启动
```

### 测试

```bash
./test.sh                # 快速套件：单元 + API，约 10 秒（日常改完代码就跑这个）
./test.sh all            # 全量，含端到端浏览器测试，约 20 秒（提交前 / 收工前）
./test.sh unit           # 只跑单元层
./test.sh api            # 只跑 API 集成层
./test.sh e2e            # 只跑端到端（需要系统 Chrome）
./test.sh -k 关键字 -vv  # 其余参数原样透传给 pytest
./test.sh secrets        # 扫一遍有没有硬编码密钥
./test.sh install-hooks  # 启用提交前检查（新 clone 后跑一次，见下）
```

**新 clone 后请先跑一次 `./test.sh install-hooks`。** git 不会把 `core.hooksPath`
随仓库带过来，不跑这一步的话 `.githooks/` 里的提交前检查（密钥扫描 + 快速套件）
静默不生效。

首次准备：`.venv/bin/pip install -r requirements-dev.txt`（E2E 用系统已装的
`/usr/bin/google-chrome`，不需要 `playwright install` 下载浏览器）。

**测试绝不会碰生产数据。** 全程用临时 SQLite 与临时 uploads 目录，并且
`tests/conftest.py` 劫持了 `sqlite3.connect`：任何测试进程一旦试图打开
生产 `data.db` 会直接抛异常，而不是悄悄写进去。

#### 提交前自动检查

```bash
./test.sh install-hooks     # 新 clone 之后跑一次，之后每次 git commit 自动执行
```

它把 `core.hooksPath` 指向仓库里的 `.githooks/`，于是每次提交会依次跑：

1. `scripts/scan-secrets.sh --staged` —— 扫暂存区有没有硬编码密钥（毫秒级）
2. `./tests/run.sh` —— 快速套件（约 10 秒）；只改文档/图片时自动跳过

任一步没过就拦下提交，并打印**具体是哪个用例、失败原因是什么**。
端到端测试太慢，不进 hook，由每天的定时任务负责。

急事绕过：

| 方式 | 跳过什么 | 提示 |
| --- | --- | --- |
| `git commit --no-verify` | 密钥扫描 + 测试 | 提交后 `post-commit` 打一行醒目警告 |
| `TM_SKIP_TESTS=1 git commit` | 只跳测试，密钥照扫 | 提交时和提交后各提示一次 |

密钥扫描命中时**不要**用 `--no-verify` 硬闯：密钥一旦推上去只能吊销换发。

#### 关于 xfail

带 `@pytest.mark.xfail` 的用例是**已记录在案的已知缺陷**，属于预期状态，
不算失败、不触发告警；数量随缺陷被修复而变化也不算故障。反过来，
如果某条 xfail 用例突然通过了（xpass），说明缺陷多半被顺手修好了，
这时候应该把 xfail 标记摘掉，让它转成正式的回归防线。

### 已知边界

- SQLite 单写入者，不适合高并发写入场景
- 路线规划依赖高德/OSRM，境外部分区域可能只能拿到直线兜底
- 端到端测试跑在系统 Chrome 上（`channel="chrome"`），没装 Chrome 的机器只能跑单元 + API 两层

### 许可

见 [LICENSE](LICENSE)。

---

<a name="english"></a>

## English

### What is this

Travel Memory is a self-hosted, multi-user travel memory system with two faces:

- **For humans**: a mobile-first web app. Pin every stop of a trip on a map, attach photos and videos, write down what you were thinking. Later, replay the whole route, browse the trip's album, or hand a friend a read-only share link.
- **For agents**: an **MCP (Model Context Protocol) Streamable HTTP** endpoint exposing 34 tools. Any remote agent connects with its own API key, fully tenant-isolated. Let an AI read your travel memories, find patterns, write an annual report, or answer "when was I last in Kyoto?"

The stack is deliberately boring: **FastAPI + SQLite + vanilla JavaScript**. No frontend build step, no ORM, no Redis. Clone it, install a handful of Python packages, run it.

### Features

**Trips & map**

- Multiple trips, each an ordered list of points; four statuses (planning / traveling / completed / archived)
- Real driving routes (AMap Driving, OSRM fallback), per-segment clickable with alternate routes, results cached
- Distance labelled at the midpoint of every segment; selecting a point highlights the inbound leg (green) and outbound leg (orange)
- Elevation shown next to each point (OpenTopoData / SRTM 90m, fetched once then cached)
- Layers: administrative boundaries / scenic POIs / satellite imagery / live traffic / photos & videos / footprint heatmap
- Route replay animation along the real route, with speed control
- Map fullscreen, one-tap fit-whole-trip
- My location: blue dot with accuracy ring, one tap to recentre on yourself (uses AMap geolocation so the coordinate system matches the tiles)

**Photos & video**

- Upload photos/videos, automatic thumbnails, EXIF time and coordinates extracted
- Photos cluster into "stacks" at each point on the map, scaling with zoom level
- Album: a flat whole-trip grid (iOS Photos style), pinch to change columns, filter by type, sort by time
- Point detail: a WeChat-moments-style grid — long-press to drag-reorder, edit a caption right in the cell
- Save to device: a download button in the lightbox corner — photos download the original,
  videos let you pick a quality. Filenames are built from date/place/index
  (`旅行记忆-20260910-京都-01.mp4`). Anonymous share-page visitors can download too,
  with exactly the same permissions as viewing. WeChat's in-app browser blocks downloads,
  so there it degrades to a "long-press to save" hint rather than pretending
- Lightbox: finger-tracking carousel, tap for fullscreen, pinch zoom and pan, drag past the edge to page (Android gallery behaviour)
- Every photo can carry a caption **with Markdown support** (bold/italic, lists, quotes, links, inline code)

**Sharing**

- Read-only share links with optional expiry and password, revocable at any time
- The share page mirrors the main UI (layers, fullscreen, replay, album, lightbox) but deliberately **excludes cross-trip data** — the footprint heatmap and the global photo layer never appear on a public page
- Generates a portrait share card with a QR code; on mobile it saves straight to the photo library

**AI & open platform**

- 34 MCP tools covering trip CRUD, memory generation, pattern discovery, annual reports, time travel, memory recall, semantic search
- API key management (scopes: read / write / share / admin, with expiry and revocation) built into the developer console
- Pluggable LLM provider: Claude CLI / Anthropic API / DeepSeek (`TRAVEL_LLM_PROVIDER`)

**Mobile polish**

More effort went into mobile interaction than into features: draggable panel height, card-carousel tab swiping, directional trip-switch animation, gesture arbitration (long-press drag vs. tab swipe vs. pinch zoom), back-navigation interception (edge swipe won't drop you out of the site), and icon legibility under dark mode and satellite basemaps.

### Quick start

All you need is **Python 3.10+**. Two commands:

```bash
git clone https://github.com/<you>/travel-memory.git
cd travel-memory

./setup.sh      # create venv, install deps, initialise the database
./start.sh      # run
```

Open `http://localhost:8084` and register the first account. `data.db` is created and
migrated automatically — no schema setup, no config files to edit.

**Want to see it populated first?** Seed demo data (3 trips, 12 points, 28 synthetic
photos, 1 video, 24 Markdown captions):

```bash
./.venv/bin/python scripts/seed_test_user.py
```

That creates account `test` / `test1234` with a one-tap login button.

**The one thing you must configure by hand: an AMap key.** It starts fine without one —
trips, photos and sharing all work — the map area is just blank, and a banner at the top
of the page tells you where to put the key. Once you have it:

```bash
echo 'your-key'            > amap_key.txt
echo 'your-security-code'  > amap_security.txt
# restart to apply
```

> Optional: `ffmpeg`. Only needed to extract poster frames from uploaded videos;
> photos work regardless. `sudo apt install ffmpeg` / `brew install ffmpeg`.

#### Optional environment variables

None are required. Add them as you need them:

| Variable | Purpose | Default |
|---|---|---|
| `AMAP_APPNAME` | Whitelist appname for your AMap key; only needed for the administrative-boundary layer, must match what you set in the AMap console | `http://localhost:8084/` |
| `TRAVEL_LLM_PROVIDER` | Which model backs the AI features: `auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | Key for the chosen provider; unnecessary if you don't use AI features | empty |
| `DEMO_USER` | Account name the daily reset targets (see below) | `test` |

#### Daily demo-account reset (optional)

A public demo account gets edited by everyone; after a day the data is a mess. The repo
ships a pair of systemd user units that restore it to factory state at 03:00 daily —
nothing from that day is kept:

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

Run it once by hand: `python3 scripts/reset_demo.py`. Don't install the timer if you
don't want a demo account.

### About the AMap key

The map uses AMap JS API 2.0. Get a **Web (JS API)** key from the [AMap console](https://console.amap.com/dev/key/app) along with its paired **securityJsCode**.

Both are delivered to the browser (that's how JS API 2.0 works), so:

> ⚠️ **Configure a domain allowlist for your key in the AMap console**, or anyone can lift it and burn your quota.

Elevation comes from the public [OpenTopoData](https://www.opentopodata.org/) instance (SRTM 90m), rate-limited to 1000 calls/day. Points are few and results are cached, so you won't hit it in normal use; self-host or swap the source if you bulk-import trips.

### Deployment

A single uvicorn process is enough (SQLite is single-writer). For production:

- Put Nginx / Caddy in front for TLS
- Run it under a user-level `systemd` service
- A backup only needs to capture `data.db` + `uploads/` + the three key files

### Layout

```
app.py                 FastAPI app, 95 HTTP endpoints
db.py                  SQLite schema, migrations and every query
media.py               thumbnails / EXIF / video frame extraction
download.py            media "save as": filename building, RFC 5987, quality whitelist
mcp_http.py            MCP Streamable HTTP endpoint (34 tools)
mcp_server.py          MCP stdio variant
companion.py           companion process (scheduled jobs)
domain/                domain logic: memory generation, association, search,
                       geocoding, LLM provider, token auth
static/
  index.html app.js    main UI (~4,900 lines of JS)
  share.html share.js  read-only share page
  console.html         developer console (API key management)
  style.css
scripts/               seeding, demo reset, secret scanning and utilities
deploy/                optional systemd unit samples (daily demo reset)
tests/                 automated tests (unit / api / e2e)
.githooks/             pre-commit checks (secret scan + fast suite)
test.sh setup.sh start.sh   test / one-command install / run
```

### Tests

```bash
./test.sh                # fast suite: unit + API, ~10s (run this after any change)
./test.sh all            # everything incl. end-to-end browser tests, ~20s
./test.sh unit           # unit layer only
./test.sh api            # API integration layer only
./test.sh e2e            # end-to-end only (needs system Chrome)
./test.sh -k keyword -vv # remaining args are passed straight to pytest
./test.sh secrets        # scan for hard-coded credentials
./test.sh install-hooks  # enable the pre-commit checks (run once per clone)
```

**Run `./test.sh install-hooks` once after cloning.** Git does not carry
`core.hooksPath` across a clone, so without this step the pre-commit checks
(secret scan + fast suite) silently never run.

One-time setup: `.venv/bin/pip install -r requirements-dev.txt`. E2E reuses the
system `/usr/bin/google-chrome` via Playwright's `channel="chrome"`, so there is
no `playwright install` download step.

**Tests never touch production data.** Everything runs against a temporary
SQLite file and a temporary uploads directory, and `tests/conftest.py` patches
`sqlite3.connect` so that any attempt to open the production `data.db` raises
instead of silently writing to it.

#### Pre-commit checks

```bash
./test.sh install-hooks     # run once after cloning
```

This points `core.hooksPath` at the in-repo `.githooks/`. Every commit then runs:

1. `scripts/scan-secrets.sh --staged` — hard-coded credentials in staged lines
2. `./tests/run.sh` — the fast suite (~10s), skipped for docs-only commits

Either failure blocks the commit and prints **which test failed and why**. E2E is
too slow for a hook; the nightly job covers it.

Escape hatches: `git commit --no-verify` skips both (a `post-commit` hook prints a
loud warning afterwards), `TM_SKIP_TESTS=1 git commit` skips only the tests and
still scans for secrets. Never force past a secret-scan hit — a pushed credential
has to be revoked and rotated.

#### About xfail

Tests marked `@pytest.mark.xfail` are documented known defects. They are an
expected state: never counted as failures, never alerted on, and the count
changing as defects get fixed is not an incident. If an xfail test starts
passing (xpass), the defect was probably fixed — drop the marker so the test
becomes a real regression guard.

### Known limits

- SQLite is single-writer; not built for write-heavy concurrency
- Routing depends on AMap/OSRM; some regions outside China fall back to straight lines
- End-to-end tests drive the system Chrome (`channel="chrome"`); without it only the unit and API layers can run

### License

See [LICENSE](LICENSE).
