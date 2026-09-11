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
  el0.classList.toggle('hide-pname', z < NAME_MIN_ZOOM);
}

function unhighlightMapPoints() {
  markers.forEach(m => {
    if (m._dot) m._dot.classList.remove('highlight');
    if (m.setzIndex) m.setzIndex(m._baseZ);
  });
}

// 里程格式化：>=1km 显示 x.x km，<1km 显示整数米
function fmtKm(m) {
  if (!m) return '';
  return m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m';
}

function clearSegLabels() {
  segLabels.forEach(m => m.setMap(null));
  segLabels = [];
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
      }
    }
    render();
  });
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

// 把每个堆挪到它经纬度对应的屏幕位置。地图一动就要重算，所以要够轻。
function positionPhotoStacks() {
  if (!map || !map.lngLatToContainer) return;
  // 地图每动一次都顺手确认层还挂在正确的地方（高德重建图层树时会掉出来）
  if (photoStackLayer) ensurePhotoStackLayer();
  // 📷 角标和照片堆在同一层，一起重排
  for (let i = 0; i < photoBadgeItems.length; i++) {
    const b = photoBadgeItems[i];
    const q = map.lngLatToContainer(new AMap.LngLat(b.lng, b.lat));
    b.el.style.left = Math.round(q.x + b.dx) + 'px';
    b.el.style.top = Math.round(q.y + b.dy) + 'px';
  }
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
