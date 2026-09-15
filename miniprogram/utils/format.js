/**
 * 展示层的格式化。同样是纯函数，能在 node 里测（tests/utils.test.js）。
 */

const STATUS_TEXT = {
  planning: '计划中',
  ongoing: '进行中',
  completed: '已完成',
  archived: '已归档',
};

function statusText(s) {
  return STATUS_TEXT[s] || s || '';
}

function statusClass(s) {
  return STATUS_TEXT[s] ? 'badge-' + s : 'badge-archived';
}

/** 米 -> "12.3 km" / "840 m"。超过 1 公里才换单位，免得写成 "0.8 km"。 */
function distance(meters) {
  // Number.isFinite 而不是 isFinite：后者会把 null / '' 当成 0，
  // distance(null) 就变成 "0 m"，页面上凭空多出一个里程。
  const m = Number(meters);
  if (!Number.isFinite(m) || meters === null || meters === '' || m < 0) return '';
  if (m < 1000) return Math.round(m) + ' m';
  return (m / 1000).toFixed(1) + ' km';
}

/** "2026-03-28" -> "3月28日"。已经是空/脏数据就原样退回，别抛。 */
function shortDate(iso) {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return String(iso);
  return Number(m[2]) + '月' + Number(m[3]) + '日';
}

/** 起止日期 -> "3月28日 – 4月4日"；同一天只显示一个。 */
function dateRange(start, end) {
  const a = shortDate(start);
  const b = shortDate(end);
  if (a && b && a !== b) return a + ' – ' + b;
  return a || b || '';
}

/** 天数（含首尾）。日期缺失或颠倒时返回 0，页面上就不显示这一项。 */
function dayCount(start, end) {
  if (!start || !end) return 0;
  const a = Date.parse(start + 'T00:00:00Z');
  const b = Date.parse(end + 'T00:00:00Z');
  if (!isFinite(a) || !isFinite(b) || b < a) return 0;
  return Math.round((b - a) / 86400000) + 1;
}

/** "2026-03-31 17:20:00" -> "17:20"。 */
function timeOfDay(ts) {
  if (!ts) return '';
  const m = String(ts).match(/(\d{2}):(\d{2})/);
  return m ? m[1] + ':' + m[2] : '';
}

/**
 * 媒体的展示地址。后端返回的 url 已经是 OSS 绝对地址；
 * 万一是相对路径（本机开发没配 OSS），补上站点前缀。
 */
function mediaUrl(m, baseUrl) {
  if (!m) return '';
  const u = m.url || '';
  if (!u) return '';
  if (/^https?:\/\//.test(u)) return u;
  return (baseUrl || '').replace(/\/$/, '') + u;
}

/** 缩略图地址。列表里用它，别拿原图铺网格 —— 一屏几十张会把流量打爆。 */
function thumbUrl(m, baseUrl, mediaHost) {
  if (!m) return '';
  const name = m.thumb_filename;
  if (!name) return mediaUrl(m, baseUrl);       // 直传后还没回源补缩略图
  const host = (mediaHost || baseUrl || '').replace(/\/$/, '');
  return host + '/uploads/thumbs/' + name;
}

module.exports = {
  STATUS_TEXT, statusText, statusClass,
  distance, shortDate, dateRange, dayCount, timeOfDay,
  mediaUrl, thumbUrl,
};
