/**
 * utils/media.js 和 utils/upload.js 里那些纯函数的测试。
 *
 * 跑法和 utils.test.js 一样：
 *
 *     node --test miniprogram/tests/*.test.js
 *
 * 这两个模块顶上 require 了 config，但只在函数体里碰 wx.*，所以能直接 require 进来。
 */
const test = require('node:test');
const assert = require('node:assert');

const media = require('../utils/media');
const upload = require('../utils/upload');
const config = require('../config');

// ---------------------------------------------------------------- 签名新鲜度

test('signaturesStale：从没拉过数据时必然算「该换了」', () => {
  assert.strictEqual(media.signaturesStale(0), true);
  assert.strictEqual(media.signaturesStale(undefined), true);
});

test('signaturesStale：刚拉到的不算过期', () => {
  assert.strictEqual(media.signaturesStale(Date.now()), false);
});

test('signaturesStale：留了余量，不是卡在 TTL 那一秒才换', () => {
  // TTL 差 1 分钟到期 —— 已经进了余量区间，应该判「该换了」
  const almost = Date.now() - (config.MEDIA_SIG_TTL_MS - 60 * 1000);
  assert.strictEqual(media.signaturesStale(almost), true);
  // 余量之外还早得很
  const fresh = Date.now() - (config.MEDIA_SIG_TTL_MS - config.SIG_REFRESH_MARGIN_MS - 60 * 1000);
  assert.strictEqual(media.signaturesStale(fresh), false);
});

test('小程序的 TTL 常量和后端 config.py 的 TRAVEL_MEDIA_URL_TTL 默认值对得上', () => {
  // 对不上的表现是「过期之后一直用旧链接」，而且没人会立刻发现
  assert.strictEqual(config.MEDIA_SIG_TTL_MS, 1800 * 1000);
});

// ---------------------------------------------------------------- 清晰度

test('capForNetwork：手机流量默认不上 1080p，wifi 才给', () => {
  assert.strictEqual(media.capForNetwork('wifi'), 1080);
  assert.strictEqual(media.capForNetwork('4g'), 720);
  assert.strictEqual(media.capForNetwork('5G'), 720);
  assert.strictEqual(media.capForNetwork('3g'), 480);
});

test('capForNetwork：拿不到网络类型时保守走 720', () => {
  assert.strictEqual(media.capForNetwork(''), 720);
  assert.strictEqual(media.capForNetwork(undefined), 720);
  assert.strictEqual(media.capForNetwork('unknown'), 720);
});

const LADDER = [
  { label: 'original', name: '原画', url: 'o.mp4', status: 'done' },
  { label: '1080p', name: '1080p', url: 'a.mp4', status: 'done' },
  { label: '720p', name: '720p', url: 'b.mp4', status: 'done' },
  { label: '480p', name: '480p', url: 'c.mp4', status: 'done' },
];

test('pickQuality：在上限之内取最大的一档', () => {
  assert.strictEqual(media.pickQuality(LADDER, 1080).label, '1080p');
  assert.strictEqual(media.pickQuality(LADDER, 720).label, '720p');
  assert.strictEqual(media.pickQuality(LADDER, 480).label, '480p');
});

test('pickQuality：还没转好的档位不算数', () => {
  const half = [
    { label: 'original', url: 'o.mp4', status: 'done' },
    { label: '1080p', url: null, status: 'pending' },
    { label: '720p', url: 'b.mp4', status: 'done' },
  ];
  assert.strictEqual(media.pickQuality(half, 1080).label, '720p');
});

test('pickQuality：一档都没转出来就退回原画（这条回退路不能断）', () => {
  const none = [
    { label: 'original', url: 'o.mp4', status: 'done' },
    { label: '480p', url: null, status: 'failed' },
  ];
  assert.strictEqual(media.pickQuality(none, 480).label, 'original');
});

test('pickQuality：照片没有 qualities，返回 null 让调用方用 m.url', () => {
  assert.strictEqual(media.pickQuality(undefined, 720), null);
  assert.strictEqual(media.pickQuality([], 720), null);
});

// ---------------------------------------------------------------- 分片上传

test('partRange：最后一片按剩下多少算，不会读过文件尾', () => {
  assert.deepStrictEqual(upload.partRange(1, 5, 12), { position: 0, length: 5 });
  assert.deepStrictEqual(upload.partRange(2, 5, 12), { position: 5, length: 5 });
  assert.deepStrictEqual(upload.partRange(3, 5, 12), { position: 10, length: 2 });
});

test('partRange：整除时最后一片是满的', () => {
  assert.deepStrictEqual(upload.partRange(2, 5, 10), { position: 5, length: 5 });
});

test('missingParts：跳过云上已经有的片（断点续传就靠它）', () => {
  assert.deepStrictEqual(upload.missingParts(4, [{ number: 1 }, { number: 3 }]), [2, 4]);
  assert.deepStrictEqual(upload.missingParts(3, []), [1, 2, 3]);
  assert.deepStrictEqual(upload.missingParts(2, [{ number: 1 }, { number: 2 }]), []);
});

test('missingParts：received 缺失时当成一片都没传', () => {
  assert.deepStrictEqual(upload.missingParts(2, undefined), [1, 2]);
});

test('basename/fingerprint：指纹带上大小，换个文件就不会撞上老会话', () => {
  assert.strictEqual(upload.basename('http://tmp/wxfile://a/b.mp4'), 'b.mp4');
  assert.strictEqual(upload.basename(''), 'unknown');
  assert.notStrictEqual(
    upload.fingerprint('wxfile://tmp/a.mp4', 100),
    upload.fingerprint('wxfile://tmp/a.mp4', 200));
});
