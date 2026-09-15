/**
 * 地图相关的纯计算。没有任何 wx.* 调用，所以能在 node 里直接跑单元测试
 * （见 tests/utils.test.js）—— 小程序没法在这台机器上真机调试，
 * 能用测试兜住的逻辑就尽量搬到这里来。
 *
 * 坐标系：后端的点都是 gcj02（points.coord_sys 实测只有这一个值），
 * 微信 <map> 也要 gcj02，所以全程不需要转换。真出现别的坐标系时
 * assertGcj02 会喊出来，而不是默默画歪。
 */

/**
 * 包住所有点的矩形。给 map 的 includePoints 用，比自己算 zoom 稳。
 *
 * 坐标校验一律用 Number.isFinite，**不要**用全局 isFinite —— 后者会先做
 * Number() 转换，于是 isFinite(null)、isFinite('')、isFinite([]) 全是 true，
 * lat 为 null 的点就这么混进来，把 min/max 比较结果污染成 null。
 */
function bounds(points) {
  const pts = (points || []).filter(
    (p) => p && Number.isFinite(p.lat) && Number.isFinite(p.lng)
  );
  if (!pts.length) return null;
  let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
  for (const p of pts) {
    if (p.lat < minLat) minLat = p.lat;
    if (p.lat > maxLat) maxLat = p.lat;
    if (p.lng < minLng) minLng = p.lng;
    if (p.lng > maxLng) maxLng = p.lng;
  }
  return { minLat, maxLat, minLng, maxLng };
}

/** 视野中心。 */
function center(points) {
  const b = bounds(points);
  if (!b) return null;
  return { lat: (b.minLat + b.maxLat) / 2, lng: (b.minLng + b.maxLng) / 2 };
}

/**
 * 单点行程没有跨度，includePoints 会把它顶到最大缩放（贴着地面看一个针）。
 * 这时候给个固定 zoom 更合适。
 */
function isDegenerate(points, eps) {
  const b = bounds(points);
  if (!b) return true;
  const e = eps === undefined ? 1e-6 : eps;
  return (b.maxLat - b.minLat) < e && (b.maxLng - b.minLng) < e;
}

/** 点列表 -> <map> 的 markers。序号直接画在气泡上，跟 Web 端一致。 */
function toMarkers(points) {
  return (points || [])
    .filter((p) => p && Number.isFinite(p.lat) && Number.isFinite(p.lng))
    .map((p, i) => ({
      id: p.id,
      latitude: p.lat,
      longitude: p.lng,
      width: 28,
      height: 28,
      iconPath: '/assets/pin.png',
      callout: {
        content: (i + 1) + '. ' + (p.name || ''),
        color: '#1A2233',
        fontSize: 12,
        borderRadius: 6,
        bgColor: '#FFFFFF',
        padding: 6,
        display: 'BYCLICK',
      },
    }));
}

/**
 * 把逐段路线拼成 <map> 的 polyline。
 *
 * segments[i] 是第 i 段的 [[lng,lat],...]，为 null 表示那一段没规划出来 ——
 * 退化成两点直连。不能整条都不画：少一段用户会以为数据丢了，
 * 画直线至少还看得出顺序（Web 端也是这么兜的）。
 */
function toPolyline(points, segments, opts) {
  opts = opts || {};
  const pts = (points || []).filter((p) => p && Number.isFinite(p.lat) && Number.isFinite(p.lng));
  if (pts.length < 2) return [];

  const coords = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const seg = segments && segments[i];
    if (seg && seg.length) {
      for (const c of seg) coords.push({ longitude: c[0], latitude: c[1] });
    } else {
      coords.push({ longitude: pts[i].lng, latitude: pts[i].lat });
      coords.push({ longitude: pts[i + 1].lng, latitude: pts[i + 1].lat });
    }
  }
  if (!coords.length) return [];

  return [{
    points: coords,
    color: opts.color || '#2F6FEDDD',
    width: opts.width || 5,
    arrowLine: true,
    borderColor: '#FFFFFF80',
    borderWidth: 1,
  }];
}

/** 两点球面距离（米）。用来在没有真实路线时给个里程估计。 */
function haversine(a, b) {
  const R = 6371000;
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLng = (b.lng - a.lng) * rad;
  const s = Math.sin(dLat / 2) ** 2
    + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** 坐标系兜底检查：出现非 gcj02 时返回提示串，正常返回 ''。 */
function assertGcj02(points) {
  const bad = (points || [])
    .map((p) => p && p.coord_sys)
    .filter((c) => c && c !== 'gcj02');
  return bad.length ? ('存在非 gcj02 坐标：' + Array.from(new Set(bad)).join(',')) : '';
}

module.exports = {
  bounds, center, isDegenerate, toMarkers, toPolyline, haversine, assertGcj02,
};
