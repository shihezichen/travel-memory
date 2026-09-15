// 旅行记忆系统 — 前端逻辑
// ---------- 登录门禁（2026-08-26 刚哥修正：普通用户 WEB 访问 = 用户名+密码，取消访问口令） ----------
// 页面加载即检查会话；未登录 → 弹出登录框，登录成功后进入应用
(async function sessionGate() {
  try {
    const res = await fetch('/api/me');
    if (res.status === 401) {
      showLogin();
    }
  } catch (e) { /* 服务不可达时不阻塞页面渲染 */ }
})();

let map = null;
let markers = [];       // 当前行程的点 marker
let polyline = null;    // 轨迹线
let drivingInstance = null; // 驾车路线规划（懒加载单例）
let routeCandidates = []; // 多方案选择时临时绘制的候选路线（Polyline）
let segPolylines = [];  // 逐段绘制的路线（每段可点击），对应 segmentData
let segLabels = [];     // 选中点时贴在相邻两段路线中点的里程标签
let segmentData = [];   // 每段路线的信息：{ i, from, to, mode, choices, path, picked }
                        // picked：用户在「选择路线方案」里挑中的那条 choice，没挑过为 null
let fallbackSegs = false; // 直线兜底临时分段标记（真实路线未就绪时高亮相邻段用）
let renderSeq = 0;      // 渲染序号，防止异步路线规划竞态
let viewRestored = false; // 本次渲染是否用了保存的地图视野（用了就别再自适应顶掉它）
let lastPoints = [];    // 当前行程的点（用于切换连线方式时重画）
let lastStats = null;   // 当前行程 stats（路线就绪后刷新统计用）
let photoMarkers = [];  // 照片地图打点 marker（有照片/视频的地点）
let footprintMarkers = []; // 足迹点亮 CircleMarker（所有去过的地方，Fog of World 风格）
let footprintData = null;   // 全量足迹坐标缓存（/api/footprint）
// 点数多了之后服务端会按当前缩放级别做网格聚合（响应里 aggregated=true）。
// 聚合过的数据换个缩放级别就该重新聚一次，否则放大之后还是那几个粗格子。
// 点少时服务端返回精确坐标、aggregated=false，那就一次拉完永不重拉。
let footprintAggregated = false;
let footprintView = null;   // 当前这份数据对应哪个「缩放级别 + 可视范围」
// 照片堆不再走 AMap.Marker，改由 photoStackItems + 自建图层承载（见 ensurePhotoStackLayer）
let photoLayerData = null;  // 当前行程的照片堆数据缓存（/api/photo-layer?trip_id=，含照片+视频），换行程即失效
let currentTripId = null;
let currentPointId = null;
let currentUser = null;  // /api/me 返回（含 last_trip_id：服务端记的上次打开行程）
let autocomplete = null;
let searchSeq = 0;     // 地名联想搜索序号，防竞态
let editPointId = null; // 编辑点时的点 id
let editTripId = null;  // 编辑行程时的行程 id
let placesDict = [];    // 5 国（西/土/英/爱/法）中文↔英文地名对照词典

// 灯箱状态
let lightboxMedia = [];   // 当前点所有媒体
let lightboxIndex = 0;    // 当前索引

// 轨迹回放动画状态
let replayMarker = null;   // 移动的小圆点 marker
let replayRaf = null;      // requestAnimationFrame 句柄
let replayActive = false;  // 是否播放中
let replayPath = [];       // 播放路径（拼接后的坐标数组）
let replayWayIdx = [];     // path 中对应行程点的索引
let replayCum = [];        // 每点累计距离（米）
let replayTotal = 0;       // 路径总长（米）
let replayT = 0;           // 已播放距离（米）
let replaySpeed = 1;       // 播放速度倍率
let replayLast = 0;        // 上一帧时间戳
let replayDoneLine = null; // 已走过的路线（绿色高亮）
let replayTodoLine = null; // 未走过的路线（灰色压暗）
const REPLAY_BASE_MS = 30000; // 1x 速度播完整条轨迹的时长

// 多方案候选路线的配色（最多 5 条，循环取色）
const ROUTE_COLORS = ['#ef4444', '#f59e0b', '#10b981', '#3b82f6', '#8b5cf6'];

// ---------- 初始化地图 ----------
function initMap() {
  map = new AMap.Map('map', {
    center: [104.0, 35.0],
    zoom: 4,
    resizeEnable: true,
    viewMode: '2D',  // 微信内置浏览器 WebGL 渲染不完整，强制 2D 保证地名标注正常显示
  });

  // 地名联想插件。
  // 这个类在两个 SDK 版本里名字不一样：2.0 叫 AMap.AutoComplete（大写 C），
  // 1.4.15（微信内置浏览器走这版）叫 AMap.Autocomplete（小写 c）。
  // 原来 index.html 的 plugin= 参数和这里都只写了小写那个，2.0 的 SDK 认不出这个名字、
  // 却又把它当成「已请求」，于是回调照常触发、类根本没加载，new 出来直接抛
  // "AMap.Autocomplete is not a constructor" —— 新增轨迹点时的地名联想就这么静默失效了
  // （调用处有 if (autocomplete) 兜着，所以不崩，只是永远搜不出高德的候选）。
  // 两个名字都请求一遍，谁在就用谁。
  AMap.plugin(['AMap.AutoComplete', 'AMap.Autocomplete'], function () {
    const Ctor = AMap.AutoComplete || AMap.Autocomplete;
    if (typeof Ctor !== 'function') {
      console.warn('高德地名联想插件没加载上，新增点时只能用 Open-Meteo 那一路兜底');
      return;
    }
    autocomplete = new Ctor({});
  });

  // 记录视野（缩放/中心），刷新后恢复。
  // zoomend 必须一起绑：只缩放不平移时（双指捏合、滚轮、双击）高德不发 moveend，
  // 新的缩放级别就永远存不下来，刷新后自然回不到原来的画面。
  map.on('moveend', saveMapView);
  map.on('zoomend', saveMapView);
  map.on('zoomend', syncPhotoStackScale);   // 照片堆大小跟着缩放级别走
  map.on('zoomend', syncElevLabelsVisibility);   // 缩太小就把海拔小标收起来
  map.on('zoomend', onFootprintViewChanged);    // 足迹聚合过的话，换缩放级别要重聚
  map.on('moveend', onFootprintViewChanged);    // 平移也一样：视野变了，该聚的点也变了
}

// ---------- 工具 ----------

// ---------- 自定义对话框（替代原生 alert / confirm / prompt） ----------
// 原生弹窗的标题栏由浏览器写死为「<域名/IP> 显示」，JS 改不了，只能自绘。
// 自绘的另一个好处：样式跟站内统一，移动端不再是系统味的灰框。
function tmDialog(opts) {
  const o = Object.assign({
    title: '提示', message: '', okText: '确定', cancelText: '取消',
    danger: false, showCancel: true, input: null, multiline: false, rows: 3,
  }, opts);
  return new Promise(resolve => {
    const mask = el('div', 'tm-dialog-mask');
    const box = el('div', 'tm-dialog');
    box.innerHTML =
      `<div class="tm-dialog-title">${esc(o.title)}</div>` +
      // message 为空就整行不渲染：标题已经说清楚要干什么时，再来一行说明纯属占地方，
      // 手机上会把真正能写字的区域挤掉一行
      (o.message
        // 多行（带列表的）文案居中读着别扭，自动改左对齐
        ? `<div class="tm-dialog-msg${String(o.message).indexOf('\n') >= 0 ? ' left' : ''}">${esc(o.message)}</div>`
        : '') +
      (o.input !== null
        ? (o.multiline
            ? `<textarea class="tm-dialog-input tm-dialog-textarea" rows="${Number(o.rows) || 3}">${esc(o.input)}</textarea>`
            : `<input type="text" class="tm-dialog-input" value="${esc(o.input)}">`)
        : '') +
      `<div class="tm-dialog-actions">` +
      (o.showCancel ? `<button type="button" class="btn tm-dialog-cancel">${esc(o.cancelText)}</button>` : '') +
      `<button type="button" class="btn ${o.danger ? 'btn-danger' : 'btn-primary'} tm-dialog-ok">${esc(o.okText)}</button>` +
      `</div>`;
    mask.appendChild(box);
    document.body.appendChild(mask);
    const field = box.querySelector('.tm-dialog-input');

    function done(v) {
      mask.remove();
      document.removeEventListener('keydown', onKey, true);
      resolve(v);
    }
    // 捕获阶段 + stopPropagation：不让 Esc/Enter 漏给灯箱、地图全屏那些全局快捷键
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); done(o.input !== null ? null : false); }
      else if (e.key === 'Enter' && !o.multiline) { e.stopPropagation(); done(o.input !== null ? (field ? field.value : '') : true); }
    }
    box.querySelector('.tm-dialog-ok').onclick =
      () => done(o.input !== null ? (field ? field.value : '') : true);
    const cancel = box.querySelector('.tm-dialog-cancel');
    if (cancel) cancel.onclick = () => done(o.input !== null ? null : false);
    mask.addEventListener('click', e => {
      if (e.target === mask && o.showCancel) done(o.input !== null ? null : false);
    });
    document.addEventListener('keydown', onKey, true);
    setTimeout(() => {
      if (field) { field.focus(); field.select(); }
      else box.querySelector('.tm-dialog-ok').focus();
    }, 30);
  });
}
function tmAlert(message, title) {
  return tmDialog({ title: title || '提示', message, showCancel: false, okText: '知道了' });
}
function tmConfirm(message, title, opts) {
  return tmDialog(Object.assign({ title: title || '确认', message }, opts || {}));
}
function tmPrompt(message, defaultValue, title, opts) {
  return tmDialog(Object.assign(
    { title: title || '请输入', message, input: defaultValue || '' }, opts || {}));
}

// ---------- 界面状态持久化（刷新后保持：行程/视野/面板高度/选中点/tab，按行程隔离） ----------
const STATE_KEY = 'tm_ui_state';
function getState() {
  try { return JSON.parse(localStorage.getItem(STATE_KEY) || 'null') || {}; } catch (e) { return {}; }
}
function saveState(patch) {
  try {
    const s = getState();
    Object.assign(s, patch);
    localStorage.setItem(STATE_KEY, JSON.stringify(s));
  } catch (e) {}
}
function getTripState() {
  const s = getState();
  const k = currentTripId ? String(currentTripId) : null;
  return (k && s.trips && s.trips[k]) || {};
}
function saveTripState(patch) {
  try {
    const s = getState();
    const k = currentTripId ? String(currentTripId) : null;
    if (!k) return;
    if (!s.trips) s.trips = {};
    s.trips[k] = Object.assign(s.trips[k] || {}, patch);
    localStorage.setItem(STATE_KEY, JSON.stringify(s));
  } catch (e) {}
}

// ---------- 图层勾选 + 灯箱状态持久化（刷新保留） ----------
const LAYER_STATE_KEY = 'tm_layer_state';
const LIGHTBOX_STATE_KEY = 'tm_lightbox_state';
function getLayerState() {
  try { return JSON.parse(localStorage.getItem(LAYER_STATE_KEY) || 'null') || {}; } catch (e) { return {}; }
}
function saveLayerState(patch) {
  try {
    const s = getLayerState();
    Object.assign(s, patch);
    localStorage.setItem(LAYER_STATE_KEY, JSON.stringify(s));
  } catch (e) {}
}
function saveLightboxState() {
  try {
    const m = lightboxMedia[lightboxIndex];
    if (!m || !m.point_id) return;
    localStorage.setItem(LIGHTBOX_STATE_KEY, JSON.stringify({ point_id: m.point_id, index: lightboxIndex }));
  } catch (e) {}
}
async function restoreLayerState() {
  const s = getLayerState();
  const apply = (sel, key, fn) => {
    const cb = $(sel);
    if (!cb || s[key] === undefined) return;
    cb.checked = !!s[key];
    if (cb.checked) { try { fn(); } catch (e) {} }
  };
  apply('#lay-poi', 'poi', () => toggleLayerPoi(true));
  apply('#lay-satellite', 'satellite', () => toggleLayerSatellite(true));
  apply('#lay-traffic', 'traffic', () => toggleLayerTraffic(true));
  apply('#lay-heatmap', 'heatmap', () => toggleLayerHeatmap(true));
  // 「照片视频」默认关：照片一多，堆在地图上就把路线和地名全盖住了，
  // 第一眼该看清的是轨迹本身。想看照片的人自己勾，勾过之后一直尊重他的选择。
  const photoCb = $('#lay-photos');
  if (photoCb) {
    const want = s.photos === undefined ? false : !!s.photos;
    photoCb.checked = want;
    if (want) { try { toggleLayerPhotos(true, true); } catch (e) {} }
  }
  // 「里程标签」默认开：加开关之前它就是常驻的，没显式关过就保持原样。
  // 这里不能用上面的 apply()——那个只在勾上时才执行，而里程恰好要在**取消**勾选时
  // 才需要动手（默认状态是显示）。
  const mileageCb = $('#lay-mileage');
  if (mileageCb) {
    mileageCb.checked = s.mileage === undefined ? true : !!s.mileage;
    applySegLabelsVisibility();
  }
  // 「公路编号」同理：默认开，没显式关过就显示
  const roadCb = $('#lay-road');
  if (roadCb) {
    roadCb.checked = s.road === undefined ? true : !!s.road;
    applyRoadLabelsVisibility();
  }
}
async function restoreLightboxState() {
  try {
    const s = JSON.parse(localStorage.getItem(LIGHTBOX_STATE_KEY) || 'null');
    if (!s || !s.point_id) return;
    const media = await api('/api/points/' + s.point_id + '/media');
    if (!media || !media.length) return;
    lightboxMedia = media;
    lightboxIndex = Math.min(s.index || 0, media.length - 1);
    renderLightbox();
    $('#lightbox').classList.remove('hidden');
  } catch (e) {}
}
function saveMapView() {
  if (!map || !currentTripId) return;
  const z = map.getZoom(), c = map.getCenter();
  if (z == null || !c) return;
  saveTripState({ zoom: z, center: [c.lng, c.lat] });
}
// 轻提示（右下角浮一条，自动消失）
let _toastTimer = null;
// ---------- 版本漂移检测 ----------
// 加到桌面的 PWA / 一直没关的标签页可能几天不重新加载 index.html，于是一直跑着
// 旧 JS：新功能到不了它手上，新修的 bug 在它那儿还在。2026-09-11 就踩了一次
// （角标不刷新、上传还在走隧道）。自己的版本号从本脚本的 ?v= 里读，服务端在每个
// /api 响应上带 X-Asset-Version，一比对上不一致就提示刷新——只提示一次，不强刷。
const MY_ASSET_V = (function () {
  const src = (document.currentScript && document.currentScript.src) || '';
  const m = src.match(/[?&]v=([^&]+)/);
  return m ? m[1] : '';
})();
let _staleNoticed = false;

function noticeStaleAssets() {
  if (_staleNoticed) return;
  _staleNoticed = true;
  const bar = el('div', 'stale-assets-bar');
  bar.appendChild(el('span', '', '页面版本已更新，刷新后才是最新功能'));
  const btn = el('button', 'stale-assets-reload', '刷新');
  btn.type = 'button';
  // reload(true) 早就不做强刷了；改成给地址加一个一次性查询串，绕过所有中间缓存
  btn.onclick = () => {
    const u = new URL(location.href);
    u.searchParams.set('_r', Date.now().toString(36));
    location.replace(u.toString());
  };
  bar.appendChild(btn);
  document.body.appendChild(bar);
}

function checkAssetVersion(res) {
  if (!MY_ASSET_V) return;                       // 没带版本号（本地直接开文件）就别管
  const v = res.headers.get('X-Asset-Version');
  if (v && v !== MY_ASSET_V) noticeStaleAssets();
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  checkAssetVersion(res);
  if (res.status === 401) {
    showLogin();
    throw new Error('未登录或令牌无效');
  }
  if (!res.ok) {
    const msg = await res.text();
    throw new Error(msg || res.statusText);
  }
  return res.json();
}

// ---------- 登录 / 认证 ----------
function showLogin() { $('#login-overlay').classList.remove('hidden'); }
function hideLogin() { $('#login-overlay').classList.add('hidden'); }
function setLoginError(msg) {
  const e = $('#login-error');
  if (msg) { e.textContent = msg; e.classList.remove('hidden'); }
  else { e.textContent = ''; e.classList.add('hidden'); }
}

async function ensureAuth() {
  try {
    await api('/api/me');
    hideLogin();
    await loadCurrentUser();
    return true;
  } catch (e) {
    showLogin();
    return false;
  }
}

async function loadCurrentUser() {
  try {
    const me = await api('/api/me');
    currentUser = me;
    $('#current-user').textContent = (me.is_admin ? '👑 ' : '👤 ') +
      me.display_name + '（' + me.username + '）';
  } catch (e) { /* 忽略 */ }
}

// 认证 tab 切换
document.querySelectorAll('.auth-tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.auth-tab').forEach(x =>
      x.classList.toggle('active', x === t));
    const mode = t.dataset.auth;
    $('#login-form').classList.toggle('hidden', mode !== 'login');
    $('#register-form').classList.toggle('hidden', mode !== 'register');
    setLoginError('');
  };
});

// 体验账号一键登录：凭据写死在这里，用户不需要（也无法）输入或修改
const DEMO_USER = 'test', DEMO_PASS = 'test1234';
$('#btn-demo-login').addEventListener('click', async () => {
  const btn = $('#btn-demo-login');
  btn.disabled = true;
  setLoginError('');
  try {
    const fd = new FormData();
    fd.append('username', DEMO_USER);
    fd.append('password', DEMO_PASS);
    const res = await fetch('/api/login', { method: 'POST', body: fd });
    if (res.ok) { onLoggedIn(); return; }
    const data = await res.json().catch(() => ({}));
    setLoginError(data.error || '体验账号登录失败');
  } catch (e) {
    setLoginError('体验账号登录失败：' + e.message);
  }
  btn.disabled = false;
});

// 管理员令牌登录的入口已从登录页移除（2026-09-10）：普通用户看不懂、也用不上，
// 摆在那里只是噪音。后端 /api/login 仍然接受 token 字段，需要时用 curl 走接口即可。

// 账号登录
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#login-username').value.trim();
  const password = $('#login-password').value;
  if (!username || !password) { setLoginError('请输入用户名和密码'); return; }
  const fd = new FormData();
  fd.append('username', username);
  fd.append('password', password);
  const res = await fetch('/api/login', { method: 'POST', body: fd });
  if (res.ok) {
    onLoggedIn();
  } else {
    const data = await res.json().catch(() => ({}));
    setLoginError(data.error || '登录失败');
  }
});

// 注册
$('#register-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#reg-username').value.trim();
  const display = $('#reg-display').value.trim();
  const password = $('#reg-password').value;
  const password2 = $('#reg-password2').value;
  if (!username) { setLoginError('请输入用户名'); return; }
  if (password.length < 6) { setLoginError('密码至少 6 位'); return; }
  if (password !== password2) { setLoginError('两次密码不一致'); return; }
  const fd = new FormData();
  fd.append('username', username);
  fd.append('password', password);
  fd.append('display_name', display);
  const res = await fetch('/api/register', { method: 'POST', body: fd });
  if (res.ok) {
    onLoggedIn();
  } else {
    const data = await res.json().catch(() => ({}));
    setLoginError(data.error || '注册失败');
  }
});

// 从需要登录的页面被弹回来时（见 app.py 的 auth 中间件），?next= 记着原来要去哪
function pendingNext() {
  try {
    const n = new URLSearchParams(location.search).get('next');
    // 只接受本站的绝对路径，挡掉 //evil.com 这类开放重定向
    return (n && n.startsWith('/') && !n.startsWith('//')) ? n : null;
  } catch (e) { return null; }
}

function onLoggedIn() {
  const next = pendingNext();
  if (next) { location.replace(next); return; }
  setLoginError('');
  $('#login-username').value = '';
  $('#login-password').value = '';
  $('#reg-password').value = '';
  $('#reg-password2').value = '';
  hideLogin();
  // 登录后同样要恢复图层勾选与灯箱状态。原来只有「打开页面时已登录」那条路径调了，
  // 走登录框进来的用户图层偏好一直没被恢复（照片视频默认开也因此不生效）。
  loadCurrentUser().then(loadTrips).then(() => {
    restoreLayerState();
    restoreLightboxState();
  });
}

// 退出登录
$('#btn-logout').onclick = async () => {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
  currentUser = null;
  currentTripId = null;
  currentPointId = null;
  clearMap();
  // 退出后回站点首页（未登录状态下那里是宣传页），而不是把人留在
  // /app 的登录浮层上 —— 那个浮层是「想进去的人」才需要看到的东西。
  location.href = '/';
};

// 强制重新计算路线（清空后端缓存后重画当前行程曲线）
$('#btn-recalc').onclick = async () => {
  if (!currentTripId) return;
  // 后端 clear_route_cache() 是 DELETE 整张 route_cache 表，影响的是全部行程而不只是当前这条，
  // 且会重新走高德/OSRM 的路线规划（消耗配额、需要等待），所以先跟用户确认清楚。
  const ok = await tmConfirm(
    '会清空所有行程已缓存的路线，然后重新向地图服务请求规划。\n\n' +
    '· 影响范围是全部行程，不只是当前这条\n' +
    '· 点的位置和照片不受影响，只重算连线\n' +
    '· 行程较多时可能要等十几秒，期间路线会先显示为直线',
    '重新计算路线',
    { okText: '重新计算' }
  );
  if (!ok) return;
  const btn = $('#btn-recalc');
  btn.disabled = true;
  try {
    await api('/api/route/clear-cache', { method: 'POST' });
  } catch (e) { /* 清缓存失败也继续重画 */ }
  await loadPoints();
  btn.disabled = false;
};
function fmtDate(d) {
  if (!d) return '未定';
  if (d.length === 10) return d;
  return d;
}

// ---------- 行程 ----------
// preferId：指定这次要落到哪个行程（新建行程后传新 id，优先级高于「上次打开的」）
async function loadTrips(preferId) {
  const trips = await api('/api/trips');
  allTrips = trips;                 // 手机端的切换条/卡片选择器要用
  const sel = $('#trip-select');
  sel.innerHTML = '';
  if (trips.length === 0) {
    sel.innerHTML = '<option value="">（暂无行程，点右上角新建）</option>';
    syncTripNav();
    return;
  }
  trips.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.name;
    sel.appendChild(opt);
  });
  // 恢复上次打开的行程：服务端记录（跨设备/换浏览器有效）优先，其次本地 localStorage，
  // 都没有或指向已删除的行程才回落到第一个（id 归一为数字，兼容 localStorage 字符串）
  const saved = getState();
  const localId = (saved && saved.currentTripId != null) ? Number(saved.currentTripId) : null;
  const serverId = (currentUser && currentUser.last_trip_id != null)
    ? Number(currentUser.last_trip_id) : null;
  const valid = id => id && trips.some(t => Number(t.id) === id);
  const wanted = preferId != null ? Number(preferId) : null;
  const target = valid(wanted) ? wanted
    : (valid(serverId) ? serverId : (valid(localId) ? localId : trips[0].id));
  await selectTrip(target);
}

async function selectTrip(id, dir) {
  // 先等旧内容整段滑走再换数据——不然加载太快，过渡会一闪而过看不见
  if (dir) await beginTripSwitch(dir);
  currentTripId = Number(id);
  currentPointId = null;
  $('#trip-select').value = String(id);
  syncTripNav();
  hideDetail();
  saveState({ currentTripId: Number(id) });
  rememberLastTrip(currentTripId);
  try {
    await loadPoints();
  } finally {
    if (dir) endTripSwitch(dir);
  }
}

// 把「当前打开的行程」写回服务端，下次登录（含换设备）自动恢复。
// 失败不影响使用（localStorage 仍然兜底），故不 await、不弹错。
function rememberLastTrip(id) {
  if (!currentUser || !id) return;
  if (Number(currentUser.last_trip_id) === Number(id)) return;  // 没变化就不写
  currentUser.last_trip_id = id;
  const fd = new FormData();
  fd.append('trip_id', String(id));
  api('/api/me/last-trip', { method: 'POST', body: fd }).catch(() => {});
}

async function loadPoints() {
  if (!currentTripId) { clearMap(); return; }
  const [points, stats] = await Promise.all([
    api(`/api/trips/${currentTripId}/points`),
    api(`/api/trips/${currentTripId}/stats`),
  ]);
  lastStats = stats;
  lastPoints = points;   // 提前更新，使 renderStats 里里程兜底立即用最新点（否则停留旧值直到路线规划完成）
  segmentData = [];      // 清空旧路线，避免 renderStats 误用上一次行程的里程
  renderStats(stats, points);
  renderPointList(points);
  renderMap(points);
  restoreUiState(points);
  fillElevations(currentTripId);   // 异步补海拔，回来了只改 marker 上的小标
}

// 计算行程总里程（米）：真实路线就绪优先，规划失败的段用直线距离兜底
function calcTotalDistance() {
  let meters = 0;
  if (segmentData.length) {
    meters = segmentData.reduce((s, seg) => {
      // 和标签一样认 picked 优先，否则挑了别的方案后总里程还是默认那条的
      const src = seg.picked || (seg.choices && seg.choices[0]);
      const d = (src && src.distance) || 0;
      if (d) return s + d;
      // 该段路线规划失败 → 用球面直线距离兜底，避免总里程缺段
      if (seg.from && seg.to) return s + Math.round(distKm(seg.from, seg.to) * 1000);
      return s;
    }, 0);
  }
  if (!meters && lastPoints && lastPoints.length > 1) {
    meters = lastPoints.slice(0, -1).reduce((s, a, i) => s + Math.round(distKm(a, lastPoints[i + 1]) * 1000), 0);
  }
  return meters ? fmtKm(meters) : '';
}

// 真实路线（逐段）就绪后刷新统计中的总里程
function updateStatsDistance() {
  if (lastStats) renderStats(lastStats, lastPoints);
}

// 完整统计数据（供点击弹窗展示，不被省略号截断）
let lastStatsDetail = null;

function renderStats(stats, points) {
  const box = $('#top-stats');
  if (!stats) { box.innerHTML = ''; lastStatsDetail = null; return; }
  const parts = [];
  const rows = [];  // 汇总行：label + 值
  const tripName = stats.name || '';
  if (tripName) parts.push(tripName);
  const distText = calcTotalDistance();
  if (distText) { parts.push('总里程 ' + distText); rows.push(['总里程', distText]); }
  if (points && points.length) {
    const days = new Set(points.map(p => p.start_date).filter(Boolean)).size;
    if (days >= 2) { parts.push(`${days} 天`); rows.push(['行程天数', `${days} 天`]); }
    const cities = new Set(points.filter(p => !p.is_waypoint).map(p => p.name).filter(Boolean)).size;
    if (cities > 0) { parts.push(`${cities} 城`); rows.push(['途经城市', `${cities} 城`]); }
  }
  parts.push(`${stats.point_count} 地点`, `${stats.media_count} 媒体`);
  rows.push(['轨迹点', `${stats.point_count} 个`], ['媒体', `${stats.media_count} 个`]);
  if (stats.photo_count) { parts.push(`${stats.photo_count} 照片`); rows.push(['照片', `${stats.photo_count} 张`]); }
  if (stats.video_count) { parts.push(`${stats.video_count} 视频`); rows.push(['视频', `${stats.video_count} 个`]); }
  if (stats.start_date) {
    const d = stats.start_date + (stats.end_date && stats.end_date !== stats.start_date ? ' ~ ' + stats.end_date : '');
    parts.push(d);
    rows.push(['起止日期', d]);
  }
  // 各点明细：序号 + 名称 + 日期 + 类型 + 距上点里程
  const pointRows = (points || []).map((p, i) => ({
    no: i + 1,
    name: p.name || '（未命名）',
    date: fmtDate(p.start_date),
    waypoint: !!p.is_waypoint,
    distFromPrev: i > 0 ? fmtKm(segDistanceMeters(i - 1, points), 0) : '',
  }));
  lastStatsDetail = { name: tripName, description: stats.description || '', rows, points: pointRows };
  box.textContent = parts.join(' · ');
}

// 点击顶部统计 → 弹窗展示完整信息（含各点明细）
function openStatsModal() {
  if (!lastStatsDetail) return;
  const d = lastStatsDetail;
  // 行程信息区块（名称 + 描述）
  let tripHtml = '';
  if (d.name || d.description) {
    const bits = [];
    if (d.name) bits.push(`<div class="stat-row"><span>行程名称</span><b>${esc(d.name)}</b></div>`);
    if (d.description) bits.push(`<div class="stat-row stat-desc"><span>描述</span><b>${esc(d.description)}</b></div>`);
    tripHtml = `<div class="stat-sub">行程信息</div>` + bits.join('');
  }
  const rowsHtml = d.rows.map(r =>
    `<div class="stat-row"><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`
  ).join('');
  const ptsHtml = d.points.map(p =>
    `<div class="stat-row stat-point"><span>${p.no}. ${esc(p.name)}${p.waypoint ? '（途经）' : ''}</span><b>${esc(p.date)}${p.distFromPrev ? ' · 距上点 ' + esc(p.distFromPrev) : ''}</b></div>`
  ).join('');
  $('#stats-modal-title').textContent = d.name || '行程统计';
  $('#stats-modal-body').innerHTML =
    tripHtml +
    `<div class="stat-sub">汇总</div>` + rowsHtml +
    (d.points.length ? `<div class="stat-sub">各点明细（${d.points.length}）</div>` + ptsHtml : '');
  $('#stats-modal').classList.remove('hidden');
}
$('#top-stats').onclick = openStatsModal;
$('#stats-modal-close').onclick = () => $('#stats-modal').classList.add('hidden');
// 点弹窗外的遮罩也关闭，和其他弹窗（编辑框/分享设置）保持一致
$('#stats-modal').addEventListener('click', (e) => {
  if (e.target === $('#stats-modal')) $('#stats-modal').classList.add('hidden');
});

function renderPointList(points) {
  const ul = $('#point-list');
  ul.innerHTML = '';
  points.forEach((p, i) => {
    const li = el('li', '');
    li.dataset.id = p.id;
    li.dataset.date = p.start_date ?? '';
    if (p.id === currentPointId) li.classList.add('active');
    if (p.is_waypoint) li.classList.add('waypoint');

    // 上下交换箭头：仅当与相邻条目同日期时显示（只能同日期内交换次序）
    const curDate = p.start_date ?? '';
    const canUp = i > 0 && (points[i - 1].start_date ?? '') === curDate;
    const canDown = i < points.length - 1 && (points[i + 1].start_date ?? '') === curDate;

    li.appendChild(el('span', 'point-index', p.is_waypoint ? '' : String(i + 1)));
    const info = el('div', 'point-info');
    const nameDiv = el('div', 'point-name', p.name);
    if (p.is_waypoint) nameDiv.appendChild(el('span', 'waypoint-tag', '途经'));
    info.appendChild(nameDiv);
    info.appendChild(el('div', 'point-date', fmtDate(p.start_date) +
      (p.end_date && p.end_date !== p.start_date ? ' ~ ' + fmtDate(p.end_date) : '')));
    const mediaParts = [];
    if (p.photo_count) mediaParts.push(`📷 ${p.photo_count}`);
    if (p.video_count) mediaParts.push(`🎬 ${p.video_count}`);
    if (mediaParts.length) info.appendChild(el('div', 'point-media', mediaParts.join('  ')));
    li.appendChild(info);

    if (canUp || canDown) {
      const arrows = el('span', 'move-arrows');
      if (canUp) {
        const up = el('button', 'move-arrow', '↑');
        up.type = 'button';
        up.title = '与上一条交换次序';
        up.onclick = (ev) => { ev.stopPropagation(); movePoint(i, -1); };
        arrows.appendChild(up);
      }
      if (canDown) {
        const down = el('button', 'move-arrow', '↓');
        down.type = 'button';
        down.title = '与下一条交换次序';
        down.onclick = (ev) => { ev.stopPropagation(); movePoint(i, 1); };
        arrows.appendChild(down);
      }
      li.appendChild(arrows);
    }
    li.onclick = () => showDetail(p.id);
    ul.appendChild(li);

    // 连线方式切换：按钮放在「两个点之间」的分隔条上（最后一点后无分隔条），
    // 点击弹出与地图点线段一致的面板（选择驾车/直线/路线方案）
    if (i < points.length - 1) {
      const mode = p.route_mode || 'drive';
      const seg = el('div', 'seg-divider');
      seg.dataset.segIndex = String(i);
      const rm = el('button', 'rm-seg', mode === 'drive' ? '🚗' : '📏');
      rm.type = 'button';
      rm.title = `${p.name} → 下一点：点击选择驾车 / 直线 / 路线方案`;
      rm.onclick = (ev) => {
        ev.stopPropagation();
        showSegmentPanel(i);
      };
      seg.appendChild(rm);
      ul.appendChild(seg);
    }
  });
}

// ---------- 点排序：上下箭头交换（仅同日期） ----------

async function movePoint(i, dir) {
  const pts = lastPoints;
  const j = i + dir;
  if (!pts.length || j < 0 || j >= pts.length) return;
  const cur = pts[i], nxt = pts[j];
  if ((cur.start_date ?? '') !== (nxt.start_date ?? '')) return; // 只允许同日期交换

  const ul = $('#point-list');
  const liA = ul.querySelector(`li[data-id="${cur.id}"]`);
  const liB = ul.querySelector(`li[data-id="${nxt.id}"]`);
  if (!liA || !liB) return;
  const ra = liA.getBoundingClientRect();
  const rb = liB.getBoundingClientRect();

  // 交换数据
  [pts[i], pts[j]] = [pts[j], pts[i]];

  // 保存到后端
  try {
    const fd = new FormData();
    fd.append('ordered_ids', pts.map(p => p.id).join(','));
    await api(`/api/trips/${currentTripId}/points/reorder`, { method: 'POST', body: fd });
  } catch (err) {
    tmAlert('排序保存失败：' + err.message);
    [pts[i], pts[j]] = [pts[j], pts[i]]; // 回滚
    return;
  }

  // 重绘列表 + 地图（序号更新）
  renderPointList(pts);
  renderMap(pts);

  // FLIP 动画：让交换的两个卡片从旧位置平滑过渡到新位置 + 高亮闪烁
  const liA2 = ul.querySelector(`li[data-id="${cur.id}"]`);
  const liB2 = ul.querySelector(`li[data-id="${nxt.id}"]`);
  if (!liA2 || !liB2) return;
  const ra2 = liA2.getBoundingClientRect();
  const rb2 = liB2.getBoundingClientRect();
  const flip = (el, from, to) => {
    el.style.transition = 'none';
    el.style.transform = `translateY(${from - to}px)`;
    el.classList.add('swap-flash');
  };
  flip(liA2, ra.top, ra2.top);
  flip(liB2, rb.top, rb2.top);
  requestAnimationFrame(() => {
    liA2.style.transition = 'transform .3s cubic-bezier(.2,.8,.2,1)';
    liB2.style.transition = 'transform .3s cubic-bezier(.2,.8,.2,1)';
    liA2.style.transform = '';
    liB2.style.transform = '';
    setTimeout(() => {
      liA2.style.transition = '';
      liB2.style.transition = '';
      liA2.style.transform = '';
      liB2.style.transform = '';
      liA2.classList.remove('swap-flash');
      liB2.classList.remove('swap-flash');
    }, 320);
  });
}

// 恢复地图视野：有保存的缩放/中心就恢复，否则自适应全部点
function restoreMapView() {
  const st = getTripState();
  if (st.zoom != null && st.center && st.center.length === 2) {
    // 第三个参数 immediately=true：直接落位，不做从默认视野飞过去的动画
    try { map.setZoomAndCenter(st.zoom, st.center, true); viewRestored = true; return; } catch (e) {}
  }
  viewRestored = false;
  map.setFitView(null, false, [60, 60, 60, 60]);
}

