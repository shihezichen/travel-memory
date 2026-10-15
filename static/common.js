// 旅行记忆系统 — 主应用与分享页的公共前端逻辑
//
// 这里只放【在 app.js 与 share.js 中曾经逐字节完全相同】的顶层函数。
// 由 static/app.js / static/share.js 机械抽取而来，函数体未做任何改写。
// index.html / share.html 都在各自的 app.js / share.js 之前引入本文件。
//
// 收录标准（三条全部满足才进来）：
//   1. 两个文件里的函数体逐字节相同；
//   2. 它引用的每一个自由标识符在两个文件中都有定义（没有单边全局依赖）；
//   3. 它（含传递调用链）不依赖任何“同名但实现不同”的函数
//      —— 那些函数已在 share.js 中改名加 share 前缀，不再同名。
//
// 共 55 个函数。改动本文件会同时影响两个页面。

function $(sel) { return document.querySelector(sel); }

// 媒体 URL 前缀。window.MEDIA_BASE 由服务端注入（见 config.MEDIA_BASE_URL）：
// 空串 = 媒体走本机 /uploads/；填了就指向 OSS/CDN，媒体流量绕开 frp 隧道。
function mediaUrl(path) {
  const base = window.MEDIA_BASE || '';
  return base && path.indexOf('/uploads/') === 0 ? base + path : path;
}

// 一条媒体的对外地址。优先用服务端算好的 m.url / m.thumb_url ——
// 只有服务端知道这个文件同步到 OSS 没有（见 config.media_url 的水位判断）；
// 前端这个 mediaUrl 是无脑拼前缀，刚传完的文件拼出来就是 404。
// 老接口 / 没带 url 字段的旧数据退回自己拼，保持能用。
function mediaSrc(m, kind, qs) {
  const q = qs || '';
  if (kind === 'thumb') {
    if (m && m.thumb_url) return m.thumb_url + q;
    return m && m.thumb_filename ? mediaUrl('/uploads/thumbs/' + m.thumb_filename) + q : '';
  }
  if (m && m.url) return m.url + q;
  return m && m.filename ? mediaUrl('/uploads/' + m.filename) + q : '';
}

// OSS 上还没有（水位判断失手、或者同步那一趟正好失败）时回退到本机那份。
// 水位是第一道防线，这是第二道：服务端判断错了也不该在页面上留一块裂图。
function mediaFallbackToLocal(elm, localPath) {
  if (!elm || !localPath) return;
  elm.addEventListener('error', function onErr() {
    const src = elm.currentSrc || elm.src || '';
    // 已经是本机路径还报错，那就是文件真没了，别再兜圈子
    if (!src || src.indexOf(location.origin) === 0) return;
    elm.removeEventListener('error', onErr);
    elm.src = localPath;
    if (elm.tagName === 'VIDEO') { try { elm.load(); } catch (e) {} }
  });
}

// OSS 签名 URL 是会过期的（默认 2 小时，见 config.MEDIA_URL_TTL）。播放器把
// src 设一次就不管了，播到一半链接过期会直接断流、而且用户看不到任何提示 ——
// 只能手动刷新页面。这里给视频挂上「过期前主动续签 + 失败时兜底续签」。
//
// 续签走 /api/media/{id}/variants（刻意不鉴权，分享页也能用），它每次返回的
// 都是**新签出来**的 URL。不重新拉整个点详情，省一次大请求。
//
// 为什么是「提前续签」而不是「等它挂掉再续」：等挂了再续，播放器已经进入
// error 状态、缓冲清空，用户会看到一次真实的卡顿；提前换则几乎无感。
var QUALITY_RESIGN_MARGIN = 5 * 60;      // 过期前 5 分钟动手

// 从 OSS 签名 URL 里读 Expires（Unix 秒）。不是签名的 URL 返回 null ——
// 本机 /uploads/ 路径永远不过期，不需要续。
function tmUrlExpires(url) {
  if (!url || url.indexOf('OSSAccessKeyId=') < 0) return null;
  var m = /[?&]Expires=(\d+)/.exec(url);
  return m ? parseInt(m[1], 10) * 1000 : null;
}

// 拉一次这条媒体最新签出来的档位。失败返回 null（调用方保留原样，
// 续签是锦上添花，绝不能因为它把播放搞挂）。
function tmFetchFreshQualities(mediaId) {
  return fetch('/api/media/' + encodeURIComponent(mediaId) + '/variants')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { return (d && d.qualities) || null; })
    .catch(function () { return null; });
}

// 给一个正在播的视频安排续签。
//   v     —— <video> 元素
//   m     —— 媒体行（要 m.id）
//   label —— 当前正在播的档位名；续签后要换回同一档，不能偷偷降级
//
// 返回一个 stop()，在关灯箱/切走时调用，把定时器和监听都收掉。
function tmKeepFresh(v, m, label) {
  if (!v || !m || !m.id) return function () {};
  var stopped = false;
  var timer = null;
  var renewing = false;

  function currentUrl() { return v.currentSrc || v.src || ''; }

  // 换 src 会丢播放进度，所以复刻 tmSwitchQuality 的做法：记时间 → 换源 → seek 回。
  function swap(url) {
    var at = v.currentTime || 0;
    var wasPlaying = !v.paused && !v.ended;
    var onReady = function () {
      v.removeEventListener('loadedmetadata', onReady);
      try {
        if (at > 0 && isFinite(v.duration)) v.currentTime = Math.min(at, v.duration - 0.05);
        else if (at > 0) v.currentTime = at;
      } catch (e) {}
      if (wasPlaying) { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
    };
    v.addEventListener('loadedmetadata', onReady);
    v.src = url;
    v.load();
  }

  function renew() {
    if (stopped || renewing) return;
    renewing = true;
    tmFetchFreshQualities(m.id).then(function (qs) {
      renewing = false;
      if (stopped || !qs) return;
      // 优先拿同一档；那一档没了（比如转码失败被清）才退回原画，绝不静默换档
      var hit = null;
      for (var i = 0; i < qs.length; i++) {
        if (qs[i].label === label && qs[i].status === 'done' && qs[i].url) { hit = qs[i]; break; }
      }
      if (!hit) {
        for (var j = 0; j < qs.length; j++) {
          if (qs[j].label === 'original' && qs[j].url) { hit = qs[j]; break; }
        }
      }
      if (!hit || !hit.url) return;
      m.qualities = qs;                       // 回写，后面切档用新的
      if (hit.url === currentUrl()) return;   // 没变（本机路径等）就不用折腾
      swap(hit.url);
      schedule();
    });
  }

  function schedule() {
    if (stopped) return;
    clearTimeout(timer);
    var exp = tmUrlExpires(currentUrl());
    if (exp === null) return;                 // 本机路径 / 没有签名，不需要续
    var wait = exp - Date.now() - QUALITY_RESIGN_MARGIN * 1000;
    // 已经进入续签窗口就立刻续；否则等到窗口起点再动
    timer = setTimeout(renew, Math.max(wait, 0));
  }

  // 兜底：万一定时器没赶上（标签页休眠、时钟跳变），播放器报错时再续一次。
  // 但只在「确实是因为过期」时报错才续 —— 不然文件真的坏了会无限重试。
  function onError() {
    var exp = tmUrlExpires(currentUrl());
    if (exp !== null && Date.now() >= exp - QUALITY_RESIGN_MARGIN * 1000) renew();
  }

  v.addEventListener('error', onError);
  schedule();

  return function stop() {
    stopped = true;
    clearTimeout(timer);
    v.removeEventListener('error', onError);
  };
}


function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function tmToast(text, ms) {
  const old = document.querySelector('.tm-toast');
  if (old) old.remove();
  clearTimeout(_toastTimer);
  const t = el('div', 'tm-toast', text);
  document.body.appendChild(t);
  _toastTimer = setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 260);
  }, ms || 1800);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function syncElevLabelsVisibility() {
  const el0 = document.getElementById('map');
  if (!el0 || !map || !map.getZoom) return;
  const z = map.getZoom();
  el0.classList.toggle('hide-elev', z < ELEV_MIN_ZOOM);
  // 地名不再随缩放隐藏：缩到全国视野时，名字正是唯一能说明「这条线经过哪儿」的
  // 东西，藏起来只剩一条光秃秃的线。挤是挤了点，但比读不出来强。
  // 公路编号的门槛比海拔低一档：全国视野下 G318 这种号反而是最有用的信息，
  // 但再缩下去（洲际视野）线都挤成一坨了，标了也读不出对应哪一截。
  el0.classList.toggle('hide-road', z < ROAD_MIN_ZOOM);
}

function unhighlightMapPoints() {
  markers.forEach(m => {
    if (m._dot) m._dot.classList.remove('highlight');
    if (m.setzIndex) m.setzIndex(m._baseZ);
  });
}

// 里程格式化：>=1km 显示 km，<1km 显示整数米。
// digits 默认 1 位小数（总里程用），两点之间的距离传 0 —— 地图上密密麻麻的段
// 标签里那个小数位没人看，去掉之后标签更短、更不容易互相压住。
function fmtKm(m, digits) {
  if (!m) return '';
  const d = digits === undefined ? 1 : digits;
  return m >= 1000 ? (m / 1000).toFixed(d) + ' km' : Math.round(m) + ' m';
}

// 里程标签和公路编号标签永远同生共死（换行程、路线重算、分段撤掉都是一起的），
// 所以清里程就顺手把编号也清了 —— 分开清过一次，结果换行程后上一条线的编号还浮在图上。
function clearSegLabels() {
  segLabels.forEach(m => m.setMap(null));
  segLabels = [];
  clearRoadLabels();
}

// ---------- 里程标签的显隐（图层控制里的「里程」）----------
// 默认显示：这是原来就有的行为，没勾选过的老用户不该因为加了开关就少看见东西。
// 面板里没有这个勾选框时（理论上不会发生）也按显示处理，宁可多显示不要静默消失。
function segLabelsVisible() {
  const cb = document.querySelector('#lay-mileage');
  return cb ? cb.checked : true;
}
// 标签会被反复重建 —— 真实路线算完、切行程、临时分段建好各会重建一次。
// 所以显隐不能只在「点勾选框」那一刻生效：每批新标签建出来都得再对齐一次当前状态，
// 否则关掉里程之后，路线一算完标签就自己冒回来了。
function applySegLabelsVisibility() {
  const on = segLabelsVisible();
  segLabels.forEach(m => {
    // 主手段是给我们自己的胶囊加 class（.seg-label-off { display:none }）。
    // 不直接依赖 AMap 的 show/hide：那是它的内部实现，跨版本行为不保证，
    // 而这个 class 作用在我们自己生成的元素上，行为完全可控、也问得到状态。
    if (m._pill) m._pill.classList.toggle('seg-label-off', !on);
    // 再顺手让 AMap 别去排它的版 —— 纯属省事，不作为显隐的依据。
    try { on ? m.show() : m.hide(); } catch (e) {}
  });
}

function isInChina(lng, lat) {
  return lng >= 73.5 && lng <= 135.0 && lat >= 18.0 && lat <= 53.5;
}

// WGS-84（国际标准坐标，Open-Meteo 等返回）→ GCJ-02（火星坐标，高德地图/Driving 使用）
function wgs84ToGcj02(lng, lat) {
  const a = 6378245.0;
  const ee = 0.00669342162296594323;
  function outOfChina(l, t) {
    return l < 72.004 || l > 137.8347 || t < 0.8293 || t > 55.8271;
  }
  function transformLat(x, y) {
    let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return ret;
  }
  function transformLng(x, y) {
    let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return ret;
  }
  if (outOfChina(lng, lat)) return { lng, lat };
  let dLat = transformLat(lng - 105.0, lat - 35.0);
  let dLng = transformLng(lng - 105.0, lat - 35.0);
  const radLat = lat / 180.0 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * Math.PI);
  dLng = (dLng * 180.0) / (a / sqrtMagic * Math.cos(radLat) * Math.PI);
  return { lng: lng + dLng, lat: lat + dLat };
}

