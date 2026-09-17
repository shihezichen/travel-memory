const api = require('../../utils/api');
const fmt = require('../../utils/format');
const media = require('../../utils/media');
const config = require('../../config');

Page({
  data: { tripId: 0, media: [], loading: true, videoUrl: '', qualityName: '' },

  onLoad(q) {
    // 这个页面兼作视频全屏播放器的落点（相册格子和点位页都跳这儿）。
    // 小程序没有「弹层」这种东西，全屏播放只能是一个独立页面。
    //
    // 传过来的是**媒体 id 加它所属的行程/点位**，不是地址 —— 地址是 30 分钟
    // 就作废的签名 URL，塞进页面参数里带过来，用户从后台切回来时就已经是废的了。
    // 带 id 的话随时能重新解析一个新的出来（见 resolveVideo）。
    if (q.mid) {
      wx.setNavigationBarTitle({ title: '视频' });
      this.videoRef = { mid: Number(q.mid), trip: Number(q.trip || 0), point: Number(q.point || 0) };
      return this.resolveVideo();
    }
    this.setData({ tripId: Number(q.trip) });
    this.refresh();
  },

  onShow() {
    // 媒体地址 30 分钟就作废。页面被压在栈里很久再回来，手上那批链接多半
    // 已经过期了 —— 静默换一批，用户不该看见一屏裂图。
    if (this.data.tripId && media.signaturesStale(this.fetchedAt)) this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    if (!this.data.tripId) return Promise.resolve();
    return api.tripMedia(this.data.tripId)
      .then((list) => {
        // 后端返回的是 {items: [...], count: n}（app.py 的 api_trip_media）。
        // 另外两种写法只是兼容：老接口的裸数组、以及手写的调试数据。
        const items = Array.isArray(list) ? list : ((list && (list.items || list.media)) || []);
        this.fetchedAt = Date.now();
        this.setData({
          loading: false,
          media: items.map((m) => Object.assign({}, m, {
            thumb: fmt.thumbUrl(m, config.BASE_URL),
            full: fmt.mediaUrl(m, config.BASE_URL),
          })),
        });
      })
      .catch((e) => {
        this.setData({ loading: false });
        wx.showToast({ title: e.message || '加载失败', icon: 'none' });
      });
  },

  // ---------------------------------------------------------------- 视频

  /**
   * 拿着 media id 重新解析一个能播的地址。
   *
   * 每次进页面、以及播放器报错时都走这里：报错绝大多数就是签名过期
   * （OSS 回 403，播放器只会给一个没有细节的 MEDIA_ERR）。重解析一次
   * 比让用户自己退出去再点一遍强。
   */
  resolveVideo() {
    const ref = this.videoRef;
    const load = ref.point ? api.pointMedia(ref.point) : api.tripMedia(ref.trip);
    return Promise.all([load, media.currentCap()])
      .then(([list, cap]) => {
        const items = Array.isArray(list) ? list : ((list && (list.items || list.media)) || []);
        const m = items.filter((x) => x.id === ref.mid)[0];
        if (!m) throw new Error('这个视频已经不在了');
        const q = media.pickQuality(m.qualities, cap);
        this.setData({
          loading: false,
          videoUrl: (q && q.url) || fmt.mediaUrl(m, config.BASE_URL),
          // 让用户知道现在看的是哪一档 —— 手机上默认不上 1080p，不说一声
          // 会被当成「画质变差了」
          qualityName: (q && q.name) || '',
        });
      })
      .catch((e) => {
        this.setData({ loading: false });
        wx.showToast({ title: e.message || '视频加载失败', icon: 'none' });
      });
  },

  /** <video> 报错。节流一下，别让一次错误引发连环重解析。 */
  onVideoError() {
    if (this._reresolving) return;
    this._reresolving = true;
    this.resolveVideo().then(() => {
      setTimeout(() => { this._reresolving = false; }, 3000);
    });
  },

  // ---------------------------------------------------------------- 网格

  /** 签名可能过期了就先换一批再动作，否则 previewImage 打开是一片白。 */
  withFresh(fn) {
    if (!media.signaturesStale(this.fetchedAt)) return fn();
    wx.showLoading({ title: '加载中…', mask: true });
    return this.refresh().then(() => { wx.hideLoading(); fn(); });
  },

  onPreview(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    this.withFresh(() => {
      const m = this.data.media[idx];
      if (!m) return;
      if (m.type === 'video') {
        return wx.navigateTo({
          url: '/pages/album/album?mid=' + m.id + '&trip=' + this.data.tripId,
        });
      }
      const photos = this.data.media.filter((x) => x.type !== 'video');
      wx.previewImage({ current: m.full, urls: photos.map((x) => x.full) });
    });
  },
});
