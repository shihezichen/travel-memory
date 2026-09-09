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