function distKm(a, b) {
  const R = 6371;
  const dLat = (b.lat - a.lat) * Math.PI / 180;
  const dLng = (b.lng - a.lng) * Math.PI / 180;
  const la1 = a.lat * Math.PI / 180, la2 = b.lat * Math.PI / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function applyMediaCols() {
  const grid = document.getElementById('media-grid');
  if (!grid) return;
  const cols = MEDIA_COLS[mediaZoom];
  grid.style.setProperty('--media-cols', cols);
  grid.dataset.cols = cols;      // 给 CSS 一个钩子：排得密时把日期和删除钮缩一号
}

// 播放进度双色线：已走过 → 绿色高亮；未走过 → 灰色压暗（叠加在原路线上方）
function ensureReplayLines() {
  if (replayDoneLine && replayTodoLine) return;
  clearReplayLines();
  if (!replayPath || replayPath.length < 2) return;
  replayDoneLine = new AMap.Polyline({
    path: [replayPath[0]],
    strokeColor: '#16a34a',
    strokeWeight: 5,
    strokeOpacity: 1,
    zIndex: 130,
    lineJoin: 'round',
    lineCap: 'round',
  });
  replayTodoLine = new AMap.Polyline({
    path: replayPath,
    strokeColor: '#94a3b8',
    strokeWeight: 4,
    strokeOpacity: 0.9,
    zIndex: 129,
    lineJoin: 'round',
    lineCap: 'round',
  });
  replayDoneLine.setMap(map);
  replayTodoLine.setMap(map);
}

// 按 replayT 刷新双色线路径：done = 起点→当前插值点，todo = 当前插值点→终点
function updateReplayLines() {
  if (!replayDoneLine || !replayTodoLine || !replayPath.length) return;
  if (replayT >= replayTotal) {
    // 播完：全部已走，todo 缩为终点
    replayDoneLine.setPath(replayPath);
    replayTodoLine.setPath([replayPath[replayPath.length - 1]]);
    return;
  }
  let i = 1;
  while (i < replayCum.length - 1 && replayCum[i] < replayT) i++;
  const segLen = (replayCum[i] - replayCum[i - 1]) || 1;
  const t = Math.min(Math.max((replayT - replayCum[i - 1]) / segLen, 0), 1);
  const a = replayPath[i - 1], b = replayPath[i];
  const pos = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  const donePath = replayPath.slice(0, i);
  donePath.push(pos);
  replayDoneLine.setPath(donePath);
  const todoPath = replayPath.slice(i);
  todoPath.unshift(pos);
  replayTodoLine.setPath(todoPath);
}

function clearReplayLines() {
  if (replayDoneLine) { replayDoneLine.setMap(null); replayDoneLine = null; }
  if (replayTodoLine) { replayTodoLine.setMap(null); replayTodoLine = null; }
}

// ==========================================================================
// 视频清晰度分层 / 自动降级
//
// 背景（后端 transcode.py 顶部有完整诊断）：手机直出的 mp4 常把 moov 索引写在
// 文件尾、码率又高到 5~6 Mbps，移动网络上必卡。后端现在会把原画重封装成
// faststart，并额外转出 1080p/720p/480p 三档（只降不升）。这里负责：
//   1) 默认档位自动降级（网络 + 屏幕 + DPR，手机默认不上 1080p）
//   2) 用户手动选档，记 localStorage 下次沿用
//   3) 切档保持播放进度和播放状态
//   4) 还在转码的档位灰掉标「转码中」，转好了自动刷出来
// ==========================================================================

const VIDEO_QUALITY_KEY = 'tm_video_quality';
const QUALITY_LABELS = ['original', '1080p', '720p', '480p'];
// 各档位对应的短边像素，用来和「屏幕/网络预算」比大小。原画按无穷大算。
const QUALITY_SHORT_SIDE = { original: Infinity, '1080p': 1080, '720p': 720, '480p': 480 };
const QUALITY_NAMES = { original: '原画', '1080p': '1080P', '720p': '720P', '480p': '480P' };

// 正在轮询转码进度的定时器，切页/关灯箱时要清掉，否则后台一直打接口
let _qualityPollTimer = null;

// 读用户手动选过的档位。没选过 / 存了脏值 → null（表示走自动降级）
function loadQualityPref() {
  try {
    const raw = localStorage.getItem(VIDEO_QUALITY_KEY);
    if (raw && QUALITY_LABELS.includes(raw)) return raw;
  } catch (e) {}
  return null;
}

function saveQualityPref(label) {
  try { localStorage.setItem(VIDEO_QUALITY_KEY, label); } catch (e) {}
}

// 回到「自动」。手动选过一次之后就一直沿用那一档 —— 要是当初随手点了「原画」，
// 之后每段视频都按原画起播，在移动网络上就是一路缓冲，而界面上没有任何回头路。
function clearQualityPref() {
  try { localStorage.removeItem(VIDEO_QUALITY_KEY); } catch (e) {}
}

// 自动降级：算一个「短边预算」，取不超过预算的最高档。
// 拿不到 navigator.connection（Safari/Firefox 都没有）就按未知处理 —— 未知一律保守。
function tmQualityBudget() {
  // --- 网络预算 ---
  let netCap = 720;            // 未知网络的默认上限：720，手机默认不上 1080p
  try {
    const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (c) {
      if (c.saveData) {
        netCap = 480;                                  // 用户开了省流量，尊重它
      } else if (c.effectiveType === 'slow-2g' || c.effectiveType === '2g'
                 || c.effectiveType === '3g') {
        netCap = 480;
      } else if (typeof c.downlink === 'number' && c.downlink > 0) {
        // downlink 是 Mbps 估计值。720p 实测约 0.8 Mbps，留 2~3 倍余量。
        netCap = c.downlink < 1.5 ? 480 : (c.downlink < 4 ? 720 : 1080);
      } else if (c.effectiveType === '4g') {
        netCap = 1080;
      }
    }
  } catch (e) {}

  // --- 屏幕预算 ---
  // 灯箱里视频最多铺满视口，超过「视口短边 × DPR」的像素是白给的。
  // DPR 截到 2：3x 屏再上 3 倍分辨率，肉眼收益极小、流量翻倍。
  let scrCap = 1080;
  try {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const shortSide = Math.min(window.innerWidth || 360, window.innerHeight || 640);
    scrCap = Math.round(shortSide * dpr);
  } catch (e) {}

  return Math.min(netCap, scrCap);
}

// 在可用档位里挑一个默认档。qualities 是后端给的档位数组。
function tmPickQuality(qualities) {
  const usable = (qualities || []).filter(q => q.status === 'done' && q.url);
  if (!usable.length) return null;

  // 用户手动选过就沿用（那一档必须真的可用，否则继续走自动）
  const pref = loadQualityPref();
  if (pref) {
    const hit = usable.find(q => q.label === pref);
    if (hit) return hit;
  }

  const cap = tmQualityBudget();
  // 转码档（非原画）里，短边不超预算的最高一档
  const tiers = usable.filter(q => q.label !== 'original')
    .sort((a, b) => (QUALITY_SHORT_SIDE[b.label] || 0) - (QUALITY_SHORT_SIDE[a.label] || 0));
  const fit = tiers.find(q => (QUALITY_SHORT_SIDE[q.label] || 0) <= cap);
  if (fit) return fit;
  // 所有转码档都超预算 → 取最低那档（总比原画省）
  if (tiers.length) return tiers[tiers.length - 1];
  // 一档都没转好 → 原画兜底，播放永远不会因为转码没跟上而挂掉
  return usable.find(q => q.label === 'original') || usable[0];
}

// 切换清晰度：保持当前进度和播放状态。
// 换 src 会把播放位置清零，所以要先记 currentTime，等新源的元数据到了再 seek 回去。
function tmSwitchQuality(v, url) {
  if (!v || !url) return;
  const at = v.currentTime || 0;
  const wasPlaying = !v.paused && !v.ended;
  const onReady = () => {
    v.removeEventListener('loadedmetadata', onReady);
    try {
      // seek 目标可能超过新源时长（理论上不会，防一手），夹一下
      if (at > 0 && isFinite(v.duration)) v.currentTime = Math.min(at, v.duration - 0.05);
      else if (at > 0) v.currentTime = at;
    } catch (e) {}
    if (wasPlaying) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
  };
  v.addEventListener('loadedmetadata', onReady);
  v.src = url;
  v.load();
}

// 档位列表兜底：接口没带 qualities（老缓存/相册页自己拼的对象）时，
// 先按「只有原画」渲染，再异步拉一次补上。
function tmFallbackQualities(m) {
  return [{
    label: 'original', name: '原画', status: 'done',
    url: mediaSrc(m, 'full', '?v=1'),
  }];
}

// 给一格视频装上清晰度按钮 + 菜单，并选好默认档开播。
function tmSetupQuality(wrap, v, m) {
  const btn = document.createElement('button');
  btn.className = 'lb-quality-btn';
  btn.type = 'button';
  const menu = document.createElement('div');
  menu.className = 'lb-quality-menu hidden';
  wrap.appendChild(btn);
  wrap.appendChild(menu);

  let qualities = (m.qualities && m.qualities.length) ? m.qualities : tmFallbackQualities(m);
  let current = null;

  // 当前播放的档位变了 → 换一条续签定时器（续签逻辑见 tmKeepFresh）。
  // 起播、手动切档、自动切档、转码完成自动升级，全都得走这里。
  let stopFresh = null;
  function rearmFresh() {
    if (stopFresh) { stopFresh(); stopFresh = null; }
    if (!current) return;
    stopFresh = tmKeepFresh(v, m, current.label);
  }

  function render() {
    const auto = !loadQualityPref();
    btn.textContent = current ? (QUALITY_NAMES[current.label] || current.label) : '清晰度';
    menu.innerHTML = '';
    // 「自动」永远排第一：它是默认状态，也是手动选过之后唯一的回头路
    const autoRow = document.createElement('button');
    autoRow.type = 'button';
    autoRow.className = 'lb-quality-item' + (auto ? ' active' : '');
    autoRow.innerHTML = '<span>自动</span><span class="lb-quality-tail">'
      + esc(auto && current ? (QUALITY_NAMES[current.label] || current.label) : '按网络/屏幕')
      + '</span>';
    autoRow.onclick = (ev) => {
      ev.stopPropagation();
      menu.classList.add('hidden');
      clearQualityPref();
      const pick = tmPickQuality(qualities);
      if (pick && (!current || pick.label !== current.label)) {
        current = pick;
        tmSwitchQuality(v, pick.url);
        rearmFresh();
        tmToast('已切换到自动（' + (QUALITY_NAMES[pick.label] || pick.label) + '）');
      } else {
        tmToast('已切回自动选档');
      }
      render();
    };
    menu.appendChild(autoRow);
    qualities.forEach(q => {
      const row = document.createElement('button');
      row.type = 'button';
      const ready = q.status === 'done' && q.url;
      row.className = 'lb-quality-item'
        + (ready ? '' : ' disabled')
        + (!auto && current && q.label === current.label ? ' active' : '');
      row.disabled = !ready;
      let tail = '';
      if (q.status === 'pending' || q.status === 'running') tail = '转码中';
      else if (q.status === 'failed') tail = '失败';
      else if (q.filesize) tail = (q.filesize / 1048576).toFixed(1) + 'M';
      row.innerHTML = '<span>' + esc(QUALITY_NAMES[q.label] || q.label) + '</span>'
        + '<span class="lb-quality-tail">' + esc(tail) + '</span>';
      if (ready) {
        row.onclick = (ev) => {
          ev.stopPropagation();
          menu.classList.add('hidden');
          if (current && q.label === current.label) return;
          current = q;
          saveQualityPref(q.label);          // 手动选过就记住，下次沿用
          tmSwitchQuality(v, q.url);
          rearmFresh();
          render();
          tmToast('已切换到 ' + (QUALITY_NAMES[q.label] || q.label));
        };
      }
      menu.appendChild(row);
    });
  }

  btn.onclick = (ev) => {
    ev.stopPropagation();
    menu.classList.toggle('hidden');
  };
  // 点视频区域别的地方就收起菜单
  wrap.addEventListener('click', (ev) => {
    if (ev.target !== btn && !menu.contains(ev.target)) menu.classList.add('hidden');
  });

  // 选默认档并起播
  current = tmPickQuality(qualities);
  v.src = (current && current.url) || mediaSrc(m, 'full', '?v=1');
  // 原画档还没同步到 OSS 时，回退本机那份（转码档位 variants 同理，由上面的 url 决定）
  mediaFallbackToLocal(v, '/uploads/' + m.filename + '?v=1');
  rearmFresh();                            // 起播：给签名 URL 安排过期前续签
  render();

  // 还有档位在转码 → 轮询刷新（转好了菜单里就能选了）。
  // 没带 qualities 的情况也走这里，顺便把真实档位补回来。
  const needPoll = !m.qualities
    || qualities.some(q => q.status === 'pending' || q.status === 'running');
  if (needPoll && m.id) tmPollQualities(m, wrap, (list) => {
    qualities = list;
    // 用户没手动选过、且当前还停在原画上时，转码档一转好就自动切过去
    if (!loadQualityPref() && current && current.label === 'original') {
      const better = tmPickQuality(list);
      if (better && better.label !== 'original') {
        current = better;
        tmSwitchQuality(v, better.url);
        rearmFresh();                      // 换档了，续签要跟着新档位走
      }
    }
    render();
  });

  // 关灯箱 / 这一格被摘掉时把续签定时器收掉，别让它对着已经没了的元素转
  const mo = new MutationObserver(() => {
    if (!wrap.isConnected && stopFresh) { stopFresh(); stopFresh = null; mo.disconnect(); }
  });
  try { mo.observe(document.body, { childList: true, subtree: true }); } catch (e) {}
}

// 轮询某个视频的转码进度，转完就停。最多盯 3 分钟，避免后台一直打接口。
function tmPollQualities(m, wrap, onUpdate) {
  let tries = 0;
  clearTimeout(_qualityPollTimer);
  const tick = () => {
    // 这一格已经从 DOM 上摘掉了（翻页/关灯箱）→ 停止轮询
    if (!wrap.isConnected || tries++ > 36) return;
    fetch('/api/media/' + encodeURIComponent(m.id) + '/variants')
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (!d || !wrap.isConnected) return;
        if (d.qualities && d.qualities.length) {
          m.qualities = d.qualities;         // 回写，翻回这一格时不用再拉
          onUpdate(d.qualities);
        }
        if (d.transcoding) _qualityPollTimer = setTimeout(tick, 5000);
      })
      .catch(() => {});
  };
  _qualityPollTimer = setTimeout(tick, 1500);
}

// ---------- 保存到本地 ----------
// 为什么不能直接 <a href="/uploads/xxx.mp4" download>：
//   1. 同源的 download 属性在**桌面** Chrome/Firefox 上好用，但 iOS Safari 从来
//      不认它，视频会直接跳去内联播放；
//   2. 微信 webview（X5 / WKWebView）更彻底，连 Content-Disposition 都会拦。
// 所以真正促成「下载而不是播放」的是服务端的 attachment 响应头（download.py），
// download 属性只留着让桌面浏览器别新开标签页。微信这一档老实降级成文字提示 ——
// 假装点了有用，比明说下不了更糟。

function tmIsWeChat() {
  return /MicroMessenger|wxwork/i.test(navigator.userAgent || '');
}

function tmIsIOS() {
  const ua = navigator.userAgent || '';
  // iPadOS 13+ 默认发桌面版 UA，只能靠「Mac + 有触摸点」认出来
  return /iPad|iPhone|iPod/.test(ua)
    || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
}

// 下载地址。两条路对应两种「能看」的方式，权限各自在服务端校验：
//   分享页 → /api/share/{token}/media/{id}/download（匿名，但校验 token+密码+归属）
//   主应用 → /api/media/{id}/download（要登录，且必须是本人/管理员）
function tmDownloadUrl(m, label) {
  const qs = [];
  if (label && label !== 'original') qs.push('quality=' + encodeURIComponent(label));
  const seg = location.pathname.split('/').filter(Boolean);
  if (seg[0] === 'share' && seg[1]) {
    // 密码分享：share.js 把用户输过的密码存在 sessionStorage，这里沿用同一个键
    const pwd = sessionStorage.getItem('share_pwd_' + seg[1]) || '';
    if (pwd) qs.push('password=' + encodeURIComponent(pwd));
    return '/api/share/' + encodeURIComponent(seg[1]) + '/media/'
      + encodeURIComponent(m.id) + '/download' + (qs.length ? '?' + qs.join('&') : '');
  }
  return '/api/media/' + encodeURIComponent(m.id) + '/download'
    + (qs.length ? '?' + qs.join('&') : '');
}

function tmTriggerDownload(m, label) {
  if (tmIsWeChat()) {
    // 微信里点了也不会下载，所以干脆不发请求，直接给能用的办法
    tmToast(m.type === 'video'
      ? '微信里无法直接下载。点右上角「···」→ 在浏览器打开，或长按视频保存'
      : '微信里无法直接下载。长按图片即可保存到相册', 4000);
    return;
  }
  const a = document.createElement('a');
  a.href = tmDownloadUrl(m, label);
  a.rel = 'noopener';
  a.download = '';           // 留空 = 用服务端给的文件名（Content-Disposition）
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (tmIsIOS()) {
    // iOS Safari 会下进「文件」App 而不是相册，说清楚，省得人以为没成功
    tmToast('已开始下载，可在「文件」App 里查看；想存进相册请长按' +
      (m.type === 'video' ? '视频' : '图片'), 4000);
  } else {
    tmToast('开始下载…');
  }
}

// 灯箱翻到某一格时刷新右上角的下载按钮。app.js / share.js 的 renderLightbox 各调一次。
function tmUpdateDownloadBtn(m) {
  const btn = document.getElementById('lightbox-download');
  const menu = document.getElementById('lightbox-download-menu');
  if (!btn) return;
  if (menu) { menu.classList.add('hidden'); menu.innerHTML = ''; }
  if (!m || !m.id) { btn.hidden = true; return; }   // 没 id 就没法拼下载地址
  btn.hidden = false;
  btn.onclick = (ev) => {
    ev.stopPropagation();
    // 视频：列出**已转好**的档位让人挑。数据直接取 m.qualities —— 播放器那边
    // 已经拉过、轮询也会回写到同一个对象上，这里不再多打一次接口。
    const ready = (m.type === 'video' && m.qualities ? m.qualities : [])
      .filter(q => q.status === 'done' && q.url);
    if (!menu || ready.length <= 1) { tmTriggerDownload(m, 'original'); return; }
    if (!menu.classList.contains('hidden')) { menu.classList.add('hidden'); return; }
    menu.innerHTML = '';
    ready.forEach(q => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'lb-download-item';
      const size = q.filesize ? (q.filesize / 1048576).toFixed(1) + 'M' : '';
      row.innerHTML = '<span>' + esc(QUALITY_NAMES[q.label] || q.label) + '</span>'
        + '<span class="lb-quality-tail">' + esc(size) + '</span>';
      row.onclick = (e) => {
        e.stopPropagation();
        menu.classList.add('hidden');
        tmTriggerDownload(m, q.label);
      };
      menu.appendChild(row);
    });
    menu.classList.remove('hidden');
  };
}

// 点别处收起下载菜单。挂在 document 上而不是 #lightbox 上，是因为
// 菜单要能被灯箱外的点击（比如误触页面其他地方）一起收掉。
document.addEventListener('click', (ev) => {
  const menu = document.getElementById('lightbox-download-menu');
  if (!menu || menu.classList.contains('hidden')) return;
  const btn = document.getElementById('lightbox-download');
  if (menu.contains(ev.target) || (btn && btn.contains(ev.target))) return;
  menu.classList.add('hidden');
});

// ---------- 灯箱控件自动隐藏 ----------
// 看视频的时候，右上角的关闭/下载、左右翻页箭头、清晰度按钮全都压在画面上。
// 播放器的通行做法：一段时间没操作就淡出，再点一下画面又回来。
// 只藏「操作控件」，不藏底部的说明文字 —— 那是内容不是控件。
const LB_IDLE_MS = 3000;
let _lbIdleTimer = 0;

function tmLightboxWake() {
  const box = document.getElementById('lightbox');
  if (!box || box.classList.contains('hidden')) return;
  box.classList.remove('idle');
  clearTimeout(_lbIdleTimer);
  _lbIdleTimer = setTimeout(() => {
    // 菜单开着说明人正在选清晰度/下载档位，这时候藏掉等于把操作打断
    if (box.querySelector('.lb-quality-menu:not(.hidden)')
        || box.querySelector('.lb-download-menu:not(.hidden)')) {
      tmLightboxWake();
      return;
    }
    box.classList.add('idle');
  }, LB_IDLE_MS);
}

// 控件已经淡出时，第一下点击只负责把它们唤回来 —— 不该顺带进全屏放大态。
// 主界面和分享页的灯箱手势入口都先问一句这个。
function tmLightboxIdleTapGuard() {
  const box = document.getElementById('lightbox');
  if (!box) return false;
  const wasIdle = box.classList.contains('idle');
  tmLightboxWake();
  return wasIdle;
}

(function initLightboxIdle() {
  const box = document.getElementById('lightbox');
  if (!box) return;
  ['pointerdown', 'pointermove', 'touchstart', 'wheel', 'keydown'].forEach(ev =>
    box.addEventListener(ev, tmLightboxWake, { capture: true, passive: true }));
  document.addEventListener('keydown', tmLightboxWake);
  // 灯箱开合是靠 .hidden 类切的，盯着类变化即可，不用每个调用点都去插一行。
  // 只在「显隐真的翻转了」时才动作 —— 否则我们自己加的 .idle 也会触发这个回调，
  // 回调再把 .idle 摘掉，两下互相触发就转成死循环了。
  let wasHidden = box.classList.contains('hidden');
  new MutationObserver(() => {
    const nowHidden = box.classList.contains('hidden');
    if (nowHidden === wasHidden) return;
    wasHidden = nowHidden;
    if (nowHidden) {
      clearTimeout(_lbIdleTimer);
      box.classList.remove('idle');
    } else {
      tmLightboxWake();
    }
  }).observe(box, { attributes: true, attributeFilter: ['class'] });
})();