// 恢复界面状态：面板高度 / 选中点 / tab（内容已刷新，这里只恢复位置与选中状态）
function restoreUiState(points) {
  const st = getTripState();
  // 面板高度（px）
  if (st.panelHeight) {
    const mapEl = document.getElementById('map');
    if (mapEl) {
      mapEl.style.height = st.panelHeight + 'px';
      syncAddPointBar();
      if (map && typeof map.resize === 'function') setTimeout(() => map.resize(), 30);
    }
  }
  // tab 恢复（无论面板收起与否）
  if (st.tab) switchTab(st.tab);
  // 选中点恢复（仅当面板展开，避免强行展开用户已收起的面板）
  const panelCollapsed = $('.panel').classList.contains('collapsed');
  if (panelCollapsed || !st.pointId || !points.some(p => p.id === st.pointId)) {
    restorePointListScroll(st);
    return;
  }
  // 原本停在「点详情」tab → 完整恢复详情；否则（轨迹点 tab）只恢复选中高亮，绝不切 tab
  suppressAutoPan = true;                 // 恢复期间别动视野
  setTimeout(() => { suppressAutoPan = false; }, 0);
  if (st.tab === 'detail') {
    showDetail(st.pointId);
  } else {
    currentPointId = st.pointId;
    // renderPointList 已先跑完，需手动补列表 active 高亮（保持活动位置）
    document.querySelectorAll('#point-list li').forEach(li => {
      const hit = String(li.dataset.id) === String(st.pointId);
      li.classList.toggle('active', hit);
    });
    highlightMapPoint(st.pointId);
    // 恢复用户滚动位置（刷新前看到第几条就停第几条，不强制滚到选中项）；
    // 无滚动记录时回退滚动到选中项，避免长列表刷新后停在顶部（"回到第一天"）
    if (st.listScroll != null) {
      restorePointListScroll(st);
    } else {
      const activeLi = document.querySelector('#point-list li.active');
      if (activeLi) activeLi.scrollIntoView({ block: 'nearest' });
    }
  }
}

// 恢复轨迹点列表滚动位置（长列表刷新后不回顶部）
function restorePointListScroll(st) {
  const view = $('#points-view');
  if (view && st && st.listScroll != null) view.scrollTop = st.listScroll;
}

// 保存轨迹点列表滚动位置（rAF 节流，避免滚动时频繁写 localStorage）
let _scrollSavePending = false;
function savePointListScroll() {
  if (_scrollSavePending) return;
  _scrollSavePending = true;
  requestAnimationFrame(() => {
    _scrollSavePending = false;
    const view = $('#points-view');
    if (!view || !currentTripId) return;
    saveTripState({ listScroll: view.scrollTop });
  });
}

// ---------- 海拔 ----------
// 数据来自后端 /api/trips/{id}/elevation（OpenTopoData SRTM 90m），查到就缓存在库里。
// 没有数据时标签留空，CSS 的 :empty 会把它隐藏，不占位。
function setMarkerElev(marker, elev) {
  if (!marker || !marker._elevEl) return;
  const v = (elev === null || elev === undefined || isNaN(elev)) ? null : Math.round(Number(elev));
  marker._elevEl.textContent = v === null ? '' : v + ' m';
}
// 缩得太小时满屏都是海拔小标，反而糊住地名；低于这个级别只收海拔，地名照常显示
// （类名打在 #map 上，见 style.css 的 .map.hide-elev）
const ELEV_MIN_ZOOM = 7;
// 加载完行程后补一次海拔：纯锦上添花，失败就当没这回事，不弹错也不重试
async function fillElevations(tripId) {
  if (!tripId) return;
  let res;
  try {
    res = await api(`/api/trips/${tripId}/elevation`, { method: 'POST' });
  } catch (e) { return; }
  const map0 = res && res.elevations;
  if (!map0 || !Object.keys(map0).length) return;
  if (Number(tripId) !== Number(currentTripId)) return;   // 期间切了行程就别改了
  (lastPoints || []).forEach(p => {
    if (map0[String(p.id)] !== undefined) p.elevation = map0[String(p.id)];
  });
  markers.forEach(m => {
    const id = m._pointIds && m._pointIds[0];
    const p = (lastPoints || []).find(x => Number(x.id) === Number(id));
    if (p) setMarkerElev(m, p.elevation);
  });
}

function renderMap(points) {
  clearMap();
  lastPoints = points || [];
  if (points.length === 0) return;
  const seq = ++renderSeq;

  const path = [];
  // 按坐标分组：环线会多次经过同一地点，同坐标的点合并为一个 marker，序号全部显示
  const groups = new Map();
  points.forEach((p, i) => {
    const key = p.lng.toFixed(5) + ',' + p.lat.toFixed(5);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ p, idx: i });
    path.push([p.lng, p.lat]);
  });

  groups.forEach((group) => {
    const multi = group.length > 1;
    const allWaypoint = group.every(g => g.p.is_waypoint);
    const nums = group.map(g => g.idx + 1);
    const dot = el('div', 'marker-dot' + (multi ? ' multi' : '') + (allWaypoint ? ' waypoint' : ''));
    dot.textContent = nums.join('·');
    // 圆点外面套一层，下方挂一条小标签。标签里放「地名 + 海拔」：
    // 圆点本身只有序号，而它又正好压住底图原有的地名，导致看着一个点不知道是哪儿。
    // 海拔是异步回填的，所以先建出空的 span 留位，CSS 用 :empty 隐藏，
    // 数据回来只改文字，不用重建整个 marker。
    const wrap = el('div', 'marker-wrap');
    const label = el('div', 'marker-label');
    const elevEl = el('span', 'marker-elev');
    // 纯途经点是辅助性质的，名字多半是自动生成的坐标串，标出来只会糊满地图
    if (!allWaypoint) {
      const nameEl = el('span', 'marker-name');
      nameEl.textContent = group.map(g => g.p.name).join(' / ');
      label.appendChild(nameEl);
    }
    label.appendChild(elevEl);
    wrap.appendChild(label);
    wrap.appendChild(dot);
    const marker = new AMap.Marker({
      position: [group[0].p.lng, group[0].p.lat],
      title: group.map(g => g.p.name).join(' / '),
      content: wrap,
      offset: new AMap.Pixel(-14, -14),
      zIndex: allWaypoint ? 100 : 120,
    });
    marker._dot = dot;
    marker._elevEl = elevEl;
    setMarkerElev(marker, group[0].p.elevation);
    // 辅助点（纯途经点）地图上不可点击；正式点点击查看详情/选点
    if (!allWaypoint) {
      marker.on('click', () => {
        if (multi) showPointPicker(group);
        else showDetail(group[0].p.id);
      });
    }
    // 记录该 marker 对应的所有点 id（环线合并点含多次经过），供详情高亮对应
    marker._pointIds = group.map(g => g.p.id);
    marker._baseZ = allWaypoint ? 100 : 120;
    marker.setMap(map);
    markers.push(marker);

  });

  // 📷 角标（数量气泡）单独一趟，见 renderPhotoBadges —— 媒体增删之后要能
  // 只重建角标，不惊动路线和地图视野
  renderPhotoBadges(points);
  syncElevLabelsVisibility();
  // 照片图层的数据是按行程取的，上面 clearMap 已经把旧的清空，这里按新行程重建。
  // 勾选状态不变，用户不用每次换行程都重新勾一次。
  if ($('#lay-photos') && $('#lay-photos').checked) toggleLayerPhotos(true, true);

  // 先全直线兜底，再逐段按各自 route_mode 规划驾车路线
  drawPolyline(path);
  // 先按直线距离把里程标出来，真实路线算完后 drawRoute 会用真实中点重建一次。
  // 这样路线规划失败（或很慢）时也不至于一个里程都看不到。
  renderSegLabels();
  restoreMapView();
  drawRoute(points, seq);
  // 轨迹回放入口：不足两个点无法回放，隐藏入口；控制条按需出现（播放时才显示）
  $('#replay-launch').classList.toggle('hidden', points.length < 2);
  $('#replay-bar').classList.add('hidden');
}

// 有照片/视频的点旁边那枚「📷 + 数量」角标。
// 单独成函数的理由：上传/删除一张媒体之后，地图上的数量必须跟着变（否则角标
// 一直停在打开页面时的数字），而为了一个数字重跑一遍 renderMap 会把路线、
// 里程标签、地图视野全部推倒重来 —— 代价完全不成比例。
function renderPhotoBadges(points) {
  clearPhotoBadges();
  // 和照片堆一样放进自建图层，而不是 AMap.Marker。
  // 高德的 DOM marker 全在一个 z-index:300 的容器里，那个容器整体压在
  // 路线层(110)之上 —— 实测这个 📷 角标会把路线截断（角标处线消失）。
  // 放进 z=100 的自建层，路线就画在它上面了。
  const box = ensurePhotoStackLayer();
  // 地图图层树还没建好：这一趟画不出来，但显隐状态该同步还得同步
  // （类挂在 #map 上，之后新建的角标会自动跟随）
  // box 还没就绪时角标建不出来，但位置该同步还得同步（下次 positionPhotoBadges 会补）
  if (!box) { syncPhotoMarkersVisibility(); positionPhotoBadges(); return; }
  // 分组规则要和 renderMap 一致：环线会多次经过同一坐标，合并成一枚角标、数量相加
  const groups = new Map();
  (points || []).forEach((p) => {
    const key = p.lng.toFixed(5) + ',' + p.lat.toFixed(5);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  groups.forEach((group) => {
    const n = group.reduce((s, p) => s + (p.photo_count || 0) + (p.video_count || 0), 0);
    if (n <= 0) return;
    const pmEl = el('div', 'photo-marker on-stack-layer');
    pmEl.appendChild(el('span', 'photo-marker-icon', '📷'));
    pmEl.appendChild(el('span', 'photo-marker-badge', n > 99 ? '99+' : String(n)));
    pmEl.title = `${n} 张照片/视频`;
    // 500ms 去重：触摸兜底和浏览器合成的 click 有可能都到，别开两次灯箱
    pmEl.onclick = (e) => {
      e.stopPropagation();
      const now = Date.now();
      if (pmEl.__last && now - pmEl.__last < 500) return;
      pmEl.__last = now;
      openPointPhotos(group[0].id);
    };
    box.appendChild(pmEl);
    // 角标放在轨迹点的**右上角**：点的正下方（top:32px）挂着「地名 + 海拔」小标，
    // 原来的右下方 (10,10) 会和它撞在一起——关掉「照片视频」图层时角标才显示出来，
    // 于是一关图层就看到 📷 压着海拔。右上方只有照片堆会占，而照片堆只在图层【开】时
    // 出现，那时角标本来就是隐藏的，两者永远不同框。
    // -36 = 圆点半径外的 45° 切点(-10) 再减角标自身高度(26)，让它斜贴着圆点右上。
    photoBadgeItems.push({ el: pmEl, lng: group[0].lng, lat: group[0].lat, dx: 10, dy: -36 });
  });
  // 新建出来的角标要按当前「照片图层」的开关状态决定显不显示
  syncPhotoMarkersVisibility();
  positionPhotoBadges();   // 刚建出来时位置还是 0,0，必须立刻摆好
}

// 媒体增删之后刷新所有「跟数量有关」的地方：地图角标、点列表的 📷/🎬 计数、
// 顶部统计条、照片图层里的照片堆。
// 合并一次性刷新（250ms 去抖）—— 一次选 9 张图会连着回调 9 次，没必要打 9 轮接口。
let _mediaCountTimer = 0;
function scheduleMediaCountRefresh() {
  clearTimeout(_mediaCountTimer);
  _mediaCountTimer = setTimeout(refreshMediaCounts, 250);
}

async function refreshMediaCounts() {
  const tripId = currentTripId;
  if (!tripId) return;
  let points, stats;
  try {
    [points, stats] = await Promise.all([
      api(`/api/trips/${tripId}/points`),
      api(`/api/trips/${tripId}/stats`),
    ]);
  } catch (e) { return; }
  if (Number(tripId) !== Number(currentTripId)) return;   // 期间用户换了行程，别写脏
  lastPoints = points;
  lastStats = stats;
  renderPhotoBadges(points);
  // 点列表里每条也印着 📷 3 / 🎬 1，一并刷新；滚动位置保持住
  const view = document.getElementById('points-view');
  const top = view ? view.scrollTop : 0;
  renderPointList(points);
  if (view) view.scrollTop = top;
  renderStats(stats, points);
  // 照片图层的堆是按行程一次性拉的，缓存作废后按需重建
  photoLayerData = null;
  const cb = document.getElementById('lay-photos');
  if (cb && cb.checked) { clearPhotoStackLayer(); toggleLayerPhotos(true, true); }
}

function clearMap() {
  renderSeq++; // 使进行中的异步路线规划失效
  markers.forEach(m => m.setMap(null));
  markers = [];
  photoMarkers.forEach(m => m.setMap(null));
  photoMarkers = [];
  clearPhotoBadges();      // 📷 角标现在在自建层里，得单独清
  // 照片图层也要清。之前漏了这一步：换行程时旧行程的照片堆会一直留在地图上，
  // 于是在「独库公路」的地图上能看到「关西赏樱」的照片堆。
  clearPhotoStackLayer();
  photoLayerData = null;        // 数据是按行程取的，换行程必须重取
  if (polyline) { polyline.setMap(null); polyline = null; }
  segPolylines.forEach(p => p.setMap(null));
  segPolylines = [];
  segmentData = [];
  routeSegGeom = [];
  if (routeSvgLayer) { routeSvgLayer.innerHTML = ''; routePathEls = []; }
  clearSegLabels();
  fallbackSegs = false;
  clearRouteCandidates();
  stopReplay(false); // 切换行程/清空时停止轨迹回放
}

// 进入点详情时，高亮地图上对应的 marker 并把视野移过去（以示列表/详情与地图的对应关系）
// suppressAutoPan：刷新后恢复「上次选中的点」时用。
// 那种场景下保存的视野才是用户想看到的画面，把地图平移到选中点会把它顶掉
// —— 表现就是"刷新后总是跳到某个轨迹点居中"。
let suppressAutoPan = false;
function highlightMapPoint(pointId) {
  let target = null;
  markers.forEach(m => {
    const hit = m._pointIds && m._pointIds.includes(pointId);
    if (m._dot) m._dot.classList.toggle('highlight', !!hit);
    if (m.setzIndex) m.setzIndex(hit ? 600 : m._baseZ);
    if (hit) target = m;
  });
  if (target && !suppressAutoPan) {
    const pos = target.getPosition();
    // 只平移视野到该点，不改变缩放级别（避免点击点后地图突然放大、破坏用户调好的视野）
    map.panTo(pos);
  }
}

// 清除全部 marker 高亮（关闭详情/切换行程时调用）

// ---------- 相邻轨迹段高亮（来时路/即将去） ----------
// 选中点后：前一段（来时路）绿色、后一段（即将去）橙色，其余段淡化
function highlightAdjacentSegments(pointId) {
  if (!lastPoints || lastPoints.length < 2) return;
  const idx = lastPoints.findIndex(p => String(p.id) === String(pointId));
  if (idx < 0) return;
  ensureSegmentsForHighlight();
  segPolylines.forEach((seg, i) => {
    if (i === idx - 1) {
      seg.setOptions({ strokeColor: '#16a34a', strokeWeight: 6, strokeOpacity: 1 });
    } else if (i === idx) {
      seg.setOptions({ strokeColor: '#f97316', strokeWeight: 6, strokeOpacity: 1 });
    } else {
      // 其余段保持默认蓝，不做淡化：来时路/即将去自己有颜色就够醒目了，
      // 再把别的段压暗只会让整条行程看起来断了一截
      seg.setOptions({ strokeColor: '#2563eb', strokeWeight: 4, strokeOpacity: 1 });
    }
  });
  // 上面几行把不透明度改回了 1，自绘层开着的话高德那条线会重新露出来，得再压回去
  buildRouteOverlay();
}

// 保证分段折线可用：真实路线（segPolylines）未就绪时，隐藏直线兜底并用直线临时分段
function ensureSegmentsForHighlight() {
  if (segPolylines.length > 0) return;
  if (!polyline || !lastPoints || lastPoints.length < 2) return;
  polyline.setMap(null); // 临时隐藏整条直线兜底
  for (let i = 0; i < lastPoints.length - 1; i++) {
    const a = lastPoints[i], b = lastPoints[i + 1];
    const seg = new AMap.Polyline({
      path: [[a.lng, a.lat], [b.lng, b.lat]],
      strokeColor: '#2563eb',
      strokeWeight: 4,
      // 不透明。原来是 0.85，本意是让底图透出来一点；但路线要压在照片堆上走，
      // 15% 的照片透上来会把线染暗（实测 #2563eb 被染成 (34,84,174)），
      // 看着就像被照片盖住了一层。路线是主体内容，得干净利落。
      strokeOpacity: 1,
      lineJoin: 'round',
      zIndex: 50,   // 路线在照片堆(40)之上、轨迹点(100/120)之下
    });
    seg.setMap(map);
    segPolylines.push(seg);
  }
  fallbackSegs = true;
  renderSegLabels();
  buildRouteOverlay();
}

// 清除相邻段高亮，恢复默认蓝色样式
function unhighlightSegments() {
  styleSegLabels(null);        // 标签常驻，只把颜色恢复成默认
  if (fallbackSegs) {
    // 移除临时直线分段，恢复原整条直线兜底
    segPolylines.forEach(p => p.setMap(null));
    segPolylines = [];
    fallbackSegs = false;
    clearSegLabels();          // 分段没了，标签也跟着撤
    if (polyline) polyline.setMap(map);
    buildRouteOverlay();
    return;
  }
  segPolylines.forEach(seg => {
    seg.setOptions({ strokeColor: '#2563eb', strokeWeight: 4, strokeOpacity: 1 });
  });
  buildRouteOverlay();       // 同上：不透明度被改回 1 了，重新压住
}

// 获取第 i 段里程（米）：真实路线取当前选中方案 distance，直线/兜底用球面距离
function segDistanceMeters(i, pts) {
  // picked 是用户在「选择路线方案」里挑的那条；没挑过就用默认（choices[0]，即最快）。
  // 不看 picked 的话，挑了「最短」之后标签还挂着「最快」的里程，和线对不上。
  const sd = segmentData[i];
  const src = (sd && sd.picked) || (sd && sd.choices && sd.choices[0]);
  const d = src ? src.distance : 0;
  if (d) return d;
  const P = pts || lastPoints;
  if (P && P[i] && P[i + 1]) {
    return distKm(P[i], P[i + 1]) * 1000;
  }
  return 0;
}

// ---------- 每段路线的里程标签 ----------
// 每一段都常驻一个里程标签，贴在**沿路线走一半**的位置（按累计长度算，不是取中间那个顶点——
// 真实路线的顶点疏密不均，取中间顶点常常落到离视觉中点很远的地方）。
// 选中某个点时，它的来时路变绿、即将去变橙，其余淡化——颜色已经把归属说清楚了，
// 所以标签里只留数字，不写「来时路 / 即将去」。
// 沿折线走到总长一半的那个点。返回 [lng, lat]。
// 第 i 段走过的各条公路（[{code, meters, lng, lat}]）。
// 直线段没有路可言；驾车段没捞到编号（海外线路 / 全是无名道路）就返回空，不标。
function segRoadRunsOf(i) {
  const sd = segmentData[i];
  if (!sd || sd.mode === 'line') return [];
  const src = sd.picked || (sd.choices && sd.choices[0]);
  return (src && src.roadRuns) || [];
}
function segMidPoint(i) {
  const seg = segPolylines[i];
  let path = null;
  if (seg && seg.getPath) {
    const p = seg.getPath() || [];
    path = p.map(v => (Array.isArray(v) ? v : [v.lng, v.lat]));
  }
  if (!path || path.length < 2) {
    const P = lastPoints || [];
    const a = P[i], b = P[i + 1];
    if (!a || !b) return null;
    path = [[a.lng, a.lat], [b.lng, b.lat]];
  }
  // 平面近似即可：一段路线的跨度上，经度按纬度做个余弦缩放就够准了
  const kx = Math.cos(path[0][1] * Math.PI / 180) || 1;
  const d = [0];
  for (let k = 1; k < path.length; k++) {
    const dx = (path[k][0] - path[k - 1][0]) * kx;
    const dy = path[k][1] - path[k - 1][1];
    d.push(d[k - 1] + Math.hypot(dx, dy));
  }
  const half = d[d.length - 1] / 2;
  if (!half) return path[0];
  let k = 1;
  while (k < d.length && d[k] < half) k++;
  const t = (half - d[k - 1]) / ((d[k] - d[k - 1]) || 1);
  return [
    path[k - 1][0] + (path[k][0] - path[k - 1][0]) * t,
    path[k - 1][1] + (path[k][1] - path[k - 1][1]) * t,
  ];
}
// 重建全部里程标签。segPolylines 变化后（真实路线绘制完 / 临时分段建好）调用。
function renderSegLabels() {
  clearSegLabels();
  const n = (lastPoints || []).length - 1;
  if (n < 1) return;
  for (let i = 0; i < n; i++) {
    const pos = segMidPoint(i);
    const km = fmtKm(segDistanceMeters(i), 0);
    if (!pos || !km) continue;
    // 外层是个零尺寸锚点，里面的胶囊用 translate(-50%,-50%) 精确骑在线上；
    // 否则标签宽度一变（12 km / 1234 m）就会偏出去。
    const anchor = el('div', 'seg-label-anchor');
    const pill = el('span', 'seg-label', km);
    anchor.appendChild(pill);
    const m = new AMap.Marker({
      position: pos,
      content: anchor,
      offset: new AMap.Pixel(0, 0),
      zIndex: 700,
      bubble: true,          // 点标签等同于点地图/线，不拦截路线自身的点击
    });
    m._pill = pill;
    m.setMap(map);
    segLabels.push(m);
  }
  applySegLabelsVisibility();   // 关着里程时，这批新标签建出来就不该露面
  // 公路编号是另一批标签（骑在各自那截路的中点上，不跟着里程走），
  // 但重建时机完全一致 —— 换行程、真实路线算完、改路线方案，两边都得重来。
  renderRoadLabels(lastPoints.slice(0, -1).map((_, i) => segRoadRunsOf(i)));
  styleSegLabels(currentPointId);
}
// 按当前选中点给标签上色：来时路绿、即将去橙、其余淡化（与线的配色一致）
function styleSegLabels(pointId) {
  scheduleRouteDraw();          // 高亮变了，自绘线的配色也要跟着变
  const idx = pointId
    ? (lastPoints || []).findIndex(p => String(p.id) === String(pointId))
    : -1;
  segLabels.forEach((m, i) => {
    const pill = m._pill;
    if (!pill) return;
    // 只给相邻两段上色，其余保持默认蓝——不淡化，理由同 highlightAdjacentSegments
    pill.classList.remove('in', 'out');
    if (idx < 0) return;
    if (i === idx - 1) pill.classList.add('in');
    else if (i === idx) pill.classList.add('out');
  });
}

// 多次经过同一地点的点（环线）：点击合并 marker 后弹出选择，查看某一次经过的详情
function showPointPicker(group) {
  const ov = document.createElement('div');
  ov.className = 'modal';
  const items = group.map(g => `
    <button type="button" class="btn point-pick" data-id="${g.p.id}" style="text-align:left">
      <span class="pick-num">${g.idx + 1}</span>
      <span class="pick-name">${esc(g.p.name)}</span>
      <span class="pick-date">${fmtDate(g.p.start_date)}</span>
    </button>`).join('');
  ov.innerHTML = `
    <div class="modal-box">
      <h2>多次经过的地点</h2>
      <p style="font-size:12px;color:var(--muted);margin:0 0 8px">该地点在行程中被多次经过，请选择要查看的一次：</p>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${items}
        <button type="button" class="btn" data-cancel style="text-align:center;opacity:.7">取消</button>
      </div>
    </div>`;
  document.body.appendChild(ov);
  ov.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-id], button[data-cancel]');
    if (!btn) return;
    ov.remove();
    if (btn.hasAttribute('data-cancel')) return;
    showDetail(parseInt(btn.getAttribute('data-id'), 10));
  });
}

function drawPolyline(path) {
  if (polyline) { polyline.setMap(null); polyline = null; }
  if (!path || path.length < 2) return;
  polyline = new AMap.Polyline({
    path: path,
    strokeColor: '#2563eb',
    strokeWeight: 4,
    strokeOpacity: 1,
    zIndex: 50,   // 路线在照片堆(40)之上、轨迹点(100/120)之下
  });
  polyline.setMap(map);
}

// 相邻两点距离（km）
// 判断坐标是否在中国大陆大致范围内（高德驾车规划仅支持国内）

// 确保 AMap.Driving 插件已加载（懒加载，drivingInstance 作加载标记）
function getDriving() {
  return new Promise((resolve) => {
    if (drivingInstance) return resolve();
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      drivingInstance = true; // 无论成功与否都标记已尝试，避免反复重试
      resolve();
    };
    try {
      AMap.plugin('AMap.Driving', finish);
    } catch (e) {
      finish();
    }
    // 超时兜底：Driving 插件加载失败（key 权限/网络）也不阻塞路线规划，
    // 海外线路本就走 OSRM，国内线路失败则回退直线
    setTimeout(finish, 4000);
  });
}

// 按指定策略规划两点驾车路线：返回该策略下的全部备选路线数组（可能为空）
function driveOnce(policy, from, to) {
  return new Promise((resolve) => {
    try {
      const driving = new AMap.Driving({ policy, hideMarkers: true });
      driving.search(from, to, (status, result) => {
        if (status === 'complete' && result.routes && result.routes.length) {
          const routes = result.routes.map(route => {
            const path = [];
            (route.steps || []).forEach(step => {
              (step.path || []).forEach(p => path.push([p.lng, p.lat]));
            });
            // steps 里顺手把公路编号连同它覆盖的那截路一起捞出来，见 segRoadRuns。
            // 每条备选路线各算各的——「最快」走高速、「最短」走国道，走的路本来就不一样。
            const roadRuns = segRoadRuns(route.steps, route.distance);
            return { path, roadRuns, time: route.time || 0, distance: route.distance || 0 };
          }).filter(r => r.path && r.path.length >= 2);
          resolve(routes);
        } else {
          resolve([]);
        }
      });
    } catch (e) {
      // 高德对海外坐标可能直接抛错，兜底返回空，让调用方走 OSRM 全球路线
      resolve([]);
    }
  });
}

// 用多种策略规划两点之间的备选路线，去重后返回 choices 数组
async function planSegmentChoices(from, to) {
  const strategies = [
    { label: '最快', policy: AMap.DrivingPolicy.LEAST_TIME },
    { label: '最短', policy: AMap.DrivingPolicy.LEAST_DISTANCE },
    { label: '少收费', policy: AMap.DrivingPolicy.LEAST_FEE },
  ];
  const choices = [];
  const seen = new Set();
  for (const s of strategies) {
    const routes = await driveOnce(s.policy, from, to);
    for (const r of routes) {
      // 用首尾坐标 + 里程(取整到100m) 去重，避免不同策略返回同一条路
      const head = r.path[0], tail = r.path[r.path.length - 1];
      const key = head[0].toFixed(4) + ',' + head[1].toFixed(4) + '|'
                + tail[0].toFixed(4) + ',' + tail[1].toFixed(4) + '|'
                + Math.round(r.distance / 100);
      if (!seen.has(key)) {
        seen.add(key);
        choices.push({ label: s.label, path: r.path, roadRuns: r.roadRuns || [],
                       time: r.time, distance: r.distance });
      }
    }
  }
  return choices;
}

// 用 OSRM 全球驾车路线兜底（高德不支持海外）；返回 choices 数组（对齐 planSegmentChoices）
async function routeViaOsm(a, b) {
  try {
    const res = await fetch(
      `/api/route?from_lng=${a.lng}&from_lat=${a.lat}&to_lng=${b.lng}&to_lat=${b.lat}`);
    if (!res.ok) return [];
    const data = await res.json();
    const routes = data.routes || [];
    const choices = [];
    const seen = new Set();
    routes.forEach(r => {
      if (!r.path || r.path.length < 2) return;
      const head = r.path[0], tail = r.path[r.path.length - 1];
      const key = head[0].toFixed(4) + ',' + head[1].toFixed(4) + '|'
                + tail[0].toFixed(4) + ',' + tail[1].toFixed(4) + '|'
                + Math.round(r.distance / 100);
      if (!seen.has(key)) {
        seen.add(key);
        // OSRM 的 steps 已经在后端整理成跟高德同样的字段，这里共用一套解析
        choices.push({ label: '', path: r.path, time: r.duration, distance: r.distance,
                       roadRuns: segRoadRuns(r.steps, r.distance) });
      }
    });
    return choices;
  } catch (e) {
    return [];
  }
}

// 只跟 OSRM 要公路编号，路径还用高德的。返回 { runs, meters }，meters 是 OSRM
// 认为这一段有多长 —— 调用方拿它算覆盖率，好跟高德那份比一比谁更全。
//
// 起因：高德有些路段给的是路名而不是编号 —— 独库公路那段 328 公里，steps 里写的是
// 「独库公路」「铁力买提隧道」，一个 G 字都没有。同一段路 OSRM 的 ref 给得出
// G218（开头 12 公里）和 G217（随后 293 公里），两截都标得出来。
async function roadRunsViaOsm(a, b) {
  const cs = await routeViaOsm(a, b);
  return { runs: (cs[0] && cs[0].roadRuns) || [], meters: (cs[0] && cs[0].distance) || 0 };
}

// 在高德和 OSRM 两份编号里挑更全的那份。谁把这段路解释得更完整就用谁 ——
// 不做合并：两边走的路本来就可能不一样，混在一起会出现同一截路两个编号。
async function betterRoadRuns(a, b, amap) {
  const mine = roadRunsCoverage(amap.roadRuns, amap.distance);
  if (mine >= ROAD_COVERAGE_MIN) return amap.roadRuns;
  const alt = await roadRunsViaOsm(a, b);
  if (!roadDistLooksSame(amap.distance, alt.meters)) return amap.roadRuns;
  const theirs = roadRunsCoverage(alt.runs, alt.meters);
  return theirs > mine ? alt.runs : amap.roadRuns;
}

// 按实际结果贴标签，而不是沿用请求时用的策略名。
// 高德的「最快」是 LEAST_TIME 这个**请求参数**，不是结果 —— 独山子→那拉提
// 那一段，标着「最快」的方案要 395 分钟，而另一条 163 分钟就到，看着像在胡说。
function relabelChoices(choices) {
  if (!choices || choices.length < 2) return choices || [];
  let fast = 0, short = 0;
  choices.forEach((c, i) => {
    if ((c.time || Infinity) < (choices[fast].time || Infinity)) fast = i;
    if ((c.distance || Infinity) < (choices[short].distance || Infinity)) short = i;
  });
  choices.forEach((c, i) => {
    const tags = [];
    if (i === fast) tags.push('最快');
    if (i === short) tags.push('最短');
    c.label = tags.join(' · ');      // 两头都不占的那条不贴标签，只报里程和时间
  });
  return choices;
}

// 高德那几条都绕远了的话，补一条 OSRM 的进备选，并把它排到最前当默认。
// 不是无条件补：绝大多数段高德规划得挺好，白白多发一次请求不值当。
async function withShorterAlternative(a, b, choices) {
  const straight = distKm(a, b) * 1000;
  if (!choices.length || straight <= 0) return choices;
  const best = Math.min(...choices.map(c => c.distance || Infinity));
  if (!isFinite(best) || best / straight <= DETOUR_SUSPECT) return choices;
  const alt = await routeViaOsm(a, b);
  // 只在明显更短时才插队。差不多长就说明高德没绕，是这一带本来就得那么走
  if (!alt.length || !(alt[0].distance < best * 0.8)) return choices;
  alt[0].label = '更短';
  return [alt[0]].concat(choices);
}

// 规划单段（第 i 段：points[i] → points[i+1]）的驾车/直线路线，返回段信息
// 这一点上定下来的路线（用户在「选择路线方案」里挑过的）。没定过返回 null。
function savedRouteOf(p) {
  if (!p || !p.route_json) return null;
  try {
    const r = typeof p.route_json === 'string' ? JSON.parse(p.route_json) : p.route_json;
    return (r && r.path && r.path.length >= 2) ? r : null;
  } catch (e) { return null; }
}

async function planOneSegment(i, a, b) {
  const mode = a.route_mode || 'drive';
  let choices = [];
  let seg = null;
  // 定过就直接用，不再去规划。高德按「此刻能不能走」给结果，同一段路今天 328
  // 公里明天 1361 公里（独库公路十月到五月封山），每次加载都重算的话，用户挑定
  // 的那条线一刷新就没了。
  const saved = mode === 'drive' ? savedRouteOf(a) : null;
  if (saved) {
    const pick = { label: saved.label || '已选定', path: saved.path,
                   time: saved.time || 0, distance: saved.distance || 0,
                   roadRuns: saved.roadRuns || [] };
    return { i, from: a, to: b, mode, choices: [pick], path: saved.path, picked: pick };
  }
  if (mode === 'drive' && distKm(a, b) > 1) { // 直线段或短于 1km 直接直线，省请求
    const inCN = isInChina(a.lng, a.lat) && isInChina(b.lng, b.lat);
    if (inCN) {
      // 国内：高德 Driving 多方案，默认最快一条，其余存起来供点击段时选择
      const from = new AMap.LngLat(a.lng, a.lat);
      const to = new AMap.LngLat(b.lng, b.lat);
      choices = await planSegmentChoices(from, to);
      // 高德绕得离谱时，把 OSRM 那条也摆进备选，并让它当默认。
      // 高德只按「此刻能不能走」规划，碰上季节性封闭的路（独库公路十月到五月封山）
      // 就给一条绕大半个新疆的方案：那拉提→库车直线 203 公里，它规划 1361 公里，
      // 三种策略给的全是绕行，连个能换的都没有。
      choices = relabelChoices(await withShorterAlternative(a, b, choices));
      // seg 要在补完备选之后再取：补进来的那条排在最前，它才是默认展示的
      if (choices.length) seg = choices[0].path;
      // 高德对编号的识别不全时换成 OSRM 的。只处理默认展示的那条 ——
      // 其余备选走的是别的路，硬套过去反而会标错。
      // 10km 以下不值得为标签多发一次请求（里程门槛本来也过不了）。
      if (choices.length && distKm(a, b) > 10) {
        choices[0].roadRuns = await betterRoadRuns(a, b, choices[0]);
      }
    }
    // 海外（或国内高德无结果）：用 OSRM 全球路线（后端已按端点坐标缓存，命中秒回）
    if (!seg) {
      choices = await routeViaOsm(a, b);
      if (choices.length) seg = choices[0].path;
    }
  }
  const path = (seg && seg.length) ? seg : [[a.lng, a.lat], [b.lng, b.lat]];
  return { i, from: a, to: b, mode, choices, path };
}

// 并行规划相邻点之间的驾车路线（总耗时=最慢一段；后端缓存命中时几乎瞬时），
// 全部算完后一次性绘制，避免「先直线兜底再逐段变曲线」的闪烁
async function drawRoute(points, seq) {
  if (points.length < 2) return;
  try {
    await getDriving();
    const segCount = points.length - 1;
    const segInfo = await Promise.all(
      Array.from({ length: segCount }, (_, i) => planOneSegment(i, points[i], points[i + 1]))
    );
    if (seq !== renderSeq) return; // 行程已切换，丢弃
    // 清除直线兜底，一次性绘制所有可点击的路线
    if (polyline) { polyline.setMap(null); polyline = null; }
    segPolylines.forEach(p => p.setMap(null)); // 清掉高亮用的临时直线分段（若有）
    segPolylines = [];
    fallbackSegs = false;
    segmentData = segInfo;
    segInfo.forEach((info) => {
      const poly = new AMap.Polyline({
        path: info.path,
        strokeColor: '#2563eb',
        strokeWeight: 4,
        strokeOpacity: 1,
        lineJoin: 'round',
        zIndex: 50,   // 路线在照片堆(40)之上、轨迹点(100/120)之下
      });
      poly.on('click', () => showSegmentPanel(info.i));
      poly.setMap(map);
      segPolylines.push(poly);
    });
    // 真实路线就绪后重建里程标签（位置要按真实路线算，不能再用直线中点）
    renderSegLabels();
    buildRouteOverlay();     // 自绘路线层跟着重建
    // 真实路线就绪后，若有点处于选中态则恢复相邻段高亮
    if (currentPointId) highlightAdjacentSegments(currentPointId);
    // 真实路线就绪后刷新统计中的总里程
    updateStatsDistance();
    // 无选中点时重新适配视野到完整真实路线，避免全景下曲线被误认为直线。
    // 但如果这次是「恢复了上次保存的视野」，就不要自适应——否则用户存的视野会被顶掉。
    if (!currentPointId && !viewRestored && segPolylines.length && typeof map.setFitView === 'function') {
      try { map.setFitView(segPolylines, false, [60, 60, 60, 60], 300); } catch (e) {}
    }
  } catch (e) {
    // 路线规划异常则保持直线兜底
  }
}

// 清除多方案选择时临时绘制的候选路线
function clearRouteCandidates() {
  routeCandidates.forEach(l => l.setMap(null));
  routeCandidates = [];
}

// 高亮第 idx 条候选路线，其余淡化
function highlightCandidate(idx) {
  routeCandidates.forEach((line, i) => {
    line.setOptions({
      strokeWeight: i === idx ? 7 : 4,
      strokeOpacity: i === idx ? 1 : 0.32,
    });
  });
}

