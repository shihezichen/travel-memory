const api = require('../../utils/api');
const fmt = require('../../utils/format');
const config = require('../../config');

Page({
  data: {
    pointId: 0, point: {}, dateText: '',
    media: [], captioned: [], loading: true, uploading: false,
  },

  onLoad(q) {
    this.setData({ pointId: Number(q.id) });
    this.refresh();
  },

  refresh() {
    return api.pointMedia(this.data.pointId)
      .then((list) => {
        const media = (list || []).map((m) => Object.assign({}, m, {
          thumb: fmt.thumbUrl(m, config.BASE_URL, config.MEDIA_HOST),
          full: fmt.mediaUrl(m, config.BASE_URL),
          timeText: fmt.timeOfDay(m.exif_time),
        }));
        this.setData({
          loading: false,
          media,
          captioned: media.filter((m) => m.caption),
        });
      })
      .catch((e) => {
        this.setData({ loading: false });
        wx.showToast({ title: e.message || '加载失败', icon: 'none' });
      });
  },

  onPreview(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    const m = this.data.media[idx];
    if (!m) return;
    if (m.type === 'video') {
      // 视频不能混进 previewImage，单独用全屏播放器
      return wx.navigateTo({
        url: '/pages/album/album?video=' + encodeURIComponent(m.full),
      });
    }
    // 只把照片传给 previewImage：混了视频地址进去，滑到那一张是白屏
    const photos = this.data.media.filter((x) => x.type !== 'video');
    wx.previewImage({
      current: m.full,
      urls: photos.map((x) => x.full),
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
        this.setData({ uploading: true });
        wx.showLoading({ title: '上传中…', mask: true });
        api.uploadMedia(this.data.pointId, f.tempFilePath)
          .then(() => this.refresh())
          .catch((e) => wx.showToast({ title: e.message || '上传失败', icon: 'none' }))
          .then(() => {
            wx.hideLoading();
            this.setData({ uploading: false });
          });
      },
    });
  },
});
