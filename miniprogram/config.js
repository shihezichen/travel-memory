/**
 * 小程序的运行时配置。
 *
 * BASE_URL 必须是**已备案的 HTTPS 域名**，而且要在微信公众平台
 *「开发 → 开发管理 → 服务器域名 → request 合法域名」里登记过 —— 没登记的话
 * 真机上所有 wx.request 直接失败，开发者工具里也只有勾了「不校验合法域名」才通。
 *
 * MEDIA_HOST 是 OSS 的域名。这里**不再用它拼地址**（Bucket 关掉公共读之后
 * 裸链一律 403，所有地址都由后端签好名返回），留着只有一个用处：提醒你这个
 * 域名要同时登记进两个白名单 ——
 *   - downloadFile：<image> / <video> / previewImage 要从它拉媒体；
 *   - request：直传分片是 PUT 到它上面的签名 URL（见 utils/upload.js）。
 * 漏了前者的表现是「接口都正常，就是图全白」，漏了后者是「上传一片就失败」。
 */
const config = {
  BASE_URL: 'https://top001.xin',
  MEDIA_HOST: 'https://travel-memory-media.oss-cn-qingdao.aliyuncs.com',

  // 演示账号。放在这里只是为了让「先逛逛」能一键进去，它本来就是公开的
  // 体验账号（首页那个「一键体验，无需注册」用的是同一个）。
  DEMO: { username: 'test', password: 'test1234' },

  // 逐段路线规划有网络开销，点多的行程会打很多次。超过这个数就只连直线，
  // 免得开图先卡十几秒。用户可以在行程页手动点「加载真实路线」。
  AUTO_ROUTE_MAX_POINTS: 12,

  // 媒体签名 URL 的有效期，**必须和后端 config.py 的 TRAVEL_MEDIA_URL_TTL
  // 保持一致**（默认 1800 秒）。小程序靠它判断「手上这批链接是不是该换了」，
  // 对不上就会在过期之后一直用旧链接，表现是图全裂、视频点了不动。
  // 网页端的同一个常量在 static/share.js 里叫 MEDIA_SIG_TTL_MS。
  MEDIA_SIG_TTL_MS: 30 * 60 * 1000,
  // 留一点余量，别卡在过期的那一秒上（拉一次列表本身也要时间）。
  SIG_REFRESH_MARGIN_MS: 5 * 60 * 1000,
};

module.exports = config;