// 造一格媒体。只有当前这格的视频才自动播放，相邻格预载但不出声。
// 视频格：把说明栏占的高度从舞台里让出来，别压在 <video> 的原生控制条上。
//
// 说明栏是 `position:absolute; bottom:0` 铺满整格底部的一条渐变，而视频
// `max-height:100%` 时它的原生控制条正好在同一个位置 —— 于是点控制条右下角的
// 全屏按钮，点到的其实是说明栏，主页面上还会因此弹出「添加描述」。
// 修法是把 .lb-slide 的 padding-bottom 撑到说明栏的实际高度，视频被顶上去，
// 两者不再重叠。说明栏展开/收起、改完描述都会变高，所以用 ResizeObserver 跟着量。
//
// 注意：说明栏是绝对定位的，它的 left/right/bottom 参照的是 .lb-slide 的
// **padding 边**，改 padding 不会反过来改变它的尺寸 —— 不存在互相触发的死循环。
const LB_SLIDE_PAD_BOTTOM = '4vh';     // 与 style.css 里 .lb-slide 的 padding 保持一致
function tmReserveCaptionSpace(slide) {
  if (!slide || !slide.classList.contains('has-video')) return;
  const cap = slide.querySelector('.lb-caption');
  if (!cap) return;
  let last = -1;
  const apply = () => {
    const h = Math.round(cap.offsetHeight);
    if (h === last) return;
    last = h;
    slide.style.paddingBottom = 'calc(' + LB_SLIDE_PAD_BOTTOM + ' + ' + h + 'px)';
  };
  apply();
  // 首帧量到 0 是常态（灯箱刚从 display:none 变可见），补量几次
  requestAnimationFrame(apply);
  setTimeout(apply, 120);
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(apply);
    ro.observe(cap);
    slide._capRO = ro;                 // 换格时 stage.innerHTML='' 会让它自然失效
  }
}

function buildLightboxMedia(m, isCurrent) {
  if (m.type === 'video') {
    // 只有「当前这一格」才真的建 <video>。相邻格用缩略图占位——否则浏览器
    // 会因为页面上存在视频元素而一直挂着「全屏/投屏」悬浮按钮，翻到照片后还在。
    if (!isCurrent) {
      const ph = document.createElement('div');
      ph.className = 'lightbox-media lb-video-poster';
      if (m.thumb_filename) {
        const im = document.createElement('img');
        im.src = mediaSrc(m, 'thumb');
        mediaFallbackToLocal(im, '/uploads/thumbs/' + m.thumb_filename);
        ph.appendChild(im);
      }
      ph.appendChild(Object.assign(document.createElement('span'),
        { className: 'lb-video-poster-play', textContent: '▶' }));
      return ph;
    }
    // 视频用外层 wrap 包一下：清晰度按钮要和 <video> 平级。
    // 不能塞进 <video> 里（HTML 不允许），也不能让它变成 .lightbox-media ——
    // lbApplyZoom 会给 .lightbox-media 加 transform，菜单会跟着一起被缩放位移。
    const wrap = document.createElement('div');
    wrap.className = 'lb-video-wrap';

    const v = document.createElement('video');
    v.controls = true;
    v.autoplay = true;
    v.preload = 'auto';
    v.className = 'lightbox-media';
    wrap.appendChild(v);

    tmSetupQuality(wrap, v, m);
    return wrap;
  }
  const img = document.createElement('img');
  img.src = mediaSrc(m, 'full');
  img.className = 'lightbox-media';
  // 依次回退：OSS 原图 → 本机原图（还没同步上去）→ 缩略图（原图是真的没了）
  let tried = 0;
  img.onerror = () => {
    tried += 1;
    if (tried === 1 && m.filename && img.src.indexOf(location.origin) !== 0) {
      img.src = '/uploads/' + m.filename;
    } else if (m.thumb_filename && img.src.indexOf('/thumbs/') === -1) {
      img.src = mediaSrc(m, 'thumb');
    }
  };
  return img;
}

// 彻底停掉灯箱里的视频。
// 直接 innerHTML='' 把 <video> 摘掉是不够的：浏览器仍认为「有媒体在播放」，
// 于是那个「全屏/投屏」的悬浮按钮会一直挂在页面上——翻到照片后还在，
// 关掉灯箱后也还在。必须显式 pause + 断源 + load()，并退出画中画。
function stopLightboxVideos() {
  clearTimeout(_qualityPollTimer);   // 顺手停掉转码进度轮询，别在后台空转
  const box = document.getElementById('lightbox');
  if (!box) return;
  box.querySelectorAll('video').forEach(v => {
    try {
      v.pause();
      v.removeAttribute('src');
      v.srcObject = null;
      v.load();                       // 中止加载、释放解码器，媒体会话随之结束
    } catch (e) { /* 个别内核不支持就算了 */ }
  });
  try {
    if (document.pictureInPictureElement && box.contains(document.pictureInPictureElement)) {
      document.exitPictureInPicture().catch(() => {});
    }
  } catch (e) {}
}

function mdEscape(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// 只放行 http/https/mailto：javascript: data: 这类一律降级成纯文本
function mdSafeUrl(u) {
  const t = String(u).trim();
  return /^(https?:\/\/|mailto:)/i.test(t) ? t : '';
}

function mdInline(s) {
  // 行内代码最先抠走：里面的 * _ [ 都不该再被当成语法。
  // 占位符用私用区字符 U+E000，正常输入里不会出现。
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, function (_, c) {
    codes.push(c);
    return '' + (codes.length - 1) + '';
  });
  // 链接目标允许一层括号嵌套：alert(1)、维基那种 ..._(disambiguation) 都能整段吃掉，
  // 不会在结尾漏出一个孤零零的 )
  const URLPAT = '((?:[^()\\s]|\\([^()\\s]*\\))+)';
  s = s.replace(new RegExp('!\\[([^\\]]*)\\]\\(' + URLPAT + '\\)', 'g'), '$1');   // 图片只留 alt，不外链图源
  s = s.replace(new RegExp('\\[([^\\]]+)\\]\\(' + URLPAT + '\\)', 'g'), function (whole, t, u) {
    const href = mdSafeUrl(u);
    // 协议不在白名单里就把原文原样留着，别把用户写的字吃掉一半
    return href ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + t + '</a>' : whole;
  });
  s = s.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  // _斜体_ 要求两侧不是字母数字，免得把 file_name_here 这种下划线当成语法
  s = s.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  s = s.replace(/(\d+)/g, function (_, i) { return '<code>' + codes[+i] + '</code>'; });
  return s;
}

function renderMarkdown(src) {
  const lines = mdEscape(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let list = '';          // '' | 'ul' | 'ol'
  let para = [];
  function flushPara() {
    if (!para.length) return;
    // 段内换行保留成 <br>：大多数人在说明里手动断行就是想要那个断行
    out.push('<p>' + mdInline(para.join('<br>')) + '</p>');
    para = [];
  }
  function flushList() { if (list) { out.push('</' + list + '>'); list = ''; } }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].replace(/\s+$/, '');
    if (!line.trim()) { flushPara(); flushList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); flushList();
      // 说明栏就巴掌大，h1 顶格太夸张，整体降两级：# → h3
      const lv = Math.min(6, m[1].length + 2);
      out.push('<h' + lv + '>' + mdInline(m[2]) + '</h' + lv + '>');
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushPara(); flushList(); out.push('<hr>'); continue;
    }
    if ((m = line.match(/^&gt;\s?(.*)$/))) {        // > 转义后已经变成 &gt;
      flushPara(); flushList();
      out.push('<blockquote>' + mdInline(m[1]) + '</blockquote>');
      continue;
    }
    if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; }
      out.push('<li>' + mdInline(m[1]) + '</li>');
      continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + mdInline(m[1]) + '</li>');
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return out.join('');
}

// 底部高亮条跟随当前 tab 滑动：量出激活 tab 的宽度与左偏移，写进 CSS 变量由 CSS 过渡。
function syncTabIndicator() {
  const tabs = document.querySelector('.tabs');
  if (!tabs) return;
  const ind = tabs.querySelector('.tab-indicator');
  const act = tabs.querySelector('.tab.active');
  if (!ind || !act) return;
  const w = act.offsetWidth;
  if (!w) return;   // 面板收起时量不到，等展开再同步
  ind.style.setProperty('--tab-w', w + 'px');
  ind.style.setProperty('--tab-x', act.offsetLeft + 'px');
  // 首帧先落位再开启过渡，否则高亮条会从最左边"飞"过来
  requestAnimationFrame(() => tabs.classList.add('ready'));
}

// 把三个 tab 页按索引摆到轨道上：当前页在 0，前一页在 -100%，后一页在 +100%。
// dragPx 是手指的实时横向位移，拖动过程中整条轨道跟着走。
function positionViews(activeName, dragPx) {
  const track = document.querySelector('.view-track');
  if (!track) return;
  const idx = Math.max(0, TAB_ORDER.indexOf(activeName));
  TAB_ORDER.forEach((n, i) => {
    const v = document.getElementById(n + '-view');
    if (!v) return;
    const base = (i - idx) * 100;
    v.style.setProperty('--vx', dragPx ? `calc(${base}% + ${dragPx}px)` : `${base}%`);
    v.setAttribute('aria-hidden', i === idx ? 'false' : 'true');
  });
  // 相片墙跟着 tab 走。挂在这儿而不是 .tab 的 click 上：横滑切页不经过 click。
  // 拖动过程中（dragPx）每帧都会调一次，那会儿页还没定下来，别动。
  if (!dragPx && typeof pwSyncVisibility === 'function') pwSyncVisibility();
}

function loadPhotoView() {
  const d = { filter: 'all', sort: 'desc', zoom: 3 };
  try {
    const s = JSON.parse(localStorage.getItem(PHOTO_VIEW_KEY) || 'null') || {};
    return {
      filter: ['all', 'photo', 'video'].includes(s.filter) ? s.filter : d.filter,
      sort: s.sort === 'asc' ? 'asc' : d.sort,
      zoom: Number.isInteger(s.zoom) && s.zoom >= 0 && s.zoom < PHOTO_COLS.length ? s.zoom : d.zoom,
    };
  } catch (e) { return d; }
}

function savePhotoView() {
  try { localStorage.setItem(PHOTO_VIEW_KEY, JSON.stringify(photoView)); } catch (e) {}
}

function syncPhotoToolbar() {
  document.querySelectorAll('#photo-filter .seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === photoView.filter);
  });
  $('#btn-photo-sort').textContent = photoView.sort === 'desc' ? '↓ 新→旧' : '↑ 旧→新';
  $('#btn-photo-zoom-in').disabled = photoView.zoom >= PHOTO_COLS.length - 1;
  $('#btn-photo-zoom-out').disabled = photoView.zoom <= 0;
}

function pinchDist(e) {
  const [a, b] = e.touches;
  return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
}

function panelReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function isMobileLayout() {
  return !(window.matchMedia && window.matchMedia('(min-width: 769px)').matches);
}

// 动画过程中容器在变，高德的画布得跟着重算，否则中途会看到拉伸/留白
// ---------- 相册墙自动播放 --------------------------------------------------
/* 把首页那张照片墙的五拍动画搬到这里（用户 2026-09-17 要求「仿照首页照片墙」）：
     ① 整面墙静止
     ② 随机挑一格「选中」：亮起来 + 描边 + 抬起，停
     ③ 这一格放大到 1.5 倍，停
     ④ 大图从它原来那一格长开、铺满**整个相册网格**，停
     ⑤ 边淡出边缩到 0，墙上什么痕迹都不留
     ⑥ 渐隐的**同时**抽下一格，接新一轮 —— 收尾和新一轮各用一个铺满层，
        所以两者能真正同时跑，中间没有空档（同首页，见那边 swapFill 的注释）。

   与首页那套的三处关键差异，都是这里的地形逼出来的：

   ① **每格是 <img>，不是 CSS 背景图。** 首页从 `getComputedStyle(cell)`
      读 backgroundImage，这里读不到东西 —— 铺满层的地址得从 <img> 上拿。
      而且要拿**完整分辨率**那张（m.url），不能拿 200px 的缩略图：
      手机上把缩略图拉满整屏会糊成一片。缩略图这里反而拿不到原图 URL，
      于是把 m 原样存进 cellWallByCell 的 Map 里。

   ② **尺寸不能靠 .photo-grid 自己算。** 首页 .album 的行高由 aspect-ratio
      推出来，量 box.clientHeight 就是真实高度；而 .photo-grid 的
      `align-content: start` 只让内容占满用掉的行，容器还摊在可滚动高度上 ——
      量它会把「网格底下那片空白」也算进墙里，大图铺到一半就停、位置也偏。
      所以墙的几何一律由**格子自己**推：宽 = 列数 × 格子宽 + 间隙，
      高 = 行数 × 格子高 + 间隙（见 wallRect）。

   ③ **一轮 = 整面墙每一格都放过一次。** 用一个「已放过」的集合（pwPlayed）
      而不是队列：队列要跟着筛选/排序/缩放重建，而集合天然容错 ——
      网格换了一批新格子，它们本来就不在集合里，新的一轮自然从头开始。
      集合被清空 = 这一遍走完了，接着开新的一遍，**不会自己停**。
*/

// 是否在播。**只有「播 / 停」两态，没有暂停** —— 停在铺满那一拍时大图正盖着
// 整个网格，用户按了按钮还是看不见缩略图，那台按钮就像失灵了（见 pwToggle）。
let pwOn = false;

/* ---- 这一轮在哪面「墙」上播 ------------------------------------------------
   两处网格用同一套动画：相册页是 #photo-grid / .photo-cell（正方格、有内边距），
   点详情是 #media-grid / .media-item（同样正方格，无内边距，最后一格是「＋」）。
   几何全部由 wallBoxInfo 现场量，所以除了选择器不需要别的差异；
   同一时刻只可能有一面墙在播（pwSetTarget 会先把上一面停掉）。 */
const PW_TARGETS = {
  albums: { grid: '#photo-grid', cell: '.photo-cell', btn: '#btn-photo-play', view: '#albums-view', tab: 'albums',
            panel: '.view-track' },
  detail: { grid: '#media-grid', cell: '.media-item', btn: '#btn-detail-play', view: '#detail-view', tab: 'detail',
            panel: '.view-track' },
  /* 语义搜图的结果墙。格子和相册页是同一个 .photo-cell，所以这里除了选择器
     没有别的差异。panel 指向 .search-overlay 本身 —— 它是 position:fixed，
     本来就是个定位祖先，wallEnsureLayer 那段临时 relative 不会被触发。 */
  search: { grid: '#search-grid', cell: '.photo-cell', btn: '#btn-search-play', view: '#search-view', tab: null,
            panel: '.search-overlay' },
};
let pwTargetKey = 'albums';
const pwTarget = () => PW_TARGETS[pwTargetKey];
const pwGrid = () => $(pwTarget().grid);
const pwCellSel = () => pwTarget().cell;

/* 换墙。铺满层是挂在具体某个网格下面的，换墙必须先把上一面彻底停掉复位，
   否则旧墙上会留一层永远不再被收拾的大图。 */
function pwSetTarget(key) {
  if (!PW_TARGETS[key]) return;
  if (pwTargetKey === key) return;
  if (pwOn) pwStop();
  else pwReset();
  pwTargetKey = key;
  pwFillA = null; pwFillB = null; pwCur = null;
  pwSetButton();
}
// 当前轮次/收尾用的两个铺满层。**轮换用**：这一轮在 a 上长开时，
// 上一轮正在 b 上收尾，互不打扰。
let pwFillA = null, pwFillB = null;
let pwCur = null;              // 本轮正在用的那一个，脚本只通过它读写
let pwPrevCell = null;         // 上一拍抽中的格子，用来避免连着抽同一格
// 本轮已经放过的格子。空集 = 新的一轮刚开始（见 wallPickCell）。
const pwPlayed = new Set();
let pwTimers = [];             // 本轮在飞的定时器，每一拍之后都要能整体叫停
// 格子的「本轮是第几轮」标记，见 wallRoundCell 的说明。
let pwRound = 0;
let pwSel = null;              // 当前被抽中的格子（选中/放大中/已铺满）
let pwSettling = null;         // 正在滑回原位的那一格（阶段 ④ 之后）
/* 「人正在操作铺满层上那条视频」的两个标记。看着像冗余，其实各挡各的：
     pwVideoHold —— 控制条亮着**或**正在全屏。给 resize 用：进/退全屏本身就会
       触发 window.resize，而那个 handler 里是 pwRestart() —— 等于把用户正在看
       的视频当场拆掉。有人在操作就别动几何，等他看完。
     pwVideoFs   —— 只在真的全屏时为真。给 Esc 用：全屏时按 Esc 是「退出全屏」，
       浏览器自己会处理，不能顺手把整面墙也停了。控制条亮着的时候 Esc 仍然停播
       （那是用户明确要结束），所以这两个标记不能合成一个。 */
