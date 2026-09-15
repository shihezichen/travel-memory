const api = require('../../utils/api');
const fmt = require('../../utils/format');
const config = require('../../config');

const app = getApp();

Page({
  data: {
    loading: true,
    demoing: false,
    user: null,
    trips: [],
  },

  onShow() {
    // onShow 而不是 onLoad：从行程详情返回时行程名/状态可能改过了，
    // 每次露面都刷一遍，比在详情页回传事件简单可靠。
    this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    return app.whenReady()
      .then((user) => {
        this.setData({ user });
        if (!user) return [];
        return api.trips();
      })
      .then((trips) => {
        this.setData({ loading: false, trips: (trips || []).map(decorate) });
      })
      .catch((e) => {
        this.setData({ loading: false });
        // 401 不弹提示：whenReady 已经把 user 置空，页面会显示体验入口
        if (e.status !== 401) toast(e.message || '加载失败');
      });
  },

  onDemo() {
    if (this.data.demoing) return;
    this.setData({ demoing: true });
    api.login(config.DEMO.username, config.DEMO.password)
      .then(() => app.restore())
      .then(() => this.refresh())
      .catch((e) => toast(e.message || '进不去，稍后再试'))
      .then(() => this.setData({ demoing: false }));
  },

  onLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  onOpen(e) {
    wx.navigateTo({ url: '/pages/trip/trip?id=' + e.currentTarget.dataset.id });
  },

  onLogout() {
    wx.showModal({
      title: '退出登录',
      content: '确定要退出吗？',
      success: (r) => {
        if (!r.confirm) return;
        api.logout()
          .catch(() => {})            // 服务端失败也要清本地，否则退不掉
          .then(() => {
            api.setToken('');
            app.globalData.user = null;
            this.setData({ user: null, trips: [] });
          });
      },
    });
  },
});

function decorate(t) {
  return Object.assign({}, t, {
    statusText: fmt.statusText(t.status),
    statusClass: fmt.statusClass(t.status),
    range: fmt.dateRange(t.start_date, t.end_date),
    days: fmt.dayCount(t.start_date, t.end_date),
  });
}

function toast(title) {
  wx.showToast({ title: String(title).slice(0, 20), icon: 'none' });
}
