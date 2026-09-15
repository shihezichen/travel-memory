/**
 * 小程序纯逻辑的单元测试。用 node 自带的 test runner 跑：
 *
 *     node --test miniprogram/tests/
 *
 * 为什么只测这些：小程序的页面逻辑要在微信开发者工具里才跑得起来，
 * 而开发者工具只有 Windows/Mac 版 —— 这个项目的开发机是 Linux，
 * 真机和模拟器都碰不到。所以刻意把「算得出对错」的部分全挪进
 * utils/ 里的纯函数（坐标、折线拼接、格式化、表单编码、cookie 解析），
 * 让它们至少有测试兜着；页面那层只剩取数和 setData。
 *
 * 换句话说：测试绿了**不等于**小程序能跑。样式、组件行为、接口域名白名单、
 * 审核规则这些只有导进开发者工具才暴露得出来。见 miniprogram/README.md。
 */
const test = require('node:test');
const assert = require('node:assert');

const geo = require('../utils/geo');
const fmt = require('../utils/format');

// utils/api.js 顶上 require 了 config，但不碰 wx.*（只在函数体里用），
// 所以可以直接 require 进来测那两个纯函数。
const api = require('../utils/api');

// ---------------------------------------------------------------- geo

test('bounds 忽略坐标缺失的点', () => {
  const b = geo.bounds([
    { lat: 43.8, lng: 87.6 },
    { lat: null, lng: 100 },
    { lat: 26.8, lng: 100.2 },
    null,
  ]);
  assert.deepStrictEqual(b, { minLat: 26.8, maxLat: 43.8, minLng: 87.6, maxLng: 100.2 });
});

test('bounds 空列表返回 null，调用方据此走兜底视野', () => {
  assert.strictEqual(geo.bounds([]), null);
  assert.strictEqual(geo.bounds(null), null);
});

test('isDegenerate 认出单点行程', () => {
  assert.strictEqual(geo.isDegenerate([{ lat: 30, lng: 120 }]), true);
  assert.strictEqual(geo.isDegenerate([{ lat: 30, lng: 120 }, { lat: 31, lng: 121 }]), false);
});

test('toMarkers 的序号从 1 开始，且带上原始 id', () => {
  const ms = geo.toMarkers([{ id: 7, lat: 1, lng: 2, name: '大阪' }]);
  assert.strictEqual(ms.length, 1);
  assert.strictEqual(ms[0].id, 7);
  assert.strictEqual(ms[0].callout.content, '1. 大阪');
});

test('toPolyline 用真实路线时把各段坐标首尾相接', () => {
  const pts = [{ lat: 1, lng: 1 }, { lat: 2, lng: 2 }, { lat: 3, lng: 3 }];
  const segs = [[[1, 1], [1.5, 1.5], [2, 2]], [[2, 2], [3, 3]]];
  const pl = geo.toPolyline(pts, segs);
  assert.strictEqual(pl.length, 1);
  assert.strictEqual(pl[0].points.length, 5);
  assert.deepStrictEqual(pl[0].points[1], { longitude: 1.5, latitude: 1.5 });
});

test('toPolyline 对取不到路线的那一段退化成直连，而不是整条不画', () => {
  const pts = [{ lat: 1, lng: 1 }, { lat: 2, lng: 2 }, { lat: 3, lng: 3 }];
  const pl = geo.toPolyline(pts, [null, [[2, 2], [2.5, 2.5], [3, 3]]]);
  // 第一段两点直连 + 第二段三个点
  assert.strictEqual(pl[0].points.length, 5);
  assert.deepStrictEqual(pl[0].points[0], { longitude: 1, latitude: 1 });
  assert.deepStrictEqual(pl[0].points[1], { longitude: 2, latitude: 2 });
});

test('toPolyline 少于两个点时不画线', () => {
  assert.deepStrictEqual(geo.toPolyline([{ lat: 1, lng: 1 }], null), []);
  assert.deepStrictEqual(geo.toPolyline([], null), []);
});

test('haversine 对已知距离给出合理结果（乌鲁木齐–丽江约 2300km）', () => {
  const d = geo.haversine({ lat: 43.8256, lng: 87.6168 }, { lat: 26.8721, lng: 100.2299 });
  assert.ok(d > 2_100_000 && d < 2_500_000, '实际 ' + Math.round(d / 1000) + ' km');
});