let pwVideoHold = false;
let pwVideoFs = false;
/* 当前这条视频的「退出全屏」入口。由 pwPlayFillVideo 装上、拆场时置空。
   Esc 那一支需要它：多数浏览器会自己把 Esc 当成退出全屏、根本不把这个键
   交给页面；但也有把键先给页面的（部分 webview）。两种都要能退出来，
   所以这里主动再退一次 —— exitFullscreen 幂等，已经退出了再调也没事。 */
let pwVideoFsExit = null;

// 时间常量。**改了手感只动这几个数**，但有两个数别乱动：
// PW_GROW_FULL 必须和 CSS `.pw-fill` 的 transition-duration 相等，
// PW_BURST 必须和 `.pw-fill[data-burst]` 两条 animation 的时长相等。
const PW_HOLD_STILL = 700;     // 开播前先让整面墙静一下
const PW_HOLD_PICK  = 560;     // 「选中」之后、开始放大之前的停留
const PW_GROW_MS    = 420;     // 原地放大 / 收回的时长，对齐 CSS .42s
const PW_GROW       = 1.5;     // 放大倍数 —— **必须和 CSS `.photo-cell[data-pw-pick]{--pw-g}` 一致**
/* 描边总厚度。**必须和 CSS `.photo-cell[data-pw-pick]` 那两圈 box-shadow 的
   外径相等**（3px 白 + 3px 主色 = 6px）—— 它同时就是 wallGrowPos 的让位余量，
   两个数一致，放大后描边的**外沿正好压在墙边上**，不多留一条缝。
   （原来这里是 8，而描边只有 2px，于是贴边那几格的描边离墙边空出 6px。） */
const PW_RING       = 6;
const PW_EDGE_PAD   = PW_RING;
const PW_HOLD_GROW  = 460;     // 放大到位之后、开始铺满之前的停留
const PW_GROW_FULL  = 760;     // 从格子长到铺满的时长，对齐 CSS .76s
const PW_DWELL_FULL = 2000;    // 铺满之后停多久
const PW_BURST      = 1080;    // 渐隐时长，对齐 CSS 那两条 animation
// 抽到视频就**整段播完**再换下一张（不再跳过）。这个上限只是防一条几十分钟的
// 长片把整轮播放卡死的保险丝，正常的旅行视频碰不到它。
const PW_VIDEO_MAX  = 180000;  // 单条视频最多播 3 分钟
/* 用户自己按了暂停之后换的那根保险丝。人是有意停下来看一帧的，不能按 3 分钟算；
   但也不能永远等 —— 手机揣兜里走了，这面墙得自己接着放下去。 */
const PW_VIDEO_PAUSE_MAX = 600000;   // 手动暂停后最多等 10 分钟


/* ---- 页面挂钩：各页的「原图地址」取法不同，由各自的脚本覆盖 ----------------
   wallMediaUrl(m) —— 铺满层该用哪张图。分享页直接读 m.url（接口带回来了）；
   主页面必须走 mediaSrc(m,'full')，那里才有「OSS 水位」的判断。
   默认实现按分享页的语义写，页面脚本可在 wallAttach() 之前覆盖。 */
var wallMediaUrl = function (m) { return m && m.url; };

// 每一格 -> 它的媒体对象。大图要 m.url（原图），而 DOM 上只有缩略图的地址，
// 所以重建网格时顺手存一份。用 Map 而不是往 cell 上挂属性，是为了让
// 这个引用随网格重建自然失效，不用手动清理。
const cellWallByCell = new Map();

function pwReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* 铺满层要盖住**下面板留下来的可见区**，不是那面墙。它是绝对定位，
   所以要挂在最近的已定位祖先下 —— 绝不能放进格子里（每一格 .photo-cell
   都是 position:relative，放进去只会盖住那一格），必须挂在 .view 的直接子层。

   **为什么是 .view 而不是网格**（用户 2026-09-18 报的 bug）：
   以前铺满层挂在网格上、盖住整面墙。可墙的高度是**照片数量**决定的 ——
   只有一行时墙只有一行高，大图「铺满」就只铺满那一行；照片多到墙比面板还高时，
   大图又会顺着墙铺到面板外面去。而用户看到的那块地方是**面板**，不是墙。
   所以终点尺寸一律由面板可见区说了算：墙只负责两件事 ——
   起点格子在哪儿、从哪儿抽格子（见 wallGeom / wallBoxInfo 的分工）。

   挂到 .view 之后 inset:0 不能用了（那会拉伸成整个可滚动内容，比屏幕还高），
   所以 top / height 全部由脚本给死（见 wallFillRel / wallSizeFill）。
   .view 自己的定位交给 CSS（它本来就是 absolute，是 .view-track 的定位子元素）；
   面板 .view-track 再兜一层 relative，防止将来 .view 的定位被改掉时
   铺满层一路往上找到更外层的定位祖先、盖到整个布局上去。
   记在 dataset 上，停播时按原样恢复（别把页面自己的定位改没了）。 */
function wallEnsureLayer() {
  const grid = pwGrid();
  const view = $(pwTarget().view);
  const panel = $(pwTarget().panel);
  if (!grid || !view || !panel) return;
  if (getComputedStyle(panel).position === 'static') {
    panel.dataset.pwPos = panel.style.position || '';
    panel.style.position = 'relative';
  }
  // 必须连 parentElement 一起判：切墙之后旧的两层还「连在文档里」，
  // 只判 isConnected 会把上一面墙的层当成这一面的，大图就长在看不见的那个 tab 上。
  if (pwFillA && pwFillA.parentElement === view && pwFillB && pwFillB.parentElement === view) return;
  view.querySelectorAll('.pw-fill').forEach(n => n.remove());
  pwFillA = el('div', 'pw-fill'); pwFillA.setAttribute('aria-hidden', 'true');
  pwFillB = el('div', 'pw-fill'); pwFillB.setAttribute('aria-hidden', 'true');
  view.appendChild(pwFillA);
  view.appendChild(pwFillB);
  pwCur = pwFillA;
  pwSel = null; pwSettling = null;
}

/* 铺满层的高度**只能由这里给死**：它不再是 inset:0 那种由包含块拉伸的
   绝对定位（挂到 .view 之后 inset:0 会铺满整个可滚动内容，比屏幕还高），
   所以不写 height 的话高度就是 0，什么都看不见。
   高度取**滚动容器的可视高度** —— .view 自己避开了工具栏和底部按钮条，
   量它就等于量「面板留给照片的那块地方」，比任何反推都准。
   宽度交给样式表（width:100%），横向永远铺满整宽。 */
function wallSizeFill() {
  const view = $(pwTarget().view);
  if (!view) return;
  [pwFillA, pwFillB].forEach(f => { if (f) f.style.height = view.clientHeight + 'px'; });
}

/* 墙的几何 —— 只回答「格子在哪儿、多大」和「一行几格」。
   ⚠ **这里的 h / rows 不再是铺满层的终点高度**（2026-09-18 改）：
   以前拿网格 clientHeight 反推行数，再乘出一面「墙」当铺满终点，于是
   一行照片铺满成一行、多行照片铺到面板外面去。现在终点是 wallFillRel().h。
   h/rows 留着只为了两件事：抽格子的候选范围、以及 offsetTop 相对谁量。

   格子尺寸用 offsetWidth 而不是 getBoundingClientRect()：后者是**变换后**的
   尺寸，而放大到 1.5 倍的那一拍格子上正挂着 scale(1.5)，
   量它会得到 1.5 倍的墙 —— 大图的起点和落点会一起错开半格。
   宽高一律取浮点：clientWidth 减去左右边距和间隙，再除以列数 ——
   offsetHeight 是取整值，83.5 会报成 84，铺满整屏时被放大几十倍能看出错位。 */
function wallBoxInfo() {
  const grid = pwGrid();
  const cell = grid && grid.querySelector(pwCellSel());
  if (!cell || !grid) return null;
  const cs = getComputedStyle(grid);
  const cols = parseInt(cs.gridTemplateColumns.split(' ').length, 10) || 1;
  const gap = parseFloat(cs.rowGap) || 0;
  const padL = parseFloat(cs.paddingLeft) || 0;
  const padR = parseFloat(cs.paddingRight) || 0;
  const padT = parseFloat(cs.paddingTop) || 0;
  const padB = parseFloat(cs.paddingBottom) || 0;

  const w = grid.clientWidth;
  const h = grid.clientHeight;
  const cw = (w - padL - padR - gap * (cols - 1)) / cols;
  const rows = Math.max(1, Math.round((h - padT - padB + gap) / (cw + gap)));
  const ch = cw;   // 两处格子都是正方形（.photo-cell / .media-item 的 aspect-ratio:1）
  return { grid, w, h, cw, ch, gap, cols, rows, padL, padR, padT, padB };
}

/* 铺满层盖哪一块：**面板的可见区**（= 用户真正看得见的那个框）。
   它每次都按当前滚动位置重算，所以往下滚时它跟着走 ——
   用户看到的大图永远填满面板留给照片的地方，跟照片有多少张无关。
   返回 null = 面板可见区整个落在网格之外（滚过头了 / 面板塌了），
   这一拍没地方铺，调用方应当跳过。 */
function wallFillRel(info) {
  const view = $(pwTarget().view);
  if (!view) return null;
  wallSizeFill();            // 面板尺寸可能刚变过（横竖屏、工具栏换行），先钉住高度
  const h = view.clientHeight;
  if (!h) return null;       // 面板整个塌了（隐藏的 tab）—— 没地方铺
  /* 层钉在哪儿：**面板可见区的顶端**，用滚动偏移直接算。

     ⚠ 这里**不能用 position:sticky**（第一版就是那么写的，实测三组全错）：
     层是网格后面的流内兄弟，sticky 只在它自己的包含块里有行程，而它在
     「网格之后」这块地方根本没有行程 —— top:0 形同虚设，它老老实实待在
     网格末尾（实测 3 张时层顶在 989px，而面板可见段是 460~788px）。
     而且 sticky 会把层算进流里、把文档撑高，越弄越乱。

     改成 absolute + 每次重算 top：层的顶跟着**滚动位置**走，但夹在网格的
     上下边界之间 —— 面板展示网格的哪一段，大图就贴在哪一段的顶上。
     网格顶就是 .view 内容的顶，所以「网格起点」对应 scrollTop 0。 */
  /* 「网格顶在 .view 内容里的偏移」用 rect 差推，不用 offsetTop：
     两个元素不一定共用一个 offsetParent，减出来的数没有意义。
     rect 差是屏幕坐标，但 view 正滚着的时候 grid.top 和 view.top 一起动，
     它们的**差**恰好就是网格顶相对 view 可视段顶的偏移，正是要的那个数。
     加回 scrollTop 就换算成「相对 view 内容顶」的坐标系，和层的 top 同一套。 */
  const gr = info.grid.getBoundingClientRect(), vr = view.getBoundingClientRect();
  const gridTopInView = (gr.top - vr.top) + view.scrollTop;
  const maxTop = Math.max(0, info.h - h);          // 层顶能滑到的最下面
  const top = Math.min(maxTop, Math.max(0, view.scrollTop - gridTopInView));
  return { top, left: 0, w: info.w, h, maxTop };
}

/* 某一格在墙里的几何：中心偏移（相对墙中心）和「起点缩放」。
   起点缩放必须分 x/y 两个 —— 格子是正方形、整面墙不是，
   只写一个 scale 会让大图从一个比格子窄的条形里长出来（首页踩过，见那边 xf()）。 */
/* 某一格相对**铺满层**的几何：中心偏移 + 起点缩放。
   ⚠ 这里的 w/h 是铺满层自己的尺寸（= 面板可见区），不是墙。
   transform 的百分比/像素都相对元素自身，所以偏移要减掉层自身的一半。
   起点缩放必须分 x/y —— 格子是正方形、面板可见区不是，
   只写一个 scale 会让大图从一个比格子窄的条形里长出来（首页踩过，见那边 xf()）。 */
function wallGeom(cell) {
  const info = wallBoxInfo();
  if (!info) return null;
  const f = wallFillRel(info);
  if (!f) return null;                 // 面板可见区在网格外面，这一拍铺不了
  const { w, cw, ch, gap, padL, padT } = info;
  const col = Math.round((cell.offsetLeft - gap / 2) / (cw + gap));
  const row = Math.round((cell.offsetTop - gap / 2) / (ch + gap));
  // 格子在网格可滚动内容里的绝对中心。用 col/row 反推而不是直接读 offsetWidth/2，
  // 是为了和 wallGrowPos 的让位量用**同一套**浮点步长推，两边才严丝合缝。
  const p = wallGrowPos(cell, info);
  // 绝对中心 → 相对铺满层：先减面板可见区的左边缘，再减层的中心。
  // f.top 是层的**顶端**而非中心，所以要补上 f.h/2（原来墙这一项是 h/2，
  // 但那时 h 就是终点高度；现在终点高度是 f.h，别混用 —— 这正是这个 bug 的根）。
  const absX = padL + col * (cw + gap) + cw / 2 + p.dx;
  const absY = padT + row * (ch + gap) + ch / 2 + p.dy;
  return {
    cx: absX - f.left - w / 2,
    cy: absY - (f.top + f.h / 2),
    // 起点缩放用**浮点**的 cw/ch，不用 offsetWidth/offsetHeight（那是取整值）：
    // 83.5 对 84 差 0.5px，铺满整屏时会被放大几十倍，边角上能看出错位。
    sx: cw / w,
    sy: ch / f.h,
  };
}

/* 放大这一格时要不要往里让。
   1.5 倍比「贴边那格原地放大不出去」的上限大，所以放大量得花在可见区里：
   哪边不够就往反方向推那么多。正中间那些格子两边都够，算出来是 0 —— 原地放大。
   描边要贴着可见区的边而不是越出去，所以可用空间再减掉 PW_RING —— 让位之后，
   描边的外沿正好落在边上（w/h 量的是 clientWidth/clientHeight，也就是内边距盒）。 */
function wallGrowPos(cell, info) {
  const ii = info || wallBoxInfo();
  if (!ii) return { dx: 0, dy: 0 };
  const { cw, ch } = ii;
  // ⚠ 「墙」的边界现在是**面板可见区**，不是网格（2026-09-18 改）：
  // 放大那一拍要贴着用户看得见的那块地方的边，而不是贴着一面可能比屏幕高
  // 好几倍的网格的边。面板可见区取不到（滚到网格外了）就退化成原地放大。
  const f = wallFillRel(ii);
  if (!f) return { dx: 0, dy: 0 };
  const over = (PW_GROW - 1) / 2;
  // offsetLeft/offsetTop 是相对 offsetParent（就是网格自己，position:relative）的
  // **内边距盒**量的，网格自身的 padding 已经算在里面了 —— 再加一次 padL/padT
  // 就多算了一个 padding，贴边那几格会被往里推多那么些（相册 padding 2px，
  // 加上原来 8 的余量，实测离墙边差了 4px）。
  // 竖直方向先减掉面板可见区在网格里的偏移，换算进「相对可见区」的坐标系。
  const x = cell.offsetLeft, y = cell.offsetTop - f.top;
  const l = x - PW_EDGE_PAD,      r = f.w - (x + cw) - PW_EDGE_PAD;
  const t = y - PW_EDGE_PAD,      b = f.h - (y + ch) - PW_EDGE_PAD;
  // 负数是允许的：贴边那一格本来就没有 PW_RING 那么多余量，
  // 让位量会多推出这一点，把描边挤回墙里。
  return {
    dx: Math.max(0, over * cw - l) - Math.max(0, over * cw - r),
    dy: Math.max(0, over * ch - t) - Math.max(0, over * ch - b),
  };
}

function wallSetGrowPos(cell, p) {
  if (!p) { cell.style.removeProperty('--pw-dx'); cell.style.removeProperty('--pw-dy'); return; }
  cell.style.setProperty('--pw-dx', p.dx + 'px');
  cell.style.setProperty('--pw-dy', p.dy + 'px');
}

/* 把一格彻底恢复成没被选中的样子。
   **在选新格子之前必须调一次**：选中态是挂在 [data-pw-pick] 上的属性规则，
   上一轮那格（阶段 ④ 改成 [data-pw-settle] 之后还能挂进这一轮）如果不清，
   它就一直带着 --pw-g / 描边 / z-index:2 —— 表现是「墙上多了一格亮着的」，
   而且紧接着改成选中态时会从 1.5 倍**硬切**回去（因为它已经没有过渡了）。 */
function wallRestoreCells() {
  const grid = pwGrid();
  if (!grid) return;
  grid.querySelectorAll('[data-pw-pick], [data-pw-settle]').forEach(c => {
    c.removeAttribute('data-pw-pick');
    c.removeAttribute('data-pw-settle');
    c.removeAttribute('data-pw-round');
    c.style.removeProperty('--pw-g');
    c.style.removeProperty('--pw-dx');
    c.style.removeProperty('--pw-dy');
    c.style.transition = '';
  });
  pwSel = null; pwSettling = null;
}

