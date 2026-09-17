# 部署单元（systemd user units）

这个目录放的是本项目在生产机上实际使用的 systemd **user** unit 的模板。

两件事先说清楚：

1. **全部是 user unit，不需要 root。** 用 `systemctl --user`，装在 `~/.config/systemd/user/`。
   除了主服务和 80 端口这两个，其余都是可选的运维件 —— 不装也能正常跑。
2. **模板里没有真实路径、公网地址和密钥。** 这个仓库是公开的，所以机器相关的值
   都换成了占位符，安装时要自己替换。下面有对照表。
3. 目录里还有一份 `Caddyfile.example` —— 反代 + TLS 的最小配置。
   **开发环境也建议挂上**：没有 https 的话，剪贴板、地理定位、`crypto.subtle`
   全部走降级分支，你调的和用户跑的不是同一条路径。理由见 README 的「让它一直跑着」。
   **注意那一节列了两条 TLS 路线**：反代终结（Caddy，反代到明文 8084）和 uvicorn
   自己终结（本目录的 `travel-memory.service` 默认这条，8443）。二选一，别叠着用。

> ⚠️ **改了监听端口/协议，记得同步改 `TRAVEL_BASE_URL`。**
> `/mcp` 端点会带着调用者的 API Key 回调本机的领域 API（`/api/ai/*`），
> 这个地址必须和 `ExecStart` 里真正监听的一致，否则 37 个 MCP 工具会**全部**
> 报「领域 API 不可达」—— 而 Web UI 一点异常都没有，很难联想到这里。
> `travel-memory.service` 里已经按 `https://127.0.0.1:8443` 配好了。

---

## 占位符对照表

| 占位符 | 含义 | 例子 |
|---|---|---|
| `%h` | systemd 自动展开成当前用户的家目录，**不用手改** | `/home/alice` |
| `%h/travel-memory` | 项目根目录。**项目不在家目录下就要手改** | `/srv/travel-memory` |
| `__AMAP_APPNAME__` | 高德 Key 的白名单 appname，必须和高德控制台配的**完全一致** | `http://203.0.113.7:8084/` |
| `__PUBLIC_HOST__` | 你的公网域名或 IP，80 端口用它做 https 跳转 | `example.com` |
| `__OPS_DIR__` | 你存放私有巡检脚本的目录（不在本仓库里） | `/srv/ops` |

> `%h` 是 systemd 的规范符，会在运行时展开，写在 unit 里就行。
> `__XXX__` 是纯文本占位符，**systemd 不认识**，必须在安装前替换掉。

---

## 安装

```bash
cd /path/to/travel-memory
mkdir -p ~/.config/systemd/user

# 1) 拷进去
cp deploy/travel-memory-*.service deploy/travel-memory-*.timer ~/.config/systemd/user/

# 2) 替换占位符（按你自己的值改这三行）
cd ~/.config/systemd/user
sed -i 's|__AMAP_APPNAME__|http://203.0.113.7:8084/|' travel-memory.service
sed -i 's|__PUBLIC_HOST__|example.com|'               travel-memory-http80.service
sed -i 's|__OPS_DIR__|/srv/ops|'                      travel-memory-healthcheck.service

# 3) 如果项目不在 ~/travel-memory，把 %h/travel-memory 换成实际路径
#    （只改这个目录下的文件，别在项目仓库里跑全局替换）
# sed -i 's|%h/travel-memory|/srv/travel-memory|g' travel-memory-*.service

# 4) 确认没有漏掉的占位符 —— 有输出就是还没替换完
grep -l '__[A-Z_]*__' travel-memory-*.service travel-memory-*.timer

systemctl --user daemon-reload
```

启用（**按需挑，不用全装**）：

```bash
# 必需：主服务
systemctl --user enable --now travel-memory.service

# 可选：80 端口（ACME 验证 + HTTPS 跳转）。用 Let's Encrypt 签证书才需要
systemctl --user enable --now travel-memory-http80.service

# 可选：各类定时任务
systemctl --user enable --now travel-memory-cert-renew.timer     # 证书续期
systemctl --user enable --now travel-memory-audit-prune.timer    # 清理审计日志
systemctl --user enable --now travel-memory-demo-reset.timer     # 演示账号重置
```

想让这些 unit 在你退出登录后继续跑（服务器场景基本都需要）：

```bash
loginctl enable-linger "$USER"
```

检查：

```bash
systemctl --user status travel-memory.service
systemctl --user list-timers 'travel-memory-*'
journalctl --user -u travel-memory.service -n 50
```