// 地图可视化路线选择（对齐高德导航选路线）：把多条候选路线用不同颜色
// 同时画在地图上，浮出方案卡片，点击切换高亮，点「确定」返回选中的 choice；取消返回 null
function showMapRouteChooser(title, choices) {
  return new Promise((resolve) => {
    clearRouteCandidates();
    let activeIdx = 0;

    // 逐条绘制候选路线（不同颜色，首条默认高亮）
    choices.forEach((c, i) => {
      const color = ROUTE_COLORS[i % ROUTE_COLORS.length];
      const line = new AMap.Polyline({
        path: c.path,
        strokeColor: color,
        strokeWeight: i === 0 ? 7 : 4,
        strokeOpacity: i === 0 ? 1 : 0.32,
        strokeStyle: 'solid',
        lineJoin: 'round',
        lineCap: 'round',
        zIndex: 50,   // 路线在照片堆(40)之上、轨迹点(100/120)之下
      });
      line.setMap(map);
      routeCandidates.push(line);
    });

    const box = document.getElementById('route-chooser');
    const list = document.getElementById('route-chooser-list');
    document.getElementById('route-chooser-sub').textContent = title;
    list.innerHTML = '';
    choices.forEach((c, i) => {
      const color = ROUTE_COLORS[i % ROUTE_COLORS.length];
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'route-option' + (i === 0 ? ' active' : '');
      const mins = Math.max(1, Math.round((c.time || 0) / 60));
      const km = ((c.distance || 0) / 1000).toFixed(1);
      const lbl = c.label ? ` · ${c.label}` : '';
      btn.innerHTML =
        `<span class="route-dot" style="background:${color}"></span>` +
        `<span class="route-option-info">` +
          `<span class="route-option-label">方案 ${i + 1}${lbl}</span>` +
          `<span class="route-option-meta">约 ${mins} 分钟 · ${km} 公里</span>` +
        `</span>`;
      btn.onclick = () => {
        activeIdx = i;
        highlightCandidate(i);
        // 点击方案后，把地图移动到该方案路径所在位置并显示，方便用户查看对比
        if (routeCandidates[i] && typeof map.setFitView === 'function') {
          try { map.setFitView(routeCandidates[i], false, [80, 80, 80, 80], 300); } catch (e) {}
        }
        list.querySelectorAll('.route-option').forEach((x, j) =>
          x.classList.toggle('active', j === i));
      };
      list.appendChild(btn);
    });

    // 打开选线浮层时，先把地图适配到首条（默认选中）方案路径
    if (routeCandidates[0] && typeof map.setFitView === 'function') {
      try { map.setFitView(routeCandidates[0], false, [80, 80, 80, 80], 300); } catch (e) {}
    }

    const okBtn = document.getElementById('route-chooser-ok');
    const cancelBtn = document.getElementById('route-chooser-cancel');
    const cleanup = () => {
      box.classList.add('hidden');
      clearRouteCandidates();
      okBtn.onclick = null;
      cancelBtn.onclick = null;
    };
    okBtn.onclick = () => {
      const picked = choices[activeIdx];
      cleanup();
      resolve(picked);
    };
    cancelBtn.onclick = () => {
      cleanup();
      resolve(null);
    };

    box.classList.remove('hidden');
  });
}

// 现场把这一段的备选重新规划一遍，并把当前定着的那条也并进去。
// 用在「定过路线的段想换一条」：那种段加载时不规划，手里只有存下来的一条。
async function replanSegmentChoices(i, from, to, current) {
  let list = [];
  if (distKm(from, to) > 1
      && isInChina(from.lng, from.lat) && isInChina(to.lng, to.lat)) {
    list = await planSegmentChoices(new AMap.LngLat(from.lng, from.lat),
                                    new AMap.LngLat(to.lng, to.lat));
    list = await withShorterAlternative(from, to, list);
  }
  if (!list.length) list = await routeViaOsm(from, to);
  // 当前定着的那条要留在列表里：它可能已经规划不出来了（封路、路网更新），
  // 不放进去的话用户一打开选择器就等于被迫换掉。里程撞车就算同一条，不重复列。
  if (current && current.path && current.path.length >= 2) {
    const dup = list.some(c => Math.abs((c.distance || 0) - (current.distance || 0)) < 500);
    if (!dup) list = [current].concat(list);
  }
  list = relabelChoices(list);
  if (segmentData[i]) segmentData[i].choices = list;
  return list;
}

// 点击路径段（地图点线段 or 侧边栏分隔条）弹出的操作面板：切换驾车/直线、选择路线方案
function showSegmentPanel(i) {
  const points = lastPoints;
  if (!points || i < 0 || i >= points.length - 1) return;
  const from = points[i], to = points[i + 1];
  const data = segmentData[i] || { i, from, to, mode: from.route_mode || 'drive', choices: [], path: null };
  const mode = data.mode;
  const choices = data.choices || [];
  const title = `${from.name || '起点'} 至 ${to.name || '终点'}`;

  const ov = document.createElement('div');
  ov.className = 'modal';
  ov.innerHTML = `
    <div class="modal-box">
      <h2>${esc(title)} 路径</h2>
      <div class="seg-mode-row">
        <button type="button" class="btn ${mode === 'drive' ? 'btn-primary' : ''}" data-mode="drive">🚗 驾车</button>
        <button type="button" class="btn ${mode === 'line' ? 'btn-primary' : ''}" data-mode="line">📏 直线</button>
      </div>
      ${mode === 'drive' ? `<button type="button" class="btn" data-choose>${
        choices.length > 1 ? `选择路线方案（${choices.length} 条）` : '重新规划路线'}</button>` : ''}
      <button type="button" class="btn" data-cancel style="opacity:.7">关闭</button>
    </div>`;
  document.body.appendChild(ov);

  ov.addEventListener('click', async (e) => {
    const modeBtn = e.target.closest('button[data-mode]');
    if (modeBtn) {
      const m = modeBtn.getAttribute('data-mode');
      ov.remove();
      if (m !== mode) await setSegmentMode(i, m);
      return;
    }
    const chooseBtn = e.target.closest('button[data-choose]');
    if (chooseBtn) {
      ov.remove();
      // 让浏览器先把「框已经没了」这一帧画出来，再去做几秒钟的规划。
      // 不让这一帧的话，点完按钮框还杵在那儿，像是没响应。
      await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
      let list = choices;
      // 定过路线的段，加载时直接用存下来的那条、不再规划，所以这里只有一条 ——
      // 不现场补一次的话，用户定完就再也改不回去了（按钮压根不出现）。
      if (list.length < 2) list = await replanSegmentChoices(i, from, to, data.picked);
      if (list.length < 2) {
        tmAlert('这一段只规划得出一条路线');
        return;
      }
      const picked = await showMapRouteChooser(title, list);
      if (picked && picked.path) redrawSegment(i, picked.path, picked);
      return;
    }
    if (e.target.closest('button[data-cancel]')) { ov.remove(); return; }
    if (e.target === ov) ov.remove();
  });
}

// 切换第 i 段的连线方式（驾车/直线）：只更新这一段，不重绘全部、不缩放地图
async function setSegmentMode(i, mode) {
  const points = lastPoints;
  if (!points || i < 0 || i >= points.length - 1) return;
  const from = points[i], to = points[i + 1];
  // 1. 更新后端
  try {
    const fd = new FormData();
    fd.append('route_mode', mode);
    await api(`/api/points/${from.id}/route-mode`, { method: 'PUT', body: fd });
  } catch (err) {
    tmAlert('切换失败：' + err.message);
    return;
  }
  // 2. 同步本地数据
  from.route_mode = mode;
  if (segmentData[i]) segmentData[i].mode = mode;

  // 3. 只重绘这一段（不触发全量渲染、不缩放地图）
  if (mode === 'line') {
    redrawSegment(i, [[from.lng, from.lat], [to.lng, to.lat]], null);
    if (segmentData[i]) segmentData[i].choices = [];
  } else {
    // 驾车：现场规划这一段的备选路线
    let choices = [];
    if (distKm(from, to) > 1) {
      if (isInChina(from.lng, from.lat) && isInChina(to.lng, to.lat)) {
        choices = await planSegmentChoices(
          new AMap.LngLat(from.lng, from.lat), new AMap.LngLat(to.lng, to.lat));
      }
      if (!choices.length) choices = await routeViaOsm(from, to);
    }
    if (segmentData[i]) segmentData[i].choices = choices;
    if (choices.length > 1) {
      // 多条路线：可视化选线（颜色区分 + 确定）
      const title = `${from.name || '起点'} → ${to.name || '终点'}`;
      const picked = await showMapRouteChooser(title, choices);
      if (picked && picked.path) redrawSegment(i, picked.path, picked);
      else if (choices[0]) redrawSegment(i, choices[0].path, choices[0]);
      else redrawSegment(i, [[from.lng, from.lat], [to.lng, to.lat]], null);
    } else if (choices.length === 1) {
      redrawSegment(i, choices[0].path, choices[0]);
    } else {
      redrawSegment(i, [[from.lng, from.lat], [to.lng, to.lat]], null);
    }
  }
  // 4. 同步侧边栏图标（不重载列表）
  updateSegmentDivider(i);
}

// 更新第 i 个分隔条的图标（切换模式后同步，不重载列表）
function updateSegmentDivider(i) {
  const seg = document.querySelector(`.seg-divider[data-seg-index="${i}"]`);
  if (!seg) return;
  const rm = seg.querySelector('.rm-seg');
  if (!rm) return;
  const mode = (lastPoints[i] && lastPoints[i].route_mode) || 'drive';
  rm.textContent = mode === 'drive' ? '🚗' : '📏';
}

// 重画第 i 段路线为指定 path（多方案选择后）。
// choice 是用户挑中的那条方案（里程 + 公路编号都跟着它走），切成直线时传 null。
function redrawSegment(i, path, choice) {
  const poly = segPolylines[i];
  if (!poly) return;
  poly.setPath(path);
  if (segmentData[i]) {
    segmentData[i].path = path;
    segmentData[i].picked = choice || null;
  }
  // 换了路线，中点、里程、公路编号全变了，标签得重建
  renderSegLabels();
  // 顶栏那行统计里的总里程也是按各段加起来的，不刷一下就还是旧数
  // —— 表现出来就是「换完路要刷新页面才看到变化」
  updateStatsDistance();
  // 挑定的方案要存下来，否则一刷新就被重新规划的结果顶掉 —— 这个功能原来
  // 只改内存，选完看着是生效的，切个行程回来又变回默认那条。
  saveSegmentRoute(i, choice);
}

// 把第 i 段挑定的路线存到这一段的起点上。choice 为空表示恢复成每次现规划。
async function saveSegmentRoute(i, choice) {
  const from = lastPoints && lastPoints[i];
  if (!from || !from.id) return;
  const body = new FormData();
  if (choice && choice.path && choice.path.length >= 2) {
    body.append('route', JSON.stringify({
      path: choice.path, distance: choice.distance || 0, time: choice.time || 0,
      label: choice.label || '', roadRuns: choice.roadRuns || [],
    }));
    from.route_json = JSON.stringify({
      path: choice.path, distance: choice.distance || 0, time: choice.time || 0,
      label: choice.label || '', roadRuns: choice.roadRuns || [],
    });
  } else {
    body.append('route', '');
    from.route_json = null;
  }
  try {
    await api(`/api/points/${from.id}/route`, { method: 'PUT', body });
  } catch (e) {
    // 存不上不该打断操作：这一次的选择在内存里是生效的，只是刷新后会回到默认
    tmAlert('路线没能保存：' + e.message);
  }
}

// ---------- 详情 ----------
function showDetail(pointId) {
  // 面板收起时点地图上的点 → 自动展开面板显示详情
  setPanelCollapsed(false);
  currentPointId = pointId;
  saveTripState({ pointId: pointId, tab: 'detail' });
  // 高亮列表
  document.querySelectorAll('#point-list li').forEach(li => {
    li.classList.toggle('active', String(li.dataset.id) === String(pointId));
  });
  // 切到详情 tab
  switchTab('detail');
  highlightMapPoint(pointId);
  highlightAdjacentSegments(pointId);
  styleSegLabels(pointId);
  loadDetail(pointId);
}

async function loadDetail(pointId) {
  const point = await api(`/api/trips/${currentTripId}/points`).then(
    pts => pts.find(p => p.id === pointId));
  if (!point) return;

  $('#detail-empty').classList.add('hidden');
  $('#detail-body').classList.remove('hidden');
  const nameEl = $('#detail-name');
  nameEl.textContent = point.is_waypoint ? '途经 · ' + point.name : point.name;
  // 经纬度不再单独占一行，收进 title：想看的时候悬停/长按仍然拿得到
  nameEl.title = `${point.name}（${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}）`;
  $('#detail-date').textContent = fmtDate(point.start_date);
  $('#detail-desc').textContent = point.description || '';

  // 辅助点（纯途经点）没有上传入口——「＋」格由 renderMedia 按 is_waypoint 决定加不加

  if (point.is_waypoint) {
    const grid = $('#media-grid');
    grid.innerHTML = '';
    lightboxMedia = [];
    return;
  }

  // 媒体
  const media = await api(`/api/points/${pointId}/media`);
  renderMedia(media);
}

// ---------- 照片描述编辑（相册网格 / 点详情网格 / 灯箱共用一个入口） ----------
// 保存成功后把新描述同步回所有还引用着这条媒体的数组，免得某个视图还显示旧文字。
// 返回 true 表示确实改了（调用方据此决定要不要重绘）。
async function editMediaCaption(m) {
  // 不给 message：标题「加点描述」已经说清楚了，再来一行说明只会挤掉能写字的地方
  const val = await tmPrompt('', m.caption || '', '加点描述',
                             { multiline: true, rows: 6, okText: '保存' });
  if (val === null) return false;                 // 取消
  const text = val.trim().slice(0, 500);
  if (text === (m.caption || '')) return false;   // 没改
  try {
    const fd = new FormData();
    fd.append('caption', text);
    await api(`/api/media/${m.id}/caption`, { method: 'PUT', body: fd });
  } catch (err) {
    tmAlert('保存失败：' + err.message);
    return false;
  }
  m.caption = text;
  [photoItems, photoShown, lightboxMedia, detailMedia].forEach(arr => {
    if (!arr) return;
    const hit = arr.find(x => x && Number(x.id) === Number(m.id));
    if (hit) hit.caption = text;
  });
  return true;
}

// 点详情的媒体网格。参考发朋友圈的九宫格：
//   · 每格左下角一个 ✎，直接改这张的描述，不用先进灯箱
//   · 长按可以拖拽排序，顺序存回服务端
//   · 最后一格是「＋」，替代原来单独占一行的「上传照片/视频」按钮
let detailMedia = [];      // 当前点详情里的媒体（拖拽排序、描述同步都基于它）
// 九宫格正在拖拽排序。置位期间，下面板的 tab 横滑要让路——
// 否则长按图片后往左右一动，切的是 tab，图片纹丝不动。
let mediaDragging = false;
// 由 initTabSwipe 注册：把已经跟手走了一段的 tab 轨道弹回原位并停止接管
let cancelTabSwipe = function () {};

function renderMedia(media) {
  const grid = $('#media-grid');
  grid.innerHTML = '';
  applyMediaCols();      // 重建后把当前缩放档位重新落到网格上
  // 服务端列表 + 刚传完但这份快照里还没有的（见 mergeRecentUploads）
  detailMedia = mergeRecentUploads(currentPointId, media || []);
  lightboxMedia = detailMedia;
  detailMedia.forEach((m) => {
    grid.appendChild(buildMediaItem(m));
  });
  // 正在上传的占位格挂回来。少了这一步，上传途中任何一次 loadDetail
  // （点 tab / 横滑切 tab / 删一张图）都会把在传的格子连同上传结果一起丢掉。
  reattachActiveUploads(grid);
  // 「＋」永远排在最后一格；辅助点（纯途经点）不给上传入口
  const pt = (lastPoints || []).find(p => Number(p.id) === Number(currentPointId));
  if (!pt || !pt.is_waypoint) grid.appendChild(buildMediaAddCell());
  // 上次没传完的（刷新/换设备回来）：异步拉一次，插「未完成的上传」占位格
  if (!pt || !pt.is_waypoint) refreshResumableCells(currentPointId);
}

function buildMediaItem(m) {
  const item = el('div', 'media-item');
  item.dataset.mid = String(m.id);
  const img = el('img');
  // 用服务端算好的地址：刚上传的文件还没同步到 OSS，拼出来的 OSS 地址是 404，
  // 界面上就是一块裂图（照片传完 / 视频转码中最容易撞上）。
  img.src = mediaSrc(m, 'thumb');
  // 缩略图还没生成好（视频转码中）或者同步慢了半拍：给个占位，别留裂图
  img.classList.add('thumb-loading');
  img.onload = () => img.classList.remove('thumb-loading');
  mediaFallbackToLocal(img, '/uploads/thumbs/' + m.thumb_filename);
  img.loading = 'lazy';
  img.draggable = false;                 // 交给我们自己的指针拖拽，不用浏览器原生 DnD
  item.appendChild(img);
  if (m.type === 'video') item.appendChild(el('span', 'video-badge', '▶'));

  // 点图看大图。用 detailMedia 现算下标：拖拽排过序后位置会变
  img.onclick = () => {
    if (item.classList.contains('just-dragged')) return;   // 刚拖完那一下不当点击
    const idx = detailMedia.findIndex(x => Number(x.id) === Number(m.id));
    openLightbox(Math.max(0, idx));
  };

  // ✎ 改描述：原来这里是拍摄日期，日期在灯箱和相册里都能看到，占着格子不划算；
  // 换成直接可用的编辑入口，省掉「先进灯箱再改」这一步
  const edit = el('button', 'media-cap', '✎');
  edit.type = 'button';
  edit.title = m.caption ? '修改描述' : '加点描述';
  if (m.caption && m.caption.trim()) edit.classList.add('has');
  edit.onclick = async (e) => {
    e.stopPropagation();
    if (await editMediaCaption(m)) {
      edit.classList.toggle('has', !!(m.caption && m.caption.trim()));
      edit.title = m.caption ? '修改描述' : '加点描述';
      if (currentTab === 'albums') renderPhotoGrid();
    }
  };
  item.appendChild(edit);

  const del = el('button', 'del', '×');
  del.type = 'button';
  // 乐观删除：先从界面上拿掉，再发请求。
  // 原来是 `await DELETE` → `loadDetail()`，也就是**两个**隧道往返之后格子才消失；
  // 同时有视频在传的时候隧道是满的，实测要等半秒以上，期间点了删除什么反馈都没有。
  // 而这个操作几乎不会失败（服务端只是几次 unlink + 一条 DELETE），失败也完全
  // 可逆 —— 把格子原位放回去 + 一条 toast 就交代清楚了。
  // 顺带去掉 loadDetail()：要拿掉的那一条我们自己就能从 detailMedia 里摘掉，
  // 为此重拉整个点的媒体列表（又一个隧道往返 + 整格重绘）不划算。
  del.onclick = async (e) => {
    e.stopPropagation();
    if (!await tmConfirm('删除这张媒体？', '删除媒体', { danger: true, okText: '删除' })) return;
    const grid = item.parentElement;
    const next = item.nextSibling;                      // 失败要放回原位，先记住锚点
    const idx = detailMedia.findIndex((x) => Number(x.id) === Number(m.id));
    item.remove();
    if (idx >= 0) detailMedia.splice(idx, 1);
    lightboxMedia = detailMedia;
    forgetRecentUpload(m.id);   // 别让「刚上传」的补丁表把刚删掉的又补回来
    scheduleMediaCountRefresh();   // 地图角标 / 列表计数 / 统计条跟着少一个
    try {
      await api(`/api/media/${m.id}`, { method: 'DELETE' });
    } catch (err) {
      if (grid && grid.isConnected) {
        if (next && next.parentElement === grid) grid.insertBefore(item, next);
        else grid.appendChild(item);
      }
      if (idx >= 0) detailMedia.splice(idx, 0, m);
      lightboxMedia = detailMedia;
      scheduleMediaCountRefresh();
      tmToast('删除失败，已放回：' + (err.message || ''));
    }
  };
  item.appendChild(del);
  return item;
}

function buildMediaAddCell() {
  const cell = el('div', 'media-item media-add');
  cell.title = '添加照片/视频';
  cell.appendChild(el('span', 'media-add-plus', '＋'));
  cell.onclick = () => pickMediaFiles();
  return cell;
}

// ---------- 选文件：停在上次用过的目录 ----------
// <input type="file"> 每次都从浏览器的默认目录开始。手机上无所谓（弹的是相册），
// 桌面上却很烦：整理一趟旅行要从同一个目录里分好几批挑照片，每次都得重新翻一遍。
//
// File System Access API 的 showOpenFilePicker 带一个 id 参数：同一个 id，浏览器
// 自己记住上次选文件的目录，下次从那儿打开，跨会话有效，不需要我们存任何东西
// （也存不了 —— 目录句柄不让持久化到 localStorage）。
//
// 不支持的浏览器（Firefox / Safari / 所有手机）原样回落到 <input type="file">，
// 手机上更是**故意**回落：系统相册选择器比文件管理器好用得多。
const MEDIA_PICKER_ID = 'travel-memory-media';

const MEDIA_PICKER_TYPES = [{
  description: '照片和视频',
  accept: {
    'image/*': ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.bmp', '.avif'],
    'video/*': ['.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv', '.3gp'],
  },
}];

async function pickMediaFiles() {
  // 手机 / 不支持的浏览器 → 原来的 input，行为完全不变
  if (!window.showOpenFilePicker || isTouchDevice()) return $('#file-input').click();

  let handles;
  try {
    handles = await window.showOpenFilePicker({
      id: MEDIA_PICKER_ID,   // ← 记住目录全靠它，别改名，改了等于把用户的记忆清零
      multiple: true,
      types: MEDIA_PICKER_TYPES,
      excludeAcceptAllOption: false,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') return;   // 用户点了「取消」，什么都不做
    // 其它异常（旧版内核不认 types、iframe 里没权限……）：回落，别把上传入口整坏了
    return $('#file-input').click();
  }

  if (!handles || !handles.length || !currentPointId) return;
  let files;
  try {
    files = await Promise.all(handles.map(h => h.getFile()));
  } catch (err) {
    return tmToast('读取文件失败，请重试');
  }
  startUploads(files);
}

// ---------- 上传：占位格 + 环形进度 + 并发 ----------
// 原来的做法是 for + await 串行上传，全程没有任何界面反馈，最后才 loadDetail 重绘一次。
// 手机上选一段视频，点完「＋」直接就回到页面了，几十秒里什么都不发生 —— 看起来就是没反应。
// 现在改成：选中文件的瞬间先在「＋」前面插入占位格（图片用本地预览、视频抽第一帧当封面），
// 上面盖一圈环形进度；「＋」自动被挤到后面，可以马上再点、再选下一批，多个文件同时传。
const UPLOAD_CONCURRENCY = 3;   // 同时处理 3 个文件：再多手机上行带宽反而互相拖慢

// ---------- 分片上传 / 断点续传 ----------
// 手机上一段视频动辄 100MB+，单请求上传网络一抖就得从 0 重来。超过阈值的文件
// 改成切片逐个传，服务端记住收到哪些片，断了接着传。
// 小文件继续走原来的单请求接口（少一次建会话往返，也让分享页/旧客户端不受影响）。
// 这两个允许被页面加载前注入的全局覆盖 —— 只为 E2E 用（拿几百 KB 的小文件
// 就能跑通「多分片 + 断点续传」，不用真造一个 100MB 的样本传上一分钟）。
// 生产环境没人设这两个全局，取的就是下面的默认值；而且服务端对 chunk_size
// 有自己的上下限钳制，前端就算乱传也开不出口子。
const CHUNK_THRESHOLD = Number(window.TM_CHUNK_THRESHOLD) || 8 * 1024 * 1024;
const CHUNK_SIZE = Number(window.TM_CHUNK_SIZE) || 4 * 1024 * 1024;
const CHUNK_PARALLEL = 3;                   // 同一个文件最多同时传几片
const CHUNK_RETRIES = 4;                    // 单片失败重试次数（指数退避）
const FP_SAMPLE_BYTES = 2 * 1024 * 1024;    // 指纹只取前 2MB —— 整文件 md5 在手机上会卡死
const UPLOAD_STORE_KEY = 'tm_uploads_v1';
const UPLOAD_STORE_TTL = 24 * 3600 * 1000;  // 和服务端会话过期时间对齐

// 全局连接预算。没有它就会变成「3 个文件 × 每个 3 片 = 9 条并发连接」，
// 上行被打满之后每一条都慢，整体反而更久，弱网下还会集体超时。
// 这个闸门管住所有上传请求（整文件的算 1 条，每个分片也算 1 条）。
const UPLOAD_CONNECTIONS = 4;
const uploadGate = (function () {
  let inUse = 0;
  const waiting = [];
  return {
    acquire() {
      if (inUse < UPLOAD_CONNECTIONS) { inUse++; return Promise.resolve(); }
      return new Promise((resolve) => waiting.push(resolve)).then(() => { inUse++; });
    },
    release() {
      inUse = Math.max(0, inUse - 1);
      const next = waiting.shift();
      if (next) next();
    },
    get inUse() { return inUse; },
  };
})();

function uploadSleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function fnv1a64Hex(bytes) {
  // crypto.subtle 只在安全上下文（https / localhost）可用。明文 http 访问时
  // 退回这个纯 JS 哈希 —— 指纹只用来「认出是不是同一个文件」，不做安全判断。
  let h1 = 0x811c9dc5, h2 = 0x01000193;
  for (let i = 0; i < bytes.length; i++) {
    h1 = (h1 ^ bytes[i]) >>> 0; h1 = Math.imul(h1, 16777619) >>> 0;
    h2 = (h2 + bytes[i]) >>> 0; h2 = Math.imul(h2 ^ (h2 >>> 7), 2246822519) >>> 0;
  }
  return ('00000000' + h1.toString(16)).slice(-8) +
         ('00000000' + h2.toString(16)).slice(-8);
}

// 文件指纹：前 2MB + 大小 + 文件名 + lastModified。
// 刻意**不**算整文件 md5 —— 100MB 的视频在手机上算一遍要好几秒还烫手，
// 而这几项组合起来重复的概率已经低到无所谓（撞了最多是白传一次）。
async function fileFingerprint(file) {
  const head = await file.slice(0, Math.min(FP_SAMPLE_BYTES, file.size)).arrayBuffer();
  const meta = new TextEncoder().encode(
    '|' + file.size + '|' + (file.name || '') + '|' + (file.lastModified || 0));
  const buf = new Uint8Array(head.byteLength + meta.length);
  buf.set(new Uint8Array(head), 0);
  buf.set(meta, head.byteLength);
  if (window.crypto && crypto.subtle && crypto.subtle.digest) {
    try {
      const d = await crypto.subtle.digest('SHA-256', buf);
      return Array.from(new Uint8Array(d))
        .map((b) => ('0' + b.toString(16)).slice(-2)).join('');
    } catch (e) { /* 非安全上下文：落到下面的兜底 */ }
  }
  return 'fnv-' + fnv1a64Hex(buf) + '-' + file.size;
}

// 会话状态存 localStorage：刷新回来还知道「这个点上有个传了一半的文件」。
// 存的只是 id 和进度，**存不了 File 句柄** —— 浏览器不允许，见 buildResumeCell 的说明。
function loadUploadStore() {
  try { return JSON.parse(localStorage.getItem(UPLOAD_STORE_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}
function saveUploadStore(store) {
  const cutoff = Date.now() - UPLOAD_STORE_TTL;
  for (const k of Object.keys(store)) {
    if (!(store[k] && store[k].updated > cutoff)) delete store[k];
  }
  try { localStorage.setItem(UPLOAD_STORE_KEY, JSON.stringify(store)); } catch (e) {}
}
function rememberUpload(rec) {
  const store = loadUploadStore();
  store[rec.session_id] = Object.assign({}, store[rec.session_id], rec,
                                        { updated: Date.now() });
  saveUploadStore(store);
}
function forgetUpload(sessionId) {
  const store = loadUploadStore();
  delete store[sessionId];
  saveUploadStore(store);
}

function isVideoFile(file) {
  return /^video\//.test(file.type || '') ||
         /\.(mp4|mov|avi|mkv|webm)$/i.test(file.name || '');
}

// 视频抽第一帧当封面。抽不到（编解码不支持、元数据没到）就安静放弃，退回图标底。
function grabVideoPoster(file, imgEl) {
  const url = URL.createObjectURL(file);
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'metadata'; v.src = url;
  let done = false;
  const finish = (dataUrl) => {
    if (done) return;
    done = true;
    URL.revokeObjectURL(url);
    if (dataUrl) { imgEl.src = dataUrl; imgEl.classList.remove('hidden'); }
  };
  v.onloadeddata = () => {
    try { v.currentTime = Math.min(0.1, (v.duration || 1) / 10); } catch (e) { finish(null); }
  };
  v.onseeked = () => {
    try {
      const c = document.createElement('canvas');
      c.width = v.videoWidth || 160; c.height = v.videoHeight || 160;
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
      finish(c.toDataURL('image/jpeg', 0.7));
    } catch (e) { finish(null); }
  };
  v.onerror = () => finish(null);
  setTimeout(() => finish(null), 4000);      // 卡住就别一直挂着
}

// ---------- 正在上传的任务：暂停 / 继续 / 取消 ----------
// job 里已经攒着这次上传的所有句柄（整文件那条是 job.xhr，分片那条是
// job.chunkXhrs），下面三个函数就是把这些句柄用起来。原来它们只被记录、
// 从来没人调用 —— 于是界面上根本没有停下来的办法。
function uploadWaitResume(job) {
  if (!job || !job.paused) return Promise.resolve();
  return new Promise((resolve) => { (job._waiters = job._waiters || []).push(resolve); });
}

function pauseUploadJob(job) {
  if (!job || job.paused || job.cancelled) return;
  job.paused = true;
  // 已经在飞的分片直接掐掉，别继续占着上行带宽 —— 恢复时这一片原样重传。
  // （服务端只认「整片收齐」，掐掉的半片不会留下脏数据）
  if (job.chunkXhrs) job.chunkXhrs.forEach((x) => { try { x.abort(); } catch (e) {} });
}

function resumeUploadJob(job) {
  if (!job || !job.paused) return;
  job.paused = false;
  const waiters = job._waiters || [];
  job._waiters = [];
  waiters.forEach((r) => r());
}

function cancelUploadJob(job) {
  if (!job || job.cancelled) return;
  job.cancelled = true;
  resumeUploadJob(job);            // 挂在暂停上的 worker 要先放出来才能看到 cancelled
  try { if (job.xhr) job.xhr.abort(); } catch (e) {}
  if (job.chunkXhrs) job.chunkXhrs.forEach((x) => { try { x.abort(); } catch (e) {} });
  // 服务端那边的临时分片一并作废，不然要等 24 小时的回收任务才清得掉
  const sid = job.session && job.session.session_id;
  if (sid) {
    api(uploadSessionUrl(job.session), { method: 'DELETE' }).catch(() => {});
    forgetUpload(sid);
  }
}

// 上传操作条的图标：全部走内联 SVG。
// 之前「继续上传」和「本地播放」都是字符 ▶，两个圆钮并排长得一模一样，
// 根本分不出哪个是放视频、哪个是接着传 —— 所以继续上传单独给一个上传箭头。
const UP_ACT_ICONS = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.14v13.72c0 .8.87 1.3 1.56.88l10.6-6.86a1.05 1.05 0 0 0 0-1.76L9.56 4.26A1.04 1.04 0 0 0 8 5.14z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"/></svg>',
  // 继续上传：向上的箭头顶着一条横线（「接着往上传」），和播放的实心三角不会认错
  resume: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.6 18.4 9h-4v7.2h-4.8V9h-4z"/><path d="M4.6 19h14.8v2.4H4.6z"/></svg>',
};

// 本地预览：传到一半也能先看看自己选的到底是哪一段。
// 放的是浏览器里的那份 File（blob: URL），完全不碰网络。
function openLocalPreview(file) {
  const url = URL.createObjectURL(file);
  const ov = el('div', 'local-preview');
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    ov.remove();
    URL.revokeObjectURL(url);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const video = isVideoFile(file);
  let media;
  if (video) {
    // 属性一个个写，不用 Object.assign：playsinline / webkit-playsinline 这两个
    // 老 iOS 只认 HTML 属性，写成 property 它当没看见，于是被顶成全屏播放器。
    media = document.createElement('video');
    media.controls = true;
    media.playsInline = true;
    media.setAttribute('playsinline', '');
    media.setAttribute('webkit-playsinline', '');
    media.preload = 'metadata';
    media.src = url;
  } else {
    media = document.createElement('img');
    media.src = url;
  }
  media.className = 'local-preview-media';
  ov.appendChild(el('div', 'local-preview-note', '本地文件预览 · 上传仍在继续'));
  const x = el('button', 'local-preview-close', '×');
  x.type = 'button';
  x.title = '关闭';
  x.onclick = (e) => { e.stopPropagation(); close(); };
  ov.appendChild(x);
  ov.appendChild(media);
  // 放不了的时候必须说话。之前解码失败就是一块纯黑，用户只会以为「点了没反应」。
  const err = el('div', 'local-preview-err');
  err.style.display = 'none';
  ov.appendChild(err);
  const fail = (msg) => {
    err.textContent = msg;
    err.style.display = '';
    media.style.display = 'none';
  };
  media.onerror = () => {
    fail(video
      ? '这个视频浏览器解不了码（多半是 iPhone 拍的 HEVC/MOV）。上传完服务端会转码，到时候就能在线播放了。'
      : '这张图片浏览器打不开。');
  };
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.addEventListener('keydown', onKey);
  document.body.appendChild(ov);
  // 起播要等元素进了 DOM 再手动调 play()：原来挂的是 autoplay 属性，
  // 带声音的自动播放会被浏览器的 autoplay policy 直接拒掉 —— 画面就停在第一帧不动，
  // 看起来正是「点了播放放不出来」。手动 play() 失败就退一步静音再试一次，
  // 还不行就把 controls 留给用户自己点，至少不是死的。
  if (video) {
    const tryPlay = () => media.play().catch(() => {
      media.muted = true;
      return media.play().catch(() => {});
    });
    if (media.readyState >= 2) tryPlay();
    else media.addEventListener('loadeddata', tryPlay, { once: true });
  }
}

function buildUploadCell(file, job) {
  const video = isVideoFile(file);
  const cell = el('div', 'media-item media-uploading' + (video ? ' is-video' : ''));
  cell._file = file;
  cell._job = job || null;
  const img = el('img', 'up-prev');
  if (video) {
    img.classList.add('hidden');             // 封面抽出来之前先不显示，露出底下的胶片纹
    grabVideoPoster(file, img);
  } else {
    const url = URL.createObjectURL(file);
    img.src = url;
    img.onload = () => URL.revokeObjectURL(url);
  }
  cell.appendChild(img);
  // 视频/图片用不同的角标区分，和上传完之后的 ▶ 角标保持一致
  cell.appendChild(el('span', 'up-kind', video ? '▶ 视频' : '图片'));
  const ring = el('div', 'up-ring');
  ring.innerHTML =
    '<svg viewBox="0 0 40 40" aria-hidden="true">' +
      '<circle class="up-bg" cx="20" cy="20" r="15" fill="none" stroke-width="3.5"/>' +
      '<circle class="up-fg" cx="20" cy="20" r="15" fill="none" stroke-width="3.5" ' +
        'stroke-dasharray="94.25" stroke-dashoffset="94.25"/>' +
    '</svg><span class="up-pct">0%</span>';
  cell.appendChild(ring);
  // 进度 100% 之后服务端还有活要干（合并分片 → 生成缩略图/抽视频封面），
  // 这一段没有百分比可报。原来只是把数字抹掉转个圈，看起来就是「卡在 100% 不动」。
  // 现在明确写出当前在干什么。
  const stage = el('span', 'up-stage');
  cell.appendChild(stage);

  // 操作条：本地预览 / 暂停续传 / 取消。
  // 少了它，两个视频一起传的时候用户什么都做不了 —— 既停不掉传错的那个，
  // 也没法确认自己选的到底是哪一段。
  const acts = el('div', 'up-acts');
  const mkAct = (cls, html, title, fn) => {
    const b = el('button', 'up-act ' + cls);
    b.type = 'button';
    b.innerHTML = html;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.onclick = (e) => { e.stopPropagation(); fn(b); };
    acts.appendChild(b);
    return b;
  };
  mkAct('up-act-play', UP_ACT_ICONS.play,
        '播放本地文件（还没传完，放的是你设备里那一份）',
        () => openLocalPreview(file));
  // 暂停只对走分片的大文件有意义 —— 小文件是一次性请求，"暂停"等于从头再来，
  // 与其给一个骗人的按钮，不如不给
  let pauseBtn = null;
  if (file.size >= CHUNK_THRESHOLD) {
    pauseBtn = mkAct('up-act-pause', UP_ACT_ICONS.pause, '暂停上传', () => {
      const j = cell._job;
      if (!j) return;
      if (j.paused) { resumeUploadJob(j); cell._setPaused(false); }
      else { pauseUploadJob(j); cell._setPaused(true); }
    });
  }
  mkAct('up-act-cancel', '×', '取消上传', () => {
    if (cell._job) cancelUploadJob(cell._job);
    else cell._cancelledBeforeStart = true;   // 还在排队没轮到，标记一下别再起跑
    cell.remove();
  });
  cell.appendChild(acts);

  // 实时速率 + 剩余时间。为什么值得占这一行字：「上传有点慢」这句话里，
  // 慢在自己的上行、慢在服务端、还是根本卡住了，用户没有任何线索可以判断；
  // 一个 380KB/s·还剩 2 分钟，比一个只会往前爬的百分比有用得多。
  // EWMA 平滑：瞬时值在手机网络上跳得没法看，α=0.3 大约是「看最近 3 秒」。
  let spd = null;
  const fmtRate = (bps) => (bps >= 1024 * 1024
    ? (bps / (1024 * 1024)).toFixed(1) + ' MB/s'
    : Math.round(bps / 1024) + ' KB/s');
  const fmtEta = (sec) => (sec >= 3600 ? '>1 小时'
    : sec >= 60 ? Math.round(sec / 60) + ' 分' : Math.max(1, Math.round(sec)) + ' 秒');
  cell._setProgress = (p) => {
    const fg = ring.querySelector('.up-fg');
    const pct = ring.querySelector('.up-pct');
    if (fg) fg.setAttribute('stroke-dashoffset', String(94.25 * (1 - Math.max(0, Math.min(1, p)))));
    if (pct) pct.textContent = Math.round(p * 100) + '%';
    if (cell.classList.contains('processing') || cell.classList.contains('paused')) return;
    const now = Date.now();
    const sent = Math.max(0, Math.min(1, p)) * file.size;
    if (!spd) { spd = { t: now, sent: sent, bps: 0 }; return; }
    const dt = (now - spd.t) / 1000;
    if (dt < 0.8) return;                       // 采样太密只会放大抖动
    const inst = Math.max(0, sent - spd.sent) / dt;
    spd.bps = spd.bps ? spd.bps * 0.7 + inst * 0.3 : inst;
    spd.t = now; spd.sent = sent;
    if (spd.bps < 1024) { stage.textContent = ''; return; }   // 刚起步/停滞时不报数
    const left = (file.size - sent) / spd.bps;
    stage.textContent = fmtRate(spd.bps) + ' · 还剩 ' + fmtEta(left);
  };
  cell._setPaused = (on) => {
    cell.classList.toggle('paused', !!on);
    if (pauseBtn) {
      // 继续用上传箭头，不用播放三角 —— 左边那颗才是「放本地视频」
      pauseBtn.innerHTML = on ? UP_ACT_ICONS.resume : UP_ACT_ICONS.pause;
      pauseBtn.title = on ? '继续上传' : '暂停上传';
      pauseBtn.setAttribute('aria-label', pauseBtn.title);
      pauseBtn.classList.toggle('is-paused', !!on);
    }
    stage.textContent = on ? '已暂停' : '';
  };
  // 传完了但服务端还在合并分片 / 生成缩略图：进度条已经满了，这段时间改成转圈 + 文字
  cell._setProcessing = (text) => {
    cell.classList.add('processing');
    const pct = ring.querySelector('.up-pct');
    if (pct) pct.textContent = '';
    stage.textContent = text || '处理中…';
  };
  cell._setFailed = (msg, onRetry, onDismiss) => {
    cell.classList.remove('processing');
    cell.classList.remove('paused');
    cell.classList.add('up-failed');
    stage.textContent = '';
    ring.innerHTML = '<span class="up-err" title="' + esc(msg || '上传失败') + '">↻</span>';
    ring.querySelector('.up-err').onclick = (e) => { e.stopPropagation(); onRetry(); };
    if (!cell.querySelector('.up-cancel')) {
      const x = el('button', 'up-cancel', '×');
      x.type = 'button';
      x.title = '不传了';
      x.onclick = (e) => { e.stopPropagation(); cell.remove(); if (onDismiss) onDismiss(); };
      cell.appendChild(x);
    }
  };
  return cell;
}

// 解析后端的错误体：FastAPI 统一是 {"detail": ...}，detail 可能是字符串也可能是对象
function uploadErrText(raw, status) {
  try {
    const j = JSON.parse(raw);
    const d = j && j.detail;
    if (typeof d === 'string') return d;
    if (d && typeof d === 'object') return d.error || JSON.stringify(d);
  } catch (e) {}
  return raw || ('HTTP ' + status);
}

// 单个文件的上传（不分片）。用 XHR 而不是 fetch —— fetch 至今拿不到上传进度。
function uploadOneFile(pointId, file, cell, job) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    if (job) job.xhr = xhr;
    xhr.open('POST', '/api/points/' + pointId + '/media');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) cell._setProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => { cell._setProgress(1); cell._setProcessing('转存中…'); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) {}
        resolve({ ok: true, data: data });
      } else if (xhr.status === 401) {
        showLogin();
        resolve({ ok: false, msg: '未登录或登录已过期' });
      } else {
        resolve({ ok: false, msg: uploadErrText(xhr.responseText, xhr.status) });
      }
    };
    xhr.onerror = () => resolve({ ok: false, msg: '网络中断' });
    xhr.onabort = () => resolve({ ok: false, cancelled: true, msg: '已取消' });
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}