/* 抽一格：在**这一轮还没放过的**格子里随机取一个。整面墙走完一遍就清空
   pwPlayed、接着开新的一遍 —— 播放本身永远不会因为「放完了」而停。
   用「剩下的里面随机取」而不是预先洗一条队列：队列要跟着筛选/排序/缩放重建，
   而集合天然容错（新格子本来就不在集合里）。
   返回 null 表示这个网格里没有格子可播。 */
function wallPlayableCells() {
  const grid = pwGrid();
  if (!grid) return [];
  // 每一拍都重新查一次 DOM：列表可能被筛选/排序/缩放整个换掉。
  // 判据是「在 cellWallByCell 里登记过」而不是「有 <img>」：
  //   · 点详情的网格里还混着「＋」和上传中的占位格，它们也有 <img>，但不是媒体；
  //   · 视频格可能只有个「🎬」占位符没有 <img>，而视频现在是要播的（见 pwPlayFillVideo）。
  return Array.from(grid.querySelectorAll(pwCellSel())).filter(c => {
    const m = cellWallByCell.get(c);
    return !!m && (m.type === 'video' || !!c.querySelector('img'));
  });
}

function wallPickCell() {
  const cells = wallPlayableCells();
  if (!cells.length) return null;
  let left = cells.filter(c => !pwPlayed.has(c));
  if (!left.length) {            // 这一遍走完了 —— 清空重来，接着放下一遍
    pwPlayed.clear();
    left = cells.slice();
  }
  let i = Math.floor(Math.random() * left.length);
  // 新一遍的第一张别又是上一遍的最后一张（只有这一处会出现「连着两次同一格」）
  if (left.length > 1 && left[i] === pwPrevCell) i = (i + 1) % left.length;
  const cell = left[i];
  pwPlayed.add(cell);
  return cell;
}

/* 往铺满层上写「这一格的照片」。
   老图加载失败（签名过期最常见）就**换这一格的缩略图**兜底 ——
   缩略图是同一张，画质差一点但总比整面墙空一拍好。
   返回 Promise：等图真的解码完再让大图出现，否则铺满那一拍会是空的。 */
function wallSetFillImage(fill, m, cell) {
  return new Promise(resolve => {
    // 视频：铺满层先铺这一格的封面（缩略图），等长到位了再把 <video> 盖上去起播。
    // 直接拿原视频当背景图当然不行；而「长开」这一拍必须有画面，
    // 不然从格子长到铺满的整段是一块纯色，看着像卡住了。
    if (m && m.type === 'video') {
      const thumb = (cell && cell.querySelector('img') && cell.querySelector('img').currentSrc)
        || (typeof mediaSrc === 'function' ? mediaSrc(m, 'thumb') : '');
      if (!thumb) { fill.style.backgroundImage = ''; resolve(true); return; }
      const iv = new Image();
      let vdone = false;
      const vfin = ok => { if (!vdone) { vdone = true; fill.style.backgroundImage = ok ? `url("${thumb}")` : ''; resolve(true); } };
      iv.onload = () => vfin(true);
      iv.onerror = () => vfin(false);
      iv.src = thumb;
      return;
    }
    const url = wallMediaUrl(m);
    if (!url) { resolve(false); return; }
    const img = new Image();
    let done = false;
    const finish = ok => { if (!done) { done = true; fill.style.backgroundImage = ok ? `url("${url}")` : ''; resolve(ok); } };
    img.onload  = () => finish(true);
    img.onerror = () => {
      // 原图不行就退到缩略图。再不行就放弃这一拍（resolve(false)），
      // 由调用方决定是跳过还是硬着头皮铺个空的。
      const thumb = cell.querySelector('img');
      if (thumb && thumb.currentSrc && thumb.currentSrc !== url) {
        const img2 = new Image();
        img2.onload = () => { img2.onload = null; finish(true); };
        img2.onerror = () => finish(false);
        img2.src = thumb.currentSrc;
        // 兜底图的地址交给它自己：把 finish(true) 里那句再走一遍
        setTimeout(() => {
          if (!done && img2.complete && img2.naturalWidth) {
            done = true; fill.style.backgroundImage = `url("${thumb.currentSrc}")`; resolve(true);
          }
        }, 1200);
        return;
      }
      finish(false);
    };
    img.src = url;
  });
}

/* 把铺满层上的 <video> 拆干净。停播、切墙、收尾都要走一趟 ——
   留着不管的话它会在看不见的地方继续出声。不传参数就清两层。 */
/* 把 CSS 兜底全屏时搬去 body 的铺满层插回原处。
   next 可能已经被别的重建摘掉了，所以要确认它还是同一个父节点的孩子。 */
function pwFillGoHome(f) {
  const h = f && f._pwHome;
  f._pwHome = null;
  if (!h || !h.parent || !h.parent.isConnected) return;
  const ref = (h.next && h.next.parentElement === h.parent) ? h.next : null;
  h.parent.insertBefore(f, ref);
}

function pwKillFillVideos(fill) {
  const layers = fill ? [fill] : [pwFillA, pwFillB];
  layers.forEach(f => {
    if (!f) return;
    f.querySelectorAll('video').forEach(v => {
      // 有 _pwTeardown 就走它：那里面连退全屏、摘 fullscreenchange 监听、
      // 清 pwVideoHold 一起做了。少走一趟的话，停播之后整块屏幕会留在全屏态，
      // 而 pwVideoHold 一直挂着，之后的 resize 就再也不敢重排几何了。
      if (v._pwTeardown) { v._pwTeardown(); return; }
      if (v._pwCap) { clearTimeout(v._pwCap); v._pwCap = null; }
      try { v.pause(); } catch (e) {}
      v.removeAttribute('src');
      try { v.load(); } catch (e) {}    // 断掉还在下载的那一段，别占着隧道
      v.remove();
    });
    // 控制条那一态跟着视频一起作废，否则下一轮换成照片时这层还挂着标记
    f.removeAttribute('data-ctl');
    if (f.hasAttribute('data-fscss')) { f.removeAttribute('data-fscss'); pwFillGoHome(f); }
    f.querySelectorAll('.pw-fill-fs').forEach(b => b.remove());
  });
  // 全屏钮平时挂在 body 上，上面那圈按层清理扫不到它。
  // **只在不带参数（= 整体停播）时扫**：带 fill 参数的那条路是收尾层的清理，
  // 此刻新一轮可能已经在另一层上跑起来了，一起扫会把它的按钮误删。
  if (!fill) document.querySelectorAll('body > .pw-fill-fs').forEach(b => b.remove());
}

/* 铺满到位之后起播这条视频，**播完了才收尾进下一张**（诉求：不跳过视频）。
   三条出路都收敛到同一个 done：正常 ended / 解不了码 / 撞上保险丝。
   声音：先按原声试一次，被 autoplay policy 拒了就静音再试一次 ——
   和灯箱里那段视频用的是同一套退让（见 app.js 的本地预览）。

   ---- 手指介入 ----
   轻触这一层会亮出**原生控制条**（暂停 / 拖进度 / 音量）和右上角的**全屏钮**，
   整面墙**不停播**。这里的关键是那一下点击**不能穿透**：铺满层是
   `pointer-events:none`，原来手指落在视频上其实是打到了底下的格子，而格子的
   onclick 第一句就是 `pwStop()` ——「碰一下视频，整个自动播放就没了」正是从这儿来的。
   所以 .pw-fill-video 单独把 pointer-events 收回来（子元素可以覆盖父层的 none），
   点击到此为止。

   暂停期间不会推进到下一张：这一拍本来就是等 ended 才收尾的，用户按暂停只是
   让这个「等」变长，保险丝顺势换成 PW_VIDEO_PAUSE_MAX 那根长的。 */
/* 这个环境有没有**原生**全屏可用。三条路任一条通就算通：
   标准 Element.requestFullscreen / 老 WebKit 的前缀版 / iPhone 上
   <video> 专属的 webkitEnterFullscreen。 */
function fillHasNativeFs(v) {
  const E = Element.prototype;
  return !!(E.requestFullscreen || E.webkitRequestFullscreen || v.webkitEnterFullscreen);
}

function pwPlayFillVideo(fill, m, cell, done) {
  const url = wallMediaUrl(m);
  if (!url) { pwLater(done, PW_DWELL_FULL); return; }
  const v = document.createElement('video');
  v.className = 'pw-fill-video';
  v.playsInline = true;
  v.setAttribute('playsinline', '');
  v.setAttribute('webkit-playsinline', '');
  v.preload = 'auto';
  v.controls = false;
  /* 原生控制条上那个全屏钮和右上角我们自己那个是重复的，藏掉一个。
     **只在原生全屏确实走得通的时候藏** —— 落到 CSS 兜底的浏览器多半也不认
     controlsList，藏不掉也不该藏：那时原生那个反而是多一条路。
     （藏掉之后全屏里的「退出」就只剩右上角那个 + Esc，实测够用。） */
  if (fillHasNativeFs(v)) v.setAttribute('controlsList', 'nofullscreen');
  let over = false;

  /* ---- 全屏 ----------------------------------------------------------------
     三条路，按平台挑一条能走的：
       ① 标准 Element.requestFullscreen —— 桌面 / 安卓 Chrome
       ② webkitRequestFullscreen —— 老 WebKit
       ③ iPhone 上 Element 级全屏**根本不存在**，只有 <video> 自己的
          webkitEnterFullscreen（进去是 iOS 原生播放器，自带退出）
     一条都走不通（微信内置浏览器偶尔如此）就退回 CSS 铺满，和地图全屏
     同一个兜底思路（见 share.js 的 initMapFullscreen）。

     ⚠ 全屏的是 fill 这一层而不是 <video>：全屏钮是 video 的**兄弟节点**，
     全屏 <video> 的话按钮不在渲染子树里，CSS 兜底那条路就没法退出来了。
     标准全屏下原生控制条仍在，iOS 那条路则由系统播放器接管。 */
  /* 全屏钮**挂在 body 上**，不在铺满层里：铺满层盖的是整个相册网格，
     而网格通常比可视区高一大截，摆在它的右下角等于摆到屏幕外面去。
     挂 body + fixed 才能稳稳钉在拇指够得到的那个角（见 style.css .pw-fill-fs）。
     进全屏时再移进铺满层 —— 原生全屏只渲染全屏元素那棵子树，留在 body 上
     的话全屏里就看不见它，也就没法退出来了。 */
  const fsBtn = document.createElement('button');
  fsBtn.type = 'button';
  fsBtn.className = 'pw-fill-fs';

  const fsNativeEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const fsOn = () => !!(fsNativeEl() === fill || v.webkitDisplayingFullscreen
                        || fill.hasAttribute('data-fscss'));

  /* CSS 兜底的进/出。**必须把这一层搬到 body 下**：三个 tab 页做成了一条
     横向轨道（切换时整条 transform 平移），而 transform 祖先会把
     position:fixed 的包含块从**视口**改成**它自己** —— 不搬走的话所谓
     「全屏」只铺满面板那一小块（实测 420×350，而视口是 420×860）。
     退出时原样插回去：铺满层是按 .view 的直接子层定位的，
     留在 body 上会让下一轮的 wallEnsureLayer 认不出它、重建出一对孤儿层。 */
  const fsCss = on => {
    if (on) {
      if (fill.hasAttribute('data-fscss')) return;
      fill._pwHome = { parent: fill.parentElement, next: fill.nextSibling };
      document.body.appendChild(fill);
      fill.setAttribute('data-fscss', '');
    } else {
      if (!fill.hasAttribute('data-fscss')) return;
      fill.removeAttribute('data-fscss');
      pwFillGoHome(fill);
    }
  };

  /* 状态同步。**两个标记都在这里更新** —— 进/退全屏、亮控制条、拆元素
     全都会走到这儿，别的地方不要再各自去改（漏一处就会卡住 resize）。 */
  const fsSync = () => {
    const on = !over && fsOn();
    // 按钮跟着全屏态换窝：全屏里进铺满层（否则不在渲染子树里，看不见也点不到），
    // 退出来回 body（否则又会掉到网格底部的屏幕外去）。
    const home = on ? fill : document.body;
    if (!over && fsBtn.parentElement !== home) home.appendChild(fsBtn);
    pwVideoFs = on;
    pwVideoHold = !over && (on || v.controls);
    fsBtn.classList.toggle('active', on);
    fsBtn.title = on ? '退出全屏' : '全屏播放';
    fsBtn.setAttribute('aria-label', fsBtn.title);
    fsBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  };

  const fsEnter = () => {
    const req = fill.requestFullscreen || fill.webkitRequestFullscreen;
    if (req) {
      try {
        const r = req.call(fill);
        if (r && r.catch) r.catch(() => { fsCss(true); fsSync(); });
      } catch (e) { fsCss(true); }
    } else if (v.webkitEnterFullscreen) {
      // 不先查 webkitSupportsFullscreen：它要等元数据到位才为真，而这里
      // 视频早就在播了。真没就绪的话 webkitEnterFullscreen 自己会抛，
      // catch 到再退 CSS 兜底 —— 比提前判假、白白放弃原生全屏强。
      try { v.webkitEnterFullscreen(); } catch (e) { fsCss(true); }
    } else {
      fsCss(true);
    }
    fsSync();
  };

  const fsExit = () => {
    if (fill.hasAttribute('data-fscss')) { fsCss(false); fsSync(); return; }
    if (v.webkitDisplayingFullscreen && v.webkitExitFullscreen) {
      try { v.webkitExitFullscreen(); } catch (e) {}
    } else if (fsNativeEl()) {
      const ex = document.exitFullscreen || document.webkitExitFullscreen;
      try { const r = ex.call(document); if (r && r.catch) r.catch(() => {}); } catch (e) {}
    }
    fsSync();
  };

  const fsExitHook = () => { if (!over) fsExit(); };
  pwVideoFsExit = fsExitHook;

  fsBtn.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    if (over) return;
    fsOn() ? fsExit() : fsEnter();
  });

  // 用户用浏览器自己的方式退出（Esc / 手势 / iOS 播放器的「完成」）也要同步过来，
  // 否则 pwVideoHold 会一直挂着，resize 永远不敢重排几何。
  const onFsChange = () => fsSync();
  document.addEventListener('fullscreenchange', onFsChange);
  document.addEventListener('webkitfullscreenchange', onFsChange);
  v.addEventListener('webkitbeginfullscreen', onFsChange);
  v.addEventListener('webkitendfullscreen', onFsChange);

  /* 拆场。finish 和 pwKillFillVideos（停播/切墙那条路）都要走一遍，
     所以挂在元素上，两边共用同一份，不会漏掉退全屏和摘监听。 */
  let tornDown = false;
  const teardown = () => {
    if (tornDown) return;
    tornDown = true;
    over = true;
    if (v._pwCap) { clearTimeout(v._pwCap); v._pwCap = null; }
    fsExit();                       // 别把整块屏幕留在全屏态
    fsCss(false);
    pwVideoFs = false; pwVideoHold = false;
    // 只在这个钩子还是自己那个的时候才摘：收尾层和新一轮理论上不会重叠，
    // 但真重叠了也不能让旧的把新的那条视频的退出入口抹掉。
    if (pwVideoFsExit === fsExitHook) pwVideoFsExit = null;
    document.removeEventListener('fullscreenchange', onFsChange);
    document.removeEventListener('webkitfullscreenchange', onFsChange);
    try { v.pause(); } catch (e) {}
    v.removeAttribute('src');
    try { v.load(); } catch (e) {}  // 断掉还在下载的那一段，别占着隧道
    v.remove();
    fsBtn.remove();
    fill.removeAttribute('data-ctl');
  };
  v._pwTeardown = teardown;

  /* 保险丝走裸 setTimeout：pwLater 会被下一轮开头的 pwClearTimers 清掉，
     那样一条卡住的长片就永远等不到收尾。挂在元素上，拆元素时一起清
     （见 pwKillFillVideos）。播放状态一变就重新上一根，长度按状态选。 */
  const arm = ms => {
    if (v._pwCap) clearTimeout(v._pwCap);
    v._pwCap = setTimeout(() => finish(0), ms);
  };
  const rearm = () => { if (!over) arm(v.paused ? PW_VIDEO_PAUSE_MAX : PW_VIDEO_MAX); };

  const finish = (delay) => {
    if (over) return;
    teardown();
    if (delay) pwLater(done, delay); else done();
  };
  v.addEventListener('ended', () => finish(0));
  // 浏览器解不了码（iPhone 原片最常见）：当成普通一拍，让封面再停一会儿就换下一张
  v.addEventListener('error', () => finish(PW_DWELL_FULL));

  /* 第一下轻触只负责「亮出控制条和全屏钮」，**不改播放状态**：顺手暂停的话，
     只想看清楚画面的人反而得再点一次才能继续。之后的点击全交给原生控件，
     这个 handler 什么也不做（所以不会和控制条上的播放键打架）。 */
  const wake = () => {
    if (over || v.controls) return;
    v.controls = true;
    fill.setAttribute('data-ctl', '');   // 铺满层自己那一态（原生控制条已亮）
    fsBtn.setAttribute('data-on', '');   // 亮出全屏钮，见 .pw-fill-fs[data-on]
    fsSync();                            // 控制条亮了就算「人在操作」，别让 resize 重起这一拍
  };
  v.addEventListener('click', wake);
  // iOS 上落在原生控制条里的 click 不一定冒到这儿，pointerdown 更早也更稳
  v.addEventListener('pointerdown', wake);

  // 播 / 停 / 拖进度都重新上保险丝：正在看的长片不该被 3 分钟那根切掉，
  // 手动暂停后则换成 10 分钟那根。ended 之后不再续（此时 over 已经为真）。
  v.addEventListener('play', rearm);
  v.addEventListener('pause', () => { if (!v.ended) rearm(); });
  v.addEventListener('seeking', rearm);

  arm(PW_VIDEO_MAX);
  fill.appendChild(v);
  document.body.appendChild(fsBtn);
  fsSync();
  v.src = url;
  if (typeof mediaFallbackToLocal === 'function' && m.filename) {
    mediaFallbackToLocal(v, '/uploads/' + m.filename);
  }
  const tryPlay = () => v.play().catch(() => {
    v.muted = true;
    return v.play().catch(() => finish(PW_DWELL_FULL));
  });
  if (v.readyState >= 2) tryPlay();
  else v.addEventListener('loadeddata', tryPlay, { once: true });
}

