/**
 * 小程序入口。
 *
 * 启动时先拿本地 token 探一下 /api/me：
 *   - 通了  -> 直接进行程列表，用户感觉不到登录这一步
 *   - 401   -> 留在列表页，由列表页引导去登录或一键体验
 * 不在这里强跳登录页 —— 冷启动就被弹一个登录框，劝退率很高，
 * 而这个产品本来就有公开的体验账号。
 */
const api = require('./utils/api');

App({
  globalData: {
    user: null,
    ready: false,       // /api/me 探测是否已经跑完
  },

  onLaunch() {
    this.restore();
  },

  /** 返回 Promise，页面可以 await 它再决定渲染什么。 */
  restore() {
    if (this._restoring) return this._restoring;
    this._restoring = api.me()
      .then((u) => { this.globalData.user = u; return u; })
      .catch(() => { this.globalData.user = null; return null; })
      .then((u) => {
        this.globalData.ready = true;
        (this._waiters || []).forEach((fn) => fn(u));
        this._waiters = [];
        this._restoring = null;
        return u;
      });
    return this._restoring;
  },

  /** 页面用：等登录态确定下来。已经确定就立刻回。 */
  whenReady() {
    if (this.globalData.ready) return Promise.resolve(this.globalData.user);
    return this.restore();
  },
});
