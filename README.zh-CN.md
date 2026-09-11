<div align="center">

# 🧭 Travel Memory · 旅行记忆

**把一次旅行沉淀成可以反复回看的记忆 —— 地图、照片、路线、文字，以及一套对 AI Agent 开放的记忆 API。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-34%20tools-7c3aed)

[English](README.md) · **中文**

</div>

---

## 这是什么

Travel Memory 是一个自托管的多用户旅行记忆系统。它有两副面孔：

- **对人**：一个移动端优先的 Web 应用。在地图上记录行程的每一个落脚点，挂上照片和视频，写下当时的想法；之后可以回放整条轨迹、按行程浏览相册、生成只读分享链接发给朋友。
- **对 Agent**：一套 **MCP (Model Context Protocol) Streamable HTTP** 端点，34 个工具，任意远程 Agent 用自己的 API Key 接入，多租户隔离。可以让 AI 读你的旅行记忆、发现模式、生成年度报告、回答「我上次去京都是什么时候」。

技术栈刻意保持简单：**FastAPI + SQLite + 原生 JavaScript**。没有前端构建步骤，没有 ORM，没有 Redis，`git clone` 之后装几个 Python 包就能跑。

## 功能

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
  传输途中可以**暂停 / 继续 / 取消**，也可以直接播放本地那份文件先看看内容；
  中途断了会在该地点留一个「未完成的上传」占位格，**重新选中同一个文件**即可接着传
  （浏览器不允许网页保存本地文件句柄，所以做不到自动续传）。24 小时没动静的会话
  连同临时分片自动回收（`scripts/prune_upload_sessions.py`）
- **视频清晰度分层**：上传后台把原片重封装成 faststart，并按短边转出 1080p / 720p / 480p
  三档（只降不升）。播放器按网络 + 屏幕 + DPR 自动选档，也可以手动切、记住选择；
  转码期间档位灰显标「转码中」，转好自动刷出来
- 地图上按轨迹点聚成「照片堆」，随缩放级别自动放大缩小；点旁的 📷 角标带媒体数量，
  上传/删除后立刻跟着变
- 相册：整个行程的扁平网格（类 iOS 照片），双指捏合改变每行数量，可按类型筛选、按时间排序
- 点详情：九宫格式媒体网格，长按拖拽排序，格内直接编辑描述
- 保存到本地：灯箱右上角一个下载按钮，照片直接下原图，视频可挑清晰度档位下载。
  文件名按「拍摄日期-地点-序号」拼（`旅行记忆-20260910-京都-01.mp4`）。
  分享页的匿名访客同样能下，权限和「能不能看」一致。
  微信内置浏览器拦下载，那里会退化成「长按保存」的提示，不假装能下
- 灯箱：跟手轮播翻页、点击进入全屏、双指缩放与平移、拖到边缘继续翻页（安卓图库式）；
  控件（关闭/下载/清晰度/翻页）几秒无操作自动淡出，点一下画面就回来
- 每张照片可写一段描述，**支持 Markdown**（粗斜体、列表、引用、链接、行内代码）

**分享**

- 生成只读分享链接，可设有效期与访问密码，可随时撤销
- 分享页与主界面交互对齐（图层、全屏、回放、相册、灯箱），但**不包含**跨行程数据（足迹热力图、全量照片图层不会出现在公开页）
- 生成竖版分享卡片（含二维码），移动端可直接保存到相册

**AI 与开放平台**

- **34 个 MCP 工具**，覆盖行程读写、记忆生成、模式发现、年度报告、时光机、回忆唤醒、语义检索。
  工具清单、参数 schema、权限分级和**每一个工具能不能真跑通**都有自动化测试盯着
  （`./test.sh -k mcp`），改坏了当场就红
- API Key 管理（scope: read / write / share / admin，可设有效期、可吊销）内置在开发者控制台
- LLM provider 可切换：Claude CLI / Anthropic API / DeepSeek（`TRAVEL_LLM_PROVIDER`）

**移动端体验**

这个项目在移动端交互上花的功夫比功能本身还多：面板拖拽调高、tab 卡片式横滑、行程切换的方向性动画、手势仲裁（长按拖拽 vs 横滑切 tab vs 双指缩放）、浏览器返回拦截（边缘右滑不会退出网站）、顶部两条可整体收起把高度让给地图、深色模式与卫星底图下的图标可读性等等。