// 传一个分片。返回 {ok} / {ok:false, fatal} —— fatal 的不该重试（越权、类型不对、会话没了）。
function putOneChunk(sessionId, idx, blob, onLoaded, job) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    if (job) job.chunkXhrs.add(xhr);
    xhr.open('PUT', '/api/upload-sessions/' + sessionId + '/chunks/' + idx);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onLoaded(e.loaded); };
    const done = (r) => { if (job) job.chunkXhrs.delete(xhr); resolve(r); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { onLoaded(blob.size); done({ ok: true }); return; }
      if (xhr.status === 401) { showLogin(); done({ ok: false, fatal: true, msg: '未登录或登录已过期' }); return; }
      // 4xx 是「请求本身不对」，重试多少次都一样；5xx / 网络错误才值得退避重来
      done({ ok: false, fatal: xhr.status >= 400 && xhr.status < 500,
             msg: uploadErrText(xhr.responseText, xhr.status) });
    };
    xhr.onerror = () => done({ ok: false, msg: '网络中断' });
    xhr.onabort = () => done({ ok: false, fatal: true, cancelled: true, msg: '已取消' });
    const fd = new FormData();
    fd.append('file', blob, String(idx));
    xhr.send(fd);
  });
}

// 分片上传全流程：建/复用会话 → 并发传缺的片（失败指数退避重试）→ 合并。
// session 已经存在时（续传）只传服务端还没收到的那些片。
// ---------- OSS 直传（浏览器 → OSS，不经过 frp 隧道） ----------
// 为什么要有这条路：媒体走服务器中转时，全程挤在 4Mbps 的隧道里（见 ops/sync-oss.sh
// 顶部的实测），一段 150MB 的视频最快也要 5 分钟，而且期间整站的 API 跟着排队。
// 直传把这段流量挪到用户自己的上行带宽上，隧道只剩下签名和 complete 这两个小请求。
//
// 和隧道分片上传共用同一套外壳：同样的并发闸门、暂停/继续、取消、指数退避重试、
// 断点续传。区别只有两点 ——
//   · 分片 PUT 到 OSS 签出来的临时 URL，不带 cookie，服务端一个字节都不经手；
//   · 「传到哪儿了」以 OSS 的 ListParts 为准，客户端说了不算（见 app.py 的 complete）。
//
// **走不通就回落**：桶没配 CORS、网络策略挡住 OSS 这类情况下，第一片就会失败，
// 这时整条链路回退到原来的隧道上传 —— 用户最多觉得慢，不会传不上去。
const OSS_DIRECT = !!(window.OSS_DIRECT || '');
const OSS_PART_PARALLEL = 4;     // 直传不占服务端资源，比隧道那条(3)可以再放开一点
const OSS_URL_BATCH = 64;        // 和服务端 OSS_URL_BATCH 对齐
let ossDirectBroken = false;     // 本次会话里已经确认直传走不通，别再试第二次

function ossDirectUsable() { return OSS_DIRECT && !ossDirectBroken; }

function ossDirectGiveUp(why) {
  if (ossDirectBroken) return;
  ossDirectBroken = true;
  // 不弹提示：对用户来说「变慢了」比「跳出一个看不懂的错误」好得多。
  // 真要排查时这条日志说明了一切。
  console.warn('[upload] OSS 直传不可用，本次会话回落到服务器中转：' + why);
}

// 会话是直传的还是隧道的。恢复上传时靠它决定后面所有请求该发去哪条路。
function isOssSession(sess) { return !!(sess && sess.mode === 'oss'); }
function uploadSessionUrl(sess) {
  return (isOssSession(sess) ? '/api/oss-uploads/' : '/api/upload-sessions/')
         + sess.session_id;
}

// PUT 一片到 OSS 的临时 URL。
// 刻意**不读 ETag**：服务端 complete 时现查 ListParts，客户端报的 ETag 一概不信，
// 于是也就不需要桶去 ExposeHeader: ETag —— 少一项 CORS 配置就少一处会配错的地方。
function putOnePartToOss(url, blob, onLoaded, job) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    if (job) job.chunkXhrs.add(xhr);
    let progressed = false;
    xhr.open('PUT', url);
    // 必须和服务端签名时用的 Content-Type 一字不差，否则 OSS 判签名不符（403）
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) { progressed = e.loaded > 0; onLoaded(e.loaded); }
    };
    const done = (r) => { if (job) job.chunkXhrs.delete(xhr); resolve(r); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { onLoaded(blob.size); done({ ok: true }); return; }
      // 403 基本都是签名过期（URL 只活 1 小时），换一张新的就好，不算失败
      done({ ok: false, expired: xhr.status === 403, status: xhr.status,
             msg: 'OSS 拒绝了这一片（HTTP ' + xhr.status + '）' });
    };
    // status 0 = 请求根本没发出去或被浏览器拦下（CORS 预检没过最常见）。
    // progressed 用来区分「一个字节都没出去」和「传到一半断网」。
    xhr.onerror = () => done({ ok: false, status: 0, noBytes: !progressed,
                               msg: progressed ? '网络中断' : '连不上 OSS' });
    xhr.onabort = () => done({ ok: false, status: 0, cancelled: true, msg: '已取消' });
    xhr.send(blob);
  });
}

// 把浏览器本地已经解出来的那一帧/那张图传给服务端当缩略图。
// 直传的原件要等后台回源才回到本机，缩略图本来得跟着一起等 —— 相册里会空一大片。
// 这张图只有几十 KB，走隧道毫无压力。失败就安静放弃，回源时服务端自己会生成。
async function uploadClientThumb(mediaId, file, cell) {
  try {
    const dataUrl = await clientThumbDataUrl(file, cell);
    if (!dataUrl) return null;
    const blob = await (await fetch(dataUrl)).blob();
    if (!blob || blob.size < 64) return null;
    const fd = new FormData();
    fd.append('file', blob, 'thumb.jpg');
    const r = await api('/api/media/' + mediaId + '/thumb', { method: 'POST', body: fd });
    return r && r.thumb ? r.thumb : null;
  } catch (e) {
    return null;
  }
}

// 拿一张能当缩略图用的 dataURL。
// 视频：占位格里那张预览就是抽好的第一帧（grabVideoPoster 存的是 dataURL），直接拿来用。
// 照片：现画一张小的 —— 原图可能有十几 MB，不能原样当缩略图传回去。
function clientThumbDataUrl(file, cell) {
  const prev = cell && cell.querySelector && cell.querySelector('.up-prev');
  if (prev && /^data:image\//.test(prev.src || '')) return Promise.resolve(prev.src);
  if (isVideoFile(file)) return Promise.resolve(null);   // 封面没抽出来（多半是 HEVC），等回源
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    const finish = (v) => { URL.revokeObjectURL(url); resolve(v); };
    img.onload = () => {
      try {
        const max = 512;
        const s = Math.min(1, max / Math.max(img.naturalWidth || 1, img.naturalHeight || 1));
        const c = document.createElement('canvas');
        c.width = Math.max(1, Math.round((img.naturalWidth || max) * s));
        c.height = Math.max(1, Math.round((img.naturalHeight || max) * s));
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        finish(c.toDataURL('image/jpeg', 0.8));
      } catch (e) { finish(null); }
    };
    img.onerror = () => finish(null);       // HEIC 之类浏览器解不了的，等回源
    setTimeout(() => finish(null), 4000);
    img.src = url;
  });
}

async function uploadDirectOss(pointId, file, cell, job) {
  job.chunkXhrs = job.chunkXhrs || new Set();
  let sess = isOssSession(job.session) ? job.session : null;
  if (sess && !sess.urls) {
    // 从「未完成」占位格恢复过来的会话只有基本信息，问一次服务端拿已传分片和 URL
    try { sess = await api('/api/oss-uploads/' + sess.session_id); }
    catch (err) { sess = null; }
  }
  if (!sess) {
    try {
      const fp = job.fingerprint || (job.fingerprint = await fileFingerprint(file));
      const fd = new FormData();
      fd.append('fingerprint', fp);
      fd.append('filename', file.name || 'unknown');
      fd.append('filesize', String(file.size));
      sess = await api('/api/points/' + pointId + '/oss-uploads',
                       { method: 'POST', body: fd });
    } catch (err) {
      // 建会话就失败：可能是没配 OSS（503），也可能是真出错了。
      // 无论哪种都回落到隧道，别让用户传不上去。
      ossDirectGiveUp('建会话失败：' + err.message);
      return { fallback: true };
    }
    job.session = sess;
  }
  job.session = sess;
  if (job.cancelled) return { ok: false, cancelled: true, msg: '已取消' };

  const partSize = sess.part_size;
  const total = sess.total_parts;
  const partLen = (n) => (n < total ? partSize : file.size - partSize * (total - 1));
  const done = new Set((sess.received || []).map((p) => p.number));
  const flying = new Map();
  const urls = Object.assign({}, sess.urls || {});

  const persist = () => rememberUpload({
    session_id: sess.session_id, point_id: pointId, fingerprint: job.fingerprint,
    filename: file.name, filesize: file.size, mode: 'oss',
    chunk_size: partSize, total_chunks: total,
    received: Array.from(done).map((n) => n - 1),
  });
  const bump = () => {
    let sent = 0;
    done.forEach((n) => { sent += partLen(n); });
    flying.forEach((v) => { sent += v; });
    cell._setProgress(Math.min(1, sent / Math.max(1, file.size)));
  };
  persist();
  bump();

  // 按需补签 URL。一次多要一批，省往返；并发下重复要几次也无害（签名是纯计算）。
  const signMore = async (must) => {
    const want = [];
    if (must) want.push(must);
    for (let n = 1; n <= total && want.length < OSS_URL_BATCH; n++) {
      if (n !== must && !done.has(n) && !urls[n]) want.push(n);
    }
    const fd = new FormData();
    fd.append('parts', want.join(','));
    const r = await api('/api/oss-uploads/' + sess.session_id + '/sign',
                        { method: 'POST', body: fd });
    Object.assign(urls, (r && r.urls) || {});
  };
  const urlFor = async (n) => {
    // 过期的那张在 res.expired 分支里已经从 urls 里删掉了，这里直接重签
    if (!urls[n]) await signMore(n);
    return urls[n];
  };

  const queue = [];
  for (let n = 1; n <= total; n++) if (!done.has(n)) queue.push(n);
  let failure = null;
  let fallback = false;
  const it = queue[Symbol.iterator]();
  const workers = [];
  for (let w = 0; w < Math.min(OSS_PART_PARALLEL, queue.length || 1); w++) {
    workers.push((async () => {
      for (const n of it) {
        if (failure || fallback || job.cancelled) return;
        const blob = file.slice((n - 1) * partSize, (n - 1) * partSize + partLen(n));
        let ok = false;
        for (let attempt = 0; attempt <= CHUNK_RETRIES; attempt++) {
          if (job.cancelled) return;
          // 暂停挂在 acquire 之前：暂停中的任务不该占着连接名额
          await uploadWaitResume(job);
          if (job.cancelled) return;
          let url;
          try { url = await urlFor(n); }
          catch (err) { failure = failure || ('签名失败：' + err.message); return; }
          if (!url) { failure = failure || ('拿不到分片 ' + n + ' 的上传地址'); return; }
          await uploadGate.acquire();
          let res;
          try {
            res = await putOnePartToOss(url, blob,
                                        (b) => { flying.set(n, b); bump(); }, job);
          } finally {
            uploadGate.release();
          }
          if (res.ok) { ok = true; break; }
          flying.delete(n);
          bump();
          if (res.cancelled && job.paused && !job.cancelled) { attempt--; continue; }
          if (job.cancelled) return;
          // 一个字节都没出去 = 浏览器根本没让这个请求发出（CORS 预检没过最常见）。
          // 再重试多少次都是一样的结果，直接整条链路回落到隧道上传。
          if (res.noBytes && done.size === 0 && attempt >= 1) {
            fallback = true;
            ossDirectGiveUp(res.msg);
            return;
          }
          if (res.expired) { delete urls[n]; continue; }   // 换张签名重来，不算一次失败
          if (attempt < CHUNK_RETRIES) {
            await uploadSleep(Math.min(8000, 500 * Math.pow(2, attempt))
                              + Math.random() * 300);
          }
        }
        if (!ok) {
          if (fallback) return;
          failure = failure || ('分片 ' + n + '/' + total + ' 上传失败');
          return;
        }
        flying.delete(n);
        done.add(n);
        bump();
        persist();
      }
    })());
  }
  await Promise.all(workers);

  if (job.cancelled) return { ok: false, cancelled: true, msg: '已取消' };
  if (fallback) {
    // 回落之前把这个没用上的 OSS 会话撤掉，别让碎片留在云上
    api('/api/oss-uploads/' + sess.session_id, { method: 'DELETE' }).catch(() => {});
    forgetUpload(sess.session_id);
    job.session = null;
    return { fallback: true };
  }
  if (failure) return { ok: false, msg: failure, session: sess };

  cell._setProgress(1);
  // 直传的 complete 很轻：服务端只是让 OSS 把片拼起来，不搬字节。
  // 真正慢的那步（把原件拉回本机）已经挪到后台了，用户不用等。
  cell._setProcessing('收尾中…');
  try {
    const d = await api('/api/oss-uploads/' + sess.session_id + '/complete',
                        { method: 'POST' });
    forgetUpload(sess.session_id);
    // 缩略图顺手补上（几十 KB），否则要等后台回源完才有图
    if (d && d.id && !d.thumb) {
      const t = await uploadClientThumb(d.id, file, cell);
      if (t) d.thumb = t;
    }
    return { ok: true, data: d };
  } catch (err) {
    return { ok: false, msg: '收尾失败：' + err.message, session: sess };
  }
}

async function uploadChunked(pointId, file, cell, job) {
  job.chunkXhrs = job.chunkXhrs || new Set();
  let sess = job.session;
  if (!sess) {
    try {
      const fp = job.fingerprint || (job.fingerprint = await fileFingerprint(file));
      const fd = new FormData();
      fd.append('fingerprint', fp);
      fd.append('filename', file.name || 'unknown');
      fd.append('filesize', String(file.size));
      fd.append('chunk_size', String(CHUNK_SIZE));
      sess = await api('/api/points/' + pointId + '/upload-sessions',
                       { method: 'POST', body: fd });
    } catch (err) {
      return { ok: false, msg: '创建上传会话失败：' + err.message };
    }
    job.session = sess;
  }
  if (job.cancelled) return { ok: false, cancelled: true, msg: '已取消' };

  const chunkSize = sess.chunk_size;
  const total = sess.total_chunks;
  const chunkLen = (i) => (i < total - 1 ? chunkSize : file.size - chunkSize * (total - 1));
  const done = new Set(sess.received || []);
  const flying = new Map();          // idx -> 这一片已经发出去多少字节
  const persist = () => rememberUpload({
    session_id: sess.session_id, point_id: pointId, fingerprint: job.fingerprint,
    filename: file.name, filesize: file.size, chunk_size: chunkSize,
    total_chunks: total, received: Array.from(done),
  });
  const bump = () => {
    let sent = 0;
    done.forEach((i) => { sent += chunkLen(i); });
    flying.forEach((v) => { sent += v; });
    // 聚合进度：已确认的片按整片算，在途的按 XHR 报的字节算 —— 所以进度是
    // 平滑上涨的，不会「卡住不动然后一跳到 100%」。
    cell._setProgress(Math.min(1, sent / Math.max(1, file.size)));
  };
  persist();
  bump();

  const queue = [];
  for (let i = 0; i < total; i++) if (!done.has(i)) queue.push(i);
  let failure = null;
  const it = queue[Symbol.iterator]();
  const workers = [];
  for (let w = 0; w < Math.min(CHUNK_PARALLEL, queue.length || 1); w++) {
    workers.push((async () => {
      for (const idx of it) {
        if (failure || job.cancelled) return;
        const blob = file.slice(idx * chunkSize, idx * chunkSize + chunkLen(idx));
        let ok = false;
        for (let attempt = 0; attempt <= CHUNK_RETRIES; attempt++) {
          if (job.cancelled) return;
          // 暂停就挂在这儿等着 —— 挂在 acquire **之前**，暂停中的任务不占连接名额，
          // 剩下那个还在传的视频能立刻用上腾出来的带宽
          await uploadWaitResume(job);
          if (job.cancelled) return;
          await uploadGate.acquire();
          let res;
          try {
            res = await putOneChunk(sess.session_id, idx, blob,
                                    (n) => { flying.set(idx, n); bump(); }, job);
          } finally {
            uploadGate.release();
          }
          if (res.ok) { ok = true; break; }
          flying.delete(idx);
          bump();
          // 暂停掐掉的那一片不算失败：恢复之后原样重传，重试次数也不该被它吃掉
          if (res.cancelled && job.paused && !job.cancelled) { attempt--; continue; }
          if (res.fatal || job.cancelled) { failure = failure || res.msg; return; }
          if (attempt < CHUNK_RETRIES) {
            // 指数退避 + 抖动：弱网抖一下时别所有分片同时重试，那只会再挤一次
            await uploadSleep(Math.min(8000, 500 * Math.pow(2, attempt))
                              + Math.random() * 300);
          }
        }
        if (!ok) { failure = failure || ('分片 ' + (idx + 1) + '/' + total + ' 上传失败'); return; }
        flying.delete(idx);
        done.add(idx);
        bump();
        persist();
      }
    })());
  }
  await Promise.all(workers);

  if (job.cancelled) return { ok: false, cancelled: true, msg: '已取消' };
  if (failure) return { ok: false, msg: failure, session: sess };

  cell._setProgress(1);
  // complete 这一步在服务端要把几十片拼成一个文件、再生成缩略图（视频还要抽封面），
  // 大文件上十几秒很正常。前台如果只显示 100% 不动，看起来就是卡死了。
  cell._setProcessing('转存中…');
  const stageTimer = setTimeout(
    () => cell._setProcessing(isVideoFile(file) ? '生成封面…' : '生成缩略图…'), 6000);
  try {
    const d = await api('/api/upload-sessions/' + sess.session_id + '/complete',
                        { method: 'POST' });
    forgetUpload(sess.session_id);
    return { ok: true, data: d };
  } catch (err) {
    return { ok: false, msg: '合并失败：' + err.message, session: sess };
  } finally {
    clearTimeout(stageTimer);
  }
}

// 正在进行中的上传。**这个登记簿是「切 tab 丢上传」那个 bug 的解药**：
// loadDetail 会把 #media-grid 整个重建，占位格连同 DOM 一起没了；
// 有了它，重建之后能把还在传的格子挂回去，传完也能把结果插进新网格。
const activeUploads = [];
// 刚传完、但服务端列表快照可能还没反映出来的媒体（loadDetail 的 GET 早于上传完成
// 却晚于它返回时会发生）。renderMedia 会把这些并回去，免得刚传好的又消失。
const recentUploads = [];

function noteRecentUpload(pointId, m) {
  recentUploads.push({ pointId: Number(pointId), m: m, at: Date.now() });
}
function forgetRecentUpload(mediaId) {
  for (let i = recentUploads.length - 1; i >= 0; i--) {
    if (Number(recentUploads[i].m.id) === Number(mediaId)) recentUploads.splice(i, 1);
  }
}
// 服务端列表 + 刚传完还没被它包含进来的那些。一旦某条出现在服务端列表里，
// 就说明服务端已经知道它了，从这个补丁表里摘掉（否则删掉它之后还会复活）。
function mergeRecentUploads(pointId, list) {
  const have = new Set((list || []).map((x) => String(x.id)));
  const out = (list || []).slice();
  const cutoff = Date.now() - 120000;
  for (let i = recentUploads.length - 1; i >= 0; i--) {
    const r = recentUploads[i];
    if (have.has(String(r.m.id)) || r.at < cutoff) { recentUploads.splice(i, 1); continue; }
    if (r.pointId === Number(pointId)) out.push(r.m);
  }
  return out;
}

// 网格被重建之后，把还在传的占位格重新挂回去（挂在真实格子之后、「＋」之前）
function reattachActiveUploads(grid) {
  for (const up of activeUploads) {
    if (Number(up.pointId) !== Number(currentPointId)) continue;
    grid.appendChild(up.cell);      // appendChild 会把节点从旧父节点搬过来
  }
}

// 把一个（可能已经脱离文档的）格子放回当前网格的「＋」之前
function placeUploadCell(cell) {
  const grid = document.getElementById('media-grid');
  if (!grid) return false;
  if (cell.parentElement === grid) return true;
  const addCell = grid.querySelector('.media-add');
  if (addCell) grid.insertBefore(cell, addCell);
  else grid.appendChild(cell);
  return true;
}

// 上传成功的收尾：把占位格换成真正的格子。
// 关键改动：**不再因为格子已经脱离文档就把结果丢掉**。原来这里是
// `if (!cell.isConnected) return;`，于是上传途中只要发生过一次 loadDetail
// （点 tab、横滑切 tab、删一张图都会），服务端明明存好了，前端却再也不显示。
// 上传完只是「文件到了」。视频还要在后台转出 1080p/720p/480p 三档（transcode.py），
// 期间只有原画可播 —— 在弱网上正是最卡的那一档。格子上挂个「转码中」的小标，
// 转完自己消失，人就知道再等一会儿播放会变顺。
// 直传的媒体还多一段「回源存档」：原件此刻只在 OSS 上，本机要先把它拉回来，
// 缩略图、EXIF、清晰度档位都得等那之后。所以这个轮询同时管两件事 ——
// 把状态小标从「存档中」推进到「转码中」再到消失，以及**封面一好就换上去**
// （直传完成时服务端还没有缩略图，格子里是空的）。
function watchTranscode(cell, mediaId, opts) {
  if (!cell || !mediaId) return;
  const o = opts || {};
  const tag = el('span', 'media-transcoding', o.backfilling ? '存档中' : '转码中');
  cell.appendChild(tag);
  const img = cell.querySelector('img');
  const needThumb = !!o.needThumb;
  let tries = 0;
  const tick = () => {
    if (!cell.isConnected || tries++ > 120) { tag.remove(); return; }
    fetch('/api/media/' + encodeURIComponent(mediaId) + '/variants')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) { tag.remove(); return; }
        // 封面出来了就立刻换上，不用等用户重新打开这个点
        if (img && d.thumb_url && (!img.getAttribute('src') || needThumb)) {
          img.src = d.thumb_url;
        }
        if (d.backfilling) { tag.textContent = '存档中'; setTimeout(tick, 5000); return; }
        if (d.transcoding) { tag.textContent = '转码中'; setTimeout(tick, 5000); return; }
        // 回源刚做完、转码还没来得及登记档位时，d.transcoding 会短暂地是 false。
        // 再看一眼，别把小标撤早了。
        if (o.type === 'video' && tries < 4) { setTimeout(tick, 3000); return; }
        tag.remove();
      })
      .catch(() => { tag.remove(); });
  };
  setTimeout(tick, 2500);
}

function finishUploadCell(pointId, cell, m) {
  noteRecentUpload(pointId, m);
  scheduleMediaCountRefresh();   // 地图角标 / 列表计数 / 统计条跟着多一个
  if (Number(pointId) !== Number(currentPointId)) {
    cell.remove();      // 用户已经切到别的点了：服务端有，下次打开自然看得到
    return;
  }
  const grid = document.getElementById('media-grid');
  if (!grid) { cell.remove(); return; }
  if (detailMedia.some((x) => Number(x.id) === Number(m.id))) {
    cell.remove();      // 期间的 loadDetail 已经把它拉回来了，别重复插一个
    return;
  }
  const real = buildMediaItem(m);
  if (cell.parentElement === grid) cell.replaceWith(real);
  else {
    cell.remove();
    const addCell = grid.querySelector('.media-add');
    if (addCell) grid.insertBefore(real, addCell);
    else grid.appendChild(real);
  }
  // 视频要盯转码；直传上来的（pending_backfill）不管照片视频都要盯回源 ——
  // 原件还没回本机，缩略图可能也还没有。
  if (m.type === 'video' || m.pending_backfill || !m.thumb_filename) {
    watchTranscode(real, m.id, { type: m.type, backfilling: !!m.pending_backfill,
                                 needThumb: !m.thumb_filename });
  }
  detailMedia.push(m);
  lightboxMedia = detailMedia;
  syncDetailMediaOrder();
}

// 一个文件走完全程：传 → 成功换成真正的格子；失败就留在原地，可以点 ↻ 重来。
// 大文件走分片（断了能续），小文件仍走单请求接口。
async function runUpload(pointId, file, cell, job) {
  job = job || {};
  job.chunkXhrs = job.chunkXhrs || new Set();
  cell._job = job;
  // 排队期间用户就点了取消：轮到它的时候直接不发请求
  if (job.cancelled || cell._cancelledBeforeStart) { cell.remove(); return; }
  const entry = { pointId: Number(pointId), file: file, cell: cell, job: job };
  activeUploads.push(entry);
  cell._job = job;
  const drop = () => {
    const i = activeUploads.indexOf(entry);
    if (i >= 0) activeUploads.splice(i, 1);
  };
  let res;
  try {
    // 先试直传。它自己判断走不通时返回 { fallback: true }，这里接着往下走老路 ——
    // 用户看到的最多是「慢了点」，绝不会因为 OSS 配置没到位就传不上去。
    // job.session 已经是一条隧道会话时（上一次走的老路，现在点了「↻ 重来」），
    // 就接着老路续传，别丢掉已经传上去的那些分片改走直传。
    if (ossDirectUsable() && (!job.session || isOssSession(job.session))) {
      res = await uploadDirectOss(pointId, file, cell, job);
      if (res && res.fallback) res = null;
    }
    if (res) {
      /* 直传已经出结果（成功或真失败），下面两条老路都不用走 */
    } else if (file.size >= CHUNK_THRESHOLD) {
      res = await uploadChunked(pointId, file, cell, job);
    } else {
      // 不分片的也要占一个连接名额，否则「3 个整文件 + 4 个分片」还是 7 条连接
      await uploadGate.acquire();
      try { res = await uploadOneFile(pointId, file, cell, job); }
      finally { uploadGate.release(); }
    }
  } catch (err) {
    res = { ok: false, msg: String((err && err.message) || err) };
  }

  if (!res.ok) {
    if (res.cancelled || Number(pointId) !== Number(currentPointId)) {
      drop();
      cell.remove();
      return;
    }
    // 失败的格子**留在登记簿里**：它现在是个「点 ↻ 重来」的入口，
    // 网格被 loadDetail 重建时得跟着挂回去，不然重试入口就没了。
    placeUploadCell(cell);
    cell._setFailed(res.msg, () => {
      // 重来：换一个干净的占位格顶上去，避免复用上一次的失败状态。
      // 分片的会话带着走 —— 重试只补没传上去的那几片，不从头再来。
      drop();
      const againJob = { session: res.session, fingerprint: job.fingerprint,
                         chunkXhrs: new Set() };
      const again = buildUploadCell(file, againJob);
      cell.replaceWith(again);
      runUpload(pointId, file, again, againJob);
    }, drop);
    return;
  }
  drop();
  const d = res.data || {};
  // 上传接口返回的字段名是 thumb，而网格用的是 thumb_filename，这里对齐
  const m = {
    id: d.id, type: d.type, filename: d.filename,
    thumb_filename: d.thumb, caption: '',
    exif_time: d.exif_time, point_id: pointId,
    // 直传：原件还在 OSS 上，本机要慢慢拉回来。格子据此显示「存档中」并等封面。
    pending_backfill: !!d.pending_backfill,
    url: d.url || undefined,
  };
  finishUploadCell(pointId, cell, m);
}

// 网格里的真实格子顺序才是准的（并发上传谁先完成不一定），据此把 detailMedia 排回来
function syncDetailMediaOrder() {
  const grid = document.getElementById('media-grid');
  if (!grid) return;
  const byId = new Map(detailMedia.map(m => [String(m.id), m]));
  const next = Array.from(grid.querySelectorAll(
    '.media-item:not(.media-add):not(.media-uploading):not(.media-resume)'))
    .map(e => byId.get(e.dataset.mid)).filter(Boolean);
  if (next.length === detailMedia.length) {
    detailMedia = next;
    lightboxMedia = detailMedia;
  }
}

// 选好文件：先把占位格全都插进去（「＋」自动排到后面），再按并发上限逐个开传
function startUploads(files) {
  const grid = document.getElementById('media-grid');
  const pointId = currentPointId;
  if (!grid || !pointId || !files || !files.length) return;
  const addCell = grid.querySelector('.media-add');
  const jobs = [];
  for (const f of files) {
    // job 先建出来交给格子：排队等待期间点取消也要立刻生效
    const job = { chunkXhrs: new Set() };
    const cell = buildUploadCell(f, job);
    if (addCell) grid.insertBefore(cell, addCell);   // 插在「＋」之前 → 「＋」被挤到新位置
    else grid.appendChild(cell);
    jobs.push(() => runUpload(pointId, f, cell, job));
  }
  // 共享一个迭代器当队列：每个 worker 处理完手上的就去取下一个
  const it = jobs[Symbol.iterator]();
  for (let i = 0; i < Math.min(UPLOAD_CONCURRENCY, jobs.length); i++) {
    (async () => { for (const job of it) { await job(); } })();
  }
}

// ---------- 未完成的上传：占位格 + 重新选文件续传 ----------
// 浏览器的安全模型不允许网页保存/恢复本地文件句柄：刷新之后 File 对象就没了，
// 只有 session_id 和「传到第几片」还在。所以**做不到自动续传**，
// 只能引导用户重新选中同一个文件 —— 这一点必须在界面上说清楚，
// 否则用户会以为关掉页面它自己会传完。
let resumeFileInput = null;

function pickResumeFile(onPick) {
  if (!resumeFileInput) {
    resumeFileInput = document.createElement('input');
    resumeFileInput.type = 'file';
    resumeFileInput.accept = 'image/*,video/*';
    resumeFileInput.style.display = 'none';
    document.body.appendChild(resumeFileInput);
  }
  resumeFileInput.onchange = (e) => {
    const f = (e.target.files || [])[0];
    e.target.value = '';
    if (f) onPick(f);
  };
  resumeFileInput.click();
}