---

## 各 unit 说明

### 必需

| Unit | 作用 | 备注 |
|---|---|---|
| `travel-memory.service` | 主服务，uvicorn 自己终结 TLS | 只监听 `127.0.0.1:8443`，公网入口靠隧道/反代转进来 |
| `travel-memory-http80.service` | 80 端口：ACME 验证 + 跳 HTTPS | 只在用 Let's Encrypt 签证书时需要 |

### 定时任务（依赖仓库内的脚本，可直接用）

| Unit | 频率 | 作用 |
|---|---|---|
| `travel-memory-audit-prune.timer` | 每天 04:10 | 删 90 天前的 `audit_log`（见下文） |
| `travel-memory-upload-prune.timer` | 每天 04:40 | 清 24 小时没动静的分片上传临时文件（见下文） |
| `travel-memory-demo-reset.timer` | 每天 03:00 | 把演示账号恢复出厂状态（`scripts/reset_demo.py`） |

### 定时任务（依赖**不在本仓库里**的脚本）

下面这几个的 `ExecStart` 指向的脚本，要么在 `acme/`、要么在 `ops/` —— 这两个目录都被
`.gitignore` 排除了（里面是 TLS 私钥、特定机器的主机名和路径，对别人没有意义）。
**你需要自己写对应脚本，或者干脆不装这几个 unit。** 模板留在这里是为了说明生产机上
到底跑着哪些东西，便于照着重建。

| Unit | 频率 | 依赖脚本 | 作用 |
|---|---|---|---|
| `travel-memory-cert-renew.timer` | 每 6 小时 | `acme/renew-ip.sh` | 短期 IP 证书续期（只有 6 天有效期） |
| `travel-memory-domain-renew.timer` | 每天 03:17 | `acme/renew.sh` | 域名证书续期 |
| `travel-memory-healthcheck.timer` | 每 2 小时 | `__OPS_DIR__/healthcheck.sh` | 巡检证书/服务/隧道/备份 |
| `travel-memory-recall.timer` | 每天 09:00 | `ops/daily-recall.sh` | 周年回忆推送 |
| `travel-memory-sync.timer` | 每天 03:00 / 15:00 | `sync-to-top001.sh` | 同步到异地冷备机 |
| `travel-memory-tests.timer` | 每天 02:40 | `ops/run-tests-and-report.sh` | 全量测试并推送结果 |

> `travel-memory-tests.service` 里有说明：只想跑测试不想推送的话，把 `ExecStart`
> 改成仓库自带的 `%h/travel-memory/tests/run.sh all` 即可。

---

## 审计日志清理

`audit_log` 表每次调用 `/api/ai/*` 都会写一行，没有保留策略就会一直涨。
`scripts/prune_audit_log.py` 删掉 N 天前的记录，默认 90 天。

```bash
# 先看看会删多少，不实际删
.venv/bin/python scripts/prune_audit_log.py --dry-run

# 手动删一次
.venv/bin/python scripts/prune_audit_log.py

# 改保留天数
.venv/bin/python scripts/prune_audit_log.py --days 30
```

装成每天自动跑：

```bash
cp deploy/travel-memory-audit-prune.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-audit-prune.timer
```

清理只删 `audit_log`，不碰 trips / points / media / users。
过期 session 是服务启动时清的（`db.init_db()`），不需要单独的 timer。

---

## 分片上传的临时文件清理

大文件走断点续传时，还没合并的分片放在 `uploads/.chunks/<session_id>/`。
传完会立刻清掉；用户中途关掉浏览器再也不回来的，就会留在盘上。

```bash
# 先看看会清掉什么
.venv/bin/python scripts/prune_upload_sessions.py --dry-run

# 手动清一次（默认 24 小时没动静算过期）
.venv/bin/python scripts/prune_upload_sessions.py --hours 24
```

装成每天自动跑：

```bash
cp deploy/travel-memory-upload-prune.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-upload-prune.timer
```

**这个 timer 是可选的**：服务进程在「新建上传会话」时自己也会顺手清一次
（每小时最多一次，见 `app.prune_upload_sessions`）。装上只是让长期没人上传的
机器也能按时回收。清理只碰 `.chunks/` 和 `upload_sessions` / `upload_chunks`，
绝不碰已经合并好的媒体文件。

---

## 卸载

```bash
systemctl --user disable --now travel-memory-<名字>.timer
rm ~/.config/systemd/user/travel-memory-<名字>.{service,timer}
systemctl --user daemon-reload
```