function pwClearTimers() {
  pwTimers.forEach(clearTimeout);
  pwTimers = [];
}

/* 自清理的定时器：每次 later 往数组里塞一个 id，触发时把自己的 id 摘掉。
   数组长度恒等于「在飞的数量」—— 不然长挂在页面上会一直涨（同首页）。 */
function pwLater(fn, ms) {
  const id = setTimeout(() => {
    const i = pwTimers.indexOf(id);
    if (i >= 0) pwTimers.splice(i, 1);
    fn();
  }, ms);
  pwTimers.push(id);
}

/* 渐隐放完之后把铺满层交还样式表，留着下一轮用。
   参数必须是**正在收尾的那一个** —— 绝不能在这里读全局 pwCur，
   因为此时它已经指向新一轮了（见 pWallRound 开头的换层）。 */
function pwCleanupFill(f) {
  pwKillFillVideos(f);
  f.removeAttribute('data-burst');
  f.removeAttribute('data-on');
  f.style.transition = 'none';
  f.style.opacity = '';
  f.style.transform = '';
  f.style.backgroundImage = '';
  requestAnimationFrame(() => { f.style.transition = ''; });
}

/* 推倒重来：清掉一切痕迹，回到「没播过」的状态。停播、切走 tab、改筛选/排序/缩放都走这里。 */
function pwReset() {
  pwClearTimers();
  pwKillFillVideos();
  const view = $(pwTarget().view);
  [pwFillA, pwFillB].forEach(f => {
    if (!f) return;
    f.removeAttribute('data-burst');
    f.removeAttribute('data-on');
    f.style.transition = 'none';
    f.style.opacity = '';
    f.style.transform = '';
    f.style.backgroundImage = '';
    f.style.top = '';            // 钉位还给样式表（默认 0），下次起拍重新算
    requestAnimationFrame(() => { if (!pwOn) f.style.transition = ''; });
  });
  if (view) view.querySelectorAll('.pw-fill').forEach(f => { if (f !== pwFillA && f !== pwFillB) f.remove(); });
  pwRestorePanelPos();           // 面板那行临时 position:relative 撤掉
  pwCur = pwFillA;
  pwPrevCell = null;
  wallRestoreCells();
}

/* 面板上那行临时定位用完要还回去。只还我们写过的那种：别把页面本来
   （或者别的脚本）设的定位给抹了。 */
function pwRestorePanelPos() {
  Object.keys(PW_TARGETS).forEach(k => {
    const p = $(PW_TARGETS[k].panel);
    if (!p || !p.dataset || p.dataset.pwPos === undefined) return;
    p.style.position = p.dataset.pwPos;
    delete p.dataset.pwPos;
  });
}

/* 一轮：换层 → 抽格 → 选中 → 放大 → 铺满 → 渐隐（渐隐的同时开下一轮）。 */
async function pWallRound() {
  if (!pwOn) return;
  wallEnsureLayer();
  if (!pwFillA || !pwFillB) { pwStop(); return; }
  wallSizeFill();

  // 先把上一轮的选中痕迹清干净：清除 + 重置过渡都在这里发生，
  // 所以只需要在 pWallRound 开头调一次。
  wallRestoreCells();

  // 换到另一个铺满层。上一个正在渐隐（或已渐隐完），从它身上长开互不干扰 ——
  // 这正是「收尾和新一轮同时跑」的底气。
  pwCur = (pwCur === pwFillA) ? pwFillB : pwFillA;
  const fill = pwCur;
  fill.removeAttribute('data-burst');
  fill.removeAttribute('data-on');
  fill.style.transition = 'none';
  fill.style.opacity = '';
  fill.style.transform = '';
  fill.style.backgroundImage = '';

  const info = wallBoxInfo();
  // ⚠ 层的 top 必须**在量几何之前**写死：top 一改会触发重排，同一帧量到的
  // rect 还是改动前那一版 —— 起点会整个错开。
  const f0 = info && wallFillRel(info);
  fill.style.top = (f0 ? f0.top : 0) + 'px';

  const cell = wallPickCell();
  const m = cell && cellWallByCell.get(cell);
  // 地址一律走 wallMediaUrl：主页面的媒体对象不一定带 m.url（同步到 OSS 之前只有
  // filename），直接判 m.url 会把这些格子当成「没得播」整片跳掉。
  if (!info || !cell || !m || !wallMediaUrl(m)) {
    // 没得播了（列表被清空 / 全是视频）：停掉并把按钮复位
    if (!cell || !cellsPlayable()) { pwStop(); return; }
    pwLater(pWallRound, PW_HOLD_STILL);   // 只是这一轮抽到没图的格子，歇一下再来
    return;
  }
  const g = wallGeom(cell);
  // g 为 null = 面板可见区整个落在网格之外（滚过头了），这一拍没地方铺 ——
  // 别硬铺，跳过去抽下一个。
  if (!g) { pwLater(pWallRound, PW_HOLD_STILL); return; }
  pwPrevCell = cell;

  fill.style.backgroundImage = '';
  fill.style.transition = 'none';
  fill.style.transform = pwXf(g.cx, g.cy, g.sx, g.sy);

  // ---- 阶段 ①：选中。亮起来 + 描边 + 抬起，先停 PW_HOLD_PICK -------------
  // 起点几何必须先写进内联样式：阶段 ③ 要把它置空才能由样式表接管动到铺满，
  // 置空那一瞬间如果还没进过 transition，会从起点**直接闪到**铺满、整个放大过程没了。
  // 清掉内联 --pw-g，让 CSS 的 `.photo-cell[data-pw-pick]{--pw-g:1.5}` 接管 ——
  // 上一轮如果给这格留过内联值（值高于样式表），不清的话它会停在 1 倍上：
  // 描边加上了、格子却没放大，看着像「选中了但不动」。
  cell.style.removeProperty('--pw-g');
  wallSetGrowPos(cell, wallGrowPos(cell, info));   // 必须在加属性**之前**写好，否则第一帧没位移
  cell.setAttribute('data-pw-pick', '');
  // 给这一格打上「本轮是第几轮」。抽签池是按格子去重的，而相册里筛选/排序/缩放
  // 都不重建 DOM，同一格自然会一轮轮被重复抽到；这个标记目前只用来兜住
  // 「异步加载失败时发现轮次已经变了」的情况（见下面 await 之后的重查）。
  cell.setAttribute('data-pw-round', String(++pwRound));
  const myRound = pwRound;
  pwSel = cell;

  pwLater(async () => {
    if (!pwOn) return;
    // ---- 阶段 ② 之后：先把图备好，再让大图长开 ------------------------
    // 图没解码完就长开的话，铺满那一拍会是块空白的背景色。
    // 这一步在「选中并停住」的这段时间里做，用户看到的是格子亮着、照片已经在准备了。
    const ok = await wallSetFillImage(fill, m, cell);
    if (!pwOn) return;
    if (!ok) {                       // 图实在拿不到：把这格恢复，跳过去抽下一个
      wallRestoreCells();
      pwLater(pWallRound, PW_HOLD_STILL);
      return;
    }
    if (cell.getAttribute('data-pw-round') !== String(myRound)) return;   // 网格被重建过了

    // ---- 阶段 ③：交接。格子开始缩回 1 倍，大图从它原来的位置长开 --------
    // 两件事同一个时长、同一条缓动 —— 视觉上是大图「取代」了那一格。
    // **置空内联 transform 是这一步的关键**：`.pw-fill[data-on]{transform:none}`
    // 是样式表规则，压不过内联值；不置空的话大图只会变透明、几何仍停在起点。
    fill.style.transition = '';
    cell.removeAttribute('data-pw-pick');
    cell.style.setProperty('--pw-g', '1');
    wallSetGrowPos(cell, null);
    // [data-pw-settle] 是「正在滑回原位」的收尾态。它比 [data-pw-pick] 晚写、
    // 同权重，所以能接住格子并把它送回 1 倍（属性一摘就回原位，这也是
    // 为什么格子**不需要**逐个复位的定时器）。
    cell.setAttribute('data-pw-settle', '');
    pwSel = null; pwSettling = cell;
    fill.setAttribute('data-on', '');
    fill.style.transform = '';

    // ---- 阶段 ⑤：边淡出边缩到 0（CSS 两条 animation）+ 同时开下一轮 ----
    const burstAndNext = () => {
      if (!pwOn) return;
      // 挂之前先摘一次并强制重算：animation 是 once 的，属性连着留两轮的话
      // 第二轮不会重新触发（浏览器认为「属性一直都在」）。
      fill.removeAttribute('data-burst');
      void fill.offsetWidth;
      fill.setAttribute('data-burst', '');
      // 渐隐期间**不要**碰它的内联 transform/opacity：keyframes 正管着 opacity，
      // 中途写内联会把它顶掉。等 PW_BURST 之后统一清场。
      const ending = fill;
      // ⚠ 这里**只能**用裸 setTimeout，不能走 pwLater。pwLater 会把 id 记进
      // pwTimers，而 pWallRound 开头的 pwClearTimers() 会把它连同本轮其它定时器
      // 一起清掉 —— 于是渐隐动画被中途放弃、铺满层永远不会被交还样式表，
      // 更糟的是它下一轮又要被 swap 到，`fill.removeAttribute('data-burst')`
      // 会当场把上一轮那层从「正在渐隐」打回「还铺着」，视觉上就是两层交替闪。
      // 实测：走 pwLater 时「收尾与新轮同帧」一帧都采不到，正是这么坏的。
      // 收尾清场是**独立于轮次**的一次性动作，用裸定时器让它跑完。
      setTimeout(() => pwCleanupFill(ending), PW_BURST);
      // ---- 阶段 ⑥：渐隐的**同时**开下一轮 -----------------------------
      // 排到 rAF 之后只隔一帧，视觉上就是同时起步；不能和上面那个同步块合并，
      // 否则浏览器把「挂 data-burst」和「新一轮写的起点」并成一次重算，
      // 渐隐的第一帧会被吞掉，整段看不见。
      requestAnimationFrame(() => { if (pwOn) pWallRound(); });
    };

    if (m.type === 'video') {
      // ---- 阶段 ④'：视频。铺满到位就起播，**整段播完**才收尾 ------------
      // 停留时长由视频自己说了算，所以这里不能用固定的 PW_DWELL_FULL。
      pwLater(() => {
        if (!pwOn) return;
        pwPlayFillVideo(fill, m, cell, burstAndNext);
      }, PW_GROW_FULL);
    } else {
      // ---- 阶段 ④：铺满，停 PW_DWELL_FULL ------------------------------
      pwLater(burstAndNext, PW_GROW_FULL + PW_DWELL_FULL);
    }
  }, PW_HOLD_PICK + PW_GROW_MS + PW_HOLD_GROW);
}

// 变换的统一写法：和 CSS `.pw-fill` 的初始 transform 一致（先平移到格子中心、再缩放）。
function pwXf(cx, cy, sx, sy) {
  return 'translate(' + cx + 'px,' + cy + 'px) scale(' + sx + ',' + sy + ')';
}

function cellsPlayable() {
  return wallPlayableCells().length > 0;
}

/* 网格重建（innerHTML='' / 换点 / 换筛选）之后，把已经脱离文档的格子从
   cellWallByCell 里摘掉。**不要直接 clear()** —— 两面墙共用这一张表，
   一面重建就把另一面的登记全抹了，切回去会发现「没得播」。 */
function wallForgetDetached() {
  cellWallByCell.forEach((_, cell) => {
    if (!cell || !cell.isConnected) cellWallByCell.delete(cell);
  });
  pwPlayed.forEach(cell => { if (!cell || !cell.isConnected) pwPlayed.delete(cell); });
}

/* 两个入口按钮（相册工具栏 / 点详情标题行）一起刷：只有**正在播的那面墙**
   对应的那颗亮起来，另一颗必须回到 ▶，否则切走之后它会一直停在「■ 播放中」。
   点详情那颗是纯图标按钮（背景图），别往里写文字 —— 打了 data-pw-icon="css" 的跳过 textContent。 */
function pwSetButton() {
  /* 播放中把 ICP 备案页脚藏掉（见 style.css 的 body.pw-wall-playing）。
     那行字是 fixed 贴底的，而铺满层会盖住整个网格 —— 不藏的话它正好压在
     照片上。挂在这里是因为开播/停播/切墙都必经这一个函数，不会漏掉一条路。 */
  document.body.classList.toggle('pw-wall-playing', pwOn);
  Object.keys(PW_TARGETS).forEach(k => {
    const b = $(PW_TARGETS[k].btn);
    if (!b) return;
    const on = pwOn && k === pwTargetKey;
    b.classList.toggle('pw-playing', on);
    if (b.dataset.pwIcon !== 'css') b.textContent = on ? '■' : '▶';
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.title = on ? '停止自动播放' : '自动播放：随机选一张放大铺满，再换下一张';
  });
}

function pwStop() {
  pwOn = false;
  pwPlayed.clear();
  pwReset();
  pwSetButton();
}

/* 「接着播」：网格被重建、列数变了、窗口尺寸变了 —— 这些都让起点几何当场作废，
   但**都不该停播**。用户只按过一次播放键，在他再按一次之前，播放这件事不结束。
   所以这里只把这一拍的残留清掉，然后从新几何上重起一拍；pwPlayed 不动，
   走到一半的那一遍会接着走完。 */
function pwRestart() {
  if (!pwOn) return;
  pwReset();
  pwEnsureLayerNow();            // 层补不回来（网格真没了）时它自己会 pwStop
  if (!pwOn) return;
  pwLater(pWallRound, PW_HOLD_STILL);
}

/* 跟着 tab 走。切走的那一页整个滑出了可视区，接着放纯属白烧 CPU（视频还会在
   背后出声），所以**停手**；但 pwOn 保持为真 —— 切回来就接着放，
   用户并没有按停止。挂在 positionViews 里，点击和横滑切页都会经过那儿。 */
function pwSyncVisibility() {
  if (!pwOn) return;
  const view = $(pwTarget().view);
  /* 判「没写成 true」而不是「写着 false」：三个 .view 一直带着这个属性，
     而搜图结果墙的 #search-view 不在 tab 轨道上、压根没人改它 ——
     按「必须等于 false」算的话，搜图这面墙会被当成切走了，一边 pwOn 为真
     一边被 pwReset 掐掉，按钮亮着却什么都不放。 */
  const shown = !!view && view.getAttribute('aria-hidden') !== 'true';
  if (shown) pwRestart();
  else pwReset();
}

function pwToggle(target) {
  /* 播放中再点一下 = **停**（不是暂停）。这里刻意不做「暂停后原地留着」：
     停在铺满那一拍时，大图正盖着整个网格，用户按了按钮却还是看不见缩略图 ——
     看着就像按钮失灵了。真正要看某一张，去点那一格开大图（见 cell.onclick）。
     所以这里一路走 pwStop，把两层都清干净、网格立刻露出来。 */
  if (pwOn && (!target || target === pwTargetKey)) { pwStop(); return; }
  if (target) pwSetTarget(target);      // 换墙（内部会先把上一面停掉复位）
  if (pwReducedMotion()) return;        // 系统里勾了「减少动态效果」就完全不播
  if (!cellsPlayable()) return;
  pwOn = true;
  pwEnsureLayerNow();
  pwSetButton();
  pwLater(pWallRound, PW_HOLD_STILL);
}

// 网格重建（切 tab / 改筛选 / 改排序 / 缩放）会把铺满层一起冲掉 ——
// 这里在重建之后立刻补回来，免得整段动画中途没了遮挡层。
function pwEnsureLayerNow() {
  wallEnsureLayer();
  wallSizeFill();
  const ok = pwFillA && pwFillA.isConnected && pwFillB && pwFillB.isConnected;
  if (!ok && pwOn) pwStop();
}