function buildResumeCell(pointId, sess) {
  const cell = el('div', 'media-item media-resume');
  const pct = Math.min(99, Math.floor(100 * (sess.received_bytes || 0) /
                                      Math.max(1, sess.filesize)));
  cell.title = sess.filename + '：已传 ' + pct + '%，重新选中同一个文件即可继续';
  cell.appendChild(el('div', 'resume-title', '未完成的上传'));
  cell.appendChild(el('div', 'resume-name', sess.filename));
  cell.appendChild(el('div', 'resume-pct', '已传 ' + pct + '%'));
  cell.appendChild(el('div', 'resume-hint', '需重新选中同一文件'));

  const go = el('button', 'resume-go', '继续');
  go.type = 'button';
  go.onclick = (e) => {
    e.stopPropagation();
    tmToast('请重新选中「' + sess.filename + '」，浏览器不允许网页自己读取本地文件');
    pickResumeFile(async (f) => {
      const fp = await fileFingerprint(f);
      if (fp !== sess.fingerprint || f.size !== sess.filesize) {
        tmAlert('这不是同一个文件（或内容已改动），没法接着上次的进度传。\n' +
                '请选中原来那个「' + sess.filename + '」，或者点「放弃」重新传一次。');
        return;
      }
      if (Number(currentPointId) !== Number(pointId)) return;
      const againJob = { session: sess, fingerprint: fp, chunkXhrs: new Set() };
      const again = buildUploadCell(f, againJob);
      cell.replaceWith(again);
      runUpload(pointId, f, again, againJob);
    });
  };
  cell.appendChild(go);

  const drop = el('button', 'resume-drop', '放弃');
  drop.type = 'button';
  drop.onclick = async (e) => {
    e.stopPropagation();
    if (!await tmConfirm('放弃这个没传完的上传？已经传上去的分片会被删除。',
                         '放弃上传', { danger: true, okText: '放弃' })) return;
    try { await api(uploadSessionUrl(sess), { method: 'DELETE' }); }
    catch (err) { /* 会话可能已经被清理过了，照样把格子撤掉 */ }
    forgetUpload(sess.session_id);
    cell.remove();
  };
  cell.appendChild(drop);
  return cell;
}

// 拉这个点上「传了一半」的会话，渲染成占位格。renderMedia 之后异步调用。
async function refreshResumableCells(pointId) {
  if (!pointId) return;
  let sessions = [];
  try { sessions = await api('/api/points/' + pointId + '/upload-sessions'); }
  catch (err) { return; }
  if (Number(pointId) !== Number(currentPointId)) return;
  const grid = document.getElementById('media-grid');
  if (!grid) return;
  grid.querySelectorAll('.media-resume').forEach((e) => e.remove());
  const addCell = grid.querySelector('.media-add');
  for (const s of sessions) {
    // 本页正在传的那个就是它 —— 已经有进度环了，别再显示一个「未完成」
    if (activeUploads.some((u) => u.job && u.job.session &&
                                  u.job.session.session_id === s.session_id)) continue;
    const cell = buildResumeCell(pointId, s);
    if (addCell) grid.insertBefore(cell, addCell);
    else grid.appendChild(cell);
  }
}

// ---------- 拖拽排序（长按触发，仿微信朋友圈九宫格） ----------
// 用 touch 事件而不是 pointer 事件：这里必须能 preventDefault 掉 touchmove，
// 否则下面板的横滑（切 tab）和纵向滚动都会跟着一起动，拖拽根本进行不下去。
// 关键点：
//   · 长按 320ms 才起拖，起拖前手指移动超过 SLOP 就当成滚动/横滑，放弃
//   · 起拖瞬间置 mediaDragging，并主动把已经跟手的 tab 横滑取消掉、弹回原位
//   · 每次换位后重新按「当前布局位置 → 手指位置」算 transform，
//     这样图片在换位之后仍然稳稳粘在手指下，不会跳一下
(function initMediaReorder() {
  const grid = document.getElementById('media-grid');
  if (!grid) return;
  const HOLD_MS = 320;
  const SLOP = 10;
  let holdTimer = null, dragEl = null, moved = false;
  let startX = 0, startY = 0, grabDX = 0, grabDY = 0;
  // 鼠标按下待起拖的那一格。桌面端和手机端的起拖手势本来就不一样：
  //   手机——必须长按，否则和「上下滚动 / 左右切 tab」分不开；
  //   鼠标——按下就拖，没人会先按住不动等三分之一秒。
  // 原来两端共用长按逻辑，而 onMove 里手一动就把长按计时器取消了，
  // 结果 PC 上「按下→立刻拖」永远起不了拖，看起来就是这功能没做。
  let pendingCell = null;

  // ---- 拖到边缘自动滚屏 ----
  // 网格比一屏高的时候，原来只能在当前可见范围里换位置：想把最后一张拖到最前面，
  // 拖到屏幕顶边就走不动了，因为拖拽期间 touchmove 被 preventDefault 掉了，
  // 浏览器自己的滚动根本不会发生。
  // 做法：指针进入容器上/下边缘的触发带后，按 rAF 逐帧滚容器，
  // 速度随「探入边缘的深度」平方增长，并且进入后还要 RAMP_MS 才涨到满速 ——
  // 这样贴边的一瞬间是缓缓起步，而不是猛地窜出去。
  const EDGE = 80;            // 边缘触发带宽度(px)
  const MAX_SPEED = 620;      // 满速 620px/s，约合每秒 7 行，手还跟得上
  const RAMP_MS = 220;        // 从 0 加速到满速所需时间
  let scroller = null;        // 当前这次拖拽作用的滚动容器
  let autoRaf = 0, autoPrevT = 0, autoInT = 0;
  let lastX = 0, lastY = 0;   // 最后一次指针位置（自动滚屏期间指针可能一直不动）
  let lastOver = null;        // 上一次已经换过位的目标格，避免同一格反复换、抖动

  // 找最近的可滚动祖先。手机端和宽屏的面板结构不同（底部抽屉 / 右侧栏），
  // 但滚动的都是 .view，与其写死选择器，不如按 overflow 实际算。
  function findScroller(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ov = getComputedStyle(p).overflowY;
      if ((ov === 'auto' || ov === 'scroll') && p.scrollHeight > p.clientHeight + 1) return p;
    }
    return null;
  }

  // 当前该以多快的速度滚（px/s，正数向下）；不在边缘带里返回 0
  function edgeSpeed(y, rect) {
    const top = y - rect.top, bot = rect.bottom - y;
    if (top < EDGE) { const t = Math.min(1, (EDGE - top) / EDGE); return -MAX_SPEED * t * t; }
    if (bot < EDGE) { const t = Math.min(1, (EDGE - bot) / EDGE); return MAX_SPEED * t * t; }
    return 0;
  }

  function autoScrollTick(ts) {
    autoRaf = 0;
    if (!dragEl || !scroller) { autoPrevT = autoInT = 0; return; }
    const dt = autoPrevT ? Math.min(0.05, (ts - autoPrevT) / 1000) : 0.016;
    autoPrevT = ts;
    const rect = scroller.getBoundingClientRect();
    let v = edgeSpeed(lastY, rect);
    if (v) {
      if (!autoInT) autoInT = ts;                        // 刚进边缘带，开始计时
      v *= Math.min(1, (ts - autoInT) / RAMP_MS);        // 缓起步
      const max = scroller.scrollHeight - scroller.clientHeight;
      const before = scroller.scrollTop;
      scroller.scrollTop = Math.max(0, Math.min(max, before + v * dt));
      if (scroller.scrollTop !== before) {
        moveTo(lastX, lastY);        // 内容滚走了，被拖的格子要重新贴回指针
        reorderAt(lastX, lastY);     // 新滚进视野的格子也要参与换位
      }
    } else {
      autoInT = 0;                   // 离开边缘带，下次重新缓起步
    }
    autoRaf = requestAnimationFrame(autoScrollTick);
  }

  function startAutoScroll() {
    if (autoRaf || !scroller) return;
    autoPrevT = 0; autoInT = 0;
    autoRaf = requestAnimationFrame(autoScrollTick);
  }
  function stopAutoScroll() {
    if (autoRaf) cancelAnimationFrame(autoRaf);
    autoRaf = 0; autoPrevT = 0; autoInT = 0;
  }

  const pt = (e) => {
    if (e.touches && e.touches.length) return e.touches[0];
    if (e.changedTouches && e.changedTouches.length) return e.changedTouches[0];
    return e;
  };

  // 把被拖的格子挪到手指下。先清 transform 量出它当前的布局位置，再按差值设回去——
  // 换位后布局变了，只有这样重算才能继续贴着手指，不会突然跳开。
  function moveTo(x, y) {
    if (!dragEl) return;
    dragEl.style.transform = '';
    const r = dragEl.getBoundingClientRect();
    dragEl.style.transform =
      'translate(' + (x - grabDX - r.left) + 'px, ' + (y - grabDY - r.top) + 'px) scale(1.06)';
  }

  function beginDrag(cell, x, y) {
    dragEl = cell;
    const r = cell.getBoundingClientRect();
    grabDX = x - r.left;
    grabDY = y - r.top;
    lastX = x; lastY = y; lastOver = null;
    scroller = findScroller(grid);
    startAutoScroll();
    mediaDragging = true;
    cancelTabSwipe();              // 手指可能已经带着 tab 轨道走了一点，先弹回去
    cell.classList.add('dragging');
    grid.classList.add('reordering');
    if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) {} }
    moveTo(x, y);
  }

  function overCell(x, y) {
    // dragEl 上有 pointer-events:none，所以这里拿到的是它下面那一格
    const el0 = document.elementFromPoint(x, y);
    const cell = el0 && el0.closest ? el0.closest('.media-item') : null;
    if (!cell || cell === dragEl || cell.classList.contains('media-add')) return null;
    if (!grid.contains(cell)) return null;
    return cell;
  }

  // 换位判定。拆出来单独一个函数，是因为自动滚屏时指针不动、内容在动，
  // 也需要重新判一次；那时不该再调 moveTo（tick 里已经调过了）。
  function reorderAt(x, y) {
    const cell = overCell(x, y);
    if (!cell) { lastOver = null; return; }
    // 同一格不重复换位：自动滚屏时每帧都会走到这里，不设这道闸会来回抖
    if (cell === lastOver) return;
    lastOver = cell;
    const items = Array.from(grid.querySelectorAll('.media-item:not(.media-add)'));
    const from = items.indexOf(dragEl), to = items.indexOf(cell);
    if (from < 0 || to < 0) return;
    // 其余格子用 FLIP 平滑让位；被拖的那个不参与，否则它的跟手 transform 会被覆盖掉
    flipGrid(grid, () => {
      if (from < to) cell.after(dragEl); else cell.before(dragEl);
    }, dragEl);
    moveTo(x, y);                  // 换位后重新贴回手指
  }

  function dragMove(x, y) {
    lastX = x; lastY = y;
    moveTo(x, y);
    reorderAt(x, y);
  }

  function endDrag() {
    clearTimeout(holdTimer); holdTimer = null;
    pendingCell = null;
    stopAutoScroll();
    scroller = null; lastOver = null;
    if (!dragEl) { mediaDragging = false; return; }
    const d = dragEl;
    d.classList.remove('dragging');
    d.style.transform = '';
    d.classList.add('just-dragged');            // 吃掉紧随其后的那次 click
    setTimeout(() => d.classList.remove('just-dragged'), 260);
    grid.classList.remove('reordering');
    dragEl = null;
    mediaDragging = false;
    if (moved) persistOrder();
    moved = false;
  }

  async function persistOrder() {
    const ids = Array.from(grid.querySelectorAll('.media-item:not(.media-add)'))
      .map(e => e.dataset.mid).filter(Boolean);
    if (!ids.length || !currentPointId) return;
    const byId = new Map(detailMedia.map(m => [String(m.id), m]));
    const next = ids.map(id => byId.get(id)).filter(Boolean);
    if (next.length === detailMedia.length) {
      detailMedia = next;
      lightboxMedia = detailMedia;              // 灯箱翻页顺序跟着一起变
    }
    try {
      const fd = new FormData();
      fd.append('ids', ids.join(','));
      await api(`/api/points/${currentPointId}/media/order`, { method: 'PUT', body: fd });
    } catch (e) {
      tmToast('顺序保存失败');
    }
  }

  function onStart(e) {
    const isMouse = (e.type === 'mousedown');
    if (isMouse && e.button !== 0) return;
    if (e.touches && e.touches.length !== 1) { clearTimeout(holdTimer); holdTimer = null; return; }
    const t = pt(e);
    const cell = e.target.closest && e.target.closest('.media-item');
    if (!cell || cell.classList.contains('media-add')) return;
    if (e.target.closest('.del, .media-cap')) return;      // 按钮上起手不拖
    startX = t.clientX; startY = t.clientY; moved = false;
    clearTimeout(holdTimer); holdTimer = null;
    pendingCell = null;
    if (isMouse) {
      pendingCell = cell;          // 等第一次移动超过 SLOP 再起拖
      e.preventDefault();          // 免得变成拖选文字/原生图片拖拽
    } else {
      holdTimer = setTimeout(() => {
        holdTimer = null;
        beginDrag(cell, startX, startY);
      }, HOLD_MS);
    }
  }

  function onMove(e) {
    const t = pt(e);
    lastX = t.clientX; lastY = t.clientY;
    if (pendingCell) {
      // 鼠标：移动超过阈值就起拖（不足阈值当成普通点击，留给看大图）
      if (Math.hypot(t.clientX - startX, t.clientY - startY) <= SLOP) return;
      const cell = pendingCell; pendingCell = null;
      beginDrag(cell, startX, startY);
    }
    if (holdTimer) {
      // 还没起拖：手指一动就说明是在滚动/横滑，放弃长按
      if (Math.hypot(t.clientX - startX, t.clientY - startY) > SLOP) {
        clearTimeout(holdTimer); holdTimer = null;
      }
      return;
    }
    if (!dragEl) return;
    if (e.touches && e.touches.length !== 1) { endDrag(); return; }   // 变成双指，收工
    // 关键：吃掉这次 touchmove，纵向滚动和 tab 横滑都别再跟着动
    if (e.cancelable) e.preventDefault();
    moved = true;
    dragMove(t.clientX, t.clientY);
  }

  grid.addEventListener('touchstart', onStart, { passive: true });
  grid.addEventListener('touchmove', onMove, { passive: false });
  grid.addEventListener('touchend', endDrag, { passive: true });
  grid.addEventListener('touchcancel', endDrag, { passive: true });
  // 桌面端鼠标：move/up 绑在 document 上，拖出网格外也跟得住
  grid.addEventListener('mousedown', onStart);
  document.addEventListener('mousemove', (e) => { if (pendingCell || holdTimer || dragEl) onMove(e); });
  document.addEventListener('mouseup', () => { if (pendingCell || holdTimer || dragEl) endDrag(); });
  // 拖拽期间禁掉系统的长按菜单（否则手机上会弹「保存图片」）
  grid.addEventListener('contextmenu', (e) => { if (dragEl || holdTimer) e.preventDefault(); });
})();

// ---------- 点详情的媒体网格：同样支持捏合缩放重排 ----------
// 和相册用同一套交互，但档位单独一套、也单独记：这里的格子上还压着删除按钮和日期，
// 排到 8 列会小到点不准，所以最密只到 5 列。
const MEDIA_COLS = [5, 4, 3, 2];
const MEDIA_ZOOM_KEY = 'tm_media_zoom';
let mediaZoom = (function () {
  try {
    // 注意先判 null：没存过时 getItem 返回 null，Number(null) 是 0，
    // 0 又正好是合法档位，会把默认值悄悄变成「最密的 5 列」
    const raw = localStorage.getItem(MEDIA_ZOOM_KEY);
    if (raw !== null && raw !== '') {
      const v = Number(raw);
      if (Number.isInteger(v) && v >= 0 && v < MEDIA_COLS.length) return v;
    }
  } catch (e) {}
  return 2;      // 默认 3 列，和原来一样
})();
function setMediaZoom(delta) {
  const next = Math.min(MEDIA_COLS.length - 1, Math.max(0, mediaZoom + delta));
  if (next === mediaZoom) return;
  mediaZoom = next;
  try { localStorage.setItem(MEDIA_ZOOM_KEY, String(next)); } catch (e) {}
  const grid = document.getElementById('media-grid');
  flipGrid(grid, applyMediaCols);
}
(function initMediaGridZoom() {
  // 同相册：手势绑在**整个 tab 页**上。网格只到最后一行缩略图为止，
  // 下面到面板底部那片空白属于 .view，绑在网格上就成了「空白处捏不动」。
  const view = document.getElementById('detail-view');
  if (!view) return;
  let start = 0;
  const d2 = (e) => Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                               e.touches[0].clientY - e.touches[1].clientY);
  view.addEventListener('touchstart', (e) => {
    if (mediaDragging) return;                 // 正在拖排序，别顺手把网格缩放了
    if (e.touches.length !== 2) return;
    // 抢在浏览器把双指判成整页缩放之前接管；晚一步 touchmove 就不可取消了
    if (e.cancelable) e.preventDefault();
    start = d2(e);
  }, { passive: false });
  view.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 2 || !start) return;
    if (e.cancelable) e.preventDefault();
    const d = d2(e), r = d / start;
    if (r > 1.18) { setMediaZoom(1); start = d; }
    else if (r < 0.85) { setMediaZoom(-1); start = d; }
  }, { passive: false });
  view.addEventListener('touchend', () => { start = 0; }, { passive: true });
  view.addEventListener('touchcancel', () => { start = 0; }, { passive: true });
  // 桌面：Ctrl/⌘ + 滚轮，和相册一致
  view.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setMediaZoom(e.deltaY < 0 ? 1 : -1);
  }, { passive: false });
})();

// 照片地图打点：点击 📷 角标直接打开该点的媒体灯箱
async function openPointPhotos(pointId) {
  const media = await api(`/api/points/${pointId}/media`);
  if (!media || !media.length) return;
  // 这条路径拿到的是单点媒体，没带地点名；补上，灯箱底部那行「地点 · 时间」才有内容
  const pt = (lastPoints || []).find(p => Number(p.id) === Number(pointId));
  lightboxMedia = pt ? media.map(m => Object.assign({ point_name: pt.name }, m)) : media;
  lightboxIndex = 0;
  renderLightbox();
  $('#lightbox').classList.remove('hidden');
}

function hideDetail() {
  unhighlightMapPoints();
  unhighlightSegments();
  $('#detail-empty').classList.remove('hidden');
  $('#detail-body').classList.add('hidden');
  $('#media-grid').innerHTML = '';
}

// ---------- 轨迹回放动画 ----------
// 沿轨迹匀速移动一个小圆点，模拟旅程回放；速度 1x/2x/4x
// 路径优先取真实路线（segmentData 逐段拼接），未就绪时用直线兜底
function buildReplayPath() {
  if (segmentData.length) {
    const path = [];
    const wayIdx = [0];
    segmentData.forEach(seg => {
      if (seg.path && seg.path.length) {
        if (!path.length) path.push(...seg.path);
        else path.push(...seg.path.slice(1));
        wayIdx.push(path.length - 1);
      }
    });
    if (path.length >= 2) return { path, wayIdx };
  }
  if (lastPoints && lastPoints.length >= 2) {
    return {
      path: lastPoints.map(p => [p.lng, p.lat]),
      wayIdx: lastPoints.map((_, i) => i),
    };
  }
  return { path: [], wayIdx: [] };
}

function startReplay() {
  const { path, wayIdx } = buildReplayPath();
  if (path.length < 2) return;
  stopReplay(false);
  replayPath = path;
  replayWayIdx = wayIdx;
  replayCum = [0];
  let total = 0;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i];
    total += distKm({ lng: a[0], lat: a[1] }, { lng: b[0], lat: b[1] }) * 1000;
    replayCum.push(total);
  }
  replayTotal = total;
  if (!replayTotal) return;
  replayT = 0;
  replayActive = true;
  replayLast = 0;
  $('#replay-launch').classList.add('hidden');
  $('#replay-bar').classList.remove('hidden');
  setReplayIcon('pause');
  if (!replayMarker) {
    replayMarker = new AMap.Marker({
      position: path[0],
      content: '<div class="replay-dot"></div>',
      offset: new AMap.Pixel(-9, -9),
      zIndex: 500,
    });
  }
  replayMarker.setPosition(path[0]);
  replayMarker.setMap(map);
  ensureReplayLines();
  updateReplayLines();
  playLoop();
}

function playLoop() {
  if (!replayActive) return;
  replayRaf = requestAnimationFrame((now) => {
    if (!replayLast) replayLast = now;
    const dt = now - replayLast;
    replayLast = now;
    replayT += replaySpeed * replayTotal * dt / REPLAY_BASE_MS;
    if (replayT >= replayTotal) {
      replayT = replayTotal;
      finishReplay();
      return;
    }
    // 定位当前所在段
    let i = 1;
    while (i < replayCum.length - 1 && replayCum[i] < replayT) i++;
    const segLen = (replayCum[i] - replayCum[i - 1]) || 1;
    const t = Math.min(Math.max((replayT - replayCum[i - 1]) / segLen, 0), 1);
    const a = replayPath[i - 1], b = replayPath[i];
    replayMarker.setPosition([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    updateReplayLines();
    $('#replay-fill').style.width = (replayT / replayTotal * 100).toFixed(2) + '%';
    updateReplayName(replayT);
    playLoop();
  });
}

// 进度条下方显示当前已到达的行程点（「4/17」叠加在进度条上，地名独占更多空间）
function updateReplayName(t) {
  let wi = 0;
  for (let i = 1; i < replayWayIdx.length; i++) {
    if (replayCum[replayWayIdx[i]] <= t + 1) wi = i;
    else break;
  }
  const p = lastPoints[wi];
  if (p) {
    $('#replay-name').textContent = p.name;
    $('#replay-count').textContent = `${wi + 1}/${lastPoints.length}`;
  }
}

// 播放/暂停/重播图标用内联 SVG，保证在蓝色圆钮里绝对居中（文字字形自带偏移，不居中）
const REPLAY_ICONS = {
  play: '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M8 5.14v13.72c0 .8.87 1.3 1.56.88l10.6-6.86a1.05 1.05 0 0 0 0-1.76L9.56 4.26A1.04 1.04 0 0 0 8 5.14z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"/></svg>',
  replay: '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/></svg>',
};
function setReplayIcon(state) {
  $('#replay-toggle').innerHTML = REPLAY_ICONS[state] || REPLAY_ICONS.play;
}

function pauseReplay() {
  replayActive = false;
  if (replayRaf) { cancelAnimationFrame(replayRaf); replayRaf = null; }
  setReplayIcon('play');
}

function finishReplay() {
  pauseReplay();
  updateReplayLines(); // 播完：整条路线变绿（todo 缩为终点）
  $('#replay-fill').style.width = '100%';
  updateReplayName(replayTotal); // 播完：进度条落到最后一个行程点（修复 16/17 错觉）
  setReplayIcon('replay'); // 播完：点击重播
}

function stopReplay(hide = true) {
  replayActive = false;
  if (replayRaf) { cancelAnimationFrame(replayRaf); replayRaf = null; }
  if (replayMarker) replayMarker.setMap(null);
  clearReplayLines();
  if (hide) {
    $('#replay-bar').classList.add('hidden');
    // 恢复回放入口按钮（点数足够时）
    const pts = lastPoints || [];
    $('#replay-launch').classList.toggle('hidden', !pts || pts.length < 2);
  }
  replayT = 0;              // 停止 = 复位；不归零的话 toggleReplay 会一直判成「暂停中」，点了没反应
  $('#replay-fill').style.width = '0%';
  $('#replay-name').textContent = '';
  $('#replay-count').textContent = '';
  setReplayIcon('play');
}

// 关闭回放条 = 仅最小化（不影响正在播放的轨迹）；真正的停止走控制按钮三态循环
function minimizeReplay() {
  $('#replay-bar').classList.add('hidden');
  const pts = lastPoints || [];
  $('#replay-launch').classList.toggle('hidden', !pts || pts.length < 2);
}

// 从暂停处接着走。stopReplay 会把 marker 摘下地图、清掉路线，所以这里按需补挂回来，
// 不然续播时点是在跑，地图上却什么都看不见。
function resumeReplay() {
  if (!replayTotal || replayPath.length < 2) { startReplay(); return; }
  replayActive = true;
  replayLast = 0;   // 归零，否则 dt 会把暂停的那段时长一次性补进去、画面直接跳一大截
  $('#replay-launch').classList.add('hidden');
  $('#replay-bar').classList.remove('hidden');
  setReplayIcon('pause');
  if (replayMarker) replayMarker.setMap(map);
  else { startReplay(); return; }
  ensureReplayLines();
  updateReplayLines();
  playLoop();
}

// 点击「▶ 回放」：暂停/播完后最小化过的 → 只恢复回放条；没播过或已停止 → 从头开始
function openReplayBar() {
  $('#replay-launch').classList.add('hidden');
  $('#replay-bar').classList.remove('hidden');
  if (!replayTotal || replayPath.length < 2 || replayT <= 0) startReplay();
}

// 控制按钮：播放中→暂停，暂停中→从断点续播，播完→重播
function toggleReplay() {
  if (replayActive) { pauseReplay(); return; }
  if (replayT > 0 && replayT < replayTotal) { resumeReplay(); return; } // 暂停中 → 续播
  startReplay(); // 播完 / 停止 / 首次 → 从头播
}

// ---------- 媒体灯箱 ----------
function openLightbox(idx) {
  lightboxIndex = idx;
  renderLightbox();
  $('#lightbox').classList.remove('hidden');
}
let lightboxTrack = null;   // 当前的三格轨道（prev/cur/next）
let lbAnimating = false;    // 翻页动画进行中，期间忽略新的翻页请求

function renderLightbox() {
  const m = lightboxMedia[lightboxIndex];
  if (!m) return;
  const stage = $('#lightbox-stage');
  stopLightboxVideos();          // 先停旧视频，再换内容
  stage.innerHTML = '';
  // 轨道里放三格：相邻两格预先渲染好，拖动时才能被一起推进视野
  const track = el('div', 'lb-track axis-x');
  [[-1, 'prev'], [0, 'cur'], [1, 'next']].forEach(([off, cls]) => {
    const item = lightboxMedia[lightboxIndex + off];
    const slide = el('div', 'lb-slide ' + cls);
    if (item) {
      // 视频格打个标记：说明栏得给原生控制条让位，见 tmReserveCaptionSpace
      if (item.type === 'video' && off === 0) slide.classList.add('has-video');
      slide.appendChild(buildLightboxMedia(item, off === 0));
      slide.appendChild(buildLightboxCaption(item, lightboxIndex + off));
      tmReserveCaptionSpace(slide);
    }
    track.appendChild(slide);
  });
  stage.appendChild(track);
  lightboxTrack = track;
  $('#lightbox-counter').textContent = `${lightboxIndex + 1} / ${lightboxMedia.length}`;
  $('#lightbox-prev').style.visibility = lightboxIndex > 0 ? 'visible' : 'hidden';
  $('#lightbox-next').style.visibility = lightboxIndex < lightboxMedia.length - 1 ? 'visible' : 'hidden';
  tmUpdateDownloadBtn(m);        // 右上角「保存到本地」跟着当前这格走
  saveLightboxState();
}

// 灯箱底部的文字说明。主页面可编辑：有描述就显示描述，没有就显示「＋ 添加描述」，
// 整条可点开编辑；改完就地更新，不用重开灯箱。
// 「地点 · 拍摄时间」，让人一眼看出这张图属于哪个轨迹点——
// 相册是整个行程的，没有这一行就分不清图片的归属。
// ---------- 极简 Markdown（只用于图片说明） ----------
// 说明文字是用户输入，而且会原样呈现给分享页的访客，所以顺序是**先转义、再解析**：
// 进入解析前所有 < > & " 已经变成实体，之后往里加的标签全是我们自己生成的，
// 不存在把用户写的 <script>/onerror 放进 DOM 的路径。链接另外限制协议白名单。
// 只做说明文字用得上的那几种语法，不追求完整 CommonMark：
//   **粗** *斜* ~~删除~~ `代码` [文字](链接) # 标题 - 列表 1. 列表 > 引用 ---

function lbMetaLine(m) {
  return [m.point_name, (m.taken_at || m.exif_time || '').slice(0, 16).replace('T', ' ')]
    .filter(Boolean).join(' · ');
}

function buildLightboxCaption(m, idx) {
  const box = el('div', 'lb-caption');
  const metaText = lbMetaLine(m);
  if (metaText) box.appendChild(el('div', 'lb-caption-meta', metaText));
  const txt = el('div', 'lb-caption-text');
  const more = el('button', 'lb-caption-more', '展开');
  more.type = 'button';
  more.hidden = true;
  // 编辑入口：整个说明栏唯一的进编辑方式（文字区和整条都不再触发）。
  // 没有描述时也在——「＋ 添加描述」那行既然不再可点，这里不常驻就没入口了。
  // 常驻的代价只是 26px 的点击区（见 style.css 的 ::before），说明栏高度不变。
  const edit = el('button', 'lb-caption-edit', '✎');

  const render = () => {
    const has = !!(m.caption && m.caption.trim());
    // 说明支持 Markdown。renderMarkdown 内部已先做 HTML 转义，这里的 innerHTML
    // 只会拿到我们自己生成的标签，用户写的 < > 早变成实体了。
    if (has) { txt.innerHTML = renderMarkdown(m.caption); txt.classList.add('md'); }
    else { txt.textContent = '＋ 添加描述'; txt.classList.remove('md'); }
    box.classList.toggle('empty', !has);
    box.classList.remove('expanded');
    more.textContent = '展开';
    // 文字真的超过 3 行时才给「展开」。测量依赖布局，单靠一次 rAF 在某些时机
    // （灯箱刚从 display:none 变可见、字体还没就位）会量到 0，所以多测几次。
    const syncMore = () => {
      if (!has) { more.hidden = true; return; }
      if (!txt.clientHeight) return;                 // 还没布局，等下一次
      more.hidden = txt.scrollHeight <= txt.clientHeight + 1;
    };
    requestAnimationFrame(syncMore);
    setTimeout(syncMore, 120);
    setTimeout(syncMore, 400);
  };
  render();
  const row = el('div', 'lb-caption-row');
  row.appendChild(txt);
  row.appendChild(more);
  row.appendChild(edit);
  box.appendChild(row);

  // 文字区：展开/收起（没超行就什么也不做，避免误触）
  // 这里**不**进编辑。描述区横跨整幅画面，手指落上去的概率极高，而用户多半
  // 只是想选文字或者滑动翻页；以前点一下就把编辑器弹出来，误触发太频繁。
  // 现在只有右边那个 ✎ 才进编辑，和「展开/收起」彻底分开。
  txt.onclick = (e) => {
    e.stopPropagation();
    // 点说明里的链接就是打开链接，别顺手把说明栏展开/收起了
    if (e.target.closest && e.target.closest('a')) return;
    if (more.hidden) return;
    const on = box.classList.toggle('expanded');
    more.textContent = on ? '收起' : '展开';
  };
  more.onclick = (e) => { e.stopPropagation(); txt.onclick(e); };

  // ✎：编辑（只有这里进编辑，和展开/收起分开，互不误触）
  const startEdit = async (e) => {
    e.stopPropagation();
    if (!await editMediaCaption(m)) return;
    render();
    if (currentTab === 'albums') renderPhotoGrid();
    if (currentTab === 'detail') {
      // 点详情网格上那个 ✎ 的「已写过描述」状态也要跟着变
      const cell = document.querySelector(`#media-grid .media-item[data-mid="${m.id}"] .media-cap`);
      if (cell) cell.classList.toggle('has', !!(m.caption && m.caption.trim()));
    }
  };
  edit.onclick = startEdit;
  // 整条说明栏不再接管点击（原来是 empty 时点一下就开始写）。除了误触，这块
  // 区域还是唯一能**真实选中说明文字**的地方——挂上点击就等于用户刚框选完，
  // 手指一松编辑器就弹出来，selection 当场丢掉。现在一律不拦。
  return box;
}
function closeLightbox() {
  if (typeof lbExitZoom === 'function') lbExitZoom(false);
  stopLightboxVideos();
  $('#lightbox').classList.add('hidden');
  $('#lightbox-stage').innerHTML = '';
  try { localStorage.removeItem(LIGHTBOX_STATE_KEY); } catch (e) {}
}
// 翻页：让轨道继续滑满一屏，滑完再切索引重建。axis 决定横滑还是竖滑。
function lightboxNav(dir, axis) {
  if (lbAnimating) return false;
  const next = lightboxIndex + dir;
  if (next < 0 || next >= lightboxMedia.length) return false;   // 到头了，没翻成
  const track = lightboxTrack;
  if (!track) { lightboxIndex = next; renderLightbox(); return true; }
  track.classList.toggle('axis-y', axis === 'y');
  track.classList.toggle('axis-x', axis !== 'y');
  lbAnimating = true;
  const pct = dir > 0 ? -100 : 100;
  track.classList.add('settling');
  track.style.transform = axis === 'y' ? `translateY(${pct}%)` : `translateX(${pct}%)`;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    track.removeEventListener('transitionend', finish);
    lbAnimating = false;
    lightboxIndex = next;
    renderLightbox();
  };
  track.addEventListener('transitionend', finish);
  setTimeout(finish, 500);   // transitionend 偶尔不触发时的兜底
  return true;
}

// ---------- Tab 切换 ----------
let currentTab = 'points';
window.addEventListener('resize', syncTabIndicator);

function switchTab(name, slideFrom) {
  // 点击切换没传方向：按两个 tab 的前后关系推出来，让点击也有和滑动一致的动画
  if (slideFrom === undefined) {
    const from = TAB_ORDER.indexOf(currentTab), to = TAB_ORDER.indexOf(name);
    if (from >= 0 && to >= 0 && from !== to) slideFrom = to > from ? 'left' : 'right';
  }
  currentTab = name;
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  // 轨道整体平移到目标页：上一页往一侧滑出、下一页从另一侧滑入是同一次位移
  const track = document.querySelector('.view-track');
  if (track) track.classList.add('animating');
  positionViews(name);
  syncTabIndicator();
  syncAddPointBar();
  if (name === 'albums') loadAlbums();
  saveTripState({ tab: name });
}

// ---------- 移动端：左右滑动切换 tab ----------
// 只认「明确的横向手势」：首次位移就要横向占优，且总位移够长，否则一律放行给纵向滚动。
// 双指（相册捏合缩放）直接忽略；不循环（在两端继续滑不跳到另一端）。
const TAB_ORDER = ['points', 'detail', 'albums'];
(function initTabSwipe() {
  const panel = document.querySelector('.panel');
  if (!panel) return;
  const H_MIN = 55;      // 触发切换的最小横向位移(px)
  const LOCK = 10;       // 超过这个位移才判定方向，避免误伤轻点
  let sx = 0, sy = 0, axis = '', active = false;

  // 九宫格起拖时会调这个：把已经跟手走了一段的轨道弹回去，并交出手势
  cancelTabSwipe = function () {
    if (!active) return;
    active = false; axis = '';
    const track = document.querySelector('.view-track');
    if (track) track.classList.add('animating');
    positionViews(currentTab);
  };

  panel.addEventListener('touchstart', (e) => {
    if (mediaDragging) { active = false; return; }
    if (e.touches.length !== 1) { active = false; return; }
    // 加号按钮上起手不算滑动
    if (e.target.closest && e.target.closest('#add-point-bar')) { active = false; return; }
    active = true; axis = '';
    sx = e.touches[0].clientX;
    sy = e.touches[0].clientY;
  }, { passive: true });

  panel.addEventListener('touchmove', (e) => {
    if (mediaDragging) { cancelTabSwipe(); return; }          // 正在拖图片排序，横滑让路
    if (!active) return;
    if (e.touches.length !== 1) { active = false; return; }   // 变成捏合，交还给相册
    const dx = e.touches[0].clientX - sx;
    const dy = e.touches[0].clientY - sy;
    if (!axis) {
      if (Math.abs(dx) < LOCK && Math.abs(dy) < LOCK) return;
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (axis === 'y') { active = false; return; }           // 纵向：完全不插手，正常滚动
      const track = document.querySelector('.view-track');
      if (track) track.classList.remove('animating');         // 跟手期间不要过渡，否则发黏
    }
    if (axis === 'x') {
      if (e.cancelable) e.preventDefault();                   // 压住浏览器的横向/侧滑返回
      // 已经在第一页还想往右、或最后一页还想往左：加阻尼，划不动
      const i = TAB_ORDER.indexOf(currentTab);
      const endStop = (dx > 0 && i <= 0) || (dx < 0 && i >= TAB_ORDER.length - 1);
      positionViews(currentTab, endStop ? dx * 0.3 : dx);
    }
  }, { passive: false });

  panel.addEventListener('touchend', (e) => {
    if (!active || axis !== 'x') { active = false; return; }
    active = false;
    const t = e.changedTouches && e.changedTouches[0];
    if (!t) return;
    const dx = t.clientX - sx;
    const track = document.querySelector('.view-track');
    if (track) track.classList.add('animating');               // 松手后的吸附要带过渡
    const i = TAB_ORDER.indexOf(currentTab);
    const next = Math.abs(dx) >= H_MIN ? TAB_ORDER[i + (dx < 0 ? 1 : -1)] : null;
    if (!next) { positionViews(currentTab); return; }           // 位移不够或已在两端 → 弹回
    switchTab(next);
    if (next === 'detail' && currentPointId) loadDetail(currentPointId);
  }, { passive: true });

  panel.addEventListener('touchcancel', () => {
    active = false;
    const track = document.querySelector('.view-track');
    if (track) track.classList.add('animating');
    positionViews(currentTab);
  }, { passive: true });
})();

// 添加地点浮动按钮的显隐：只在「轨迹点」tab 显示，且面板要矮到装不下它时也藏掉——
// 否则面板被拖到很低时按钮会溢出到面板上沿之外，看着像浮在地图上、不属于面板。
function syncAddPointBar() {
  const bar = $('#add-point-bar');
  if (!bar) return;
  const panel = $('.panel');
  const tabs = panel ? panel.querySelector('.tabs') : null;
  const panelH = panel ? panel.getBoundingClientRect().height : 0;
  const tabsH = tabs ? tabs.getBoundingClientRect().height : 0;
  const needed = 16 + 50 + 8;   // 按钮下边距 + 按钮直径 + 与 tab 条之间留的最小间隙
  const tooShort = (panelH - tabsH) < needed;
  bar.classList.toggle('hidden', currentTab !== 'points' || tooShort);
}
window.addEventListener('resize', syncAddPointBar);

// ---------- 网格重排的 FLIP 过渡 ----------
// 缩放换列数时，直接改 CSS 变量会让所有格子瞬间跳到新位置，很生硬。
// FLIP：先量旧位置(First) → 改列数(Last) → 给每个格子套上「回到旧位置」的
// 逆变换(Invert) → 下一帧放开让它过渡回原位(Play)。
// 关键是**不重建 DOM**：重建会让缩略图重走一遍加载，中间闪一下白。
// 只给视口附近的格子做动画——列表可能上百张，全做会掉帧，看不见的直接归位。
// skip：拖拽排序时被拖的那一格要一直跟着手指，不能参与 FLIP，
// 否则它的 transform 会被逆变换和结尾的清空覆盖掉，看起来就是「拖着拖着图片自己飞回去了」
function flipGrid(grid, mutate, skip) {
  if (!grid) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const cells = Array.from(grid.children).filter(
    c => c.nodeType === 1 && !c.classList.contains('empty-hint') && c !== skip);
  if (reduce || !cells.length) { mutate(); return; }

  const vh = window.innerHeight || 800;
  const first = cells.map(c => c.getBoundingClientRect());
  mutate();
  const last = cells.map(c => c.getBoundingClientRect());

  const moving = [];
  cells.forEach((c, i) => {
    const a = first[i], b = last[i];
    if (!a.width || !b.width) return;
    // 旧位置和新位置都在视口外的，没人看得见，跳过
    const near = (r) => r.bottom > -vh * 0.5 && r.top < vh * 1.5;
    if (!near(a) && !near(b)) return;
    const dx = a.left - b.left, dy = a.top - b.top, s = a.width / b.width;
    if (!dx && !dy && Math.abs(s - 1) < 0.002) return;
    c.style.transformOrigin = 'top left';
    c.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + s + ')';
    moving.push(c);
  });
  if (!moving.length) return;

  void grid.offsetHeight;                      // 强制应用逆变换，否则下面这步会被合并掉
  requestAnimationFrame(function () {
    moving.forEach(function (c) {
      c.classList.add('flipping');
      c.style.transform = '';
      const done = function () {
        c.classList.remove('flipping');
        c.style.transformOrigin = '';
      };
      c.addEventListener('transitionend', done, { once: true });
      setTimeout(done, 500);                   // transitionend 没来时的兜底
    });
  });
}

// ---------- 相册：扁平缩略图网格（类 iOS 照片） ----------
// 每行图标数量由 zoom 档位决定，档位越靠后图越大、每行越少
const PHOTO_COLS = [8, 6, 5, 4, 3, 2];
const PHOTO_VIEW_KEY = 'tm_photo_view';
let photoItems = [];    // 当前行程全部媒体（后端按时间升序）
let photoShown = [];    // 筛选+排序后的列表（灯箱按它翻页）
let photoView = loadPhotoView();

async function loadAlbums() {
  if (!currentTripId) return;
  const grid = $('#photo-grid');
  try {
    const res = await api(`/api/trips/${currentTripId}/media`);
    photoItems = res.items || [];
  } catch (e) {
    grid.innerHTML = '<div class="empty-hint">加载失败：' + esc(e.message) + '</div>';
    return;
  }
  syncPhotoToolbar();
  renderPhotoGrid();
}

// 工具栏按钮状态跟着 photoView 走（刷新/切行程后保持上次的选择）

function renderPhotoGrid() {
  const grid = $('#photo-grid');
  const list = photoItems.filter(m => photoView.filter === 'all' || m.type === photoView.filter);
  // 后端已按时间升序返回；降序时整体反转（同一时间的相对次序也随之反转，符合直觉）
  photoShown = photoView.sort === 'desc' ? list.slice().reverse() : list;

  const n = photoShown.length;
  const total = photoItems.length;
  // 写明范围：这里是「整个行程」的全部媒体，不是某个轨迹点的
  $('#photo-count').textContent = total === 0 ? ''
    : (n === total ? `本行程 · ${total} 项` : `本行程 · ${n} / ${total} 项`);

  grid.style.setProperty('--photo-cols', PHOTO_COLS[photoView.zoom]);
  grid.innerHTML = '';
  if (!n) {
    grid.innerHTML = total === 0
      ? '<div class="empty-hint">暂无媒体，去「点详情」上传照片/视频吧</div>'
      : '<div class="empty-hint">该类型下没有内容</div>';
    return;
  }
  photoShown.forEach((m, idx) => {
    const cell = el('div', 'photo-cell');
    cell.title = [m.caption, m.point_name, (m.taken_at || '').slice(0, 16)]
      .filter(Boolean).join(' · ');
    if (m.thumb_url || m.type === 'photo') {
      const img = el('img');
      img.src = m.thumb_url || m.url;
      img.loading = 'lazy';
      img.decoding = 'async';
      // 缩略图丢失时回退原图，避免整片空白
      img.onerror = () => { if (img.src !== location.origin + m.url) img.src = m.url; };
      cell.appendChild(img);
    } else {
      cell.appendChild(el('div', 'photo-cell-blank', '🎬'));
    }
    if (m.type === 'video') cell.appendChild(el('span', 'photo-video-badge', '▶'));
    // 有文字描述的缩略图打个小标记。它同时是编辑入口——原来只是个装饰，
    // 点上去和点图片一样进灯箱，看着像按钮却不响应，很容易让人以为坏了。
    if (m.caption && m.caption.trim()) {
      const badge = el('button', 'photo-cap-badge', '✎');
      badge.type = 'button';
      badge.title = '修改描述';
      badge.onclick = async (e) => {
        e.stopPropagation();
        if (await editMediaCaption(m)) renderPhotoGrid();
      };
      cell.appendChild(badge);
    }
    cell.onclick = () => {
      lightboxMedia = photoShown;
      openLightbox(idx);
    };
    grid.appendChild(cell);
  });
}

function setPhotoZoom(delta) {
  const next = Math.min(PHOTO_COLS.length - 1, Math.max(0, photoView.zoom + delta));
  if (next === photoView.zoom) return;
  photoView.zoom = next;
  savePhotoView();
  syncPhotoToolbar();
  // 只改列数、不重建 DOM——重建会让每张缩略图重走一遍加载，中间闪白。
  // 位置变化交给 FLIP 过渡。
  const grid = $('#photo-grid');
  flipGrid(grid, function () {
    grid.style.setProperty('--photo-cols', PHOTO_COLS[next]);
  });
}

document.querySelectorAll('#photo-filter .seg-btn').forEach(b => {
  b.addEventListener('click', () => {
    photoView.filter = b.dataset.filter;
    savePhotoView();
    syncPhotoToolbar();
    renderPhotoGrid();
  });
});
$('#btn-photo-sort').addEventListener('click', () => {
  photoView.sort = photoView.sort === 'desc' ? 'asc' : 'desc';
  savePhotoView();
  syncPhotoToolbar();
  renderPhotoGrid();
});
$('#btn-photo-zoom-in').addEventListener('click', () => setPhotoZoom(1));
$('#btn-photo-zoom-out').addEventListener('click', () => setPhotoZoom(-1));

// 双指捏合缩放（手机）
let pinchStart = 0;
// 手势绑在**整个 tab 页**上，而不是 #photo-grid 上。
// 网格只覆盖到最后一行缩略图为止，下面那片空白属于 .view；绑在网格上的话，
// 手指落在空白处就完全收不到事件，看起来就是「有的地方能缩放、有的地方不能」。
const albumsView = $('#albums-view');
albumsView.addEventListener('touchstart', e => {
  if (e.touches.length !== 2) return;
  // 赶在浏览器把双指判成整页缩放之前接管；晚一步 touchmove 就变成不可取消了
  if (e.cancelable) e.preventDefault();
  pinchStart = pinchDist(e);
}, { passive: false });
albumsView.addEventListener('touchmove', e => {
  if (e.touches.length !== 2 || !pinchStart) return;
  if (e.cancelable) e.preventDefault();   // 压住页面自身的缩放
  const d = pinchDist(e);
  const ratio = d / pinchStart;
  if (ratio > 1.18) { setPhotoZoom(1); pinchStart = d; }
  else if (ratio < 0.85) { setPhotoZoom(-1); pinchStart = d; }
}, { passive: false });
albumsView.addEventListener('touchend', () => { pinchStart = 0; }, { passive: true });
albumsView.addEventListener('touchcancel', () => { pinchStart = 0; }, { passive: true });

// Ctrl/⌘ + 滚轮缩放（桌面），与浏览器自身缩放区分开
albumsView.addEventListener('wheel', e => {
  if (!e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  setPhotoZoom(e.deltaY < 0 ? 1 : -1);
}, { passive: false });

// ---------- 模态框 ----------
function openModal(title, fieldsHtml) {
  $('#modal-title').textContent = title;
  $('#modal-fields').innerHTML = fieldsHtml;
  $('#modal').classList.remove('hidden');
}
function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal-fields').innerHTML = '';
  editPointId = null;
  editTripId = null;
}

// 新建/编辑行程
// 行程状态：值 → [中文, 卡片标签的配色 class]。'done' 是历史遗留值，等同 completed。
const TRIP_STATUS = [
  ['planning',  '计划中', ''],
  ['traveling', '进行中', 'traveling'],
  ['completed', '已完成', 'done'],
  ['archived',  '已归档', 'archived'],
];
function tripStatusLabel(v) {
  const k = v === 'done' ? 'completed' : (v || 'planning');
  const hit = TRIP_STATUS.find(s => s[0] === k);
  return hit ? [hit[1], hit[2]] : ['', ''];
}

function tripFormHtml(trip) {
  const t = trip || {};
  const cur = (t.status === 'done' ? 'completed' : t.status) || 'planning';
  // 状态用分段按钮而不是 <select>：手机上原生下拉会弹系统滚轮，多一次确认；
  // 四个选项一行摆开，一次点选就完事。
  const segs = TRIP_STATUS.map(([v, label]) =>
    `<button type="button" class="status-seg-btn${v === cur ? ' active' : ''}" data-status="${v}">${label}</button>`
  ).join('');
  return `
    <div class="field"><label>行程名称</label><input name="name" value="${esc(t.name)}" required></div>
    <div class="field-row">
      <div class="field"><label>开始日期</label><input name="start_date" type="date" value="${esc(t.start_date)}"></div>
      <div class="field"><label>结束日期</label><input name="end_date" type="date" value="${esc(t.end_date)}"></div>
    </div>
    <div class="field">
      <label>状态</label>
      <div class="status-seg" id="trip-status-seg">${segs}</div>
      <input type="hidden" name="status" id="trip-status-input" value="${cur}">
    </div>
    <div class="field"><label>描述</label><textarea name="description">${esc(t.description)}</textarea></div>
  `;
}

// 状态分段按钮的点击（表单是动态插进模态框的，用事件委托）
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.status-seg-btn');
  if (!btn) return;
  e.preventDefault();
  const seg = btn.closest('.status-seg');
  seg.querySelectorAll('.status-seg-btn').forEach(b => b.classList.toggle('active', b === btn));
  const input = document.getElementById('trip-status-input');
  if (input) input.value = btn.dataset.status;
});

