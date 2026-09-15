# 旅行记忆 · 微信小程序

Web 版的移动端伴侣：看行程、看地图路线、翻相册、往某个点补拍照片。
复用同一套后端接口（`app.py` / `routers/`），没有单独的服务端。

## ⚠️ 这份代码从来没有在真机或开发者工具里跑过

必须先说清楚，否则会误判它的成熟度。

开发这个项目的机器是 Linux，而**微信开发者工具只有 Windows 和 macOS 版**。
也就是说下面这些事，一次都没做过：

- 在模拟器里打开过任何一个页面
- 真机预览 / 真机调试
- 看过任何一个组件的实际渲染效果（`<map>`、`<video>`、`previewImage`）
- 跑过体验评分、代码质量扫描
- 提交过审核

**测试绿了不等于它能跑。** `tests/` 覆盖的是抽出来的纯函数（坐标计算、折线
拼接、格式化、表单编码、cookie 解析）—— 那些是「算得出对错」的部分，刻意
从页面里挪出来就是为了至少能有测试兜底。页面那层只剩取数和 `setData`，
但样式、组件行为、生命周期时序这些一个都没验证过。

已知**一定要在开发者工具里确认**的点：

| 要确认什么 | 为什么不放心 |
| --- | --- |
| `<map>` 的 marker / polyline 渲染 | `iconPath`、`callout`、`arrowLine` 的实际效果只能看 |
| `include-points` 的留白 | 路线可能贴边，多半要加 `padding` |
| `wx.chooseMedia` 上传 | 视频大小限制、`sizeType: original` 是否真的保留 EXIF |
| cookie 鉴权 | 见下面「登录态」一节，这是最可能翻车的地方 |
| 深色模式 / 不同机型字号 | 完全没适配过 |

## 跑起来需要什么（这些我给不了，得你来）

1. **AppID** —— `project.config.json` 里现在是 `touristappid`（游客模式占位）。
   换成你自己的小程序 AppID。没有 AppID 只能用游客模式，而游客模式**不能**
   调用 `wx.request`，等于什么都做不了。

2. **服务器域名白名单** —— 微信公众平台 → 开发 → 开发管理 → 服务器域名：

   - `request` 合法域名：`https://top001.xin`
   - `uploadFile` 合法域名：`https://top001.xin`
   - `downloadFile` 合法域名：`https://travel-memory-media.oss-cn-qingdao.aliyuncs.com`

   最后那条最容易漏：照片和视频的地址是后端直接返回的 OSS 绝对地址
   （见 `config.media_url`），不登记的话表现是「接口全正常，图片全白」。

   域名必须已备案 + HTTPS。开发阶段可以在开发者工具里勾「不校验合法域名」
   绕过，但真机上绕不过去。

3. **地图额度** —— `<map>` 组件用的是微信内置地图，不需要另外申请 key，
   但有调用量限制，量大了要在公众平台看配额。

## 登录态：这块和 Web 端不一样

后端用 `tm_token` 这个 httpOnly cookie 认身份。浏览器会自动带上，
**而 `wx.request` 完全不管 cookie** —— 所以 `utils/api.js` 自己做了一个
极简的 cookie jar：

1. 登录响应里从 `Set-Cookie` 抠出 `tm_token`，存进 `wx.setStorageSync`
2. 之后每个请求手动塞 `Cookie: tm_token=...` 头
3. 收到 401 就清掉本地 token，**但不强跳登录页** ——
   后台请求触发的 401 把用户正在看的页面顶掉，体验很差

（`httpOnly` 是浏览器的概念，`wx.request` 的 `res.header` 里照样拿得到
`Set-Cookie`，所以这条路是通的。但**没在真机上验证过** ——
不同基础库版本对多条 `Set-Cookie` 的合并方式不一致，
`extractToken` 已经按「大小写不敏感 + 可能是数组 + 可能被拼成一个串」
三种情况都处理了，但只有测试覆盖，没有真机数据。）

`wx.uploadFile` 不共享 `wx.request` 的逻辑，所以 `uploadMedia` 里又塞了一次头。

## 目录

```
miniprogram/
├── app.js / app.json / app.wxss    入口、路由、全局样式
├── config.js                       BASE_URL、OSS 域名、体验账号
├── utils/
│   ├── api.js                      接口客户端 + cookie jar + 表单编码
│   ├── geo.js                      坐标/折线/视野计算（纯函数，有测试）
│   └── format.js                   日期、里程、缩略图地址（纯函数，有测试）
├── pages/
│   ├── trips/                      行程列表；未登录时显示「一键体验」
│   ├── trip/                       地图 + 点位列表 + 统计
│   ├── point/                      点位详情、照片网格、上传
│   ├── album/                      整趟相册；兼作视频全屏播放页
│   └── login/                      用户名密码登录
├── assets/pin.png                  地图 marker 图标
└── tests/utils.test.js             node --test 跑的单元测试
```

## 跑测试

```bash
node --test miniprogram/tests/
```

不需要装任何依赖（用 node 自带的 test runner，node ≥ 18）。
这套测试**不在** pytest 套件里 —— 它测的是 JS，跟后端没关系。

## 没做的功能

当前只覆盖「看」和「补拍照片」。以下 Web 端有、小程序没有：

- 建行程 / 建点位 / 拖拽排序
- 路线回放动画
- 足迹热力图
- 分享链接的生成与管理
- AI 接口 / MCP 相关的一切
- 大文件分片续传（现在走单请求上传，大视频弱网下会失败；
  后端的分片接口 `/api/points/{id}/upload-sessions` 还没接）

这些不是不能做，是先把「能看」这条链路走通再说 —— 毕竟连一次真机都还没跑过。