// 手动翻页、缩放、筛选、排序 —— 任何「用户自己动了一下」都停播：
// 他显然想看别的东西了，这时候还在背后放大缩小只会添乱。
/* ---- 一次性装配：绑定按钮和那些「用户动一下就别播了」的事件 --------------
   两页的 DOM 结构一致（同一套 .tab / #photo-grid / #lightbox），所以这段是共用的。
   各页脚本调一次 wallAttach() 即可。 */
var wallAttached = false;
function wallAttach() {
  if (wallAttached) return;
  wallAttached = true;

  Object.keys(PW_TARGETS).forEach(k => {
    const b = $(PW_TARGETS[k].btn);
    if (b) b.addEventListener('click', () => pwToggle(k));
  });
  // ⚠ 这里原来还挂着「在这一页上滚一下 / 捏一下就 pwStop」。去掉了：
  // 手机上随便滚两下页面就把播放掐了，而用户只按过播放、没按过停止。
  // 缩放确实会让几何作废，但那由改列数的地方调 pwRestart 接着播（见各页的 setPhotoZoom）。

  // 页面切到后台：停手不熄火，回到前台接着放
  document.addEventListener('visibilitychange', () => {
    if (!pwOn) return;
    if (document.hidden) pwReset();
    else pwRestart();
  });

  // 窗口尺寸一变，格子边长和墙的尺寸全变，起点几何立刻失效 ——
  // 从新几何重起一拍，而不是停播
  let rz;
  window.addEventListener('resize', () => {
    if (!pwOn) return;
    // 有人正在操作铺满层上那条视频（控制条亮着 / 正在全屏）就别动。
    // **进/退全屏自己就会触发 resize** —— 不挡住的话 pwRestart 会把用户
    // 刚点开的那条视频当场拆掉，看上去就是「一进全屏画面就没了」。
    if (pwVideoHold) return;
    clearTimeout(rz);
    rz = setTimeout(pwRestart, 200);
  });

  // Esc 停播。两种情况让路：灯箱开着（它自己要吃这个键）、
  // 正在全屏看视频（这一下 Esc 是「退出全屏」，浏览器自己处理，
  // 不能顺手把整面墙也停了）。只亮了控制条没进全屏时 Esc 照常停播 ——
  // 那是用户明确要结束，不该再拦。
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && pwOn) {
      if (pwVideoFs) { if (pwVideoFsExit) pwVideoFsExit(); return; }
      const lb = $('#lightbox');
      if (!lb || lb.classList.contains('hidden')) pwStop();
    }
  });
}


function pumpMapResize() {
  if (!map || typeof map.resize !== 'function') return;
  [60, 150, 240, PANEL_ANIM_MS + 40].forEach(t => setTimeout(() => {
    try { map.resize(); } catch (e) {}
  }, t));
}

function lbCurrentMedia() {
  return document.querySelector('#lightbox .lb-slide.cur .lightbox-media');
}

function lbApplyZoom(animate) {
  const m = lbCurrentMedia();
  if (!m) return;
  m.classList.toggle('zoom-anim', !!animate);
  m.style.transform = `translate(${lbZoom.tx}px, ${lbZoom.ty}px) scale(${lbZoom.scale})`;
}

// 平移量夹住，别把图拖出屏幕外
function lbClampPan() {
  const m = lbCurrentMedia();
  if (!m) return;
  const w = m.offsetWidth * lbZoom.scale, h = m.offsetHeight * lbZoom.scale;
  const maxX = Math.max(0, (w - window.innerWidth) / 2);
  const maxY = Math.max(0, (h - window.innerHeight) / 2);
  lbZoom.tx = Math.min(maxX, Math.max(-maxX, lbZoom.tx));
  lbZoom.ty = Math.min(maxY, Math.max(-maxY, lbZoom.ty));
}

function lbEnterZoom() {
  const m = lbCurrentMedia();
  if (!m || lbZoom.on) return;
  // 轮播里图片四周有留白，进全屏时先放大到贴住屏幕短边——这就是「放到全屏」的动画
  const r = m.getBoundingClientRect();
  const fit = r.width && r.height
    ? Math.min(window.innerWidth / r.width, window.innerHeight / r.height) : 1;
  lbZoom.on = true;
  lbZoom.fit = Math.max(1, fit);
  lbZoom.scale = lbZoom.fit;
  lbZoom.tx = 0; lbZoom.ty = 0;
  document.getElementById('lightbox').classList.add('zoomed');
  lbApplyZoom(true);
}

function lbExitZoom(animate) {
  const m = lbCurrentMedia();
  lbZoom.on = false;
  lbZoom.scale = 1; lbZoom.tx = 0; lbZoom.ty = 0;
  document.getElementById('lightbox').classList.remove('zoomed');
  if (m) {
    m.classList.toggle('zoom-anim', animate !== false);
    m.style.transform = '';
  }
}

function setLocateBtnState(state) {
  const b = document.getElementById('btn-locate');
  if (!b) return;
  b.classList.toggle('locating', state === 'locating');
  b.classList.toggle('located', state === 'located');
  b.disabled = state === 'locating';
}

// 画/移动蓝点。accuracy 是米，用来画精度圈；拿不到就不画圈。
function showMyLocation(lngLat, accuracy) {
  myLocLngLat = lngLat;
  if (!myLocMarker) {
    const dot = el('div', 'my-loc');
    dot.appendChild(el('span', 'my-loc-pulse'));
    dot.appendChild(el('span', 'my-loc-dot'));
    myLocMarker = new AMap.Marker({
      position: lngLat,
      content: dot,
      offset: new AMap.Pixel(-11, -11),
      zIndex: 160,          // 高于轨迹点(120)：自己的位置永远要看得见
      bubble: true,
    });
    myLocMarker.setMap(map);
  } else {
    myLocMarker.setPosition(lngLat);
  }
  // 精度圈：太准（<20m）就不画了，一个小圈没意义反而糊
  if (accuracy && accuracy > 20) {
    if (!myLocCircle) {
      myLocCircle = new AMap.Circle({
        center: lngLat, radius: accuracy,
        strokeColor: '#1a73e8', strokeWeight: 1, strokeOpacity: 0.35,
        fillColor: '#1a73e8', fillOpacity: 0.12,
        zIndex: 35,         // 压在路线(50)之下，不挡轨迹
      });
      myLocCircle.setMap(map);
    } else {
      myLocCircle.setCenter(lngLat);
      myLocCircle.setRadius(accuracy);
    }
  } else if (myLocCircle) {
    myLocCircle.setMap(null); myLocCircle = null;
  }
}

// 浏览器原生定位兜底。国内需要 WGS-84 → GCJ-02，否则会偏几百米。
function locateByBrowser() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error('浏览器不支持定位'));
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const g = wgs84ToGcj02(pos.coords.longitude, pos.coords.latitude);
        resolve({ lng: g.lng, lat: g.lat, accuracy: pos.coords.accuracy });
      },
      (err) => reject(err),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 });
  });
}

function locateByAMap() {
  return new Promise((resolve, reject) => {
    if (!window.AMap || !AMap.Geolocation) return reject(new Error('插件不可用'));
    const run = () => {
      try {
        if (!_amapGeo) {
          _amapGeo = new AMap.Geolocation({
            enableHighAccuracy: true, timeout: 10000,
            needAddress: false, showButton: false, showMarker: false, showCircle: false,
          });
        }
        _amapGeo.getCurrentPosition((status, r) => {
          if (status === 'complete' && r && r.position) {
            resolve({ lng: r.position.getLng(), lat: r.position.getLat(),
                      accuracy: r.accuracy || 0 });
          } else {
            reject(new Error((r && (r.message || r.info)) || '定位失败'));
          }
        });
      } catch (e) { reject(e); }
    };
    if (AMap.Geolocation) run();
    else AMap.plugin('AMap.Geolocation', run);
  });
}

function locateErrorText(e) {
  const msg = String((e && (e.message || e.info)) || e || '');
  // PositionError.code：1=拒绝 2=不可用 3=超时
  if (e && e.code === 1) return '定位被拒绝。请在浏览器地址栏的权限设置里允许获取位置';
  if (e && e.code === 3) return '定位超时，换个信号好一点的地方再试';
  if (!window.isSecureContext) return '当前不是 HTTPS 访问，浏览器不允许定位';
  if (/denied|permission/i.test(msg)) return '定位被拒绝。请在浏览器权限设置里允许获取位置';
  return '定位失败：' + (msg || '未知原因');
}

async function goToMyLocation() {
  if (locating) return;
  // 已经定过位：再点一次就是「回到我的位置」，不重新请求，响应更快
  if (myLocLngLat) {
    map.setZoomAndCenter(Math.max(map.getZoom(), 15), myLocLngLat, false, 400);
  }
  locating = true;
  setLocateBtnState('locating');
  let loc = null;
  try {
    loc = await locateByAMap();
    loc.via = 'amap';
  } catch (e1) {
    try {
      loc = await locateByBrowser();
      loc.via = 'browser';
    } catch (e2) {
      locating = false;
      setLocateBtnState(myLocLngLat ? 'located' : '');
      tmToast(locateErrorText(e2));
      return;
    }
  }
  locating = false;
  const ll = [loc.lng, loc.lat];
  myLocVia = loc.via;          // 'amap' | 'browser'，出问题时便于判断走的哪条路
  showMyLocation(ll, loc.accuracy);
  setLocateBtnState('located');
  map.setZoomAndCenter(Math.max(map.getZoom(), 15), ll, false, 400);
}

// 景区 POI：以当前视野中心搜索附近景点（仅国内有数据）
function clearPoiMarkers() {
  poiMarkers.forEach(function (m) { m.setMap(null); });
  poiMarkers = [];
}

function doPoiSearch() {
  if (map.getZoom() < 6) { clearPoiMarkers(); return; } // 缩太小不搜，避免密密麻麻
  if (!poiSearch) {
    AMap.plugin('AMap.PlaceSearch', function () {
      poiSearch = new AMap.PlaceSearch({ pageSize: 20, pageIndex: 1 });
      doPoiSearch();
    });
    return;
  }
  const c = map.getCenter();
  poiSearch.searchNearBy('景点', [c.lng, c.lat], 60000, function (status, res) {
    if (status !== 'complete' || !res || !res.poiList) { clearPoiMarkers(); return; }
    clearPoiMarkers();
    (res.poiList.pois || []).slice(0, 20).forEach(function (p) {
      if (!p.location) return;
      const m = new AMap.Marker({
        position: [p.location.lng, p.location.lat],
        content: '<div class="poi-label">' + esc(p.name) + '</div>',
        offset: new AMap.Pixel(0, 0),
        zIndex: 75,
        title: p.name
      });
      poiMarkers.push(m);
      m.setMap(map);
    });
  });
}

function toggleLayerPoi(on) {
  if (on) {
    doPoiSearch();
    map.on('moveend', onPoiMove);
  } else {
    map.off('moveend', onPoiMove);
    clearTimeout(poiTimer);
    clearPoiMarkers();
  }
}

function onPoiMove() {
  clearTimeout(poiTimer);
  poiTimer = setTimeout(doPoiSearch, 800);
}

// 卫星底图 / 路况
function toggleLayerSatellite(on) {
  if (on) {
    if (!layerSat) { layerSat = new AMap.TileLayer.Satellite(); layerRoad = new AMap.TileLayer.RoadNet(); }
    map.add([layerSat, layerRoad]);
  } else if (layerSat) {
    map.remove([layerSat, layerRoad]);
  }
  // 给地图容器打标记：卫星底图是深色的，地图上那几个无底片的图标要换成亮色描边
  const el = document.getElementById('map');
  if (el) el.classList.toggle('satellite', !!on);
}

function toggleLayerTraffic(on) {
  if (on) {
    if (!layerTraffic) layerTraffic = new AMap.TileLayer.Traffic();
    map.add(layerTraffic);
  } else if (layerTraffic) {
    map.remove(layerTraffic);
  }
}

function syncPhotoStackScale() {
  const el = document.getElementById('map');
  if (!el || !map || typeof map.getZoom !== 'function') return;
  const z = map.getZoom();
  const s = z <= 14
    ? Math.max(0.45, 0.45 + (z - 3) * 0.05)
    : Math.min(2, 1 + (z - 14) * 0.25);
  el.style.setProperty('--photo-stack-scale', s.toFixed(2));
}

function mercatorXY(lng, lat) {
  const s = Math.sin(lat * Math.PI / 180);
  return [lng / 360, -Math.log((1 + s) / (1 - s)) / (4 * Math.PI)];
}

function ensureRouteSvgLayer() {
  const host = document.querySelector('#map .amap-layers');
  if (routeSvgLayer && routeSvgLayer.isConnected) {
    if (host && routeSvgLayer.parentElement !== host) host.appendChild(routeSvgLayer);
    return routeSvgLayer;
  }
  if (!host) return null;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'route-svg-layer');
  host.appendChild(svg);
  routeSvgLayer = svg;
  return svg;
}

// 这一层必须挂在高德的 .amap-layers 里面，才能和瓦片(2)、路线(110)、marker(300)
// 排在同一个层叠上下文里比大小。
// 千万不要"找不到宿主就退而挂到 #map 上" —— #map 里 .amap-maps 是 z=0，
// 挂上去的层不管给多少 z-index 都在整张地图之上，会把路线整片盖住
// （实测这么做会吃掉 8.67% 的路线像素）。宿主还没就绪就先不画，等重试。
function ensurePhotoStackLayer() {
  const host = document.querySelector('#map .amap-layers');
  if (photoStackLayer && photoStackLayer.isConnected) {
    // 自愈：万一之前挂错了地方（或高德重建了图层树），搬回正确宿主，子元素跟着走
    if (host && photoStackLayer.parentElement !== host) {
      host.appendChild(photoStackLayer);
      photoStackLayer.classList.remove('fallback');
    }
    return photoStackLayer;
  }
  if (!host) return null;                 // 地图还没建好，交给调用方重试
  const box = document.createElement('div');
  box.className = 'photo-stack-layer';
  host.appendChild(box);
  photoStackLayer = box;
  return box;
}

// 地图图层自检：把当前渲染方式和层级顺序摊出来。
// 加 ?diag=map 打开页面就会在右下角显示，用来判断某台设备上路线到底画在哪一层。
function mapLayerDiag() {
  const host = document.querySelector('#map .amap-layers');
  const rows = [];
  rows.push('AMap ' + ((window.AMap && (AMap.version || AMap.v)) || '?'));
  rows.push('canvas ' + document.querySelectorAll('#map canvas').length +
            ' / svg ' + document.querySelectorAll('#map svg').length);
  if (!host) {
    rows.push('无 .amap-layers（1.4 结构）');
  } else {
    [...host.children].forEach(c => {
      const kids = [...c.children].map(k => k.tagName.toLowerCase()).slice(0, 2).join(',');
      rows.push('z=' + getComputedStyle(c).zIndex + ' ' +
                String(c.className).replace(/amap-layer\s*/, '').slice(0, 22) +
                (kids ? ' <' + kids + '>' : ''));
    });
  }
  rows.push('堆 ' + document.querySelectorAll('.photo-stack').length +
            (document.querySelector('.photo-stack-layer.fallback') ? '（降级层）' : ''));
  return rows.join('\n');
}

// ---------- 照片堆 / 📷 角标的点击命中 ----------
// 照片堆在折线层**之下**，而高德的矢量覆盖物层（SVG，pointer-events: auto）整片
// 盖在上面，命中测试永远落不到照片堆上 —— 实测在堆的范围里取样 64 个点，
// 命中堆 0 个、命中 SVG 层 40 个。所以点击得自己接：在 #map 的捕获阶段做一次
// 矩形命中判定，落在某个堆里就当成点了这个堆。

// 点/触在某个照片堆（或 📷 角标）的矩形里吗？命中就触发它，返回 true。
function tmHitPhotoTargetAt(clientX, clientY) {
  const mapEl = document.getElementById('map');
  if (!mapEl) return false;
  // 照片图层开着 → 判照片堆；关着 → 判 📷 角标（两者互斥显示）
  const on = mapEl.classList.contains('photo-layer-on');
  const list = on ? photoStackItems : photoBadgeItems;
  if (!list || !list.length) return false;
  // 倒序遍历：后加进 DOM 的在上面，重叠时优先命中
  for (let i = list.length - 1; i >= 0; i--) {
    const item = list[i].el;
    if (!item.offsetParent) continue;      // 当前不可见的跳过
    const r = item.getBoundingClientRect();
    if (!r.width) continue;
    const pad = 6;                         // 手指比鼠标粗，命中区四周放宽 6px
    if (clientX >= r.left - pad && clientX <= r.right + pad &&
        clientY >= r.top - pad && clientY <= r.bottom + pad) {
      item.click();          // 复用堆/角标自己的处理（照片堆那边含 500ms 去重）
      return true;
    }
  }
  return false;
}

// 轨迹点、地图按钮、图层面板这些自己有事件，不能抢
function tmIsMapOwnControl(target) {
  return !!(target && target.closest && target.closest(
    '.amap-marker, .map-fab, .layer-panel, .replay-bar, #add-point-bar, .amap-controls'));
}

