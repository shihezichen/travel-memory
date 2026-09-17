/**
 * 媒体链接的两件事：签名什么时候该换、视频该播哪一档。
 *
 * 都是纯函数，放在这里是为了能被 node --test 覆盖 —— 页面那层只剩「什么时候
 * 调它们」，那部分只有开发者工具里才验得了。
 */
const config = require('../config');

/**
 * 手上这批地址的签名是不是快过期了。
 *
 * 2026-09-16 起 OSS Bucket 关掉了公共读，后端返回的每个媒体地址都带
 * Expires + Signature，30 分钟就作废（见后端 config.MEDIA_URL_TTL）。
 * 网页端有 onerror 兜底可以「裂了再换」，小程序的 <image> 静默失败、
 * previewImage 更是直接白屏，所以这里改成**到点前主动换**：
 * 页面重新露面、或者用户要看大图/播视频之前，先问一次这个函数。
 *
 * fetchedAt 是 0（还没拉过数据）时返回 true —— 没有数据当然要先拉。
 */
function signaturesStale(fetchedAt) {
  const ttl = config.MEDIA_SIG_TTL_MS - config.SIG_REFRESH_MARGIN_MS;
  return Date.now() - (fetchedAt || 0) > ttl;
}

// 后端 transcode.LADDER 的镜像。短边越大排越前，original 当成无穷大。
const QUALITY_SHORT_SIDE = { original: Infinity, '1080p': 1080, '720p': 720, '480p': 480 };

/**
 * 网络类型 -> 能接受的最大短边。
 *
 * 和网页端 static/common.js 一个取向：**手机默认不上 1080p**。移动网络下
 * 拉一个原画 4K 视频既慢又烧流量，而手机屏幕上看 720p 已经没什么差别。
 * 拿不到网络类型（老基础库 / 接口失败）就按 720 走，宁可保守。
 */
function capForNetwork(networkType) {
  switch (String(networkType || '').toLowerCase()) {
    case 'wifi': return 1080;
    case '5g':
    case '4g': return 720;
    case '3g':
    case '2g': return 480;
    default: return 720;
  }
}

/**
 * 从后端给的 qualities 里挑一档来播。
 *
 * 规则：在「已转好 + 短边不超过 cap」的档位里取最大的一档；一个都没有
 * （转码还没跑完、或者源片本来就比最低档还小）就退回原画 —— 原画档后端保证
 * 永远在第一位且永远可用，这条回退路不能断。
 *
 * qualities 整个缺失（照片，或者老接口）时返回 null，调用方用 m.url。
 */
function pickQuality(qualities, cap) {
  if (!Array.isArray(qualities) || !qualities.length) return null;
  const limit = typeof cap === 'number' ? cap : 720;
  let best = null;
  let original = null;
  for (const q of qualities) {
    if (!q || !q.url || q.status !== 'done') continue;
    const side = QUALITY_SHORT_SIDE[q.label];
    if (q.label === 'original') { original = q; continue; }
    if (side === undefined || side > limit) continue;
    if (!best || QUALITY_SHORT_SIDE[best.label] < side) best = q;
  }
  return best || original;
}

/**
 * 问一次当前网络类型，换算成清晰度上限。
 *
 * 唯一碰 wx.* 的函数，而且只在函数体里碰 —— 上面那些纯函数照样能在
 * node --test 里 require 进来。拿不到网络类型一律走保守默认值。
 */
function currentCap() {
  return new Promise((resolve) => {
    try {
      wx.getNetworkType({
        success: (r) => resolve(capForNetwork(r && r.networkType)),
        fail: () => resolve(capForNetwork('')),
      });
    } catch (e) {
      resolve(capForNetwork(''));
    }
  });
}

module.exports = {
  signaturesStale, capForNetwork, pickQuality, currentCap,
  QUALITY_SHORT_SIDE,
};
