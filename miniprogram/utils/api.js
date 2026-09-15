/**
 * 后端接口客户端。
 *
 * 认证这块是全文件唯一麻烦的地方：Web 端靠浏览器自动带 tm_token cookie，
 * 而小程序的 wx.request **不管 cookie** —— 要自己从登录响应里把 Set-Cookie
 * 抠出来存起来，之后每个请求手动塞进 Cookie 头。
 *
 * （后端那个 cookie 标了 httpOnly，但那是浏览器的概念；wx.request 的
 * res.header 里照样拿得到 Set-Cookie，所以这条路是通的。）
 *
 * 表单编码也要注意：后端的写接口收的是 application/x-www-form-urlencoded
 * （FastAPI 的 Form(...)），不是 JSON。传 JSON 过去会得到 422。
 */
const config = require('../config');

const TOKEN_KEY = 'tm_token';

function getToken() {
  try {
    return wx.getStorageSync(TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

function setToken(v) {
  try {
    if (v) wx.setStorageSync(TOKEN_KEY, v);
    else wx.removeStorageSync(TOKEN_KEY);
  } catch (e) { /* 存储满了也不该让登录流程炸掉 */ }
}

/**
 * 从 Set-Cookie 响应头里取出 tm_token 的值。
 *
 * 微信不同版本给的头名大小写不一致（Set-Cookie / set-cookie），而且多条
 * cookie 可能被拼成一个字符串，所以这里全大小写扫一遍 + 正则抠。
 */
function extractToken(header) {
  if (!header) return '';
  for (const k of Object.keys(header)) {
    if (k.toLowerCase() !== 'set-cookie') continue;
    const raw = Array.isArray(header[k]) ? header[k].join(';') : String(header[k]);
    const m = raw.match(/tm_token=([^;,\s]+)/);
    if (m) return m[1];
  }
  return '';
}

/** 对象 -> a=1&b=2。值一律 encodeURIComponent，行程名里带 & 也不会截断。 */
function encodeForm(data) {
  return Object.keys(data || {})
    .filter((k) => data[k] !== undefined && data[k] !== null)
    .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(data[k]))
    .join('&');
}

function request(method, path, data, opts) {
  opts = opts || {};
  const token = getToken();
  const header = {};
  if (token) header.Cookie = TOKEN_KEY + '=' + token;

  let body = data;
  let url = config.BASE_URL + path;
  if (method === 'GET') {
    const qs = encodeForm(data);
    if (qs) url += (url.indexOf('?') === -1 ? '?' : '&') + qs;
    body = undefined;
  } else {
    header['content-type'] = 'application/x-www-form-urlencoded';
    body = encodeForm(data);
  }

  return new Promise((resolve, reject) => {
    wx.request({
      url,
      method,
      data: body,
      header,
      timeout: opts.timeout || 20000,
      success(res) {
        const fresh = extractToken(res.header);
        if (fresh) setToken(fresh);

        if (res.statusCode === 401) {
          // 会话过期（后端是 3 小时无活动滑动过期）。清掉本地 token，
          // 让调用方决定是跳登录还是静默重试，别在这儿硬跳页面 ——
          // 后台轮询触发的 401 把用户正在看的页面顶掉，体验很差。
          setToken('');
          const err = new Error('unauthorized');
          err.status = 401;
          return reject(err);
        }
        if (res.statusCode >= 400) {
          const d = res.data || {};
          const err = new Error(d.error || d.detail || ('HTTP ' + res.statusCode));
          err.status = res.statusCode;
          err.data = d;
          return reject(err);
        }
        resolve(res.data);
      },
      fail(e) {
        const err = new Error(e.errMsg || '网络请求失败');
        err.isNetwork = true;
        reject(err);
      },
    });
  });
}

const api = {
  getToken,
  setToken,
  extractToken,        // 导出给单元测试
  encodeForm,          // 同上

  get: (p, d, o) => request('GET', p, d, o),
  post: (p, d, o) => request('POST', p, d, o),
  put: (p, d, o) => request('PUT', p, d, o),
  del: (p, d, o) => request('DELETE', p, d, o),

  // ---------- 账号 ----------
  login: (username, password) => request('POST', '/api/login', { username, password }),
  logout: () => request('POST', '/logout', {}).then(() => setToken('')),
  me: () => request('GET', '/api/me'),

  // ---------- 行程 ----------
  trips: () => request('GET', '/api/trips'),
  trip: (id) => request('GET', '/api/trips/' + id + '/stats'),
  points: (tripId) => request('GET', '/api/trips/' + tripId + '/points'),
  tripMedia: (tripId) => request('GET', '/api/trips/' + tripId + '/media'),

  // ---------- 点与媒体 ----------
  pointMedia: (pointId) => request('GET', '/api/points/' + pointId + '/media'),
  setCaption: (mediaId, caption) =>
    request('PUT', '/api/media/' + mediaId + '/caption', { caption }),
  deleteMedia: (mediaId) => request('DELETE', '/api/media/' + mediaId),

  /** 逐段驾车路线。返回 [[lng,lat],...]；失败返回 null，交给调用方画直线。 */
  segment: (from, to) =>
    request('GET', '/api/route', {
      from_lng: from.lng, from_lat: from.lat, to_lng: to.lng, to_lat: to.lat,
    }).then((r) => {
      const routes = (r && r.routes) || [];
      return routes.length && routes[0].path ? routes[0].path : null;
    }).catch(() => null),

  /**
   * 上传一张照片 / 一段视频到某个点。
   *
   * 必须用 wx.uploadFile 而不是 wx.request —— 后者没法发 multipart。
   * 代价是它不共享 request 的那套 cookie 逻辑，所以这里要再塞一次头。
   */
  uploadMedia(pointId, filePath) {
    const token = getToken();
    return new Promise((resolve, reject) => {
      wx.uploadFile({
        url: config.BASE_URL + '/api/points/' + pointId + '/media',
        filePath,
        name: 'file',
        header: token ? { Cookie: TOKEN_KEY + '=' + token } : {},
        timeout: 120000,          // 视频大，给足时间
        success(res) {
          if (res.statusCode >= 400) {
            return reject(new Error('上传失败：HTTP ' + res.statusCode));
          }
          try {
            resolve(JSON.parse(res.data));
          } catch (e) {
            reject(new Error('上传返回的不是 JSON'));
          }
        },
        fail(e) { reject(new Error(e.errMsg || '上传失败')); },
      });
    });
  },
};

module.exports = api;