## 快速开始

只需要 **Python 3.10+**。两条命令：

```bash
git clone https://github.com/shihezichen/travel-memory.git
cd travel-memory

./setup.sh      # 建虚拟环境、装依赖、初始化数据库、造一份演示数据
./start.sh      # 启动（开发用，见下面「运行方式」）
```

打开 `http://localhost:8084`。数据库 `data.db` 会自动创建并完成迁移，
不需要手工建表、不需要改配置文件。

`setup.sh` 顺手造好了一份演示数据（3 个行程、12 个地点、28 张合成照片、1 段视频、
24 条 Markdown 描述），账号 **`test` / `test1234`**，登录页有一键进入按钮 ——
不用先自己录一趟旅行才能看出这东西长什么样。不想要演示数据就跑 `./setup.sh --no-demo`；
之后想补上或者恢复出厂状态，跑 `./setup.sh --demo`。

**唯一需要手工配的一项：高德地图 Key。** 不配也能启动，行程、照片、分享都正常，
只是地图区域是空白 —— 页面顶部会挂一条提示告诉你去哪配。拿到 Key 之后：

```bash
echo '你的Key'      > amap_key.txt
echo '你的安全密钥'  > amap_security.txt
# 重启即可生效
```

> 可选：`ffmpeg`。装了才能给上传的视频抽封面、转清晰度档位，照片功能不受影响。
> Debian/Ubuntu `sudo apt install ffmpeg`，macOS `brew install ffmpeg`。

### 可选的环境变量

一个都不配也能跑，按需再加：