function openNewTrip() {
  editTripId = null;
  openModal('新建行程', tripFormHtml(null));
}

async function openEditTrip() {
  if (!currentTripId) { tmToast('请先选择行程'); return; }
  const trips = await api('/api/trips');
  const t = trips.find(x => x.id === currentTripId);
  editTripId = currentTripId;
  // 查询当前有效分享链接数量：有链接才显示撤销按钮并标注数量
  let shareCount = 0;
  try { shareCount = (await api(`/api/trips/${currentTripId}/share-count`)).count || 0; } catch (e) {}
  const revokeBtn = shareCount > 0
    ? `<button type="button" class="btn" id="btn-revoke-share">撤销分享链接</button>`
    : '';
  openModal('编辑行程', tripFormHtml(t) + `
    <div class="modal-actions" style="justify-content:space-between;margin-top:4px">
      ${revokeBtn}
      <button type="button" class="btn btn-danger" id="btn-del-trip">删除此行程</button>
    </div>
  `);
  $('#btn-del-trip').onclick = deleteCurrentTrip;
  if (shareCount > 0) $('#btn-revoke-share').onclick = revokeShare;
}

async function deleteCurrentTrip() {
  if (!await tmConfirm('删除整个行程及其所有地点和媒体？此操作不可恢复！', '删除行程', { danger: true, okText: '删除' })) return;
  await api(`/api/trips/${currentTripId}`, { method: 'DELETE' });
  closeModal();
  // 服务端已在删除时清掉 last_trip_id，本地缓存同步清掉，避免下面 loadTrips 误判
  if (currentUser && Number(currentUser.last_trip_id) === Number(currentTripId)) {
    currentUser.last_trip_id = null;
  }
  currentTripId = null;
  currentPointId = null;
  await loadTrips();
}

// ---------- 分享 ----------
async function copyText(text) {
  // 优先剪贴板 API（需 https 或 localhost）
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* 降级 */ }
  }
  // 降级：临时 textarea + execCommand
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}

// 弹出有效期选择，返回天数（null=永久，undefined=取消）
// 分享设置：有效期 + 可选密码合并成一个弹窗（原来分两步，多点一次没有意义）。
// 返回 { days, password }；days 为 null 表示永久，password 为 '' 表示不设密码；取消返回 undefined。
function chooseShareOptions(hasExisting) {
  return new Promise((resolve) => {
    const opts = [
      { label: '7 天', sub: '一周内有效', days: 7 },
      { label: '30 天', sub: '一个月有效', days: 30 },
      { label: '永久', sub: '不过期', days: null },
    ];
    const hint = hasExisting
      ? '<div class="expiry-hint">已有活跃分享链接，本次将更新其有效期与密码（链接地址不变）。</div>'
      : '';
    const ov = document.createElement('div');
    ov.className = 'modal';
    ov.innerHTML = `
      <div class="modal-box">
        <h2>分享设置</h2>
        ${hint}
        <div class="share-opt-title">有效期</div>
        <div class="expiry-grid">
          ${opts.map((o, i) => `
            <button type="button" class="expiry-card${i === 0 ? ' selected' : ''}" data-days="${o.days === null ? '' : o.days}">
              <span class="expiry-label">${o.label}</span>
              <span class="expiry-sub">${o.sub}</span>
            </button>`).join('')}
        </div>
        <div class="share-opt-title">访问密码（可选）</div>
        <input type="password" class="share-pwd-input" id="share-pwd" placeholder="留空 = 无需密码" autocomplete="off">
        <div class="share-opt-note">留空则任何拿到链接的人都能直接查看；设置后需输入密码才能访问。</div>
        <div class="modal-actions">
          <button type="button" class="btn" data-cancel>取消</button>
          <button type="button" class="btn btn-primary" data-ok>生成链接</button>
        </div>
      </div>`;
    document.body.appendChild(ov);

    let days = opts[0].days;   // 默认选中第一张卡（7 天）
    const cards = ov.querySelectorAll('.expiry-card');
    cards.forEach((card) => {
      card.onclick = () => {
        cards.forEach((x) => x.classList.remove('selected'));
        card.classList.add('selected');
        const v = card.getAttribute('data-days');
        days = v === '' ? null : parseInt(v, 10);
      };
    });

    const input = ov.querySelector('#share-pwd');
    const done = (val) => { ov.remove(); resolve(val); };
    const submit = () => done({ days, password: input.value.trim() });
    ov.querySelector('[data-ok]').onclick = submit;
    ov.querySelector('[data-cancel]').onclick = () => done(undefined);
    ov.addEventListener('click', (e) => { if (e.target === ov) done(undefined); });  // 点遮罩=取消
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    // 刻意不自动聚焦密码框：手机上会立刻弹出键盘把上面的有效期卡片顶掉
  });
}

async function shareTrip() {
  if (!currentTripId) { tmToast('请先选择行程'); return; }
  stopReplay(true); // 分享弹窗打开前收起轨迹播放条，避免遮挡
  // 判断是否已有活跃链接：有则复用 token（链接地址不变），仅覆盖有效期/密码
  let hasExisting = false;
  try { hasExisting = ((await api(`/api/trips/${currentTripId}/share-count`)).count || 0) > 0; } catch (e) {}
  const opt = await chooseShareOptions(hasExisting);
  if (!opt) return; // 用户取消
  const days = opt.days;
  const password = opt.password;
  try {
    const fd = new FormData();
    if (days !== null) fd.append('expire_days', days);
    if (password) fd.append('password', password);
    const { token } = await api(`/api/trips/${currentTripId}/share`, { method: 'POST', body: fd });
    // 分享链接附加版本参数：微信对无参数 URL 会使用缓存快照（旧版 JS 无 OSRM → 全直线），
    // 带版本参数视为新 URL，强制加载最新页面
    let _v = '1';
    try {
      const _m = document.querySelector('script[src*="app.js"]');
      if (_m) { const _vm = (_m.src.match(/[?&]v=([^&]+)/) || [])[1]; if (_vm) _v = _vm; }
    } catch (e) {}
    const url = location.origin + '/share/' + token + '?v=' + _v;
    const expiryText = days === null ? '永久有效' : `有效期 ${days} 天`;
    const pwdText = password ? `访问密码 ${password}` : '无需密码';

    // 取行程信息（名称/日期/点数）用于拼分享文案
    const trips = await api('/api/trips');
    const trip = trips.find(x => x.id === currentTripId) || {};
    let pointCount = 0;
    let pointStart = null, pointEnd = null;
    try {
      const s = await api(`/api/trips/${currentTripId}/stats`);
      pointCount = s.point_count || 0;
      pointStart = s.point_start_date || null;
      pointEnd = s.point_end_date || null;
    } catch (e) {}

    const name = trip.name || '旅行记忆行程';
    // 日期：优先行程自身日期；否则用轨迹点起止日期兜底；都没有则省略日期行
    let dateText = '';
    if (trip.start_date) {
      dateText = trip.start_date + (trip.end_date && trip.end_date !== trip.start_date ? ' 至 ' + trip.end_date : '');
    } else if (pointStart) {
      dateText = pointStart + (pointEnd && pointEnd !== pointStart ? ' 至 ' + pointEnd : '');
    }
    // 统计信息：里程（真实路线就绪后）/天数/城市数，随分享文案与卡片一起展示
    const distText = calcTotalDistance();
    let tripDays = 0, cities = 0;
    if (lastPoints && lastPoints.length) {
      tripDays = new Set(lastPoints.map(p => p.start_date).filter(Boolean)).size;
      cities = new Set(lastPoints.filter(p => !p.is_waypoint).map(p => p.name).filter(Boolean)).size;
    }
    const statsBits = [];
    if (distText) statsBits.push('总里程 ' + distText);
    if (tripDays >= 2) statsBits.push(tripDays + ' 天');
    if (cities > 0) statsBits.push(cities + ' 城');
    const statsText = statsBits.join(' · ');

    const metaBits = [];
    if (dateText) metaBits.push(dateText);
    metaBits.push('共 ' + pointCount + ' 个轨迹点');
    if (statsText) metaBits.push(statsText);
    const metaLine = metaBits.join(' · ');
    const intro = `🧭【旅行记忆】${name}\n${metaLine}\n${url}\n\n（只读查看 · ${expiryText} · ${pwdText}）`;

    const copied = await copyText(intro);
    showShareCard({ name, dateText, metaLine, pointCount, token, url, password, expiryText, pwdText, copied });
    if (!copied) {
      // 剪贴板不可用时兜底提示手动复制
      setTimeout(() => tmPrompt('剪贴板不可用，请手动复制链接：', url, '复制链接'), 200);
    }
  } catch (err) {
    tmAlert('生成分享链接失败：' + err.message);
  }
}

// 分享卡片预览：缩略图 + 二维码 + 标题 + 链接
function showShareCard({ name, dateText, metaLine, pointCount, token, url, password, expiryText, pwdText, copied }) {
  const thumbUrl = '/api/share/' + token + '/thumbnail' + (password ? '?password=' + encodeURIComponent(password) : '');
  const qrUrl = '/api/share/' + token + '/qrcode';

  const ov = document.createElement('div');
  ov.className = 'modal';
  ov.id = 'share-card-modal';
  ov.innerHTML = `
    <div class="modal-box share-card">
      <h2>分享已生效</h2>
      <div class="share-live">
        <span class="share-live-dot"></span>
        <div class="share-live-text">
          <div class="share-live-title">链接已生效，现在就能发给别人</div>
          <div class="share-live-sub">${esc(expiryText)} · ${esc(pwdText)}${copied ? ' · 链接已复制到剪贴板' : ''}</div>
        </div>
      </div>
      <div class="share-card-title">${esc(name)}</div>
      <div class="share-card-meta">${esc(metaLine)} · ${esc(expiryText)}</div>
      <img class="share-card-thumb" id="share-card-thumb" src="${thumbUrl}" alt="路线缩略图">
      <div class="share-card-bottom">
        <div class="share-card-qrbox">
          <img class="share-card-qr" id="share-card-qr" src="${qrUrl}" alt="分享二维码">
          <div class="share-card-qrhint">扫码查看</div>
        </div>
        <div class="share-card-info">
          <div class="share-card-link" id="share-card-link">${esc(url)}</div>
          <div class="share-card-hint">长按二维码或保存卡片分享给朋友，打开后只能查看、不能修改。</div>
        </div>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" id="share-card-copy">复制链接</button>
        <button type="button" class="btn btn-primary" id="share-card-download">保存卡片图片</button>
        <button type="button" class="btn" id="share-card-close">关闭</button>
      </div>
    </div>`;
  document.body.appendChild(ov);

  ov.querySelector('#share-card-close').onclick = () => ov.remove();
  ov.querySelector('#share-card-copy').onclick = async () => {
    // 成功只用非模态 toast 飘一下，不打断操作；失败要把链接摆出来让用户手抄，才用模态
    const ok = await copyText(url);
    if (ok) tmToast('✓ 链接已复制');
    else tmPrompt('复制失败，请手动复制链接：', url, '复制链接');
  };
  ov.querySelector('#share-card-download').onclick = () => downloadShareCard(ov, { name, metaLine, pointCount, url });
  ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
}

// 用 canvas 合成分享卡片图并下载（标题中文走浏览器字体，无额外依赖）
// 保存卡片图片。手机上「下载」这个动作很别扭（文件落到下载目录、微信里甚至没反应），
// 按能力从优到劣依次降级：
//   1) Web Share API 传文件 → 唤起系统分享面板，里面就有「存储图像 / 保存到相册」
//      （iOS Safari 15+ / Chrome Android；需要 HTTPS + 用户手势，本站两者都满足）
//   2) 触屏但不支持分享（微信内置浏览器是典型）→ 全屏展示图片，提示长按保存到相册，
//      跟保存别人发来的二维码是同一套操作
//   3) 桌面 → 保持原来的下载
function isTouchDevice() {
  return ('ontouchstart' in window) || (navigator.maxTouchPoints || 0) > 0;
}

async function saveCardImage(blob, filename) {
  try {
    const file = new File([blob], filename, { type: 'image/png' });
    if (navigator.share && navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: filename });
      return;
    }
  } catch (e) {
    // 用户在系统面板里自己取消了，就到此为止，不要再弹一层降级界面
    if (e && e.name === 'AbortError') return;
  }
  if (isTouchDevice()) { showLongPressSave(blob); return; }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// 长按保存兜底层：全屏铺一张图 + 提示，长按即可保存到相册
function showLongPressSave(blob) {
  const url = URL.createObjectURL(blob);
  const ov = el('div', 'longpress-mask');
  const img = el('img', 'longpress-img');
  img.src = url;
  img.alt = '分享卡片';
  const tip = el('div', 'longpress-tip', '长按上方图片 → 保存到相册');
  const btn = el('button', 'btn btn-primary longpress-close', '完成');
  ov.appendChild(img);
  ov.appendChild(tip);
  ov.appendChild(btn);
  document.body.appendChild(ov);
  const close = () => { ov.remove(); setTimeout(() => URL.revokeObjectURL(url), 3000); };
  btn.onclick = close;
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
}

function downloadShareCard(ov, { name, metaLine, pointCount, url }) {
  const thumb = ov.querySelector('#share-card-thumb');
  const qr = ov.querySelector('#share-card-qr');
  const draw = () => {
    // 3:4 竖版：手机竖屏浏览/微信分享的常见比例；原来 640x720 接近正方形，
    // 发到聊天里上下会被大片留白包住。2 倍分辨率导出，手机上文字不糊。
    const W = 750, H = 1000, S = 2, PAD = 40;
    const c = document.createElement('canvas');
    c.width = W * S; c.height = H * S;
    const ctx = c.getContext('2d');
    ctx.scale(S, S);
    ctx.textBaseline = 'alphabetic';

    // 超宽文本截断加省略号，避免长行程名/长链接画出画布
    const ellipsize = (text, maxW) => {
      if (ctx.measureText(text).width <= maxW) return text;
      let s = text;
      while (s.length > 1 && ctx.measureText(s + '…').width > maxW) s = s.slice(0, -1);
      return s + '…';
    };

    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, W, H);
    ctx.textAlign = 'center';

    // 标题 / 副标题
    ctx.fillStyle = '#1a1a1a';
    ctx.font = 'bold 34px system-ui, sans-serif';
    ctx.fillText(ellipsize(name || '旅行记忆', W - PAD * 2), W / 2, 82);
    ctx.fillStyle = '#8a8f98';
    ctx.font = '18px system-ui, sans-serif';
    ctx.fillText(ellipsize(metaLine || '', W - PAD * 2), W / 2, 116);

    // 缩略图：按原始比例等比缩放居中（原来是硬拉成固定框，会变形）
    // 框高按缩略图接口的固有比例算（后端固定输出 640x400），这样等比缩放后
    // 正好铺满，不会在上下留出灰边
    const boxX = PAD, boxY = 144, boxW = W - PAD * 2, boxH = Math.round(boxW * 400 / 640);
    ctx.fillStyle = '#f2f4f7';
    ctx.fillRect(boxX, boxY, boxW, boxH);
    if (thumb.complete && thumb.naturalWidth) {
      const r = Math.min(boxW / thumb.naturalWidth, boxH / thumb.naturalHeight);
      const dw = thumb.naturalWidth * r, dh = thumb.naturalHeight * r;
      ctx.drawImage(thumb, boxX + (boxW - dw) / 2, boxY + (boxH - dh) / 2, dw, dh);
    }

    // 二维码
    if (qr.complete && qr.naturalWidth) {
      // 去掉 URL 那两行后腾出空间，二维码放大到 300，扫码更容易
      const qs = 300;
      ctx.drawImage(qr, W / 2 - qs / 2, 600, qs, qs);
    }

    // 扫码提示。刻意不把 URL 印在图上：图片里的文字既不能点也不能复制，
    // 只会白占一大块地方；要打开就扫码/长按识别二维码，要链接就用「复制链接」按钮。
    ctx.fillStyle = '#6b7280';
    ctx.font = '20px system-ui, sans-serif';
    ctx.fillText('扫码 / 长按识别二维码查看', W / 2, 946);
    ctx.fillStyle = '#c0c4c9';
    ctx.font = '16px system-ui, sans-serif';
    ctx.fillText('旅行记忆 · 只读分享', W / 2, 976);

    c.toBlob((blob) => {
      saveCardImage(blob, `${name || '行程'}-分享卡片.png`);
    }, 'image/png');
  };
  // 确保两张图都加载完
  const needLoad = [thumb, qr].filter(img => !(img.complete && img.naturalWidth));
  if (needLoad.length === 0) { draw(); return; }
  let remaining = needLoad.length;
  needLoad.forEach(img => {
    const done = () => { if (--remaining === 0) draw(); };
    img.addEventListener('load', done, { once: true });
    img.addEventListener('error', done, { once: true });
  });
}

async function revokeShare() {
  if (!await tmConfirm('撤销当前行程的所有分享链接？已发出的链接将立即失效。', '撤销分享', { danger: true, okText: '撤销' })) return;
  const btn = $('#btn-revoke-share');
  if (btn) { btn.disabled = true; btn.textContent = '撤销中…'; }   // 防连点
  try {
    await api(`/api/trips/${currentTripId}/share`, { method: 'DELETE' });
  } catch (e) {
    tmAlert('撤销失败：' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '撤销分享链接'; }
    return;
  }
  // 撤销成功后已经没有链接可撤了：把按钮就地换成一句状态提示，
  // 而不是留一个还能再点、点了也没意义的死按钮。
  if (btn) {
    btn.replaceWith(el('span', 'revoke-done', '✓ 分享链接已撤销'));
  } else {
    tmToast('已撤销全部分享链接');
  }
}

// 导出行程为纯文本 JSON（下载文件，便于手工编辑）
async function exportTrip() {
  if (!currentTripId) { tmToast('请先选择行程'); return; }
  let name = `travel-${currentTripId}`;
  try {
    const trips = await api('/api/trips');
    const t = trips.find(x => x.id === currentTripId);
    if (t && t.name) name = t.name;
  } catch (e) {}
  const res = await fetch(`/api/trips/${currentTripId}/export`);
  if (!res.ok) { tmAlert('导出失败：' + await res.text()); return; }
  const text = await res.text();
  const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${name}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// 从纯文本 JSON 导入行程（新建行程 + 全部点）
async function importTrip(e) {
  const file = e.target.files[0];
  e.target.value = ''; // 允许重复选同一文件
  if (!file) return;
  const text = await file.text();
  let body;
  try { body = JSON.parse(text); } catch (err) { tmAlert('文件不是有效的 JSON 轨迹文本'); return; }
  const name = body?.trip?.name || '';
  if (!await tmConfirm(`导入后将新建一个行程「${name}」并包含其全部轨迹点，确认继续？`, '导入行程')) return;
  try {
    const r = await fetch('/api/trips/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) { tmAlert('导入失败：' + await r.text()); return; }
    const result = await r.json();
    await loadTrips();
    currentTripId = result.id;
    await loadPoints();
    tmToast(`导入成功：「${name}」共 ${result.point_count} 个轨迹点`, 2600);
  } catch (err) {
    tmAlert('导入失败：' + err.message);
  }
}

// 添加/编辑点
function openPointForm(point) {
  const isEdit = !!point;
  editPointId = isEdit ? point.id : null;
  openModal(isEdit ? '编辑地点' : '添加地点', `
    <div class="field">
      <label>地名（输入关键词联想）</label>
      <input name="name" id="point-name-input" value="${isEdit ? point.name : ''}" autocomplete="off" required>
      <input name="lat" type="hidden" value="${isEdit ? point.lat : ''}">
      <input name="lng" type="hidden" value="${isEdit ? point.lng : ''}">
      <div id="autocomplete-suggest" class="suggest"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>到达日期</label><input name="start_date" type="date" value="${isEdit ? point.start_date || '' : ''}" required></div>
      <div class="field"><label>离开日期（可空）</label><input name="end_date" type="date" value="${isEdit ? point.end_date || '' : ''}"></div>
    </div>
    <div class="field"><label>描述</label><textarea name="description">${isEdit ? point.description || '' : ''}</textarea></div>
    <div class="field">
      <label class="checkbox-label">
        <input type="checkbox" name="is_waypoint" value="1" ${isEdit && point.is_waypoint ? 'checked' : ''}>
        辅助点（仅作为途经点让路线经过，地图上不可点击、不显示照片视频）
      </label>
    </div>
  `);
  // 绑定地名联想
  setupAutocomplete();
}

// 地名联想（双路：高德国内 + Open-Meteo 全球海外）
function setupAutocomplete() {
  const input = $('#point-name-input');
  const suggest = $('#autocomplete-suggest');
  if (!input) return;
  suggest.innerHTML = '';

  let debounceTimer = null;
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const kw = input.value.trim();
    if (!kw) { suggest.innerHTML = ''; return; }
    debounceTimer = setTimeout(() => searchPlaces(kw, input, suggest), 280);
  });

  // 点击空白关闭联想
  document.addEventListener('click', function closeSuggest(e) {
    if (!e.target.closest('#point-name-input') && !e.target.closest('#autocomplete-suggest')) {
      suggest.innerHTML = '';
    }
  });
}

function searchPlaces(kw, input, suggest) {
  const seq = ++searchSeq;
  suggest.innerHTML = '<div class="suggest-item suggest-loading">搜索中…</div>';

  const results = [];
  let pending = 3;
  const finish = () => {
    if (--pending > 0) return;
    if (seq !== searchSeq) return; // 已有更新的搜索，丢弃本次结果
    renderSuggest(results, input, suggest);
  };

  // 路1：高德 Autocomplete（国内地名）
  if (autocomplete) {
    autocomplete.search(kw, (status, result) => {
      if (status === 'complete' && result.tips) {
        result.tips.forEach(tip => {
          if (!tip.location) return;
          results.push({
            name: tip.name,
            sub: tip.district || '',
            lat: tip.location.lat,
            lng: tip.location.lng,
          });
        });
      }
      finish();
    });
  } else {
    finish();
  }

  // 路2：Open-Meteo 全球地理编码（含国内兜底；国内地名高德 Autocomplete 常搜不到）
  fetch('https://geocoding-api.open-meteo.com/v1/search?name=' +
        encodeURIComponent(kw) + '&count=6&language=zh&format=json')
    .then(r => r.json())
    .then(d => {
      (d.results || []).forEach(r => {
        let lat = r.latitude, lng = r.longitude;
        if (r.country_code === 'CN') {
          // 国内坐标 WGS-84 → GCJ-02，与高德地图/Driving 对齐，避免几百米偏移
          const gcj = wgs84ToGcj02(lng, lat);
          lng = gcj.lng; lat = gcj.lat;
        }
        results.push({
          name: r.name,
          sub: [r.country, r.admin1].filter(Boolean).join(' · '),
          lat: lat,
          lng: lng,
        });
      });
      finish();
    })
    .catch(() => finish());

  // 路3：中文词典匹配（5 国海外地名，中文名 → 英文名 → 坐标）
  const dictHits = matchPlacesDict(kw);
  if (dictHits.length > 0) {
    Promise.all(dictHits.map(hit =>
      fetch('https://geocoding-api.open-meteo.com/v1/search?name=' +
            encodeURIComponent(hit.en) + '&count=3&language=zh&format=json')
        .then(r => r.json())
        .then(d => {
          (d.results || []).forEach(r => {
            if (r.country_code !== hit.cc) return; // 只保留目标国，避免同名英文撞车
            results.push({
              name: hit.zh,
              sub: [r.country, r.admin1].filter(Boolean).join(' · '),
              lat: r.latitude,
              lng: r.longitude,
            });
          });
        })
        .catch(() => {})
    )).then(finish);
  } else {
    finish();
  }
}

// 中文地名匹配词典：精确 > 前缀 > 包含，取前 5 个候选
function matchPlacesDict(kw) {
  if (!placesDict.length || !kw) return [];
  const exact = [], prefix = [], contain = [];
  for (const it of placesDict) {
    if (it.zh === kw) exact.push(it);
    else if (it.zh.startsWith(kw)) prefix.push(it);
    else if (it.zh.includes(kw)) contain.push(it);
  }
  const merged = [...exact, ...prefix, ...contain];
  const seen = new Set(), out = [];
  for (const it of merged) {
    const key = it.zh + '|' + it.cc;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(it);
    if (out.length >= 5) break;
  }
  return out;
}

// 加载 5 国中文↔英文地名词典
async function loadPlacesDict() {
  try {
    const r = await fetch('/static/places-zh-en.json', { cache: 'no-store' });
    if (r.ok) placesDict = await r.json();
  } catch (e) { /* 词典加载失败不影响主体功能 */ }
}

