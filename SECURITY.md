# 安全与凭据规范 / Security & Credentials

## 报告漏洞 / Reporting a vulnerability

请发邮件到 `jeff@luckyluke.org`，不要开公开 issue。
Please email `jeff@luckyluke.org` rather than opening a public issue.

---

## 凭据绝不进仓库 / Never commit credentials

这个仓库是公开的。**任何密钥、令牌、私钥都不能出现在仓库里的任何文件中** —— 
哪怕这个文件被 `.gitignore` 排除、哪怕仓库当时还是私有的。

This repository is public. **No key, token, or private key may appear in any file in
the repo** — not even in a `.gitignore`d file, not even while the repo is still private.

### 正确的做法 / The right pattern

| 内容 | 存在哪里 | 仓库里放什么 |
|---|---|---|
| 高德地图 Key / 安全密钥 | `amap_key.txt`、`amap_security.txt`（已 gitignore） | `*.example` 模板 |
| 管理员令牌 | `token.txt`（已 gitignore） | `token.txt.example` |
| API Key（`/api/ai/*`、MCP） | 环境变量 `COMPANION_API_KEY` / `TRAVEL_API_KEY` | 什么都不放 |
| 本机运维配置（公网 IP、飞书 webhook 等） | 仓库**之外**的 `ops/site.env`（`chmod 600`） | 什么都不放 |
| TLS 私钥、Let's Encrypt 账户 | `acme/`（整个目录已 gitignore） | 什么都不放 |

代码里这样读：

```python
KEY = os.environ.get("TRAVEL_API_KEY", "")
if not KEY:
    raise SystemExit("请先设置环境变量 TRAVEL_API_KEY（网页端「设置 → API Key」生成）")
```

**不要**写"读不到就退回内置的那把 key"。那个兜底看着贴心，实际等于把凭据公开发布。

### 为什么把运维配置放到仓库之外

2026-09-09 做开源前的"敏感信息参数化"时，批量替换把 `ops/`（虽然已 gitignore，但
物理上就躺在仓库目录里）也扫了，真实公网 IP 被换成了 `YOUR_PUBLIC_IP` 占位符，
健康巡检随即每天误报"公网不可达 / 证书续期会失败"。

放到仓库目录之外，下一次同类批量操作就波及不到。

---

## 媒体分发：Bucket 私有 + 签名 URL

2026-09-16 之前，`travel-memory-media` 这个 Bucket 是 **public-read**：
任何人拿到一个媒体 URL 就能**永久**下载，URL 转发出去了也收不回来，而且
`/uploads/` 在 `is_public_path()` 里对匿名访客放行（分享页要匿名看图）。
等于整个媒体库对所有拿到过链接的人开放。

现在改成：**Bucket 私有，所有媒体链接走带 Expires 的签名 URL**。

| 链路 | 有效期 | 说明 |
|---|---|---|
| 网页 / 分享页（`config.media_url`） | 30 分钟（`TRAVEL_MEDIA_URL_TTL`） | 渲染时当场加载；短一点泄露窗口小。前端会在过期前主动续签，用户无需刷新 |
| Agent 链（`agent_media.py`） | 24 小时（`TRAVEL_AGENT_URL_TTL`） | Agent 拿到链接后可能隔很久才下载 |

**分享链接本身不过期。** share token 的 `expires_at` 可以是 NULL（永久），
签名只作用于媒体 URL。所以「发到群里过两周还有人点」这个场景不受影响 ——
页面加载时会拿到当时新签的 URL。

网页端还有一个**自愈**：页面开着超过 2 小时后（手机从后台切回来很常见），
旧签名会失效。`share.js` 的 `handleMediaError` 在图片 403 时用 share token
重新拉一次数据换新签名，并发合并成单个请求（几十张图同时过期不会打出几十个
刷新请求）。

### 降级行为

`config.media_url` 签不出来时**回落到裸链**，不抛异常。这样没关公共读的
部署、以及本地开发（没有 OSS 凭据）都不会被这条改动搞挂。代价是：**如果
签名静默失效而 Bucket 已经私有，页面会裂图**，所以改动这块之后务必实测
一遍（见下面的检查脚本）。

---

## 访问口令门禁：当前**不启用**（有意为之）

`gate_hash.txt` 不存在 = 门禁关闭（`app.py` 里 is_public_path 之后那段）。

**2026-09-16 与刚哥确认：这个站要让新用户注册，所以门禁保持关闭。**

理由 —— 门禁在这里解决不了「陌生人能访问」的担忧：

- `PUBLIC_PATHS` 里已经包含 `/`、`/app`、`/api/login`、`/api/register`，
  而门禁检查写在 `is_public_path()` **之后**，这些路径根本不走到门禁那一段。
  开了门禁，陌生人照样能打开登录页、照样能注册。
- 注册进来的账号**看不到任何别人的数据**：所有业务路由都过 `require_owner()`，
  非本人一律 404。陌生人拿到的是一个空账号。
- 门禁真正的作用是挡住已登录后的应用主体（`/console`、`/api/trips` 等）。

**所以别再"顺手把门禁打开"** —— 它不会带来预期的安全收益，只会给家人朋友
访问时多加一道卡。真要收紧，该动的是注册接口（移出 PUBLIC_PATHS + 改成
仅管理员可建号），那会直接砍掉「新用户注册」这个功能，是产品决策不是安全决策。

---

## 自动检查 / Automated check

```bash
scripts/scan-secrets.sh              # 扫工作区（已跟踪的文件）
scripts/scan-secrets.sh --staged     # 只扫已 git add 的新增行（pre-commit 用）
scripts/scan-secrets.sh --history    # 扫全部历史提交（发布前跑一次）
```

命中会打印位置并返回 1。已接入 pre-commit 钩子；确需绕过用 `git commit --no-verify`，
但请先想清楚。

---

## 万一已经提交了密钥 / If a secret was committed

**改代码是不够的。** 只要它进过一次仓库，就必须当作已泄露：

1. **立刻吊销**（网页端「设置 → API Key」，或直接把 `api_keys.revoked` 置 1），
   然后**实测**它确实已经失效 —— 本地和公网都要试。
2. 换发新的，存到环境变量或仓库外的配置里。
3. 再去清理历史（`git filter-repo` / 重写提交）。这一步是善后，不能代替第 1 步：
   Git 托管方通常仍能通过 commit SHA 访问到悬空对象，爬虫也早就抓过了。

### 本项目的处置记录

2026-09-09 发现三处硬编码（`companion.py` 一把 `read` scope、
`scripts/verify_p2_deep.py` 与 `scripts/verify_wrapped_shared.py` 共用一把
`read,write` scope，都属于 admin 账号），且已推送到远端。

处置：代码改为从环境变量读取；两把 Key 于 2026-09-10 吊销并实测确认本地与公网
均返回 `API Key 无效或已过期/吊销`；含密钥的那个提交由后续的强制推送从分支上移除。
