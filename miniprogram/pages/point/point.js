const api = require('../../utils/api');
const fmt = require('../../utils/format');
const media = require('../../utils/media');
const upload = require('../../utils/upload');
const config = require('../../config');

Page({
  data: {
    pointId: 0, point: {}, dateText: '',
    media: [], captioned: [], loading: true, uploading: false, progress: 0,
  },

  onLoad(q) {
    this.setData({ pointId: Number(q.id) });
    this.refresh();
  },

  onShow() {
    // 媒体地址是 30 分钟就作废的签名 URL；从相册/播放页返回时多半已经过期了。
    if (this.data.pointId && !this.data.uploading
        && media.signaturesStale(this.fetchedAt)) this.refresh();
  },

  refresh() {
    return api.pointMedia(this.data.pointId)
      .then((list) => {
        const items = Array.isArray(list) ? list : ((list && (list.items || list.media)) || []);
        const arr = items.map((m) => Object.assign({}, m, {
          thumb: fmt.thumbUrl(m, config.BASE_URL),
          full: fmt.mediaUrl(m, config.BASE_URL),
          timeText: fmt.timeOfDay(m.exif_time),
        }));
        this.fetchedAt = Date.now();
        this.setData({
          loading: false,
          media: arr,
          captioned: arr.filter((m) => m.caption),
        });
      })
      .catch((e) => {
        this.setData({ loading: false });
        wx.showToast({ title: e.message || '加载失败', icon: 'none' });
      });
  },

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
        // 视频不能混进 previewImage，单独用全屏播放页。
        // 传 id 不传地址：地址会过期，id 不会（见 album.js 的 resolveVideo）。
        return wx.navigateTo({
          url: '/pages/album/album?mid=' + m.id + '&point=' + this.data.pointId,
        });
      }
      // 只把照片传给 previewImage：混了视频地址进去，滑到那一张是白屏
      const photos = this.data.media.filter((x) => x.type !== 'video');
      wx.previewImage({
        current: m.full,
        urls: photos.map((x) => x.full),
      });
    });
  },

  onUpload() {
    if (this.data.uploading) return;
    wx.chooseMedia({
      count: 1,
      mediaType: ['image', 'video'],
      sourceType: ['album', 'camera'],
      sizeType: ['original'],       // 原图：EXIF 的时间和坐标要靠它
      success: (res) => {
        const f = res.tempFiles && res.tempFiles[0];
        if (!f) return;
        this.setData({ uploading: true, progress: 0 });
        wx.showLoading({ title: '上传中 0%', mask: true });
        // 大视频走分片直传（见 utils/upload.js），断一片只重传那一片；
        // 直传没开的话它自己会回落到服务器中转，这里不用关心走了哪条。
        upload.uploadMedia(this.data.pointId, f.tempFilePath, f.size, (pct) => {
          if (pct === this.data.progress) return;
          this.setData({ progress: pct });
          wx.showLoading({ title: '上传中 ' + pct + '%', mask: true });
        })
          .then(() => this.refresh())
          .catch((e) => wx.showToast({ title: e.message || '上传失败', icon: 'none' }))
          .then(() => {
            wx.hideLoading();
            this.setData({ uploading: false, progress: 0 });
          });
      },
    });
  },
});