// 给地图容器装上「点照片堆/角标」的命中处理。onMiss 是没命中时的兜底（主界面拿它
// 判路线段），可不传。
function tmBindMapPhotoTap(mapEl, onMiss) {
  if (!mapEl) return;
  mapEl.addEventListener('click', function (e) {
    if (tmIsMapOwnControl(e.target)) return;
    if (tmHitPhotoTargetAt(e.clientX, e.clientY)) {
      e.stopPropagation();     // 别让底下的路线也把「路段面板」打开
      e.preventDefault();
      return;
    }
    if (onMiss) onMiss(e);
  }, true);

  // 手机上光有 click 不够：高德 2.0 的地图容器自己接管了触摸手势并 preventDefault，
  // 浏览器于是**不再合成 click**，上面那个监听器一次都不触发 —— 表现就是
  // 「点地图上的图片集图标没有任何反应」。所以触摸单独走一遍同样的命中判定。
  // 判据：单指、位移小于 12px（不是在拖地图）、时长小于 700ms（不是长按）。
  let touchStart = null;
  mapEl.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1 || tmIsMapOwnControl(e.target)) { touchStart = null; return; }
    const t = e.touches[0];
    touchStart = { x: t.clientX, y: t.clientY, at: Date.now() };
  }, { capture: true, passive: true });
  mapEl.addEventListener('touchmove', function (e) {
    if (!touchStart || e.touches.length !== 1) { touchStart = null; return; }
    const t = e.touches[0];
    if (Math.abs(t.clientX - touchStart.x) > 12 ||
        Math.abs(t.clientY - touchStart.y) > 12) touchStart = null;   // 在拖地图
  }, { capture: true, passive: true });
  mapEl.addEventListener('touchend', function (e) {
    const st = touchStart;
    touchStart = null;
    if (!st || e.changedTouches.length !== 1) return;
    if (Date.now() - st.at > 700) return;                   // 长按，不算点击
    const t = e.changedTouches[0];
    if (Math.abs(t.clientX - st.x) > 12 || Math.abs(t.clientY - st.y) > 12) return;
    // 命中了就顺手把随后可能合成出来的 click 挡掉，避免开两次
    if (tmHitPhotoTargetAt(t.clientX, t.clientY)) {
      e.stopPropagation();
      if (e.cancelable) e.preventDefault();
    }
  }, { capture: true, passive: false });
}

// 📷 角标的重排。和照片堆分开成两个函数，是为了让调用方能控制先后：
// 两者同住一层，谁后写 left/top 谁在上面 —— 换行程时角标要走上一帧的旧坐标，
// 必须排在照片堆后面重排一次才能盖住（见 clearMap 之后的注释）。
function positionPhotoBadges() {
  if (!map || !map.lngLatToContainer) return;
  for (let i = 0; i < photoBadgeItems.length; i++) {
    const b = photoBadgeItems[i];
    const q = map.lngLatToContainer(new AMap.LngLat(b.lng, b.lat));
    b.el.style.left = Math.round(q.x + b.dx) + 'px';
    b.el.style.top = Math.round(q.y + b.dy) + 'px';
  }
}

// 把每个堆挪到它经纬度对应的屏幕位置。地图一动就要重算，所以要够轻。
function positionPhotoStacks() {
  if (!map || !map.lngLatToContainer) return;
  // 地图每动一次都顺手确认层还挂在正确的地方（高德重建图层树时会掉出来）
  if (photoStackLayer) ensurePhotoStackLayer();
  if (!photoStackItems.length) return;
  for (let i = 0; i < photoStackItems.length; i++) {
    const it = photoStackItems[i];
    const p = map.lngLatToContainer(new AMap.LngLat(it.lng, it.lat));
    // 用 left/top 定位而不是 transform：.photo-stack 的 transform 已经被
    // 「跟随缩放级别放大缩小」占用了（scale(var(--photo-stack-scale))），
    // 这里再写 transform 会把它覆盖掉，照片堆就不会随地图缩放变大变小了。
    // 偏移量和原来 AMap.Marker 的 offset(-28,-52) 一致，视觉位置不变。
    it.el.style.left = Math.round(p.x - 28) + 'px';
    it.el.style.top = Math.round(p.y - 52) + 'px';
  }
}

// 只清照片堆，不动 📷 角标 —— 角标属于 renderMap，和照片图层开关无关
function clearPhotoStackLayer() {
  photoStackItems.forEach(it => it.el.remove());
  photoStackItems = [];
}

// 重画地图时清掉旧角标
function clearPhotoBadges() {
  photoBadgeItems.forEach(it => it.el.remove());
  photoBadgeItems = [];
}

// ---------- 路段的公路编号 ----------
// 数据来源：高德驾车规划返回的 route.steps[].road（路名）。原来这个字段被直接丢掉了，
// 只取 step.path 拼几何 —— 其实里面就写着「G318」「X245」这类编号。
// 不新增任何请求：路线本来就要规划一次，顺手把路名捞出来。
//
// 为什么编号不能和里程共用一个胶囊：
// 两个轨迹点之间常常连着走好几条路（香格里拉 G214 转 G318 到鲁朗），
// 里程是**整段一个数**，编号却是「哪一截走的哪条路」—— 挤在一起既读不出对应关系，
// 路一多标签还会长到糊住整条线。所以编号各标各的：每个编号骑在它真正覆盖的
// 那一截路的中点上，里程照旧待在整段中点。
//
// 海外线路走 OSRM（后端 overview=simplified，压根没有 steps），拿不到就返回空，
// 调用方据此不标 —— 宁可不标也不要标错。

// 从一个路名里抠出公路编号。高德的 road 字段实测有这几种写法：
//   「G318」「G4217」「S217」「X245」——直接是编号
//   「G6京藏高速」「京藏高速(G6)」——编号混在名字里
//   「318国道」「245县道」——中文在后，编号在前
// 匹配不到编号的（「川藏公路」「三环路」「无名道路」）一律返回 null：
// 用户要的是编号，路名本身地图底图上已经有了，再标一遍是噪音。
const ROAD_CN_KIND = { '国道': 'G', '省道': 'S', '县道': 'X', '乡道': 'Y' };
function roadCodeOf(road) {
  if (!road || typeof road !== 'string') return null;
  // 先试中文写法（「318国道」），否则会被下面的字母规则漏掉
  const cn = road.match(/(\d{1,5})\s*(国道|省道|县道|乡道)/);
  if (cn) return ROAD_CN_KIND[cn[2]] + cn[1];
  // 字母 + 数字。前面必须不是字母数字，否则「A1B2」这种也会命中；
  // 后面必须不是数字，避免把「G4218」截成「G421」。
  const m = road.match(/(?:^|[^0-9A-Za-z])([GSXYZ])(\d{1,5})(?![0-9])/);
  return m ? m[1] + m[2] : null;
}

// 沿路径按长度取中点。平面近似即可：一段路的跨度上，经度按纬度做个余弦缩放就够准了。
// path 收 [[lng,lat],...] 或高德的 [{lng,lat},...]，返回 [lng, lat]。
function pathMidPoint(path) {
  if (!path || !path.length) return null;
  const pts = path.map(v => (Array.isArray(v) ? v : [v.lng, v.lat]));
  if (pts.length < 2) return pts[0];
  const kx = Math.cos(pts[0][1] * Math.PI / 180) || 1;
  const d = [0];
  for (let k = 1; k < pts.length; k++) {
    const dx = (pts[k][0] - pts[k - 1][0]) * kx;
    const dy = pts[k][1] - pts[k - 1][1];
    d.push(d[k - 1] + Math.hypot(dx, dy));
  }
  const half = d[d.length - 1] / 2;
  if (!half) return pts[0];
  let k = 1;
  while (k < d.length && d[k] < half) k++;
  const t = (half - d[k - 1]) / ((d[k] - d[k - 1]) || 1);
  return [
    pts[k - 1][0] + (pts[k][0] - pts[k - 1][0]) * t,
    pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * t,
  ];
}

// 一段路线最多标几个编号。再多就不是信息是花边了 —— 而且这种段本来就是市区绕行，
// 编号频繁切换，标出来也没人看。超出的按里程取最长的几条。
const ROAD_MAX_PER_SEG = 4;
// 缩到这个级别以下整体收起（类名打在 #map 上，见 syncElevLabelsVisibility）
const ROAD_MIN_ZOOM = 6;
// 一截路至少要走这么远才值得标：本段的 8%，再夹在 3km ~ 10km 之间。
// 下限挡掉上下匝道、借道几百米这种「蹭一下」的路，它们占着标签位置纯属干扰。
// 上限是后补的：8% 在长段上会变得离谱 —— 独库公路那 328 公里，门槛算出来
// 26 公里，于是开头实实在在走了 12.7 公里的 G218 被判成「蹭一下」滤掉了。
// 走了十公里以上的路，不管这一段总共多长，都该标出来。
const ROAD_MIN_METERS = 3000;
const ROAD_MIN_METERS_CAP = 10000;
const ROAD_MIN_SHARE = 0.08;

// 把 steps 切成若干「连着走同一条路」的小段，每段给出编号 + 里程 + 中点坐标。
// 返回 [{ code, meters, lng, lat }]，按沿途顺序排列。
function segRoadRuns(steps, totalMeters) {
  if (!steps || !steps.length) return [];
  const items = steps.map(s => ({
    code: roadCodeOf(s && s.road),
    meters: Number(s && s.distance) || 0,
    path: (s && s.path) || [],
  }));
  // 补洞：夹在两截同编号之间的无名 step（匝道、收费站、"无名道路"）算作同一条路。
  // 不补的话，G318 上一个服务区出入口就能把它劈成两截，两截各自达不到里程门槛，
  // 结果是走了 400 公里 G318 却一个编号都标不出来。
  for (let i = 0; i < items.length; i++) {
    if (items[i].code) continue;
    let a = i - 1; while (a >= 0 && !items[a].code) a--;
    let b = i + 1; while (b < items.length && !items[b].code) b++;
    if (a >= 0 && b < items.length && items[a].code === items[b].code) items[i].code = items[a].code;
  }
  // 连续同编号合成一截。无名段用 null 占位断开，免得中间隔着另一条路的两截被粘起来。
  const runs = [];
  items.forEach(it => {
    if (!it.code) { runs.push(null); return; }
    const last = runs[runs.length - 1];
    if (last && last.code === it.code) {
      last.meters += it.meters;
      last.path = last.path.concat(it.path);
    } else {
      runs.push({ code: it.code, meters: it.meters, path: it.path.slice() });
    }
  });
  const total = Number(totalMeters) || items.reduce((s, it) => s + it.meters, 0);
  const minMeters = Math.min(ROAD_MIN_METERS_CAP,
                             Math.max(ROAD_MIN_METERS, total * ROAD_MIN_SHARE));
  let kept = runs.filter(r => r && r.meters >= minMeters && r.path.length);
  // 同一个编号出现多截（绕出去又拐回来）只留最长的那截，省得同一个号在一段里标两遍
  const best = new Map();
  kept.forEach(r => {
    const cur = best.get(r.code);
    if (!cur || r.meters > cur.meters) best.set(r.code, r);
  });
  kept = kept.filter(r => best.get(r.code) === r);
  if (kept.length > ROAD_MAX_PER_SEG) {
    const top = new Set([...kept].sort((a, b) => b.meters - a.meters).slice(0, ROAD_MAX_PER_SEG));
    kept = kept.filter(r => top.has(r));
  }
  return kept.map(r => {
    const mid = pathMidPoint(r.path);
    return mid ? { code: r.code, meters: r.meters, lng: mid[0], lat: mid[1] } : null;
  }).filter(Boolean);
}

// 这份编号覆盖了本段的多少里程。用来在高德和 OSRM 两份结果之间挑更全的那个：
// 高德只认得出「218国道」这类写法，碰上「独库公路」「铁力买提隧道」就交白卷，
// 同一段路 OSRM 的 ref 能给出 G217 —— 谁把这段路解释得更完整就用谁。
// 高德这份编号解释了这一段的多少里程；低于这个比例就去问问 OSRM。
// 0.6 是个手感值：高德认全了的段（比如整段走 G30）覆盖率在 0.8 以上，
// 白白多发一次请求不值当；只认出零星一两截的才值得换一份。
const ROAD_COVERAGE_MIN = 0.6;
// 换用 OSRM 的编号之前，先确认两边走的是同一条路：编号标签的位置来自 OSRM 的
// 轨迹，画在图上的线却是高德的。两边里程差得多，就说明根本不是一条路 ——
// 实测见过高德给那拉提→库车规划 1361 公里的大绕行、OSRM 走 330 公里独库公路，
// 这时候把 G217 标上去，标签会飘在一片没有线经过的地方。
// 取短的比长的，低于这个比值就不换。
const ROAD_ALT_DIST_MATCH = 0.8;

// 绕行系数 = 规划里程 / 两点直线距离。超过这个值就怀疑规划绕远了，
// 值得把另一个数据源的方案也摆出来给人挑。
// 山区正常在 1.5~2.2 之间；那拉提→库车高德给到 6.7（直线 203 公里规划 1361 公里）
// —— 独库公路每年十月到次年五月封山，高德按「此刻能不能走」规划，而这条行程
// 记的是 2025 年 7 月已经走过的路。记录型产品该以走过的路为准。
const DETOUR_SUSPECT = 2.5;

// 两条路线的里程像不像同一条路。任一边缺里程就当不像，宁可不换。
function roadDistLooksSame(a, b) {
  const x = Number(a) || 0, y = Number(b) || 0;
  if (x <= 0 || y <= 0) return false;
  return Math.min(x, y) / Math.max(x, y) >= ROAD_ALT_DIST_MATCH;
}

function roadRunsCoverage(runs, totalMeters) {
  const total = Number(totalMeters) || 0;
  if (!runs || !runs.length || total <= 0) return 0;
  return runs.reduce((s, r) => s + (Number(r.meters) || 0), 0) / total;
}

// ---------- 公路编号标签 ----------
// 和里程标签是两批 marker，各自一个图层开关、各自的显隐。
let roadLabels = [];
function clearRoadLabels() {
  roadLabels.forEach(m => m.setMap(null));
  roadLabels = [];
}
// 默认显示，理由同里程标签：面板里没这个勾选框时宁可多显示，不要静默消失
function roadLabelsVisible() {
  const cb = document.querySelector('#lay-road');
  return cb ? cb.checked : true;
}
function applyRoadLabelsVisibility() {
  const on = roadLabelsVisible();
  roadLabels.forEach(m => {
    if (m._pill) m._pill.classList.toggle('seg-label-off', !on);
    try { on ? m.show() : m.hide(); } catch (e) {}
  });
}
// 编号的等级决定颜色：一眼看出走的是国道还是县道，比只给个号有用。
function roadClassOf(code) {
  const c = (code || '').charAt(0);
  if (c === 'G') return 'road-g';
  if (c === 'S') return 'road-s';
  return 'road-x';
}
// runsBySeg：下标 = 段号，值 = 该段的 segRoadRuns 结果。
// 整批重建（和里程标签一样），因为换行程、改路线方案都会让位置全变。
function renderRoadLabels(runsBySeg) {
  clearRoadLabels();
  if (!map || typeof AMap === 'undefined') return;
  (runsBySeg || []).forEach(runs => {
    (runs || []).forEach(r => {
      const anchor = el('div', 'seg-label-anchor');
      const pill = el('span', 'road-label ' + roadClassOf(r.code), r.code);
      pill.title = r.code + '，本段约 ' + Math.round(r.meters / 1000) + ' km';
      anchor.appendChild(pill);
      const m = new AMap.Marker({
        position: [r.lng, r.lat],
        content: anchor,
        // 抬到线的上方：只有一条路时它的中点和整段中点几乎重合，
        // 不抬开就正好压在里程胶囊上 —— 这次改动要解决的就是这种重叠。
        offset: new AMap.Pixel(0, -20),
        zIndex: 690,          // 比里程(700)低一档：万一还是撞上，让里程压在上面
        bubble: true,         // 点标签等同于点线，不拦截路线自身的点击
      });
      m._pill = pill;
      m.setMap(map);
      roadLabels.push(m);
    });
  });
  applyRoadLabelsVisibility();
}


/* ============================================================
   PWA：注册 Service Worker
   ------------------------------------------------------------
   只为「可安装性」——Chrome / Edge 把「注册了带 fetch 监听的 SW」当作
   可安装条件之一（iOS 的「添加到主屏幕」不看这条）。

   SW 本身**不做任何缓存**（为什么，见 static/sw.js 顶部那一大段），
   所以在这里注册它不会带来「改了看不到」「删了还在」这类风险。
   只在安全上下文（https / localhost）注册：局域网 http 地址注册会直接抛错。
   ============================================================ */
(function registerTravelMemorySW() {
  if (!('serviceWorker' in navigator)) return;
  if (!window.isSecureContext) return;
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {
      /* 注册失败不影响任何功能，静默忽略 */
    });
  });
})();
