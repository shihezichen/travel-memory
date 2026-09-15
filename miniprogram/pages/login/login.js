const api = require('../../utils/api');
const app = getApp();

Page({
  data: { username: '', password: '', busy: false, err: '' },

  onU(e) { this.setData({ username: e.detail.value, err: '' }); },
  onP(e) { this.setData({ password: e.detail.value, err: '' }); },

  onSubmit() {
    const { username, password, busy } = this.data;
    if (busy) return;
    if (!username || !password) return this.setData({ err: '用户名和密码都要填' });
    this.setData({ busy: true, err: '' });
    api.login(username, password)
      .then(() => app.restore())
      .then(() => wx.navigateBack())
      .catch((e) => this.setData({
        // 后端对错误的用户名/密码统一回「用户名或密码错误」，照搬就行
        err: e.status === 401 ? '用户名或密码错误' : (e.message || '登录失败'),
      }))
      .then(() => this.setData({ busy: false }));
  },
});
