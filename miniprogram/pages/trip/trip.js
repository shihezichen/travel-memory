const api = require('../../utils/api');
const fmt = require('../../utils/format');
const geo = require('../../utils/geo');
const config = require('../../config');

Page({
  data: {
    tripId: 0,
    stats: {},
    points: [],
    markers: [],
    polyline: [],
    includePoints: [],
    center: { lat: 35, lng: 105 },      // 兜底：全国视野，免得开图先闪到 (0,0) 的海里
    scale: 5,
    totalKm: '0',
    routeHint: '',
  },

  onLoad(q) {
    this.setData({ tripId: Number(q.id) });
    this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    const id = this.data.tripId;
    return Promise.all([api.trip(id), api.points(id)])
      .then(([stats, points]) => {
        const pts = (points || []).map((p) => Object.assign({}, p, {
          dateText: fmt.dateRange(p.start_date, p.end_date),
        }));
        wx.setNavigationBarTitle({ title: stats.name || '行程' });

        const warn = geo.assertGcj02(pts);
        if (warn) console.warn('[trip]', warn);

        const c = geo.center(pts) || this.data.center;
        const degenerate = geo.isDegenerate(pts);
        this.setData({
          stats,
          points: pts,
          markers: geo.toMarkers(pts),
          polyline: geo.toPolyline(pts, null),     // 先画直线，真实路线随后覆盖
          includePoints: degenerate ? [] : pts.map((p) => ({
            latitude: p.lat, longitude: p.lng,
          })),
          center: c,
          scale: degenerate ? 12 : this.data.scale,
          totalKm: straightKm(pts),
        });
        return pts;
      })
      .then((pts) => this.loadRoute(pts))
      .catch((e) => {
        if (e.status === 401) return wx.navigateTo({ url: '/pages/login/login' });
        wx.showToast({ title: e.message || '加载失败', icon: 'none' });
      });
  },

  /**
   * 逐段取真实驾车路线，拼成折线覆盖掉直线版本。
   *
   * 点多的时候这是 N-1 次请求，会打得后端很难受、用户也要等 —— 所以超过
   * AUTO_ROUTE_MAX_POINTS 就不自动跑，改成在地图上挂一个提示条让用户自己点。
   */
  loadRoute(pts, force) {
    if (!pts || pts.length < 2) return Promise.resolve();
    if (!force && pts.length > config.AUTO_ROUTE_MAX_POINTS) {
      this.setData({ routeHint: '点这里加载真实驾车路线（' + (pts.length - 1) + ' 段）' });
      return Promise.resolve();
    }
    this.setData({ routeHint: '正在算真实路线…' });
    const jobs = [];
    for (let i = 0; i < pts.length - 1; i++) {
      jobs.push(api.segment(
        { lat: pts[i].lat, lng: pts[i].lng },
        { lat: pts[i + 1].lat, lng: pts[i + 1].lng }
      ));
    }
    return Promise.all(jobs).then((segments) => {
      const got = segments.filter(Boolean).length;
      this.setData({
        polyline: geo.toPolyline(pts, segments),
        // 全失败就别留个「已加载」的假象，让用户知道现在看的是直线
        routeHint: got ? '' : '真实路线取不到，当前是直线连接',
      });
    });
  },

  onLoadRoute() {
    if (!this.data.routeHint) return;
    this.loadRoute(this.data.points, true);
  },

  onMarkerTap(e) {
    const id = e.detail.markerId;
    const idx = this.data.points.findIndex((p) => p.id === id);
    if (idx >= 0) wx.navigateTo({ url: '/pages/point/point?id=' + id });
  },

  onPoint(e) {
    wx.navigateTo({ url: '/pages/point/point?id=' + e.currentTarget.dataset.id });
  },

  onAlbum() {
    wx.navigateTo({ url: '/pages/album/album?trip=' + this.data.tripId });
  },
});

/** 直线总里程，只在真实路线还没到位时当占位显示。 */
function straightKm(pts) {
  let m = 0;
  for (let i = 0; i < pts.length - 1; i++) m += geo.haversine(pts[i], pts[i + 1]);
  return (m / 1000).toFixed(0);
}