function renderSuggest(results, input, suggest) {
  suggest.innerHTML = '';
  if (results.length === 0) {
    suggest.innerHTML = '<div class="suggest-item suggest-empty">未找到匹配地点，试试更具体或英文名（如 Ronda）</div>';
    return;
  }
  // 去重（按名称+近似坐标）
  const seen = new Set();
  results.forEach(r => {
    const key = r.name + '|' + Math.round(r.lat * 1000) + ',' + Math.round(r.lng * 1000);
    if (seen.has(key)) return;
    seen.add(key);
    const div = el('div', 'suggest-item', r.name + (r.sub ? ' · ' + r.sub : ''));
    div.onclick = () => {
      input.value = r.name;
      input.closest('.field').querySelector('[name="lat"]').value = r.lat;
      input.closest('.field').querySelector('[name="lng"]').value = r.lng;
      suggest.innerHTML = '';
    };
    suggest.appendChild(div);
  });
}

// ---------- 表单提交 ----------
$('#modal-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);

  try {
    const title = $('#modal-title').textContent;
    let createdTripId = null;
    if (title === '新建行程') {
      const r = await api('/api/trips', { method: 'POST', body: fd });
      createdTripId = r && r.id;
    } else if (title === '编辑行程') {
      await api(`/api/trips/${editTripId}`, { method: 'PUT', body: fd });
    } else if (editPointId) {
      await api(`/api/points/${editPointId}`, { method: 'PUT', body: fd });
    } else {
      // 校验经纬度
      const lat = fd.get('lat');
      const lng = fd.get('lng');
      if (!lat || !lng) {
        tmAlert('请在地名联想中选择一个具体地点');
        return;
      }
      await api(`/api/trips/${currentTripId}/points`, { method: 'POST', body: fd });
    }
    closeModal();
    if (title === '新建行程' || title === '编辑行程') {
      // 新建完就进去。原来只 loadTrips()，而 loadTrips 会「恢复上次打开的行程」，
      // 于是刚建好的行程明明在列表里、界面却纹丝不动停在旧行程上。
      await loadTrips(createdTripId);
      if (createdTripId) tmToast('已进入新行程，点右下角「＋」添加第一个地点');
    } else {
      await loadPoints();
    }
  } catch (err) {
    tmAlert('操作失败：' + err.message);
  }
});

// ---------- 更多操作菜单（移动端底部抽屉） ----------
function openMoreSheet() {
  // 图标用 Feather Icons（MIT）的线条几何内联成 SVG，跟主流 App（微信/抖音/小红书）
  // 的线性图标一致；emoji 在各家系统上长相差异大且偏卡通。
  const svg = (body) =>
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
  const ICON = {
    plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    // 分享：方框 + 向上箭头，iOS/Safari/微信浏览器里通用的「分享」形态
    share: '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>'
         + '<polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>'
           + '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
            + '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
          + '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
          + '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  };
  const items = [
    { ico: svg(ICON.plus),     lbl: '新建行程',     fn: openNewTrip },
    { ico: svg(ICON.edit),     lbl: '编辑行程信息', fn: openEditTrip },
    { ico: svg(ICON.share),    lbl: '分享当前行程', fn: shareTrip },
    { ico: svg(ICON.refresh),  lbl: '重新计算路线', fn: () => $('#btn-recalc').click() },
    { ico: svg(ICON.download), lbl: '导出行程',     fn: exportTrip },
    { ico: svg(ICON.upload),   lbl: '导入行程',     fn: () => $('#import-file-input').click() },
    { ico: svg(ICON.terminal), lbl: '开发者控制台', fn: () => { window.open('/console', '_blank'); } },
    { ico: svg(ICON.logout),   lbl: '退出登录',     fn: () => $('#btn-logout').click(), danger: true },
  ];
  const mask = document.createElement('div');
  mask.className = 'more-sheet-mask';
  const sheet = document.createElement('div');
  sheet.className = 'more-sheet';
  const body = items.map(it =>
    `<button type="button" class="more-sheet-item${it.danger ? ' danger' : ''}" data-idx="${items.indexOf(it)}">
      <span class="ico">${it.ico}</span><span class="lbl">${it.lbl}</span>
    </button>`).join('');
  // 底部显示当前前端版本号：改了东西却还看到老样子时，一眼能分辨是没生效还是浏览器缓存
  const ver = (function () {
    const s = [...document.scripts].map(x => x.src).find(x => /app\.js/.test(x)) || '';
    const m = s.match(/[?&]v=([\w.-]+)/);
    return m ? m[1] : '未知';
  })();
  sheet.innerHTML =
    `<div class="more-sheet-handle"></div>
     <div class="more-sheet-title">操作菜单</div>
     <div class="more-sheet-grid">${body}</div>
     <div class="more-sheet-ver">版本 ${esc(ver)}</div>`;
  mask.appendChild(sheet);
  document.body.appendChild(mask);

  const close = () => { mask.remove(); };
  mask.addEventListener('click', (e) => {
    if (e.target === mask) close();
  });
  sheet.querySelectorAll('.more-sheet-item').forEach(btn => {
    btn.onclick = () => {
      const it = items[parseInt(btn.dataset.idx, 10)];
      close();
      it.fn();
    };
  });
}
$('#btn-console').onclick = () => window.open('/console', '_blank');
$('#btn-more').onclick = openMoreSheet;

// ---------- 事件绑定 ----------
$('#btn-new-trip').onclick = openNewTrip;
$('#btn-edit-trip').onclick = openEditTrip;
$('#btn-share-trip').onclick = shareTrip;
$('#btn-export-trip').onclick = exportTrip;
$('#btn-import-trip').onclick = () => $('#import-file-input').click();
$('#import-file-input').addEventListener('change', importTrip);
$('#btn-add-point').onclick = () => {
  if (!currentTripId) { tmToast('请先新建一个行程'); return; }
  openPointForm(null);
};
$('#btn-cancel').onclick = closeModal;
$('#modal').addEventListener('click', (e) => {
  if (e.target === $('#modal')) closeModal();
});

// 编辑点
$('#btn-edit-point').onclick = async () => {
  if (!currentPointId) return;
  const points = await api(`/api/trips/${currentTripId}/points`);
  const p = points.find(x => x.id === currentPointId);
  openPointForm(p);
};

// 删除点
$('#btn-del-point').onclick = async () => {
  if (!currentPointId) return;
  if (!await tmConfirm('删除这个地点及其所有照片视频？', '删除地点', { danger: true, okText: '删除' })) return;
  await api(`/api/points/${currentPointId}`, { method: 'DELETE' });
  hideDetail();
  await loadPoints();
};

// 照片/视频上传：交给 startUploads，界面上立刻出现带进度的占位格（见上面的说明）
$('#file-input').addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';          // 先清空，这样紧接着再选同一个文件也能触发 change
  if (!files.length || !currentPointId) return;
  startUploads(files);
});

// Tab 切换
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    const name = t.dataset.tab;
    switchTab(name);
    // 切到「点详情」时若有已选点，重新加载详情（刷新后详情体已被 hideDetail 清空，
    // 仅 switchTab 切回 tab 不会恢复内容，需按需重新拉取）
    if (name === 'detail' && currentPointId) loadDetail(currentPointId);
  };
});

// ---------- 面板收起/展开 ----------
// 收起/展开都带过渡，两端用各自合适的动法：
//   手机端——面板在底部，把**地图高度**当抽屉动（地图长满 = 面板被挤没）。
//            这和拖手柄调高度是同一种视觉，用户不用另学一套。
//   宽屏——面板是右侧栏，向右滑出 + 外框宽度收拢，内容不会被压扁。
// display:none 只在动画结束后才加上，否则一上来就没了，动画根本看不见。
const PANEL_ANIM_MS = 300;
let panelAnimTimer = null;
// 展开时恢复到的地图高度：优先上次拖到的位置，没有就回默认 55vh
function restoredMapHeight(full) {
  const st = (typeof getTripState === 'function') ? getTripState() : {};
  const saved = Number(st && st.panelHeight);
  const dflt = Math.round(window.innerHeight * 0.55);
  const h = (saved > 0 && saved < full - 40) ? saved : dflt;
  return Math.max(0, Math.min(full - 40, h));
}
// 把最终状态一次性落到 DOM 上（不含动画）
function applyPanelCollapsed(collapsed) {
  $('.panel').classList.toggle('collapsed', collapsed);
  $('.layout').classList.toggle('panel-collapsed', collapsed);
  $('#btn-panel-open').classList.toggle('hidden', !collapsed);
  if (!collapsed) setTimeout(syncTabIndicator, 20);
  syncAddPointBar();
  if (map && typeof map.resize === "function") setTimeout(() => map.resize(), 80);
  try { sessionStorage.setItem('tm_panel_collapsed', collapsed ? '1' : '0'); } catch (e) {}
}

function setPanelCollapsed(collapsed, animate) {
  const layout = $('.layout'), panel = $('.panel');
  const mapEl = document.getElementById('map');
  const already = panel.classList.contains('collapsed');

  clearTimeout(panelAnimTimer);
  layout.classList.remove('panel-animating');
  panel.classList.remove('sliding-out');

  // 状态没变 / 不要动画 / 系统要求减弱动效 → 直接落状态
  if (animate === false || panelReducedMotion() || !mapEl || already === collapsed) {
    if (!collapsed && isMobileLayout() && mapEl && !mapEl.style.height) {
      mapEl.style.height = restoredMapHeight(layout.getBoundingClientRect().height) + 'px';
    }
    applyPanelCollapsed(collapsed);
    return;
  }

  const full = layout.getBoundingClientRect().height;

  if (isMobileLayout()) {
    if (collapsed) {
      // 收起：地图从当前高度长到满格，面板被顶下去、逐渐收成 0
      mapEl.style.height = Math.round(mapEl.getBoundingClientRect().height) + 'px';
      void mapEl.offsetHeight;                       // 定住起点，否则下一行会被合并掉
      layout.classList.add('panel-animating');
      mapEl.style.height = Math.round(full) + 'px';
      $('#btn-panel-open').classList.remove('hidden');  // 右下角图标同步弹出，衔接得上
      pumpMapResize();
      panelAnimTimer = setTimeout(() => {
        layout.classList.remove('panel-animating');
        mapEl.style.height = '';                     // 交回 .layout.panel-collapsed 的规则
        applyPanelCollapsed(true);
      }, PANEL_ANIM_MS);
    } else {
      // 展开：先把面板放回 DOM，但地图仍占满，再过渡回上次的高度
      const target = restoredMapHeight(full);
      applyPanelCollapsed(false);
      mapEl.style.height = Math.round(full) + 'px';
      void mapEl.offsetHeight;
      layout.classList.add('panel-animating');
      mapEl.style.height = Math.round(target) + 'px';
      pumpMapResize();
      panelAnimTimer = setTimeout(() => {
        layout.classList.remove('panel-animating');
        syncTabIndicator();
        syncAddPointBar();
      }, PANEL_ANIM_MS);
    }
    return;
  }

  // 宽屏：右侧栏滑出/滑入
  if (collapsed) {
    layout.classList.add('panel-animating');
    void panel.offsetWidth;
    panel.classList.add('sliding-out');
    $('#btn-panel-open').classList.remove('hidden');
    pumpMapResize();
    panelAnimTimer = setTimeout(() => {
      panel.classList.remove('sliding-out');
      layout.classList.remove('panel-animating');
      applyPanelCollapsed(true);
    }, PANEL_ANIM_MS);
  } else {
    applyPanelCollapsed(false);
    panel.classList.add('sliding-out');
    void panel.offsetWidth;
    layout.classList.add('panel-animating');
    panel.classList.remove('sliding-out');
    pumpMapResize();
    panelAnimTimer = setTimeout(() => {
      layout.classList.remove('panel-animating');
      syncTabIndicator();
    }, PANEL_ANIM_MS);
  }
}

// 收起按钮有两个（手机端在手柄行、宽屏端在 tab 行），统一按类绑定
document.querySelectorAll('.panel-toggle').forEach(b =>
  b.addEventListener('click', () => setPanelCollapsed(true)));
$('#btn-panel-open').addEventListener('click', () => setPanelCollapsed(false));
function restorePanelState() {
  try {
    // 首屏恢复不做动画：页面刚出来就滑一下反而像卡了一下
    if (sessionStorage.getItem('tm_panel_collapsed') === '1') setPanelCollapsed(true, false);
  } catch (e) {}
}

// ---------- 手机端：收起/唤出顶部两条 ----------
// 顶栏（logo + 行程切换）和账号/统计条在小屏上加起来近 90px。看地图的时候
// 它们只是占地方，但又不能一直藏着（行程切换、更多菜单都在上面），
// 所以做成可收起、随时点回来，并且记住选择（sessionStorage：换一次会话回到默认露出，
// 免得新用户在别的设备上打开看不到顶栏还以为坏了）。
const CHROME_KEY = 'tm_chrome_hidden';

function setChromeHidden(hidden, persist) {
  document.body.classList.toggle('chrome-hidden', !!hidden);
  const btn = document.getElementById('btn-chrome-toggle');
  if (btn) {
    btn.title = hidden ? '展开顶部栏' : '收起顶部栏';
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-expanded', hidden ? 'false' : 'true');
  }
  if (persist !== false) {
    try { sessionStorage.setItem(CHROME_KEY, hidden ? '1' : '0'); } catch (e) {}
  }
  // 顶栏高度变了，地图容器跟着变高/变矮，等过渡走完让高德重新量一次
  setTimeout(() => { if (map && typeof map.resize === 'function') map.resize(); }, 260);
}

(function initChromeToggle() {
  const btn = document.getElementById('btn-chrome-toggle');
  if (!btn) return;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setChromeHidden(!document.body.classList.contains('chrome-hidden'));
  });
  try {
    if (sessionStorage.getItem(CHROME_KEY) === '1') setChromeHidden(true, false);
  } catch (e) {}
})();

// 全览：把整条行程（所有轨迹点 + 路线）收进当前视野。
// 只喂点和路线，不用 setFitView(null)——那会把行政区划多边形、照片图层这些
// 一起算进去，行政区划动辄一个省，视野会被拉得离谱。
function fitWholeTrip() {
  if (!map || typeof map.setFitView !== 'function') return;
  const overlays = markers.concat(
    segPolylines.length ? segPolylines : (polyline ? [polyline] : []));
  if (!overlays.length) { tmToast('这个行程还没有轨迹点'); return; }
  try {
    map.setFitView(overlays, false, [60, 60, 60, 60], 300);
    viewRestored = false;   // 用户主动全览后，不再保留「恢复上次视野」的优先权
  } catch (e) {}
}
$('#btn-fit-view').addEventListener('click', fitWholeTrip);

// ---------- 地图全屏 ----------
// 做法：给 #map 加 .map-fullscreen（fixed 铺满视口），而不是对 #map 调元素级
// Fullscreen API —— 后者会让灯箱/模态框/更多菜单（都在 #map 外面）直接不可见。
// 同时对 documentElement 尽力请求一次原生全屏，只为把浏览器地址栏藏掉；
// iOS Safari / 微信内置浏览器不支持时静默忽略，CSS 铺满仍然生效。
(function initMapFullscreen() {
  const mapEl = document.getElementById('map');
  const btn = document.getElementById('btn-map-fullscreen');
  if (!mapEl || !btn) return;
  let usedNative = false;   // 原生全屏是不是我们请求的（决定退出时要不要 exitFullscreen）

  const nativeOn = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
  const isOn = () => mapEl.classList.contains('map-fullscreen');

  function sync() {
    const on = isOn();
    // 图标是 CSS 背景图（见 .fab-fullscreen / .fab-fullscreen.active），
    // 这里只切状态类，不再改 textContent
    btn.title = on ? '退出地图全屏' : '地图全屏';
    btn.setAttribute('aria-label', btn.title);
    btn.classList.toggle('active', on);
    // 尺寸变了要让高德重算；做两次是因为原生全屏的视口变化比 CSS 慢一拍
    if (map && typeof map.resize === 'function') {
      setTimeout(() => map.resize(), 60);
      setTimeout(() => map.resize(), 320);
    }
  }

  function enter() {
    mapEl.classList.add('map-fullscreen');
    document.body.classList.add('map-fullscreen-on');
    const req = document.documentElement.requestFullscreen
      || document.documentElement.webkitRequestFullscreen;
    if (req && !nativeOn()) {
      try {
        const r = req.call(document.documentElement);
        usedNative = true;
        if (r && r.catch) r.catch(() => { usedNative = false; });
      } catch (e) { usedNative = false; }
    }
    sync();
  }

  function exit() {
    mapEl.classList.remove('map-fullscreen');
    document.body.classList.remove('map-fullscreen-on');
    if (usedNative && nativeOn()) {
      const ex = document.exitFullscreen || document.webkitExitFullscreen;
      try { const r = ex.call(document); if (r && r.catch) r.catch(() => {}); } catch (e) {}
    }
    usedNative = false;
    sync();
  }

  btn.addEventListener('click', () => (isOn() ? exit() : enter()));

  // 用户用浏览器自己的方式退出原生全屏（Esc / 手势）时，CSS 全屏也跟着退出，状态不脱节
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(ev => {
    document.addEventListener(ev, () => {
      if (usedNative && !nativeOn() && isOn()) exit();
    });
  });

  // Esc 退出（原生全屏不可用时的兜底；灯箱开着时优先让灯箱吃掉这个键）
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !isOn()) return;
    if (!$('#lightbox').classList.contains('hidden')) return;
    exit();
  });

  sync();
})();

// ---------- 面板高度拖拽（移动端） ----------
// 兼容性说明：不依赖 setPointerCapture（微信内置浏览器/iOS<13.4/鸿蒙浏览器支持不完整），
// 按下后把 move/up 监听绑到 document，手指移出手柄仍持续跟踪；另加 touch 事件兜底。
// v=t 关键修复：start 阶段不 preventDefault（部分定制内核会因此抑制后续 move 事件派发，
// 导致拖拽完全无效）；另加「单击手柄=切换高度」降级方案，拖拽失效环境也能调面板。
(function initPanelResizer() {
  const resizer = document.querySelector('.panel-resizer');
  const mapEl = document.getElementById('map');
  if (!resizer || !mapEl) return;
  let dragging = false, startY = 0, startH = 0, startT = 0, moved = false;

  // 地图高度的可拖范围。下限 0：面板可以吃掉整个地图区，只剩顶栏+手柄，约占 9 成屏。
  // 地图归零后手柄仍在原位（紧贴顶栏下方），随时能拖回来。
  const MAP_MIN_VH = 0;
  const MAP_MAX_VH = 80;   // 地图最大 80vh（面板至少留 20vh，不占满全屏）
  // 单击手柄的三档循环：面板铺满 / 折中 / 地图优先
  const MAP_SNAP_VH = [0, 45, 72];

  function setMapHeight(h) {
    mapEl.style.height = Math.round(h) + 'px';
    saveTripState({ panelHeight: Math.round(h) });
    syncAddPointBar();   // 面板变矮到装不下加号时把它藏掉
    if (map && typeof map.resize === "function") setTimeout(() => map.resize(), 30);
  }
  function applyHeight(deltaY) {
    const vh = window.innerHeight / 100;
    let h = startH - deltaY;            // 向下拖=面板收起/地图变高（deltaY = startY - clientY，取反）
    h = Math.max(MAP_MIN_VH * vh, Math.min(MAP_MAX_VH * vh, h));
    setMapHeight(h);
  }
  function toggleHeight() {
    // 降级方案：单击手柄在三档之间循环（拖拽不可用的环境也能把面板拉到最高）
    const vh = window.innerHeight / 100;
    const cur = mapEl.getBoundingClientRect().height;
    // 找到比当前明显更大的下一档，已经到顶就回到最小档
    const next = MAP_SNAP_VH.find(v => v * vh > cur + 8 * vh);
    setMapHeight((next != null ? next : MAP_SNAP_VH[0]) * vh);
  }
  function onPointerMove(e) {
    if (!dragging) return;
    if (e.isPrimary === false) return;          // 多指里的非主指针，不参与拖拽
    if (Math.abs(startY - e.clientY) > 8) moved = true;
    if (e.cancelable) e.preventDefault();
    applyHeight(startY - e.clientY);
  }
  function onTouchMove(e) {
    if (!dragging || !e.touches || !e.touches.length) return;
    // 中途多了一根手指 = 用户在做双指捏合（缩放地图）。这里的监听挂在 document 上
    // 且会 preventDefault，不让开的话整个页面的捏合手势都会被掐掉——高德的地图
    // 缩放就是这么失效的。立刻结束拖拽并放行。
    if (e.touches.length > 1) { endDrag(true); return; }
    if (Math.abs(startY - e.touches[0].clientY) > 8) moved = true;
    if (e.cancelable) e.preventDefault();
    applyHeight(startY - e.touches[0].clientY);
  }
  function endDrag(aborted) {
    if (!dragging) return;
    dragging = false;
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', endDrag);
    document.removeEventListener('pointercancel', endDrag);
    document.removeEventListener('touchmove', onTouchMove);
    document.removeEventListener('touchend', endDrag);
    document.removeEventListener('touchcancel', endDrag);
    // 几乎没移动且快速松手 = 单击 → 切换高度（拖拽不可用的环境也能调面板）。
    // aborted=true 表示是被双指手势打断的，不算单击。
    if (!aborted && !moved && (Date.now() - startT) < 500) toggleHeight();
  }
  function startDrag(clientY) {
    if (dragging) return;
    dragging = true;
    moved = false;
    startY = clientY;
    startT = Date.now();
    startH = mapEl.getBoundingClientRect().height;
    document.addEventListener('pointermove', onPointerMove, { passive: false });
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);
    document.addEventListener('touchmove', onTouchMove, { passive: false });
    document.addEventListener('touchend', endDrag);
    document.addEventListener('touchcancel', endDrag);
  }
  // 注意：start 阶段不 preventDefault（鸿蒙/部分定制内核会因此抑制后续 move 事件派发）
  // 起手点落在收起按钮上时不启动拖拽，也不触发「单击手柄=切换高度」
  const onToggle = (e) => !!(e.target && e.target.closest && e.target.closest('.panel-toggle'));
  resizer.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (e.isPrimary === false) return;          // 双指的第二根手指，不当成拖拽
    if (onToggle(e)) return;
    startDrag(e.clientY);
  });
  resizer.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) return;   // 只认单指；多指交给地图缩放
    if (onToggle(e)) return;
    startDrag(e.touches[0].clientY);
  }, { passive: true });
  resizer.addEventListener('contextmenu', (e) => e.preventDefault());
})();

// 切换行程的过渡，两段式：
//   出场 beginTripSwitch(dir) —— 行程名/序号/统计条/下面板朝按键的**反**方向推出去并淡出，
//     停在末态；返回一个「出场已走完」的 Promise。调用方必须 await 它再改数据，
//     否则本地毫秒级加载会在旧内容还没滑走时就把文字换掉，看到的就成了「新名字在滑出」。
//   入场 endTripSwitch(dir) —— 新数据到位后从按键方向推回来。
// dir 为 'left'（按了 ›，内容从右边进）或 'right'（按了 ‹）。
const TRIP_ANIM_TARGETS = ['.trip-picker-name', '.trip-picker-pos', '.userbar', '.panel'];
const TRIP_ANIM_CLASSES = ['trip-out-l', 'trip-out-r', 'trip-in-l', 'trip-in-r'];
const TRIP_OUT_MS = 180;           // 与 CSS 里 .trip-out-* 的 .18s 保持一致
const TRIP_IN_MS = 340;            // 与 CSS 里 .trip-in-* 的 .34s 保持一致
function tripAnimTargets() {
  return TRIP_ANIM_TARGETS.map(s => document.querySelector(s)).filter(Boolean);
}
function tripReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
function beginTripSwitch(dir) {
  if (tripReducedMotion()) return Promise.resolve();
  const cls = dir === 'right' ? 'trip-out-r' : 'trip-out-l';
  const picker = document.querySelector('.trip-picker');
  if (picker) picker.classList.add('switching');
  tripAnimTargets().forEach(e => {
    e.classList.remove(...TRIP_ANIM_CLASSES);
    void e.offsetWidth;            // 强制重排，让同一个动画能连续触发
    e.classList.add(cls);
  });
  // 数据还没回来之前先让按下的那个箭头朝自己的方向弹一下，点击立刻有反馈
  const arrow = document.querySelector(dir === 'right' ? '#btn-trip-prev' : '#btn-trip-next');
  if (arrow) {
    const n = dir === 'right' ? 'nudge-prev' : 'nudge-next';
    arrow.classList.remove('nudge-prev', 'nudge-next');
    void arrow.offsetWidth;
    arrow.classList.add(n);
    arrow.addEventListener('animationend', () => arrow.classList.remove(n), { once: true });
  }
  return new Promise(r => setTimeout(r, TRIP_OUT_MS));
}
function endTripSwitch(dir) {
  const picker = document.querySelector('.trip-picker');
  if (picker) picker.classList.remove('switching');
  if (tripReducedMotion()) return;
  const cls = dir === 'right' ? 'trip-in-r' : 'trip-in-l';
  tripAnimTargets().forEach(e => {
    e.classList.remove(...TRIP_ANIM_CLASSES);
    void e.offsetWidth;
    e.classList.add(cls);
    // 入场动画的起点是 opacity:0，所以这个类**必须**摘掉，否则元素就停在全透明上。
    // animationend 不能全指望：页面切到后台、动画被下一次切换打断时它可能不来，
    // 所以再加一道定时兜底，两边谁先到都行（remove 幂等）。
    const done = () => e.classList.remove(cls);
    e.addEventListener('animationend', done, { once: true });
    setTimeout(done, TRIP_IN_MS + 150);
  });
}

let allTrips = [];   // 当前用户的全部行程，手机端切换条/卡片选择器共用

// 手机端顶部的「‹ 行程名 ›」切换条：同步名字、序号，以及两端箭头的可用状态
function syncTripNav() {
  const picker = $('#trip-picker');
  if (!picker) return;
  const i = allTrips.findIndex(t => Number(t.id) === Number(currentTripId));
  const cur = i >= 0 ? allTrips[i] : null;
  picker.querySelector('.trip-picker-name').textContent =
    cur ? cur.name : (allTrips.length ? '选择行程' : '暂无行程');
  picker.querySelector('.trip-picker-pos').textContent =
    allTrips.length > 1 && i >= 0 ? `${i + 1}/${allTrips.length}` : '';
  $('#btn-trip-prev').disabled = !(i > 0);
  $('#btn-trip-next').disabled = !(i >= 0 && i < allTrips.length - 1);
}

let tripSwitching = false;
async function stepTrip(delta) {
  if (tripSwitching) return;                 // 切换中忽略连点
  const i = allTrips.findIndex(t => Number(t.id) === Number(currentTripId));
  const next = allTrips[i + delta];
  if (!next) return;
  tripSwitching = true;
  $('#btn-trip-prev').disabled = true;
  $('#btn-trip-next').disabled = true;
  try {
    // 按 › 看下一个 → 内容从右边滑进来；按 ‹ 反之
    await selectTrip(next.id, delta > 0 ? 'left' : 'right');
  } finally {
    tripSwitching = false;
    syncTripNav();                           // 由它按新位置重算两端箭头的可用状态
  }
}

// 点行程名弹出卡片选择器：一屏看清每条行程的名字/日期/状态/规模，比原生下拉直观
function openTripSheet() {
  if (!allTrips.length) return;
  const mask = document.createElement('div');
  mask.className = 'more-sheet-mask';
  const sheet = document.createElement('div');
  sheet.className = 'more-sheet';
  const cards = allTrips.map((t, i) => {
    const st = tripStatusLabel(t.status);
    const date = t.start_date
      ? t.start_date + (t.end_date && t.end_date !== t.start_date ? ' ~ ' + t.end_date : '')
      : '未定日期';
    const isCur = Number(t.id) === Number(currentTripId);
    return `
      <button type="button" class="trip-sheet-card${isCur ? ' current' : ''}" data-id="${t.id}">
        <div class="trip-sheet-head">
          <span class="trip-sheet-name">${esc(t.name)}</span>
          ${st[0] ? `<span class="trip-sheet-tag ${st[1]}">${st[0]}</span>` : ''}
          ${isCur ? '<span class="trip-sheet-check">✓</span>' : ''}
        </div>
        <div class="trip-sheet-meta">${esc(date)} · 第 ${i + 1} / ${allTrips.length} 条</div>
      </button>`;
  }).join('');
  sheet.innerHTML =
    `<div class="more-sheet-handle"></div>
     <div class="more-sheet-title">选择行程（共 ${allTrips.length} 条）</div>
     ${cards}`;
  mask.appendChild(sheet);
  document.body.appendChild(mask);
  const close = () => mask.remove();
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
  sheet.querySelectorAll('.trip-sheet-card').forEach(btn => {
    btn.onclick = () => {
      const id = Number(btn.dataset.id);
      close();
      if (id === Number(currentTripId)) return;
      const from = allTrips.findIndex(t => Number(t.id) === Number(currentTripId));
      const to = allTrips.findIndex(t => Number(t.id) === id);
      selectTrip(id, to > from ? 'left' : 'right');
    };
  });
}

$('#trip-picker').addEventListener('click', openTripSheet);
$('#btn-trip-prev').addEventListener('click', () => stepTrip(-1));
$('#btn-trip-next').addEventListener('click', () => stepTrip(1));

// 在行程条上左右滑动也能切换行程（与 ‹ › 按钮同一套语义：左滑=下一个）。
// 判定沿用站内其他滑动的做法：轴锁定、位移阈值、双指忽略、两端不循环。
(function initTripNavSwipe() {
  const nav = document.getElementById('trip-nav');
  if (!nav) return;
  const H_MIN = 45;   // 触发切换的最小横向位移
  const LOCK = 10;    // 超过这个位移才判定方向
  let sx = 0, sy = 0, axis = '', active = false, swiped = false;

  nav.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { active = false; return; }
    active = true; axis = ''; swiped = false;
    sx = e.touches[0].clientX;
    sy = e.touches[0].clientY;
  }, { passive: true });

  nav.addEventListener('touchmove', (e) => {
    if (!active) return;
    if (e.touches.length !== 1) { active = false; return; }
    const dx = e.touches[0].clientX - sx;
    const dy = e.touches[0].clientY - sy;
    if (!axis) {
      if (Math.abs(dx) < LOCK && Math.abs(dy) < LOCK) return;
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (axis === 'y') { active = false; return; }   // 纵向不插手
    }
    if (axis === 'x') {
      swiped = true;
      if (e.cancelable) e.preventDefault();           // 压住浏览器的侧滑返回
    }
  }, { passive: false });

  nav.addEventListener('touchend', (e) => {
    if (!active || axis !== 'x') { active = false; return; }
    active = false;
    const t = e.changedTouches && e.changedTouches[0];
    if (!t) return;
    const dx = t.clientX - sx;
    if (Math.abs(dx) < H_MIN) return;                 // 位移不够，当没滑
    stepTrip(dx < 0 ? 1 : -1);                        // 左滑=下一个，右滑=上一个
  }, { passive: true });

  nav.addEventListener('touchcancel', () => { active = false; }, { passive: true });

  // 滑完之后浏览器可能仍补一次 click，会顺带弹出行程卡片选择器——捕获阶段拦掉
  nav.addEventListener('click', (e) => {
    if (!swiped) return;
    swiped = false;
    e.preventDefault();
    e.stopPropagation();
  }, true);
})();

// 行程切换
$('#trip-select').addEventListener('change', (e) => {
  if (e.target.value) selectTrip(Number(e.target.value));
});

// ---------- 灯箱事件 ----------
$('#lightbox-close').onclick = closeLightbox;
// ---------- 灯箱：滑动翻页 + 点击全屏放大（手机） ----------
// 两套手势共存，靠 lbZoom.on 分流：
//   轮播态：单指上下/左右滑 = 翻页（整条轨道跟手）；点一下图 = 进全屏放大态
//   放大态：双指捏合 = 缩放；单指拖 = 平移（放大后才有意义）；点一下图 = 回到轮播
// 双指捏合在轮播态下也直接生效——会先自动切进放大态，符合直觉。
let lbZoom = { on: false, scale: 1, tx: 0, ty: 0, fit: 1 };

(function initLightboxSwipe() {
  const box = document.getElementById('lightbox');
  if (!box) return;
  const MIN = 45;      // 轮播翻页的最小位移(px)
  const TAP = 10;      // 位移小于这个数就算「点了一下」
  const EDGE = 16;     // 放大态下顶到图片边缘后，再多拖这么多就转成翻页
  const MAX_SCALE = 4;
  let sx = 0, sy = 0, active = false, moved = false, axis = '';
  let mode = '';       // '' | 'swipe' | 'pan' | 'pinch'
  let pinchStartDist = 0, pinchStartScale = 1;
  let panStartX = 0, panStartY = 0;

  const dist = (e) => Math.hypot(
    e.touches[0].clientX - e.touches[1].clientX,
    e.touches[0].clientY - e.touches[1].clientY);

  // ——— 轮播态：整条轨道跟手 ———
  function drag(dx, dy) {
    const track = lightboxTrack;
    if (!track) return;
    track.classList.remove('settling');
    track.classList.toggle('axis-y', axis === 'y');
    track.classList.toggle('axis-x', axis !== 'y');
    let d = axis === 'y' ? dy : dx;
    const noNeighbour = (d < 0 && lightboxIndex >= lightboxMedia.length - 1) ||
                        (d > 0 && lightboxIndex <= 0);
    if (noNeighbour) d *= 0.28;
    track.style.transform = axis === 'y' ? `translateY(${d}px)` : `translateX(${d}px)`;
  }
  function springBack() {
    const track = lightboxTrack;
    if (!track) return;
    track.classList.add('settling');
    track.style.transform = '';
  }

  box.addEventListener('touchstart', (e) => {
    // 控件自动隐藏之后，第一下点画面只用来把它们唤回来，
    // 不该顺手把图片切进全屏放大态（见 common.js 的 tmLightboxIdleTapGuard）
    if (tmLightboxIdleTapGuard()) { active = false; return; }
    // 这些区域上起手一律不接管，交给它们自己的点击逻辑：
    // 关闭按钮 / 左右箭头 / 视频播放器 / 底部说明栏。
    // 说明栏尤其重要——之前漏了它，点「添加描述」「✎」会被这里当成「点一下图片」
    // 而进入全屏放大态，同时 touchend 的 preventDefault 还把合成 click 吃掉了，
    // 结果就是编辑入口完全点不动。
    if (e.target.closest && e.target.closest(
        '#lightbox-close, #lightbox-prev, #lightbox-next, video, .lb-caption, '
        + '.lb-quality-btn, .lb-quality-menu, '
        + '#lightbox-download, .lb-download-menu')) {
      active = false; return;
    }
    if (e.touches.length === 2) {
      // 双指：进入捏合缩放（必要时自动切到放大态）
      // preventDefault 必须赶在 touchstart 这一步：一旦浏览器把双指判成「整页缩放」
      // 交给合成器，后续 touchmove 就变成不可取消（e.cancelable === false），
      // 我们在 touchmove 里的 preventDefault 会被静默忽略，于是整个页面跟着放大——
      // 图片说明文字、关闭按钮全都变大，而不是只有图片在缩放。
      // viewport 里的 maximum-scale=1 现代浏览器出于无障碍考虑已不再遵守，挡不住这件事。
      if (e.cancelable) e.preventDefault();
      if (!lbZoom.on) lbEnterZoom();
      active = true; mode = 'pinch'; moved = true;
      pinchStartDist = dist(e);
      pinchStartScale = lbZoom.scale;
      return;
    }
    if (e.touches.length !== 1) { active = false; return; }
    active = true; moved = false; axis = '';
    sx = e.touches[0].clientX;
    sy = e.touches[0].clientY;
    if (lbZoom.on) {
      mode = 'pan';
      panStartX = lbZoom.tx; panStartY = lbZoom.ty;
    } else {
      mode = 'swipe';
    }
  }, { passive: false });   // 非被动：上面双指分支要 preventDefault

  box.addEventListener('touchmove', (e) => {
    if (!active) return;
    if (mode === 'pinch') {
      if (e.touches.length !== 2) return;
      if (e.cancelable) e.preventDefault();
      const ratio = dist(e) / (pinchStartDist || 1);
      lbZoom.scale = Math.min(MAX_SCALE, Math.max(0.6, pinchStartScale * ratio));
      lbClampPan();
      lbApplyZoom(false);
      return;
    }
    if (e.touches.length !== 1) { active = false; return; }
    const dx = e.touches[0].clientX - sx;
    const dy = e.touches[0].clientY - sy;
    if (mode === 'pan') {
      if (Math.abs(dx) > TAP || Math.abs(dy) > TAP) moved = true;
      if (e.cancelable) e.preventDefault();
      const m = lbCurrentMedia();
      const maxX = m ? Math.max(0, (m.offsetWidth * lbZoom.scale - window.innerWidth) / 2) : 0;
      const wantX = panStartX + dx;
      // 横向已经顶到图片左/右边缘还在继续拖 → 交棒给轮播翻页（安卓图库就是这个行为）。
      // 只在横向做这件事：竖向留给平移，否则高图上下看一半就被翻走了。
      const over = wantX > maxX ? wantX - maxX : (wantX < -maxX ? wantX + maxX : 0);
      if (over !== 0 && Math.abs(over) > EDGE && Math.abs(dx) > Math.abs(dy)) {
        mode = 'swipe';
        axis = 'x';
        sx = e.touches[0].clientX;   // 轮播位移从交棒这一刻重新起算
        sy = e.touches[0].clientY;
        drag(0, 0);
        return;
      }
      lbZoom.tx = wantX;
      lbZoom.ty = panStartY + dy;
      lbClampPan();
      lbApplyZoom(false);
      return;
    }
    // 轮播态
    if (!axis && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';   // 首次超过阈值时锁定轴向
    }
    if (axis) {
      moved = true;
      if (e.cancelable) e.preventDefault();   // 压住页面回弹/侧滑返回
      drag(dx, dy);
    }
  }, { passive: false });

  box.addEventListener('touchend', (e) => {
    if (!active) return;
    if (e.touches && e.touches.length > 0) return;   // 还有手指没抬，等全抬起来再判
    active = false;

    if (mode === 'pinch') {
      // 缩回 1 倍以下就当作「退出放大」，否则夹到 [1, MAX] 并归位
      if (lbZoom.scale <= 1.02) { lbExitZoom(true); }
      else {
        lbZoom.scale = Math.min(MAX_SCALE, lbZoom.scale);
        lbClampPan();
        lbApplyZoom(true);
      }
      mode = '';
      return;
    }

    const t = e.changedTouches && e.changedTouches[0];
    const dx = t ? t.clientX - sx : 0;
    const dy = t ? t.clientY - sy : 0;

    // 点一下（几乎没位移）：轮播 ↔ 全屏放大 来回切
    if (!moved && Math.abs(dx) < TAP && Math.abs(dy) < TAP) {
      if (e.cancelable) e.preventDefault();   // 顺手压掉随后的合成 click
      if (lbZoom.on) lbExitZoom(true); else lbEnterZoom();
      mode = '';
      return;
    }

    if (mode === 'pan') { mode = ''; return; }

    if (!t) { springBack(); mode = ''; return; }
    const d = axis === 'y' ? dy : dx;
    // 上/左 = 下一张，下/右 = 上一张。阈值取「45px 或四分之一屏」里小的那个
    const size = axis === 'y' ? window.innerHeight : window.innerWidth;
    const need = Math.min(MIN, size * 0.25);
    let turned = false;
    if (Math.abs(d) >= need) turned = lightboxNav(d < 0 ? 1 : -1, axis);
    if (!turned) springBack();
    mode = '';
  }, { passive: false });

  box.addEventListener('touchcancel', () => {
    active = false; mode = '';
    if (!lbZoom.on) springBack();
  }, { passive: true });
})();