test('assertGcj02 只在出现别的坐标系时报警', () => {
  assert.strictEqual(geo.assertGcj02([{ coord_sys: 'gcj02' }]), '');
  assert.match(geo.assertGcj02([{ coord_sys: 'wgs84' }]), /wgs84/);
});

// ---------------------------------------------------------------- format

test('distance 在 1 公里处换单位', () => {
  assert.strictEqual(fmt.distance(840), '840 m');
  assert.strictEqual(fmt.distance(1000), '1.0 km');
  assert.strictEqual(fmt.distance(12340), '12.3 km');
  assert.strictEqual(fmt.distance(null), '');
});

test('dateRange 同一天只显示一次', () => {
  assert.strictEqual(fmt.dateRange('2026-03-28', '2026-04-04'), '3月28日 – 4月4日');
  assert.strictEqual(fmt.dateRange('2026-03-28', '2026-03-28'), '3月28日');
  assert.strictEqual(fmt.dateRange('', ''), '');
});

test('dayCount 含首尾，日期颠倒或缺失时返回 0', () => {
  assert.strictEqual(fmt.dayCount('2026-03-28', '2026-04-04'), 8);
  assert.strictEqual(fmt.dayCount('2026-03-28', '2026-03-28'), 1);
  assert.strictEqual(fmt.dayCount('2026-04-04', '2026-03-28'), 0);
  assert.strictEqual(fmt.dayCount('', '2026-03-28'), 0);
});

test('statusClass 对未知状态也给得出一个类名', () => {
  assert.strictEqual(fmt.statusClass('planning'), 'badge-planning');
  assert.strictEqual(fmt.statusClass('weird'), 'badge-archived');
});

test('thumbUrl 走 thumbs/ 目录；没有缩略图时退回原图', () => {
  const host = 'https://oss.example.com';
  assert.strictEqual(
    fmt.thumbUrl({ thumb_filename: 'a.jpg', url: host + '/uploads/a.jpg' }, '', host),
    host + '/uploads/thumbs/a.jpg'
  );
  // 直传完成但回源还没补缩略图的那段窗口期，thumb_filename 是 null
  assert.strictEqual(
    fmt.thumbUrl({ thumb_filename: null, url: host + '/uploads/a.jpg' }, '', host),
    host + '/uploads/a.jpg'
  );
});

test('mediaUrl 对相对路径补前缀，对绝对地址原样返回', () => {
  assert.strictEqual(fmt.mediaUrl({ url: '/uploads/a.jpg' }, 'https://top001.xin/'),
    'https://top001.xin/uploads/a.jpg');
  assert.strictEqual(fmt.mediaUrl({ url: 'https://oss/x.jpg' }, 'https://top001.xin'),
    'https://oss/x.jpg');
});

// ---------------------------------------------------------------- api 的纯函数

test('encodeForm 转义值，行程名里的 & 不会把参数截断', () => {
  assert.strictEqual(api.encodeForm({ name: 'A&B', n: 1 }), 'name=A%26B&n=1');
});

test('encodeForm 跳过 undefined / null，不发 "x=undefined"', () => {
  assert.strictEqual(api.encodeForm({ a: 1, b: undefined, c: null, d: 0 }), 'a=1&d=0');
});

test('extractToken 容忍大小写不同的 Set-Cookie 头名', () => {
  assert.strictEqual(
    api.extractToken({ 'Set-Cookie': 'tm_token=abc123; Path=/; HttpOnly' }), 'abc123');
  assert.strictEqual(
    api.extractToken({ 'set-cookie': 'tm_token=xyz; Max-Age=60' }), 'xyz');
});

test('extractToken 能从多条 cookie 里挑出 tm_token', () => {
  assert.strictEqual(
    api.extractToken({ 'Set-Cookie': ['other=1; Path=/', 'tm_token=tok9; Path=/'] }), 'tok9');
});

test('extractToken 没有 cookie 时返回空串而不是抛错', () => {
  assert.strictEqual(api.extractToken(null), '');
  assert.strictEqual(api.extractToken({ 'Content-Type': 'application/json' }), '');
});
