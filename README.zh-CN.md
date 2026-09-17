<div align="center">

# 🧭 Travel Memory · 旅行记忆

**把一次旅行沉淀成可以反复回看的记忆 —— 地图、照片、路线、文字，以及一套对 AI Agent 开放的记忆 API。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-7c3aed)

[English](README.md) · **中文**

</div>

---

## 这是什么

旅行结束一个月，照片散在相册里几千张，路线只剩一点印象，当时想什么早忘光了。
Travel Memory 想解决的就是这件事：**把一趟旅行完整地存下来，并且随时能重新走一遍。**

它是自托管的 —— 数据在你自己的机器上，不上传给任何人。装好之后，你有一个手机上
顺手的网页应用，和一个可以让 AI 助手直接访问这些记忆的接口。

## 你能用它做什么

### 📍 把一趟旅行记下来

在地图上按顺序点出每个落脚点，系统自动算出**真实的驾车路线**（不是两点连直线），
每段标好里程，点旁边标好海拔。一趟旅行可以分成「计划中 / 进行中 / 已完成 / 已归档」
几个阶段，出发前先把想去的地方摆上去，路上再慢慢改。

路线不是「算一条就完事」：一段路经过几条国道省道，就把几个编号都标出来；
高德因为封路、限行之类的原因绕了远路时（比如独库公路每年十月封山，
那拉提→库车会被规划成 1361 公里），系统会自己去要一条不看封路的路线摆进备选，
明显更短就默认选它 —— 记录型产品该以**你实际走过的路**为准。
选定的方案会存下来，下次打开还是它；坐标改了才重新算。

每个地点都能挂照片和视频。手机直接拍完就传，照片里的**拍摄时间和坐标会自动读出来**。
传大视频不用守着 —— 中途可以暂停、切出去干别的、回来接着传，断网了重新选同一个文件
就从断点继续。传的同时就能点开看本地那份，不用等。

每张照片下面可以写一段话，支持 Markdown，所以你可以认真写，而不只是配个标题。

### 🔁 回过头重新走一遍

- **轨迹回放**：沿着真实路线把整趟旅行播一遍，可以调速 —— 比看一堆静态照片更能唤起当时的感觉
- **相册**：整趟旅行的照片平铺成一整面墙（类似 iOS 相册），双指捏合改变每行几张，可按时间排序、按类型筛
- **照片墙自动播放**：相册页和地点详情页各有一枚 ▶，按下去整面墙自己动起来 ——
  随机挑一格，选中、原地放大、铺满全屏、再缩回去，一张接一张。当背景放着看就行，
  挑到视频会自动播，右下角有一枚自绘的全屏钮（各端浏览器原生控制条的全屏钮差别太大，
  自己做一个才能保证「一定有、一定在同一个位置」）
- **照片堆**：地图上每个地点自动聚成一小堆照片，缩放时跟着变大变小 —— 一眼看出哪儿拍得多。
  视频也在里面，用首帧当缩略图
- **足迹热力图**：所有行程叠在一起，这些年到底走过哪些地方
- **看图**：跟手翻页、点一下全屏、双指放大、拖到边缘继续翻，和手机自带图库一个手感
- **存回本地**：照片下原图，视频可以挑清晰度，文件名自动按「日期-地点-序号」拼好

### 👥 分享给朋友

生成一条**只读链接**发出去，对方不用注册就能看，交互和你自己看时一样（图层、回放、相册都在）。
可以设有效期和访问密码，随时撤销。也可以生成一张竖版分享卡片（带二维码），
手机上直接存进相册发朋友圈。

分享页**只包含这一趟行程** —— 你的足迹热力图、其它旅行的照片不会跟着漏出去。

页面上的图片和视频链接是**带签名和有效期的临时链接**（默认 30 分钟），
所以媒体放对象存储时那个桶可以关掉公共读，不会出现「链接被转手一次就永久公开」。
有效期到了不用你刷新页面：图片会重新取一批新签名，视频在过期前 5 分钟自己续签
并保持播放进度。分享链接本身不受影响，发群里过两周还是能点开。

### 🤖 让你的 AI 助手替你翻记忆

把 Claude、Cursor 或任何支持 **MCP** 的客户端接上之后，你可以直接这样问它：

> 「我上次去京都是什么时候？那天住哪儿？」
> 「这两年我一共去过多少个城市，最常去的是哪个？」
> 「把上周那趟的照片按天理一下，每天帮我写段小结。」
> 「给我 2026 年的旅行写一份年度报告。」
> 「今天 9 月 11 日，往年的今天我在哪儿？」

