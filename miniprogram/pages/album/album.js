const api = require('../../utils/api');
const fmt = require('../../utils/format');
const config = require('../../config');

Page({
  data: { tripId: 0, media: [], loading: true },

  onLoad(q) {
    // 这个页面兼作视频全屏播放器的落点（点位页跳过来时带 video 参数）。
    // 小程序没有「弹层」这种东西，全屏播放只能是一个独立页面。
    if (q.video) {
      const url = decodeURIComponent(q.video);
      wx.setNavigationBarTitle({ title: '视频' });
      return this.setData({ loading: false, videoUrl: url });
    }
    this.setData({ tripId: Number(q.trip) });
    this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    if (!this.data.tripId) return Promise.resolve();
    return api.tripMedia(this.data.tripId)
      .then((list) => {
        const items = Array.isArray(list) ? list : (list && list.media) || [];
        this.setData({
          loading: false,
          media: items.map((m) => Object.assign({}, m, {
            thumb: fmt.thumbUrl(m, config.BASE_URL, config.MEDIA_HOST),
            full: fmt.mediaUrl(m, config.BASE_URL),
          })),
        });
      })
      .catch((e) => {
        this.setData({ loading: false });
        wx.showToast({ title: e.message || '加载失败', icon: 'none' });
      });
  },

  onPreview(e) {
    const m = this.data.media[Number(e.currentTarget.dataset.idx)];
    if (!m) return;
    if (m.type === 'video') {
      return wx.navigateTo({ url: '/pages/album/album?video=' + encodeURIComponent(m.full) });
    }
    const photos = this.data.media.filter((x) => x.type !== 'video');
    wx.previewImage({ current: m.full, urls: photos.map((x) => x.full) });
  },
});
