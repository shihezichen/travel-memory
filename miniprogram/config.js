/**
 * 小程序的运行时配置。
 *
 * BASE_URL 必须是**已备案的 HTTPS 域名**，而且要在微信公众平台
 *「开发 → 开发管理 → 服务器域名 → request 合法域名」里登记过 —— 没登记的话
 * 真机上所有 wx.request 直接失败，开发者工具里也只有勾了「不校验合法域名」才通。
 *
 * MEDIA_HOST 是 OSS 的域名。照片和视频的 url 由后端直接返回绝对地址
 * （见 config.media_url），所以它要单独登记进 downloadFile 合法域名，
 * <image> 和 <video> 才拉得到。漏了这条的表现是「接口都正常，就是图全白」。
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
};

module.exports = config;