Agent 读到的是**你自己的**数据：每把 Key 只能看到签发它的那个账号，权限分成
只读 / 可写 / 可分享 / 管理员四档，可以设有效期，随时吊销。

Agent 取到的媒体链接同样是签名临时链接（默认 24 小时，比网页那档长 ——
Agent 常常是先列一遍、过一会儿才去下载）。

接入方式：登录后进开发者控制台签一把 API Key，填进你的 MCP 客户端就行，
详细步骤见 [MCP-ACCESS.md](MCP-ACCESS.md)。控制台在手机上是另一套形态
（底部常驻 Tab + 卡片列表，Key 卡片左滑删除），Key 可以设有效期、列表里看得到
还剩几天，调用日志能看出请求来自哪儿。**Key 只存 sha256**，签发那一次之后
系统自己也读不出原文，弄丢了只能重签一把。

### 📱 关于手机

这个项目在手机端交互上花的功夫比功能本身还多：面板可以拖着调高矮、行程之间横滑切换
带方向感、长按拖拽排序和横滑切页不会打架、浏览器返回不会把你甩出网站、
顶栏可以整条收起把屏幕让给地图、深色模式和卫星底图下图标依然看得清。

## 快速开始

需要 **Python 3.10+**，然后一条命令：

```bash
git clone https://github.com/shihezichen/travel-memory.git
cd travel-memory
./start.sh
```

打开 `http://localhost:8084` 就能用了。三个入口：

| 路径 | 给谁看 |
|---|---|
| `/` | 按登录态分流：没登录的访客看公开宣传页（一段 60 秒演示视频 + 五屏轮播），登录过的直接进应用 |
| `/app` | 应用本体的固定入口，没登录就弹登录浮层 |
| `/welcome` | 宣传页的固定入口，登录状态下也想再看一眼介绍时走这里 |

（分流刻意不用 302：跳转会在浏览器历史里多留一格，退出登录后按后退就在两个页面之间来回弹。）

首次运行会自动建好虚拟环境、装依赖、初始化数据库；之后再跑 `./start.sh` 会发现
环境已经齐了，直接启动。数据库不需要你手工建表，也没有配置文件要改。

第一次启动还会**自带一份演示数据**（3 个行程、18 个地点、55 张照片、6 段视频），
账号 `test` / `test1234`，登录页上有一键进入的按钮 —— 不用先自己录一趟旅行，
打开就能看出这东西长什么样。

## 唯一需要手工配的：高德地图 Key

不配也能启动，行程、照片、分享全都正常，只是地图区域是空白的，页面顶部会挂一条提示。
配好之后地图就出来了：