$('#lightbox-prev').onclick = () => lightboxNav(-1);
$('#lightbox-next').onclick = () => lightboxNav(1);
$('#lightbox').addEventListener('click', (e) => {
  if (e.target === $('#lightbox')) closeLightbox();
});
document.addEventListener('keydown', (e) => {
  if ($('#lightbox').classList.contains('hidden')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft') lightboxNav(-1);
  else if (e.key === 'ArrowRight') lightboxNav(1);
});

// ---------- 轨迹回放事件 ----------
$('#replay-toggle').onclick = toggleReplay;
$('#replay-launch').onclick = () => openReplayBar();    // 恢复回放条（未播过才从头播，已初始化只恢复）
$('#replay-close').onclick = () => minimizeReplay();    // 关闭=仅最小化，不中断正在播放的轨迹
const _spdBtn = $('#replay-speed');
if (_spdBtn) {
  _spdBtn.onclick = () => {
    const speeds = [1, 2, 4, 8];
    const cur = speeds.indexOf(replaySpeed);
    replaySpeed = speeds[(cur + 1) % speeds.length];
    _spdBtn.textContent = replaySpeed + 'x';
  };
}

// ---------- 我的位置 ----------
// 和各家地图 App 一样：蓝点 + 精度圈，点按钮回到自己的位置。
// 定位优先走 AMap.Geolocation：国内浏览器拿到的原始坐标可能是 WGS-84 也可能已经是
// GCJ-02（各家内核不一致），自己转换很容易多偏或少偏几百米；高德这个插件直接给
// 和地图同一坐标系的结果，还自带 IP 兜底。插件不可用时才退回浏览器原生定位 + 手工转换。
let myLocMarker = null, myLocCircle = null, myLocLngLat = null, locating = false;
let myLocVia = '';   // 这次定位是高德插件('amap')还是浏览器原生('browser')给的

// 高德定位插件。resolve 的坐标已经是 GCJ-02，可以直接喂给地图。
let _amapGeo = null;

(function initLocate() {
  const b = document.getElementById('btn-locate');
  if (b) b.addEventListener('click', goToMyLocation);
})();

// ---------- 图层控制（景区 / 卫星 / 路况） ----------
let layerSat = null, layerRoad = null, layerTraffic = null;
let poiMarkers = [], poiSearch = null, poiTimer = null;

// 足迹点亮（所有去过的地方，对标 Fog of World 迷雾解锁）
// 高德 JS API 2.0 已废弃 AMap.Heatmap（构造实例无方法），改用 CircleMarker 半透明
// 光晕 + 实心点叠加，跨 1.4/2.0 兼容，无需额外插件。
// 整级取整就够了：网格大小按 2^zoom 变，半级的差别肉眼看不出来，
// 却会让每次轻微缩放都多打一趟请求。
function footprintZoomBucket() {
  return Math.round((map && map.getZoom && map.getZoom()) || 10);
}

// 地图当前可视范围，`minLng,minLat,maxLng,maxLat`。
// 服务端只聚合这一屏里的点——不带这个，放大地图时全世界的点会一起争
// 那几千个格子上限，格子被迫越聚越粗，放大了反而看不到细节。
function footprintBBox() {
  try {
    const b = map && map.getBounds && map.getBounds();
    if (!b) return '';
    const sw = b.getSouthWest(), ne = b.getNorthEast();
    if (!sw || !ne) return '';
    return [sw.lng, sw.lat, ne.lng, ne.lat].map(v => v.toFixed(5)).join(',');
  } catch (e) {
    return '';
  }
}

function footprintViewKey(zoom) {
  return zoom + '|' + footprintBBox();
}

async function loadFootprint(zoom) {
  const box = footprintBBox();
  const key = zoom + '|' + box;
  let url = '/api/footprint?zoom=' + zoom;
  if (box) url += '&bbox=' + encodeURIComponent(box);
  try {
    const res = await api(url);
    footprintData = (res && res.data) || [];
    footprintAggregated = !!(res && res.aggregated);
    footprintView = key;
  } catch (e) {
    footprintData = [];
    footprintAggregated = false;
    footprintView = key;
  }
}

// 每个足迹点画「光晕 + 实心」双层 CircleMarker，重复到访（count）越大越亮
function drawFootprint() {
  if (footprintMarkers.length) { map.remove(footprintMarkers); footprintMarkers = []; }
  (footprintData || []).forEach(d => {
    const cnt = d.count || 1;
    // 聚合之后 count 是「这一格里的到访次数」，点多的时候能轻松上百，
    // 线性放大会糊成一大坨。开方压一下，既保留强弱对比又不会越界。
    const w = footprintAggregated ? Math.sqrt(cnt) : cnt;
    footprintMarkers.push(new AMap.CircleMarker({
      center: [d.lng, d.lat],
      radius: 20 + (w - 1) * 6,
      fillColor: '#f97316', fillOpacity: 0.16,
      strokeWeight: 0, zIndex: 8,
    }));
    footprintMarkers.push(new AMap.CircleMarker({
      center: [d.lng, d.lat],
      radius: 7 + (w - 1) * 3,
      fillColor: '#ef4444', fillOpacity: 0.55,
      strokeColor: '#fff', strokeWeight: 1, strokeOpacity: 1,
      zIndex: 9,
    }));
  });
  map.add(footprintMarkers);
}

async function toggleLayerHeatmap(on) {
  const cb = $('#lay-heatmap');
  if (on) {
    if (footprintMarkers.length) { map.add(footprintMarkers); return; }
    // 1. 拉全量足迹（跨行程，缓存）
    if (!footprintData) await loadFootprint(footprintZoomBucket());
    if (!footprintData || !footprintData.length) {
      tmToast('还没有足迹数据，先添加一些地点吧');
      cb.checked = false;
      return;
    }
    // 2. 画出来
    drawFootprint();
  } else {
    map.remove(footprintMarkers);
  }
}

// 视野（缩放或平移）变了就按新的范围重聚一次 —— 只在服务端确实做了聚合时才需要。
// 点没到阈值（aggregated=false）时这里一次请求都不会多打。
let _footprintReloadTimer = null;
function onFootprintViewChanged() {
  const cb = $('#lay-heatmap');
  if (!cb || !cb.checked || !footprintAggregated) return;
  const z = footprintZoomBucket();
  if (footprintViewKey(z) === footprintView) return;
  // 连续缩放（滚轮、双指）和拖动会连着触发，等停下来再拉
  clearTimeout(_footprintReloadTimer);
  _footprintReloadTimer = setTimeout(async () => {
    if (!cb.checked) return;
    const want = footprintZoomBucket();
    if (footprintViewKey(want) === footprintView) return;
    await loadFootprint(want);
    if (cb.checked) drawFootprint();
  }, 300);
}

// 照片图层：各点照片缩略图，多图堆叠显示，可关闭
// 照片图层开着的时候，轨迹点旁边那个 📷 角标就是重复信息（同一批照片、同一个入口），
// 直接藏掉；关掉照片图层再放回来。marker 本身保留在地图上，只切显隐。
// 照片图层的照片堆按地图缩放级别缩放，分两段：
//   z 3→14：0.45 倍长到 1 倍。视野大时缩小，免得几张照片糊成一片、盖住旁边的轨迹点。
//   z 14→18：1 倍继续长到 2 倍。放到街道级说明在细看某一处，照片就该更大更好认。
//   （这张地图的缩放范围是 getZooms() = [2, 18]，18 就是最大，所以上限定在 z=18）

function syncPhotoMarkersVisibility() {
  // 用 CSS 类控制，不用 marker.hide()/show()：实测高德在 map.add/remove 之后的
  // 内部重绘会把 show() 的效果盖掉，角标再也回不来。挂在 #map 上还有个好处——
  // 后续重画地图新建出来的角标自动跟随当前状态，不用额外同步。
  const on = !!($('#lay-photos') && $('#lay-photos').checked);
  const el = document.getElementById('map');
  if (el) el.classList.toggle('photo-layer-on', on);
}

// ---------- 方案 B：自己画一层路线，压在照片堆之上 ----------
// 为什么要自己画：高德把 DOM marker 全塞进一个 z-index:300 的容器，它整体压在
// 路线层之上，所以照片堆无论如何都盖着线。我把照片堆挪进自建层（z=100）之后，
// 是否压得住线仍取决于高德内部的层级数值 —— 换个环境就可能不成立。
// 自己再画一条线放在 z=105，两层都归我管，顺序由我的 CSS 说了算，不再看高德脸色。
// 高德原来那条线设成透明（仍可点击，路段选择功能不受影响）。
//
// 性能是这个方案的关键。实测独库公路一条路线有 14683 个顶点，
// 逐点调 map.lngLatToContainer 要 19.9ms —— 每帧这么干必卡。两个优化：
//   1) 顶点先算好 Web Mercator 坐标（与缩放/平移无关，只算一次）；
//      重画时用 3 个参考点解出仿射变换，每个点只剩两次乘加。
//   2) 抽稀：投影后与上一个点不足 1.2px 的直接丢掉，路径字符串从上万点降到几百点。
let routeSvgLayer = null;
let routeSegGeom = [];      // [{i, pts:[[mx,my],...]}]，mx/my 是 Web Mercator
let routePathEls = [];
let routeDrawRaf = 0;
let routeOverlayOn = false;

// 用 3 个参考点解 Mercator -> 屏幕的仿射变换。
// 地图有俯仰角时投影不再是仿射的，这时返回 null，调用方退回高德自己的线。
function routeAffine() {
  if (!map || !map.lngLatToContainer) return null;
  const c = map.getCenter();
  const d = 0.01 / Math.pow(2, Math.max(0, map.getZoom() - 10));
  const refs = [[c.getLng(), c.getLat()],
                [c.getLng() + d, c.getLat()],
                [c.getLng(), c.getLat() + d]];
  const M = [], P = [];
  for (const [lng, lat] of refs) {
    M.push(mercatorXY(lng, lat));
    const p = map.lngLatToContainer(new AMap.LngLat(lng, lat));
    P.push([p.x, p.y]);
  }
  // 解 [x] = a*mx + b*my + c ; [y] = d*mx + e*my + f
  const x1 = M[0][0], y1 = M[0][1], x2 = M[1][0], y2 = M[1][1], x3 = M[2][0], y3 = M[2][1];
  const det = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1);
  if (!det) return null;
  const sol = (u1, u2, u3) => {
    const a = ((u2 - u1) * (y3 - y1) - (u3 - u1) * (y2 - y1)) / det;
    const b = ((u3 - u1) * (x2 - x1) - (u2 - u1) * (x3 - x1)) / det;
    return [a, b, u1 - a * x1 - b * y1];
  };
  const A = sol(P[0][0], P[1][0], P[2][0]);
  const B = sol(P[0][1], P[1][1], P[2][1]);
  // 拿第 4 个点验一下：误差大说明不是仿射（有俯仰），别硬画
  const t = [c.getLng() + d * 2, c.getLat() + d * 2];
  const [tm0, tm1] = mercatorXY(t[0], t[1]);
  const want = map.lngLatToContainer(new AMap.LngLat(t[0], t[1]));
  const gx = A[0] * tm0 + A[1] * tm1 + A[2], gy = B[0] * tm0 + B[1] * tm1 + B[2];
  if (Math.hypot(gx - want.x, gy - want.y) > 2) return null;
  return { A, B };
}

// 从高德的折线里取出几何，转成 Mercator 存好；同时把高德那条线隐藏
function buildRouteOverlay() {
  routeSegGeom = [];
  if (!segPolylines.length) { drawRouteOverlay(); return; }
  segPolylines.forEach((sp, i) => {
    const path = sp.getPath() || [];
    const pts = [];
    for (const v of path) {
      const lng = Array.isArray(v) ? v[0] : v.lng;
      const lat = Array.isArray(v) ? v[1] : v.lat;
      pts.push(mercatorXY(lng, lat));
    }
    if (pts.length >= 2) routeSegGeom.push({ i: i, pts: pts });
  });
  setAmapRouteVisible(false);
  drawRouteOverlay();
}

// 高德原线的显隐。隐藏时保留极低的不透明度而不是 0 —— 完全透明后点击热区在
// 部分版本上会失效，路段选择功能就没了。
function setAmapRouteVisible(vis) {
  routeOverlayOn = !vis;
  segPolylines.forEach(sp => {
    const o = sp.getOptions ? sp.getOptions() : {};
    sp.setOptions({ strokeOpacity: vis ? (o._tmOpacity || 1) : 0.01 });
  });
  const el = document.getElementById('map');
  if (el) el.classList.toggle('route-overlay-on', !vis);
}

// 当前每段该用什么颜色/粗细：跟高德那条线的高亮状态保持一致
function routeSegStyle(i) {
  const idx = currentPointId
    ? (lastPoints || []).findIndex(p => String(p.id) === String(currentPointId)) : -1;
  if (idx >= 0 && i === idx - 1) return ['#16a34a', 6];   // 来时路
  if (idx >= 0 && i === idx) return ['#f97316', 6];       // 即将去
  return ['#2563eb', 4];
}

function drawRouteOverlay() {
  const svg = ensureRouteSvgLayer();
  if (!svg) return;
  if (!routeSegGeom.length) { svg.innerHTML = ''; routePathEls = []; return; }
  const aff = routeAffine();
  if (!aff) {                    // 俯仰状态：交还给高德自己画
    svg.innerHTML = ''; routePathEls = [];
    if (routeOverlayOn) setAmapRouteVisible(true);
    return;
  }
  if (!routeOverlayOn) setAmapRouteVisible(false);
  const { A, B } = aff;
  const W = svg.clientWidth || 400, H = svg.clientHeight || 800;
  const MARGIN = 200;            // 视野外留一点余量，避免线头在边缘断掉
  while (routePathEls.length < routeSegGeom.length) {
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke-linejoin', 'round');
    p.setAttribute('stroke-linecap', 'round');
    svg.appendChild(p);
    // 配套一条看不见的粗线，只用来做「点到线没有」的几何判定（isPointInStroke
    // 按元素自身的 stroke-width 算，可见线只有 4px，手指点不准）
    const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    hit.setAttribute('fill', 'none');
    hit.setAttribute('stroke', 'transparent');
    hit.setAttribute('stroke-width', '18');
    hit.setAttribute('stroke-linejoin', 'round');
    hit.setAttribute('stroke-linecap', 'round');
    svg.appendChild(hit);
    p._hit = hit;
    routePathEls.push(p);
  }
  while (routePathEls.length > routeSegGeom.length) {
    const p = routePathEls.pop();
    if (p._hit) p._hit.remove();
    p.remove();
  }

  for (let s = 0; s < routeSegGeom.length; s++) {
    const seg = routeSegGeom[s], pts = seg.pts;
    let d = '', lx = -1e9, ly = -1e9, started = false;
    for (let k = 0; k < pts.length; k++) {
      const m = pts[k];
      const x = A[0] * m[0] + A[1] * m[1] + A[2];
      const y = B[0] * m[0] + B[1] * m[1] + B[2];
      const last = (k === pts.length - 1);
      // 抽稀：和上一个落点不足 1.2px 的丢掉（首尾必留，否则线会缺头少尾）
      if (started && !last && Math.abs(x - lx) < 1.2 && Math.abs(y - ly) < 1.2) continue;
      // 视野外太远的点也没必要进路径
      if (started && !last &&
          (x < -MARGIN || x > W + MARGIN || y < -MARGIN || y > H + MARGIN) &&
          (lx < -MARGIN || lx > W + MARGIN || ly < -MARGIN || ly > H + MARGIN)) {
        lx = x; ly = y; continue;
      }
      d += (started ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
      lx = x; ly = y; started = true;
    }
    const [color, width] = routeSegStyle(seg.i);
    const el = routePathEls[s];
    el.setAttribute('d', d);
    el.setAttribute('stroke', color);
    el.setAttribute('stroke-width', String(width));
    el._segIndex = seg.i;
    if (el._hit) el._hit.setAttribute('d', d);
  }
}

// 地图动就重画。用 rAF 合并，一帧最多画一次。
function scheduleRouteDraw() {
  if (routeDrawRaf) return;
  routeDrawRaf = requestAnimationFrame(() => {
    routeDrawRaf = 0;
    drawRouteOverlay();
    // 照片堆/角标、📷 角标也一起放到这一帧里挪。
    // 它们原来各自直接挂 mapmove，于是一次地图移动触发的执行顺序是
    // 「高德渲染 → 照片堆 → 角标 → 自绘路线」，中间夹着高德自己那趟耗时渲染；
    // 三条路线混在一起容易掉帧，合到同一个 rAF 里省掉一层开销。
    // 角标必须排在照片堆后面，理由见下面 clearMap 之后那次重排的注释。
    positionPhotoStacks();
    positionPhotoBadges();
  });
}
(function initRouteOverlaySync() {
  const bind = () => {
    if (!map || !map.on) return setTimeout(bind, 200);
    ['mapmove', 'zoomchange', 'movend', 'zoomend', 'resize', 'rotatechange', 'pitchchange']
      .forEach(ev => { try { map.on(ev, scheduleRouteDraw); } catch (e) {} });
  };
  bind();
})();

// ---------- 照片堆的独立图层 ----------
// 为什么不用 AMap.Marker：高德把所有 DOM marker 放进同一个 .amap-markers 容器，
// 那个容器的 z-index 是 300，而折线所在的矢量覆盖物层是 110。容器整体压在折线之上，
// 于是 marker 自己的 zIndex 再怎么调都只是在容器**内部**排序，永远盖着路线。
// 解法：自己在 .amap-layers 里插一个容器，z-index 放在瓦片(2)和折线(110)之间，
// 照片堆放进去，位置每帧按经纬度换算。层级关系就变成：
//     瓦片(2) < 照片堆(100) < 路线(110) < 轨迹点/里程标签(300 容器内)
let photoStackLayer = null;      // 自建容器
let photoStackItems = [];        // [{el, lng, lat}]
let photoBadgeItems = [];        // 点旁的 📷 角标，[{el, lng, lat, dx, dy}]
let photoLayerRetry = 0;         // 地图图层树未就绪时的重试计数
(function initMapDiag() {
  if (!/[?&]diag=map/.test(location.search)) return;
  const box = document.createElement('pre');
  box.className = 'map-diag';
  box.style.cssText = 'position:fixed;right:6px;bottom:6px;z-index:99999;margin:0;' +
    'background:rgba(0,0,0,.82);color:#0f0;font:10px/1.45 monospace;padding:8px 10px;' +
    'border-radius:8px;max-width:74vw;white-space:pre;pointer-events:none';
  document.body.appendChild(box);
  const tick = () => { try { box.textContent = mapLayerDiag(); } catch (e) { box.textContent = 'diag err'; } };
  setInterval(tick, 1500); setTimeout(tick, 1200);
  // 顺便把结构报回服务端：截图经常传不出去，让页面自己说更靠谱。
  // 只报图层名和 z-index，不含任何用户数据。
  const report = () => {
    try {
      const stacks = [...document.querySelectorAll('.photo-stack')].slice(0, 3).map(s => {
        const r = s.getBoundingClientRect();
        return { w: Math.round(r.width), x: Math.round(r.x), y: Math.round(r.y),
                 par: String(s.parentElement && s.parentElement.className).slice(0, 30) };
      });
      const host = document.querySelector('#map .amap-layers');
      const layers = host ? [...host.children].map(c2 => ({
        z: getComputedStyle(c2).zIndex,
        cls: String(c2.className).slice(0, 40),
        kid: [...c2.children].map(k => k.tagName.toLowerCase()).slice(0, 3),
      })) : null;
      const lyr = document.querySelector('.photo-stack-layer');
      fetch('/api/diag/map', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ver: (([...document.scripts].map(s => s.src).find(s => /app\.js/.test(s)) || '').split('?v=')[1] || '?'),
          amap: (window.AMap && (AMap.version || AMap.v)) || '?',
          canvas: document.querySelectorAll('#map canvas').length,
          svg: document.querySelectorAll('#map svg').length,
          zoom: (map && map.getZoom && map.getZoom()) || null,
          hasHost: !!host,
          layers: layers,
          stackLayer: lyr ? { z: getComputedStyle(lyr).zIndex,
                              par: String(lyr.parentElement.className || lyr.parentElement.id).slice(0, 30),
                              cls: String(lyr.className) } : null,
          stacks: stacks,
          layerOn: document.getElementById('map').classList.contains('photo-layer-on'),
          dpr: window.devicePixelRatio,
        }),
      }).then(() => { box.textContent += '\n[已上报]'; })
        .catch(() => { box.textContent += '\n[上报失败]'; });
    } catch (e) {}
  };
  setTimeout(report, 4000);
  setTimeout(report, 12000);   // 再报一次，覆盖「地图晚就绪」的情况
})();

// 照片堆现在在折线层**之下**，而高德的矢量覆盖物层（SVG，pointer-events: auto）
// 整片盖在上面，命中测试永远落不到照片堆上 —— 实测在堆的范围里取样 64 个点，
// 命中堆 0 个、命中 SVG 层 40 个。所以点击要自己接：在 #map 的捕获阶段做一次
// 矩形命中判定，落在某个堆里就当成点了这个堆。
(function initPhotoStackClick() {
  // 命中判定 + 触摸兜底都在 common.js 的 tmBindMapPhotoTap 里（分享页共用一份）。
  // 没命中照片堆/角标时，主界面还要接着判「是不是点在路线上」。
  tmBindMapPhotoTap(document.getElementById('map'), hitRouteAtPoint);
})();

// 点到自绘的路线上了吗？高德那条线被我压成 0.01 不透明度后就不再响应点击了，
// 路段选择功能得自己接回来。用 SVG 的 isPointInStroke 做精确几何判定，
// 判定用的是那条 18px 宽的隐形粗线，手指点得准。
function hitRouteAtPoint(e) {
  const svg = routeSvgLayer;
  if (!svg || !routePathEls.length || typeof showSegmentPanel !== 'function') return false;
  const box = svg.getBoundingClientRect();
  let pt;
  try {
    pt = svg.createSVGPoint();
  } catch (err) { return false; }
  pt.x = e.clientX - box.left;
  pt.y = e.clientY - box.top;
  for (const p of routePathEls) {
    const hit = p._hit || p;
    try {
      if (hit.isPointInStroke && hit.isPointInStroke(pt)) {
        e.stopPropagation();
        e.preventDefault();
        showSegmentPanel(p._segIndex);
        return true;
      }
    } catch (err) {}
  }
  return false;
}

// 照片堆/角标的重排已经并进 scheduleRouteDraw 那个 rAF 里了
// （见上面 initRouteOverlaySync），这里不再单独挂 mapmove —— 一个事件干两趟，
// 而且是不同的帧，反而更容易掉。

async function toggleLayerPhotos(on, silent) {
  const cb = $('#lay-photos');
  if (on) {
    if (photoStackItems.length) { positionPhotoStacks(); syncPhotoMarkersVisibility(); return; }
    // 1. 拉**当前行程**的照片点（换行程时 clearMap 会把缓存清掉，这里自然重取）
    if (!photoLayerData) {
      try {
        const q = currentTripId ? ('?trip_id=' + encodeURIComponent(currentTripId)) : '';
        const res = await api('/api/photo-layer' + q);
        photoLayerData = (res && res.data) || [];
      } catch (e) {
        photoLayerData = [];
      }
    }
    if (!photoLayerData || !photoLayerData.length) {
      if (!silent) {
        // 用户是手动勾的，得给个说法，并把勾选回弹
        tmToast('本行程还没有照片或视频，先在某个地点上传吧');
        cb.checked = false;
      }
      // silent（换行程后自动重建）时**不要**取消勾选：
      // 路过一个没照片的行程，不该把图层给永久关掉，切回有照片的行程还得再勾一次。
      syncPhotoMarkersVisibility();
      return;
    }
    // 2. 空间去重：坐标非常接近的点做小幅对角错开，避免完全重叠
    const layerBox = ensurePhotoStackLayer();
    // 地图图层树还没建好：现在画会挂错地方，等一下重试（最多等 ~6 秒）
    if (!layerBox) {
      photoLayerRetry = (photoLayerRetry || 0) + 1;
      if (photoLayerRetry <= 12) setTimeout(() => toggleLayerPhotos(true, true), 500);
      return;
    }
    photoLayerRetry = 0;
    clearPhotoStackLayer();
    const seen = {};
    photoLayerData.forEach(pt => {
      const key = pt.lng.toFixed(4) + ',' + pt.lat.toFixed(4);
      const k = seen[key] = (seen[key] || 0) + 1;
      let lng = pt.lng, lat = pt.lat;
      if (k > 1) { lng += (k - 1) * 0.00018; lat -= (k - 1) * 0.00012; }
      // 3. 构建照片堆 HTML（最多叠 3 张，超出加计数角标）
      const photos = pt.photos || [];
      const stackN = Math.min(photos.length, 3);
      let inner = '';
      for (let i = stackN - 1; i >= 0; i--) {
        const rot = (i - (stackN - 1)) * 7;   // 最上层 0deg，越往下越斜
        const off = (i - (stackN - 1)) * -9;  // 向下堆叠偏移
        inner += '<img src="' + (photos[i].thumb_url
                    || mediaUrl('/uploads/thumbs/' + photos[i].thumb)) + '" ' +
                 'style="transform: translate(' + off + 'px,' + (-off) + 'px) rotate(' + rot + 'deg); z-index:' + (10 + i) + ';">';
      }
      // 视频也用首帧缩略图参与堆叠（照片和视频在同一个点的媒体列表里，混着看才不丢东西），
      // 但视频必须在最上层打 ▶：缩略图看不出动静，不打标会被当成照片。
      const hasVideo = photos.some(m => m.type === 'video');
      if (hasVideo) inner += '<span class="photo-stack-play">▶</span>';
      // 角标就是「这个点一共多少张」。原来 >3 时前面多加了个 '+'（显示成 "+5"），
      // 那个加号本意是「还有 N 张没露出来」，但用的是总数，两头不靠，只会让人误读。
      // 现在统一显示真实数量，超过 99 才截成 99+（和点旁边 📷 角标的规则一致）。
      const badge = photos.length > 1
        ? '<span class="photo-stack-badge">' +
          (photos.length > 99 ? '99+' : photos.length) + '</span>'
        : '';
      // DOM 直绑点击（2026-08-26）：不依赖 AMap 事件系统，兼容个别内核；
      // 与 AMap click 双保险，500ms 内去重避免重复打开
      const stackEl = document.createElement('div');
      stackEl.className = 'photo-stack';
      stackEl.innerHTML = inner + badge;
      stackEl.__pid = pt.point_id;
      const openStack = function () {
        const now = Date.now();
        if (stackEl.__last && now - stackEl.__last < 500) return;
        stackEl.__last = now;
        openPointPhotos(pt.point_id);
      };
      stackEl.addEventListener('click', openStack);
      // 放进自建图层，而不是 AMap.Marker —— 见 ensurePhotoStackLayer 的说明
      if (layerBox) {
        layerBox.appendChild(stackEl);
        photoStackItems.push({ el: stackEl, lng: lng, lat: lat });
      }
    });
    positionPhotoStacks();
  } else {
    clearPhotoStackLayer();
  }
  syncPhotoMarkersVisibility();
  syncPhotoStackScale();
}

// 图层面板交互
function initLayerControls() {
  const btn = $('#btn-layers'), panel = $('#layer-panel');
  if (!btn || !panel) return;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    panel.classList.toggle('hidden');
  });
  // 点面板外面就关掉。除了 click，还要监听 pointerdown / touchstart：
  // 地图是高德自己的画布，手机上点地图常常不会冒泡出 click（触摸被地图吃掉），
  // 只绑 click 的话就变成「必须再点一次图层按钮才能关」。用捕获阶段确保收得到。
  const closeIfOutside = function (e) {
    if (panel.classList.contains('hidden')) return;
    const t = e.target;
    if (!panel.contains(t) && t !== btn && !btn.contains(t)) {
      panel.classList.add('hidden');
    }
  };
  document.addEventListener('click', closeIfOutside);
  document.addEventListener('pointerdown', closeIfOutside, true);
  document.addEventListener('touchstart', closeIfOutside, { capture: true, passive: true });
  $('#lay-poi').addEventListener('change', function () { toggleLayerPoi(this.checked); saveLayerState({ poi: this.checked }); });
  $('#lay-satellite').addEventListener('change', function () { toggleLayerSatellite(this.checked); saveLayerState({ satellite: this.checked }); });
  $('#lay-traffic').addEventListener('change', function () { toggleLayerTraffic(this.checked); saveLayerState({ traffic: this.checked }); });
  $('#lay-heatmap').addEventListener('change', function () { toggleLayerHeatmap(this.checked); saveLayerState({ heatmap: this.checked }); });
  $('#lay-photos').addEventListener('change', function () { toggleLayerPhotos(this.checked); saveLayerState({ photos: this.checked }); });
  $('#lay-mileage').addEventListener('change', function () { applySegLabelsVisibility(); saveLayerState({ mileage: this.checked }); });
  $('#lay-road').addEventListener('change', function () { applyRoadLabelsVisibility(); saveLayerState({ road: this.checked }); });
}

// 按「最上层优先」依次尝试关闭一层，关掉了返回 true。
function closeTopLayer() {
  const q = (s) => document.querySelector(s);
  const shown = (el) => el && !el.classList.contains('hidden');

  // 1. 自绘对话框（confirm/alert/prompt）：等同于点「取消」
  const dlg = q('.tm-dialog-mask');
  if (dlg) {
    const btn = dlg.querySelector('.tm-dialog-cancel') || dlg.querySelector('.tm-dialog-ok');
    if (btn) btn.click(); else dlg.remove();
    return true;
  }
  // 2. 长按保存图片的覆盖层
  const lp = q('.longpress-mask');
  if (lp) { lp.remove(); return true; }
  // 3. 底部抽屉（更多菜单 / 行程卡片选择器）
  const sheet = q('.more-sheet-mask');
  if (sheet) { sheet.remove(); return true; }
  // 4. 灯箱：先退出全屏放大态，再关灯箱
  if (shown(q('#lightbox'))) {
    if (typeof lbZoom !== 'undefined' && lbZoom.on) { lbExitZoom(true); return true; }
    closeLightbox();
    return true;
  }
  // 5. 分享卡片 / 各类模态框
  const card = q('#share-card-modal');
  if (card) { card.remove(); return true; }
  if (shown(q('#stats-modal'))) { q('#stats-modal').classList.add('hidden'); return true; }
  if (shown(q('#modal'))) { closeModal(); return true; }
  if (shown(q('#route-chooser'))) { clearRouteCandidates(); q('#route-chooser').classList.add('hidden'); return true; }
  // 6. 地图全屏
  const mapEl = q('#map');
  if (mapEl && mapEl.classList.contains('map-fullscreen')) {
    const b = q('#btn-map-fullscreen');
    if (b) b.click();
    return true;
  }
  // 7. 图层面板
  if (shown(q('#layer-panel'))) { q('#layer-panel').classList.add('hidden'); return true; }
  return false;
}

// ---------- 浏览器返回拦截 ----------
// 手机上从屏幕边缘右滑 = 后退，本来会直接退出整个网站，体验很差。
// 做法：用 History API 在栈顶压若干「哨兵」条目，把每一次「返回」都吃掉——
//   有浮层（对话框 / 抽屉 / 灯箱 / 地图全屏 …）就关掉最上面那层；
//   没有浮层就什么都不做，只给一句轻提示。返回一律不会退出本站。
// 压一整摞（DEPTH 条）而不是 1 条：手机边缘连续快滑时，浏览器可能在我们补上
// 新哨兵之前就连吃好几条，留足余量才不会被一次穿透。
(function initBackGuard() {
  const DEPTH = 8;
  let lastHint = 0;

  // 每条哨兵都带自己的序号，这样「现在还剩几条」可以直接从 history.state 读出来，
  // 而不是靠计数器猜。连续快滑时浏览器可能一次性回退好几步却只派发一个 popstate，
  // 计数器会算错、补不够，序号则怎么跳都能自我校正。
  function level() {
    const s = history.state;
    return (s && s.tmBackGuard && typeof s.i === 'number') ? s.i : -1;
  }
  function fill() {
    for (let i = level() + 1; i < DEPTH; i++) {
      try { history.pushState({ tmBackGuard: true, i: i }, '', location.href); }
      catch (e) { break; }
    }
  }
  fill();

  window.addEventListener('popstate', function () {
    if (closeTopLayer()) { fill(); return; }      // 关掉一层，这次返回就当成「关闭当前层」
    // 没有可关的层也不放行：本站是单页应用，退出请用退出登录或直接关标签页。
    const now = Date.now();
    if (now - lastHint > 2500) {                  // 连滑几次只提示一次，不刷屏
      lastHint = now;
      tmToast('已停留在当前页面');
    }
    fill();
  });

  // 从 bfcache 恢复回来时哨兵可能已经不在了（页面被整体挂起过），重新补满
  window.addEventListener('pageshow', function (e) { if (e.persisted) fill(); });
})();

// ---------- 启动 ----------
initMap();
initLayerControls();
restorePanelState();
loadPlacesDict();
$('#points-view').addEventListener('scroll', savePointListScroll, { passive: true });
positionViews(currentTab);   // 首屏先把三页在轨道上摆好（不带过渡）
syncTabIndicator();
ensureAuth().then(ok => {
  loadTrips();
  // 登录成功后才恢复图层勾选与灯箱（未登录时恢复会触发接口报错/弹窗）
  if (ok) { restoreLayerState(); restoreLightboxState(); }
});