| 变量 | 作用 | 默认 |
|---|---|---|
| `AMAP_APPNAME` | 高德 Key 的白名单 appname，用到「行政区划」图层时才需要，要和高德控制台配的一致 | `http://localhost:8084/` |
| `TRAVEL_BASE_URL` | MCP 端点回调自己领域 API 的地址，**必须和进程真正监听的协议/端口一致**（见「运行方式」） | `http://127.0.0.1:8084` |
| `TRAVEL_LLM_PROVIDER` | AI 功能走哪个模型：`auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | 对应 provider 的密钥，不用 AI 功能就不用配 | 空 |
| `TRAVEL_MEDIA_BASE_URL` | 媒体分发前缀（对象存储 / CDN 的 origin）。留空 = 媒体走本机 `/uploads/` | 空（也可写进 `media_base.txt`） |
| `DEMO_USER` | 每日重置的演示账号名（见下） | `test` |

### 演示账号每日重置（可选）

公开演示账号谁都能改，一天下来数据就乱了。仓库里带了一对 systemd user unit，
让它每天 03:00 恢复出厂状态、当天改动一律不保留：

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

手动跑一次：`./.venv/bin/python scripts/reset_demo.py`。不需要演示账号的话，不装这个 timer 即可。

## 关于高德地图 Key

地图依赖高德 JS API 2.0，需要在 [高德开放平台](https://console.amap.com/dev/key/app) 申请 **Web 端 (JS API)** 类型的 Key，同时拿到配对的**安全密钥 securityJsCode**。

两者都会随页面下发到浏览器（这是 JS API 2.0 的设计），所以：

> ⚠️ **务必在高德控制台给 Key 配置域名白名单**，否则任何人都能拿走你的 Key 消耗配额。

海拔数据来自 [OpenTopoData](https://www.opentopodata.org/) 公开实例（SRTM 90m），限 1000 次/天。点位很少且查过即缓存，正常用不到上限；大批量导入行程时请自建或换数据源。

## 运行方式

`./start.sh` 只适合「先看一眼」：明文 HTTP、终端一关就停。
**从开发环境起就按下面这套来**，不要等上生产再补 —— 理由不是仪式感，是这三件事在开发阶段就会咬人：

1. **没有 TLS，一半功能是残的。** 浏览器把「剪贴板 API、地理定位、`crypto.subtle`」
   全部锁在安全上下文（https 或 localhost）里。用手机连局域网 IP 调试时，
   复制分享链接、定位到我的位置、上传时算文件指纹全都会走降级分支 ——
   你调的是降级路径，用户跑的是正常路径，两边行为不一样，bug 只会在上线后出现。
2. **没有进程托管，改一行代码就得手动重启。** 服务挂了没人拉起来，
   跑到一半的转码任务也跟着没了。
3. **两套环境不一致，本身就是 bug 的来源。** 生产上是 uvicorn 监听回环 + 反代终结 TLS，
   开发上是 `0.0.0.0:8084` 明文，路径前缀、`X-Forwarded-*`、Cookie 的 `Secure` 属性
   在两边表现都不一样。

### 1. systemd user service 托管进程

```bash
cp deploy/travel-memory.service ~/.config/systemd/user/
# 按注释改里面的三个占位值（AMAP_APPNAME / LLM provider / 路径）
systemctl --user daemon-reload
systemctl --user enable --now travel-memory
systemctl --user status travel-memory
loginctl enable-linger "$USER"      # 没登录时也保持运行
```

日志：`journalctl --user -u travel-memory -f`。

> `deploy/` 下还有 9 组 unit：证书续期、演示账号重置、每日回忆推送、上传会话回收、
> 健康检查、定时全量测试、媒体同步到对象存储等。都是可选的，按需拷。

### 2. 前面挂 Caddy / Nginx 处理 TLS

`deploy/Caddyfile.example` 是一份能直接用的最小配置（Caddy 会自动签发和续期证书）：

```bash
cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # 改成你的域名
sudo systemctl reload caddy
```

本机开发拿不到公网域名时，用 Caddy 的内部 CA 给 `localhost` / 局域网 IP 签一张自签证书
（`tls internal`），把根证书装进手机，就能在真机上跑完整的 https 路径 —— 见示例文件里的注释。

**MCP 用户注意**：应用换到 TLS 端口之后，`/mcp` 回调自己领域 API 的地址也要跟着改，
否则 34 个工具会全部报「领域 API 不可达」：

```ini
Environment=TRAVEL_BASE_URL=https://127.0.0.1:8443
```

（回环地址的证书主机名对不上是正常的，`mcp_http.py` 只对回环跳过校验。）

### 3. 备份

拿走这几样就够了：`data.db` + `uploads/` + 三个密钥文件（`amap_key.txt`、
`amap_security.txt`、`token.txt`）。`./backup.sh` 帮你打包，用的是 SQLite 的
`backup()` API，WAL 下也能拿到一致性快照。

### 4. 媒体走对象存储（可选，但强烈建议）

默认所有图片/视频都由应用进程从 `/uploads/` 分发。这条路会和 API 抢同一条出口带宽，
如果你的公网入口是隧道（frp / cloudflared）就更明显 —— 一个人看视频，全站都卡。

把对象存储的 origin 填进 `media_base.txt`（或 `TRAVEL_MEDIA_BASE_URL`），
所有 `/uploads/` 链接改指对象存储，媒体流量彻底绕开应用进程：

```bash
echo 'https://your-bucket.oss-cn-qingdao.aliyuncs.com' > media_base.txt
```

这是**一个开关**：置空即完整回退到本机分发，不需要改任何其它代码。
`deploy/travel-memory-oss-sync.{service,timer}` 负责把新文件同步上去。
注意 `/static/` **不会**进对象存储 —— 前端 JS/CSS 必须跟着代码版本走。

## 项目结构

```
app.py                 FastAPI 应用与 Web UI 路由（90 条路径 / 105 个操作）
db.py                  SQLite 表结构、迁移与全部查询（WAL）
config.py              跨模块共享的静态配置（高德 Key、OSRM、媒体分发前缀）
media.py               缩略图 / EXIF / 视频抽帧
transcode.py           视频清晰度分层转码（faststart + 1080p/720p/480p，后台线程）
download.py            媒体「另存为」：文件名拼装 + RFC 5987 编码 + quality 白名单
mcp_http.py            MCP Streamable HTTP 端点（34 个工具 → 领域 API 的路由表）
mcp_server.py          MCP stdio 版本
companion.py           伴随进程（定时任务）+ 工具清单（TOOLS）的唯一定义处
routers/
  agent_api.py         领域 API（/api/ai/*）+ /mcp —— 走 API Key，和 Web UI 完全不相交
  console.py           开发者控制台（API Key 管理），走 session
  share.py             只读分享页与其匿名接口
  deps.py              路由间共享的请求级辅助函数
domain/                领域逻辑：记忆生成、联想、检索、地理编码、LLM provider、限流、鉴权
static/
  index.html app.js    主界面（约 5,600 行 JS）
  share.html share.js  只读分享页
  common.js            两个页面共用：地图图层、灯箱、清晰度选择、下载、Markdown
  console.html         开发者控制台
  style.css
scripts/               种子数据、演示账号重置、密钥扫描等工具脚本
deploy/                systemd unit 示例（11 个 service + 9 个 timer）+ Caddyfile 示例
tests/                 自动化测试（unit / api / e2e 三层）
.githooks/             提交前检查（密钥扫描 + 快速测试）
test.sh setup.sh start.sh   测试 / 一键安装 / 启动
```

## 测试

```bash
./test.sh                # 快速套件：单元 + API，约 20 秒（日常改完代码就跑这个）
./test.sh all            # 全量，含端到端浏览器测试，约 90 秒（提交前 / 收工前）
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

### MCP 工具的回归

`/mcp` 到领域 API 之间隔着一张手写的路由表（`mcp_http.TOOL_ROUTES`），
它和 `routers/agent_api.py` 里的真实路由之间**没有任何编译期约束**：
改个端点路径、换个字段名、调一次 scope，工具就哑了，而 Web UI 一点事都没有
（前端从不调 `/api/ai/*`）。所以 `tests/api/test_mcp_tools.py` 做三件事：

1. 工具清单（`companion.TOOLS`）和路由表两边不许有孤儿，参数 schema 不许漏字段；
2. 路由表指向的每个 `(method, path)` 必须真的注册在 FastAPI 上；
3. **34 个工具逐个真跑一遍** `tools/call`，走完整链路（鉴权 → scope → 参数拼装 →
   领域 API → 结果），任何一个报 `isError` 都算回归。

```bash
./test.sh -k mcp
```

### 提交前自动检查

```bash
./test.sh install-hooks     # 新 clone 之后跑一次，之后每次 git commit 自动执行
```

它把 `core.hooksPath` 指向仓库里的 `.githooks/`，于是每次提交会依次跑：

1. `scripts/scan-secrets.sh --staged` —— 扫暂存区有没有硬编码密钥（毫秒级）
2. `./tests/run.sh` —— 快速套件；只改文档/图片时自动跳过

任一步没过就拦下提交，并打印**具体是哪个用例、失败原因是什么**。
端到端测试太慢，不进 hook，由每天的定时任务负责。

急事绕过：

| 方式 | 跳过什么 | 提示 |
| --- | --- | --- |
| `git commit --no-verify` | 密钥扫描 + 测试 | 提交后 `post-commit` 打一行醒目警告 |
| `TM_SKIP_TESTS=1 git commit` | 只跳测试，密钥照扫 | 提交时和提交后各提示一次 |

密钥扫描命中时**不要**用 `--no-verify` 硬闯：密钥一旦推上去只能吊销换发。

### 关于 xfail

带 `@pytest.mark.xfail` 的用例是**已记录在案的已知缺陷**，属于预期状态，
不算失败、不触发告警；数量随缺陷被修复而变化也不算故障。反过来，
如果某条 xfail 用例突然通过了（xpass），说明缺陷多半被顺手修好了，
这时候应该把 xfail 标记摘掉，让它转成正式的回归防线。

## 已知边界

- **SQLite 单写入者。** 开了 WAL，读写不再互斥、写锁只在提交瞬间持有，
  个人/小团队用完全够；但写并发很高的场景（几十个人同时录行程）不适合，
  那种量级该换 Postgres。
- 媒体默认由应用进程分发，会和 API 抢带宽。介意的话把 `media_base.txt`
  指向对象存储（见「运行方式 · 4」），这条限制随之消失。
- 视频转码是本机 ffmpeg 后台线程，一次只转一条；批量导入几十个视频要等一会儿。
  没装 ffmpeg 则只有原画可播，功能不受影响。
- 路线规划依赖高德/OSRM，境外部分区域可能只能拿到直线兜底。
- 端到端测试跑在系统 Chrome 上（`channel="chrome"`），没装 Chrome 的机器只能跑单元 + API 两层。

## 许可

[MIT](LICENSE)。随便用、随便改、随便商用，保留版权声明即可。