1. 去 [高德开放平台](https://console.amap.com/dev/key/app) 申请一个 **Web 端 (JS API)** 类型的 Key，
   同时会给你一个配对的**安全密钥 securityJsCode**；
2. 分别写进两个文件，重启生效：

```bash
echo '你的Key'      > amap_key.txt
echo '你的安全密钥'  > amap_security.txt
```

> ⚠️ **一定要在高德控制台给这把 Key 配域名白名单。** JS API 2.0 的设计就是把 Key 和安全密钥
> 一起下发到浏览器，不配白名单的话任何人都能拿走它消耗你的配额。

> 可选：装个 `ffmpeg`（`sudo apt install ffmpeg` / `brew install ffmpeg`）。
> 装了才能给视频抽封面、转出多档清晰度；照片功能不受影响。

> 海拔数据来自 [OpenTopoData](https://www.opentopodata.org/) 的公开实例（SRTM 90m），
> 每天 1000 次。点位查一次就缓存，正常用远到不了上限；要批量导入几百个点的话请自建或换源。

## 让它一直跑着

上面那条 `./start.sh` 适合「先看一眼」：明文 HTTP，终端一关就停。
真正用起来之后，**建议从一开始就按下面这套来**，不要等「上生产」再补 ——
理由不是仪式感，是这几件事在自己用的阶段就会咬人：

1. **没有 TLS，一半功能是残的。** 浏览器把剪贴板、地理定位、`crypto.subtle` 这些
   全锁在安全上下文（https 或 localhost）里。手机连局域网 IP 访问时，复制分享链接、
   定位到我的位置、上传算文件指纹全都会走降级分支。
2. **没有进程托管，机器一重启站就没了**，跑到一半的视频转码也跟着没。
3. **两套环境不一致本身就是 bug 来源** —— Cookie 的 `Secure`、`X-Forwarded-*`、
   路径前缀在明文和 TLS 下表现都不一样。

### 1. 用 systemd 托管进程

```bash
cp deploy/travel-memory.service ~/.config/systemd/user/
# 按注释改里面的三个占位值（AMAP_APPNAME / LLM provider / 路径）
systemctl --user daemon-reload
systemctl --user enable --now travel-memory
loginctl enable-linger "$USER"      # 没登录时也保持运行
```

看日志：`journalctl --user -u travel-memory -f`。

> `deploy/` 下还有 9 组可选 unit：证书续期、演示账号每日重置、每日回忆推送、
> 上传会话回收、健康检查、定时全量测试、媒体同步到对象存储等，按需拷。

### 2. 前面挂 Caddy / Nginx 处理 TLS

`deploy/Caddyfile.example` 是一份能直接用的最小配置，Caddy 会自动签发和续期证书：

```bash
cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # 改成你的域名
sudo systemctl reload caddy
```

本机开发拿不到公网域名时，用 Caddy 的内部 CA 给 `localhost` / 局域网 IP 签一张自签证书
（`tls internal`），把根证书装进手机，真机上也能跑完整的 https 路径 —— 见示例文件里的注释。

**接了 MCP 的注意**：应用换到 TLS 端口之后，`/mcp` 回调自己领域 API 的地址也要跟着改，
否则所有工具会报「领域 API 不可达」：

```ini
Environment=TRAVEL_BASE_URL=https://127.0.0.1:8443
```

（回环地址的证书主机名对不上是正常的，`mcp_http.py` 只对回环跳过校验。）

### 3. 备份

拿走这几样就够了：`data.db` + `uploads/` + 三个密钥文件（`amap_key.txt`、
`amap_security.txt`、`token.txt`）。`./backup.sh` 帮你打包，用的是 SQLite 的
`backup()` API，WAL 下也能拿到一致性快照。

## 可选：让照片和视频更快

自己一个人在局域网里用，这一节可以完全跳过。
如果你把站点发到公网、还要分享给别人看，那**带宽**会是唯一真正卡住体验的东西。

### 下行：媒体走对象存储

默认所有图片和视频都由应用进程自己发，会和 API 抢同一条出口带宽 ——
公网入口是隧道（frp / cloudflared）时尤其明显：一个人在看视频，全站都跟着卡。

把对象存储的 origin 填进 `media_base.txt`，所有媒体链接立刻改指对象存储：

```bash
echo 'https://your-bucket.oss-cn-qingdao.aliyuncs.com' > media_base.txt
```

这是**一个开关**：置空就完整回退到本机分发，不用改任何代码。
`deploy/travel-memory-oss-sync.{service,timer}` 负责把新文件同步上去。
注意 `/static/` **不会**进对象存储 —— 前端 JS/CSS 必须跟着代码版本走。

**桶不需要开公共读。** 所有对外的媒体链接都由服务端现签，带 Expires：
网页和分享页那档默认 30 分钟（`TRAVEL_MEDIA_URL_TTL`），Agent 那档默认 24 小时
（`TRAVEL_AGENT_URL_TTL`）。过期由前端自愈，看的人察觉不到（见上面「分享给朋友」）。
没配 OSS 时签名这一层自动跳过，链接回落到站内 `/uploads/`。

### 上行：上传直接传给对象存储

上一节解决的是别人看你的照片，上行同样堵：浏览器把文件传到服务器，全程挤在同一条
出口带宽上，一段 150MB 的视频理论最快也要好几分钟，期间整站 API 跟着排队。

打开直传之后，浏览器把分片**直接 PUT 给对象存储**，服务端只签名和收尾，一个字节的
媒体都不经手；原件由后台队列慢慢拉回本机做缩略图、转码和备份
（`oss_backfill.py`，并发度 1，不跟任何人抢带宽）。

需要三样东西：

1. `media_base.txt` 已经填好（和上一节同一个开关，桶名/endpoint 从它反推）；
2. AK/SK —— 默认读 `~/.ossutilconfig`，也可以用 `TRAVEL_OSS_AK` / `TRAVEL_OSS_SK` 覆盖；
3. **桶开 CORS**。这是唯一的前置条件：

最省事的是去控制台点：**OSS → 选中桶 → 权限控制 → 跨域设置 → 创建规则**，
来源填站点域名，方法勾 `PUT` / `GET` / `HEAD`，Header 填 `*`，其余默认。

命令行等价写法（需要一把有 `oss:PutBucketCors` 权限的 AK，受限子账号会被拒）：

```bash
cat > cors.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>https://你的域名</AllowedOrigin>
    <AllowedMethod>PUT</AllowedMethod>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>HEAD</AllowedMethod>
    <AllowedHeader>*</AllowedHeader>
    <MaxAgeSeconds>600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>
XML
ossutil cors --method put oss://你的桶 cors.xml

# 验证：应当返回 200，而不是 "CORS is not enabled for this bucket"
curl -i -X OPTIONS "https://你的桶.oss-cn-qingdao.aliyuncs.com/uploads/_probe" \
     -H "Origin: https://你的域名" \
     -H "Access-Control-Request-Method: PUT" \
     -H "Access-Control-Request-Headers: content-type"
```

不用暴露 `ETag`：服务端合并分片时现查 ListParts，客户端报的 ETag 一概不信，
所以前端根本不读这个响应头 —— 少一项 CORS 配置就少一处会配错的地方。

没配 CORS 也不会坏：前端第一片就会拿到「零字节失败」，自动整条回落到原来的
服务器中转上传，用户只是感觉不到提速。想临时关掉直传：`TRAVEL_OSS_DIRECT=0`。

`/health` 会报 `oss_direct` 和 `oss_backfill` 队列水位，能一眼看出直传有没有生效、
还有多少原件没拉回本机。

## 其它可选配置

### 环境变量

一个都不配也能跑，按需再加：

| 变量 | 作用 | 默认 |
|---|---|---|
| `PORT` / `HOST` | `./start.sh` 的监听地址（也可以用 `./start.sh --port 9000`） | `8084` / `0.0.0.0` |
| `AMAP_APPNAME` | 高德 Key 的白名单 appname，用到「行政区划」图层时才需要，要和高德控制台配的一致 | `http://localhost:8084/` |
| `TRAVEL_BASE_URL` | MCP 端点回调自己领域 API 的地址，**必须和进程真正监听的协议/端口一致** | `http://127.0.0.1:8084` |
| `TRAVEL_LLM_PROVIDER` | AI 功能走哪个模型：`auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | 对应 provider 的密钥，不用 AI 功能就不用配 | 空 |
| `TRAVEL_MEDIA_BASE_URL` | 媒体分发前缀（对象存储 / CDN 的 origin）。留空 = 媒体走本机 `/uploads/` | 空（也可写进 `media_base.txt`） |
| `TRAVEL_MEDIA_URL_TTL` | 网页/分享页媒体签名 URL 的有效期（秒）。**改了要同步改 `static/share.js` 的 `MEDIA_SIG_TTL_MS`**，前端靠它判断该不该续签 | `1800`（30 分钟） |
| `TRAVEL_AGENT_URL_TTL` | Agent（`/api/ai/*`、`/mcp`）拿到的媒体签名 URL 有效期（秒） | `86400`（24 小时） |
| `TRAVEL_OSS_DIRECT` | 浏览器直传对象存储的总开关。`0` = 关掉，完整回落到服务器中转上传 | `1`（但配置不全时自动关闭） |
| `TRAVEL_OSS_AK` / `TRAVEL_OSS_SK` | 直传签名用的 AK/SK。不设就读 `~/.ossutilconfig` | 空 |
| `TRAVEL_OSS_PART_SIZE` | 直传分片大小（字节） | `8388608`（8MB） |
| `TRAVEL_OSS_BACKFILL_WORKERS` | 回源存档的并发度。刻意留 1，别跟正在播放的人抢带宽 | `1` |
| `DEMO_USER` | 每日重置的演示账号名（见下） | `test` |

### 演示账号每日重置

如果你把演示账号开放给别人玩，一天下来数据就乱了。仓库里带了一对 systemd user unit，
让它每天 03:00 恢复出厂状态：

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

手动重置一次：`./start.sh --demo`（只重置演示账号，不碰其它用户）。
不需要演示数据就跑 `./start.sh --no-demo`。

## 已知边界

- **SQLite 单写入者。** 开了 WAL，读写不再互斥、写锁只在提交瞬间持有，
  个人/小团队用完全够；但写并发很高的场景（几十个人同时录行程）不适合，
  那种量级该换 Postgres。
- 媒体默认由应用进程分发，会和 API 抢带宽。介意的话把 `media_base.txt`
  指向对象存储（见上一节），这条限制随之消失。
- 视频转码是本机 ffmpeg 后台线程。一个视频的几档清晰度由一个 ffmpeg 一遍解码同时编出，
  并发按**占用核数**限流（默认给系统留 20%），但仍然吃满 CPU —— 批量导入几十个视频
  要等一会儿。没装 ffmpeg 则只有原画可播，功能不受影响。
- 路线规划依赖高德/OSRM，境外部分区域可能只能拿到直线兜底。
- 「记住上次选文件的目录」依赖 File System Access API（Chrome / Edge 桌面版）。
  Firefox、Safari 用的是普通文件选择框，行为和以前一样；手机上是**故意**不启用的 ——
  系统相册选择器比文件管理器好用。

---

# 给开发者

下面的内容只有想改代码的人才需要。只是想用的话，上面就够了。

## 技术栈

**FastAPI + SQLite + 原生 JavaScript**。没有前端构建步骤，没有 ORM，没有 Redis ——
`git clone` 之后装几个 Python 包就能跑，改一行 JS 刷新页面就生效。

## 项目结构

```
app.py                 FastAPI 应用与 Web UI 路由（100 条路径 / 117 个操作）
db.py                  SQLite 表结构、迁移与全部查询（WAL）
config.py              跨模块共享的静态配置（高德 Key、OSRM、媒体前缀、签名 URL 有效期）
media.py               缩略图 / EXIF / 视频抽帧
media_purge.py         删媒体时把字节真正清掉（本地 + OSS）—— 六个删除入口共用一处
transcode.py           视频清晰度分层转码（一遍解码编出 1080p/720p/480p，按核数限流）
download.py            媒体「另存为」：文件名拼装 + RFC 5987 编码 + quality 白名单
oss.py                 OSS V1 签名 + 分片直传（浏览器 → OSS，不经过服务器）
oss_backfill.py        直传之后把对象拉回本机存档（后台队列，补缩略图/EXIF/转码）
agent_media.py         Agent 链路的媒体签名 URL（和网页那档分开，有效期更长）
formfix.py             把 form 请求体统一成百分号编码的纯 ASCII（修 agent 写中文乱码）
video_gen.py           旅程视频生成引擎（FFmpeg 模板）
mcp_http.py            MCP Streamable HTTP 端点（工具 → 领域 API 的路由表）
mcp_server.py          MCP stdio 版本
companion.py           伴随进程（定时任务）+ 工具清单（TOOLS）的唯一定义处
routers/
  agent_api.py         领域 API（/api/ai/*）+ /mcp —— 走 API Key，和 Web UI 完全不相交
  console.py           开发者控制台（API Key 管理），走 session
  share.py             只读分享页与其匿名接口
  deps.py              路由间共享的请求级辅助函数（含静态资源版本号）
domain/                领域逻辑：记忆生成、联想、检索、地理编码、LLM provider、限流、鉴权
static/
  landing.html         公开宣传页。完全自包含 —— 不引 style.css / app.js / 高德
  index.html app.js    主界面（约 6,800 行 JS）
  share.html share.js  只读分享页（约 2,800 行）
  common.js            两个页面共用（约 2,600 行）：地图图层、灯箱、清晰度选择、
                       下载、Markdown、照片墙自动播放
  console.html         开发者控制台
  style.css
  promo/               宣传页用的加工产物（截图 WebP、演示视频、解说字幕）
assets/landing-src/    宣传页轮播的**原图**。不放 static/ —— 那是对外托管目录
tools/                 make-slides.py（原图 → PC/手机两版 WebP）、shoot-landing.py
scripts/               种子数据、演示账号重置、密钥扫描、宣传片录制与合成
ops/                   运维脚本（OSS 同步、健康检查、热备更新、每日回忆）
deploy/                systemd unit 示例（11 个 service + 9 个 timer）+ Caddyfile 示例
docs/advertising/      宣传策划、文案、分镜脚本
verify/                站长验证文件（搜索引擎/平台收录用），按原样挂在站点根
tests/                 自动化测试（unit / api / e2e 三层，39 个测试文件）
.githooks/             提交前检查（密钥扫描 + 快速测试）
start.sh               装环境 + 启动（合二为一，可重复跑）
test.sh                测试入口
```

## 测试

```bash
./test.sh                # 快速套件：单元 + API，693 项约 30 秒（日常改完代码就跑这个）
./test.sh all            # 全量，含端到端浏览器测试，786 项约 2 分 20 秒（提交前 / 收工前）
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
3. **每一个工具逐个真跑一遍** `tools/call`，走完整链路（鉴权 → scope → 参数拼装 →
   领域 API → 结果），任何一个报 `isError` 都算回归。

```bash
./test.sh -k mcp
```

工具总数写在 `tests/api/test_mcp_tools.py` 的 `EXPECTED_TOOL_COUNT` 里：
增删工具时这条断言会先红，提醒你把文档一起改掉。

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

## 许可

[MIT](LICENSE)。随便用、随便改、随便商用，保留版权声明即可。
