// 旅行记忆系统 — 只读分享页逻辑
let map = null;
let markers = [];
let polyline = null;
let segPolylines = [];  // 高亮相邻段时重建的分段折线
let segLabels = [];     // 每段路线中点上的里程标签
let routeSegs = null;   // 各段真实路线 path（shareDrawRoute 结果，供重建分段用）
let segDists = null;    // 各段真实路线里程（米），与 routeSegs 对齐
let segRoads = null;    // 各段走过的公路 [{code, meters, lng, lat}]，与 routeSegs 对齐；捞不到就是空数组
let fallbackSegs = false; // 临时分段标记
let drivingLoaded = false;
let renderSeq = 0;
let shareData = null;   // { trip, points }
let currentPointId = null;
let currentTab = 'points';
let viewRestored = false; // 本次渲染是否用了保存的地图视野（用了就别再自适应顶掉它）
let photoMarkers = [];  // 照片地图打点 marker

// 灯箱状态
let lightboxMedia = [];
let lightboxIndex = 0;

// 轨迹回放动画状态
let replayMarker = null;
let replayRaf = null;
let replayActive = false;
let replayPath = [];
let replayWayIdx = [];
let replayCum = [];
let replayTotal = 0;
let replayT = 0;
let replaySpeed = 1;
let replayLast = 0;
let replayDoneLine = null; // 已走过的路线（绿色高亮）
let replayTodoLine = null; // 未走过的路线（灰色压暗）
const REPLAY_BASE_MS = 30000; // 1x 速度播完整条轨迹的时长

// ---------- 工具 ----------
function shareFmtDate(d) {
  if (!d) return '未定';
  return d.length === 10 ? d : d;
}
// 轻提示（右下角浮一条，自动消失）
let _toastTimer = null;

// 从 URL 提取分享 token
function getToken() {
  return location.pathname.split('/').filter(Boolean).pop();
}

// ---------- 自定义对话框（替代原生 alert / confirm / prompt） ----------
// 原生弹窗的标题栏由浏览器写死为「<域名/IP> 显示」，JS 改不了，只能自绘。
// 自绘的另一个好处：样式跟站内统一，移动端不再是系统味的灰框。
function shareTmDialog(opts) {
  const o = Object.assign({
    title: '提示', message: '', okText: '确定', cancelText: '取消',
    danger: false, showCancel: true, input: null, multiline: false,
  }, opts);
  return new Promise(resolve => {
    const mask = el('div', 'tm-dialog-mask');
    const box = el('div', 'tm-dialog');
    box.innerHTML =
      `<div class="tm-dialog-title">${esc(o.title)}</div>` +
      // 多行（带列表的）文案居中读着别扭，自动改左对齐
      `<div class="tm-dialog-msg${String(o.message).indexOf('\n') >= 0 ? ' left' : ''}">${esc(o.message)}</div>` +
      (o.input !== null
        ? (o.multiline
            ? `<textarea class="tm-dialog-input tm-dialog-textarea" rows="3">${esc(o.input)}</textarea>`
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
function shareTmAlert(message, title) {
  return shareTmDialog({ title: title || '提示', message, showCancel: false, okText: '知道了' });
}
function shareTmConfirm(message, title, opts) {
  return shareTmDialog(Object.assign({ title: title || '确认', message }, opts || {}));
}
function shareTmPrompt(message, defaultValue, title, opts) {
  return shareTmDialog(Object.assign(
    { title: title || '请输入', message, input: defaultValue || '' }, opts || {}));
}

// ---------- 界面状态持久化（刷新后保持：面板高度/地图视野/选中点/tab/列表滚动）----------
// 按分享 token 分桶存，同一浏览器打开多条分享链接互不干扰。
const SHARE_STATE_KEY = 'tm_share_state';
function shareStateAll() {
  try { return JSON.parse(localStorage.getItem(SHARE_STATE_KEY) || 'null') || {}; } catch (e) { return {}; }
}
function getShareState() {
  const t = getToken();
  return (t && shareStateAll()[t]) || {};
}
function saveShareState(patch) {
  try {
    const t = getToken();
    if (!t) return;
    const all = shareStateAll();
    all[t] = Object.assign(all[t] || {}, patch);
    localStorage.setItem(SHARE_STATE_KEY, JSON.stringify(all));
  } catch (e) {}
}
function shareSaveMapView() {
  if (!map) return;
  const z = map.getZoom(), c = map.getCenter();
  if (z == null || !c) return;
  saveShareState({ zoom: z, center: [c.lng, c.lat] });
}
let _scrollSavePending = false;
function shareSavePointListScroll() {
  if (_scrollSavePending) return;
  _scrollSavePending = true;
  requestAnimationFrame(() => {
    _scrollSavePending = false;
    const view = $('#points-view');
    if (view) saveShareState({ listScroll: view.scrollTop });
  });
}

// ---------- 初始化地图 ----------
function shareInitMap() {
  map = new AMap.Map('map', {
    center: [104.0, 35.0],
    zoom: 4,
    resizeEnable: true,
    viewMode: '2D',  // 微信内置浏览器 WebGL 渲染不完整，强制 2D 保证地名标注正常显示
  });
  // zoomend 一起绑：只缩放不平移时高德不发 moveend，新缩放级别存不下来
  map.on('moveend', shareSaveMapView);
  map.on('zoomend', shareSaveMapView);
  map.on('zoomend', syncPhotoStackScale);   // 照片堆大小跟着缩放级别走
  map.on('zoomend', syncElevLabelsVisibility);
}

// ---------- 数据加载 ----------
async function loadShare() {
  const token = getToken();
  const cached = sessionStorage.getItem('share_pwd_' + token) || '';
  await fetchShare(token, cached, false);
}

async function fetchShare(token, password, wasWrong) {
  let url = '/api/share/' + token;
  if (password) url += '?password=' + encodeURIComponent(password);
  try {
    const res = await fetch(url);
    if (res.status === 403) {
      // 需要密码（或密码错误）
      const pwd = await shareTmPrompt(wasWrong ? '密码错误，请重新输入：' : '此分享已加密，请输入访问密码：', '', '访问密码');
      if (pwd === null || pwd === '') { showError('需要密码才能查看此分享'); return; }
      sessionStorage.setItem('share_pwd_' + token, pwd);
      return fetchShare(token, pwd, true);
    }
    if (!res.ok) {
      showError('分享不存在或已失效');
      return;
    }
    shareData = await res.json();
    renderHeader();
    shareRenderStats();
    shareRenderPointList();
    shareRenderMap();
    restoreShareUiState();
  } catch (e) {
    showError('加载失败：' + e.message);
  }
}

// 数据渲染完后恢复上次的界面状态（面板高度 / tab / 选中点 / 列表滚动）
function restoreShareUiState() {
  const st = getShareState();
  if (st.panelHeight) {
    const mapEl = document.getElementById('map');
    if (mapEl) {
      mapEl.style.height = st.panelHeight + 'px';
      if (map && typeof map.resize === 'function') setTimeout(() => map.resize(), 30);
    }
  }
  if (st.tab) shareSwitchTab(st.tab);
  shareRestoreLayerState();
  // 选中点仅在面板展开时恢复，避免强行展开用户已收起的面板
  const collapsed = $('.panel').classList.contains('collapsed');
  const exists = st.pointId && shareData.points.some(p => p.id === st.pointId);
  if (!collapsed && exists) {
    suppressAutoPan = true;                 // 恢复期间别动视野
    setTimeout(() => { suppressAutoPan = false; }, 0);
    shareShowDetail(st.pointId);
    return;
  }
  const view = $('#points-view');
  if (view && st.listScroll != null) view.scrollTop = st.listScroll;
}

function showError(msg) {
  $('#share-title').textContent = msg;
  $('#share-meta').textContent = '';
}

function renderHeader() {
  const t = shareData.trip;
  $('#share-title').textContent = t.name;
  document.title = t.name + ' · 旅行记忆';
  let meta = shareData.points.length + ' 个地点';
  if (t.start_date) {
    meta += ' · ' + shareFmtDate(t.start_date) +
      (t.end_date && t.end_date !== t.start_date ? ' ~ ' + shareFmtDate(t.end_date) : '');
  }
  $('#share-meta').textContent = meta + '（只读）';
}

let lastStatsDetail = null;   // 顶部统计条点开的「完整统计」内容

// 分享页统计：总里程（真实路线就绪后更新）· 天数 · 城市 · 地点 · 媒体
function shareCalcTotalDistance() {
  let meters = 0;
  if (segDists && segDists.length) {
    meters = segDists.reduce((s, d) => s + (d || 0), 0);
  }
  if (!meters && shareData && shareData.points && shareData.points.length > 1) {
    const pts = shareData.points;
    meters = pts.slice(0, -1).reduce((s, a, i) => s + Math.round(distKm(a, pts[i + 1]) * 1000), 0);
  }
  return meters ? fmtKm(meters) : '';
}

function shareRenderStats() {
  const pts = shareData.points;
  const mediaCount = pts.reduce((n, p) => n + (p.media ? p.media.length : 0), 0);
  let photo = 0, video = 0;
  pts.forEach(p => (p.media || []).forEach(m => {
    if (m.type === 'photo') photo++; else if (m.type === 'video') video++;
  }));
  const parts = [];
  const distText = shareCalcTotalDistance();
  if (distText) parts.push('总里程 ' + distText);
  const days = new Set(pts.map(p => p.start_date).filter(Boolean)).size;
  if (days >= 2) parts.push(`${days} 天`);
  const cities = new Set(pts.filter(p => !p.is_waypoint).map(p => p.name).filter(Boolean)).size;
  if (cities > 0) parts.push(`${cities} 城`);
  parts.push(pts.length + ' 地点', mediaCount + ' 媒体');
  if (photo) parts.push(photo + ' 照片');
  if (video) parts.push(video + ' 视频');
  $('#top-stats').textContent = parts.join(' · ');

  // 顺带攒出「完整统计」弹窗要的明细（顶部统计条可点，与主页面一致）
  const rows = [];
  if (distText) rows.push(['总里程', distText]);
  if (days >= 2) rows.push(['行程天数', days + ' 天']);
  if (cities > 0) rows.push(['途经城市', cities + ' 城']);
  rows.push(['轨迹点', pts.length + ' 个'], ['媒体', mediaCount + ' 个']);
  if (photo) rows.push(['照片', photo + ' 张']);
  if (video) rows.push(['视频', video + ' 个']);
  const t = shareData.trip;
  if (t.start_date) {
    rows.push(['起止日期', t.start_date +
      (t.end_date && t.end_date !== t.start_date ? ' ~ ' + t.end_date : '')]);
  }
  lastStatsDetail = {
    name: t.name || '',
    description: t.description || '',
    rows,
    points: pts.map((p, i) => ({
      no: i + 1,
      name: p.name || '（未命名）',
      date: shareFmtDate(p.start_date),
      waypoint: !!p.is_waypoint,
      distFromPrev: i > 0 ? fmtKm(shareSegDistanceMeters(i - 1, pts)) : '',
    })),
  };
  // 微信分享卡片标题带上统计（微信抓取 document.title 作为分享卡片标题）
  try {
    const brief = [shareData.trip.name, distText, days >= 2 ? days + ' 天' : '', cities > 0 ? cities + ' 城' : ''].filter(Boolean).join(' · ');
    document.title = '旅行记忆 · ' + brief;
  } catch (e) {}
}

// 真实路线就绪后刷新统计中的总里程
function shareUpdateStatsDistance() {
  shareRenderStats();
}

// 完整统计弹窗（与主页面 app.js 的 openStatsModal 保持一致）
function shareOpenStatsModal() {
  if (!lastStatsDetail) return;
  const d = lastStatsDetail;
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
$('#top-stats').onclick = shareOpenStatsModal;
$('#stats-modal-close').onclick = () => $('#stats-modal').classList.add('hidden');
$('#stats-modal').addEventListener('click', (e) => {
  if (e.target === $('#stats-modal')) $('#stats-modal').classList.add('hidden');
});

function shareRenderPointList() {
  const ul = $('#point-list');
  ul.innerHTML = '';
  shareData.points.forEach((p, i) => {
    const li = el('li', '');
    li.dataset.id = p.id;
    if (p.id === currentPointId) li.classList.add('active');
    li.appendChild(el('span', 'point-index', String(i + 1)));
    const info = el('div', 'point-info');
    info.appendChild(el('div', 'point-name', p.name));
    info.appendChild(el('div', 'point-date', shareFmtDate(p.start_date) +
      (p.end_date && p.end_date !== p.start_date ? ' ~ ' + shareFmtDate(p.end_date) : '')));
    const mediaParts = [];
    const photos = (p.media || []).filter(m => m.type === 'photo').length;
    const videos = (p.media || []).filter(m => m.type === 'video').length;
    if (photos) mediaParts.push(`📷 ${photos}`);
    if (videos) mediaParts.push(`🎬 ${videos}`);
    if (mediaParts.length) info.appendChild(el('div', 'point-media', mediaParts.join('  ')));
    li.appendChild(info);
    li.onclick = () => shareShowDetail(p.id);
    ul.appendChild(li);
  });
}

function shareRenderMap() {
  shareClearMap();
  const points = shareData.points;
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
    const nums = group.map(g => g.idx + 1);
    const dot = el('div', 'marker-dot' + (multi ? ' multi' : ''));
    dot.textContent = nums.join('·');
    // 与主页面一致：圆点下方挂「地名 + 海拔」小标。分享页只展示库里已有的海拔值，
    // 不发起回填请求。
    const wrap = el('div', 'marker-wrap');
    const label = el('div', 'marker-label');
    const nameEl = el('span', 'marker-name');
    nameEl.textContent = group.map(g => g.p.name).join(' / ');
    const elevEl = el('span', 'marker-elev');
    const elev = group[0].p.elevation;
    if (elev !== null && elev !== undefined && !isNaN(elev)) {
      elevEl.textContent = Math.round(Number(elev)) + ' m';
    }
    label.appendChild(nameEl);
    label.appendChild(elevEl);
    wrap.appendChild(label);
    wrap.appendChild(dot);
    const marker = new AMap.Marker({
      position: [group[0].p.lng, group[0].p.lat],
      title: group.map(g => g.p.name).join(' / '),
      content: wrap,
      offset: new AMap.Pixel(-14, -14),
      zIndex: 120,
    });
    marker._dot = dot;
    marker.on('click', () => {
      if (multi) shareShowPointPicker(group);
      else shareShowDetail(group[0].p.id);
    });
    // 记录该 marker 对应的所有点 id（环线合并点含多次经过），供详情高亮对应
    marker._pointIds = group.map(g => g.p.id);
    marker._baseZ = 120;
    marker.setMap(map);
    markers.push(marker);

    // 照片地图打点：组内有媒体的点 → 📷 角标，点击直接看该点照片
    if (group.some(g => g.p.media && g.p.media.length)) {
      // DOM 直绑点击（2026-08-26）：不依赖 AMap 事件系统，兼容个别内核；
      // 与 AMap click 双保险，500ms 内去重避免重复打开
      const pmEl = document.createElement('div');
      pmEl.className = 'photo-marker';
      pmEl.appendChild(el('span', 'photo-marker-icon', '📷'));
      // 右上角数量气泡：组内所有点的媒体合计（与主页面一致）
      const mediaCount = group.reduce((n, g) => n + ((g.p.media && g.p.media.length) || 0), 0);
      pmEl.appendChild(el('span', 'photo-marker-badge',
        mediaCount > 99 ? '99+' : String(mediaCount)));
      pmEl.title = mediaCount + ' 张照片/视频';
      const openPmLightbox = function () {
        const now = Date.now();
        if (pmEl.__last && now - pmEl.__last < 500) return;
        pmEl.__last = now;
        const g = group.find(x => x.p.media && x.p.media.length);
        if (g) {
          // 补上地点名，灯箱底部那行「地点 · 时间」才有内容
          lightboxMedia = g.p.media.map(x => Object.assign({ point_name: g.p.name }, x));
          lightboxIndex = 0;
          shareRenderLightbox();
          $('#lightbox').classList.remove('hidden');
        }
      };
      pmEl.addEventListener('click', openPmLightbox);
      // 同主页面：放进 z=100 的自建层，否则高德的 marker 容器(z=300)会把路线截断
      const box = ensurePhotoStackLayer();
      if (box) {
        pmEl.classList.add('on-stack-layer');
        box.appendChild(pmEl);
        // 右上角，理由同主页面（右下方是「地名 + 海拔」小标的地盘）
        photoBadgeItems.push({ el: pmEl, lng: group[0].p.lng, lat: group[0].p.lat,
                               dx: 10, dy: -36 });
      }
    }
  });

  shareSyncPhotoMarkersVisibility();
  positionPhotoBadges();   // 📷 角标刚建出来，位置还是 0,0，必须立刻摆好
  // 先按直线距离标出里程，真实路线算完后 shareDrawRoute 会用真实中点重建一次
  shareDrawPolyline(path);
  shareRenderSegLabels();
  shareRestoreMapView();
  shareDrawRoute(points, seq);
  // 轨迹回放入口：不足两个点无法回放，隐藏入口；控制条按需出现（播放时才显示）
  $('#replay-launch').classList.toggle('hidden', points.length < 2);
  $('#replay-bar').classList.add('hidden');
}

// 缩得太小时满屏海拔小标会糊住地名，低于这个级别整体收起（与主页面一致）
const ELEV_MIN_ZOOM = 7;
// 地名比海拔更占地方，门槛高一档（与主页面一致）
const NAME_MIN_ZOOM = 9;
function shareClearMap() {
  renderSeq++;
  markers.forEach(m => m.setMap(null));
  markers = [];
  photoMarkers.forEach(m => m.setMap(null));
  photoMarkers = [];
  clearPhotoBadges();      // 📷 角标现在在自建层里，得单独清
  if (polyline) { polyline.setMap(null); polyline = null; }
  segPolylines.forEach(p => p.setMap(null));
  segPolylines = [];
  routeSegGeom = [];
  if (routeSvgLayer) { routeSvgLayer.innerHTML = ''; routePathEls = []; }
  clearSegLabels();
  fallbackSegs = false;
  routeSegs = null;
  segDists = null;
  segRoads = null;
  shareStopReplay(false); // 切换行程/清空时停止轨迹回放
}

// 进入点详情时，高亮地图上对应的 marker 并把视野移过去（以示列表/详情与地图的对应关系）
// 同主页面：刷新后恢复选中点时别把视野拉过去，否则保存的画面会被顶掉
let suppressAutoPan = false;
function shareHighlightMapPoint(pointId) {
  let target = null;
  markers.forEach(m => {
    const hit = m._pointIds && m._pointIds.includes(pointId);
    if (m._dot) m._dot.classList.toggle('highlight', !!hit);
    if (m.setzIndex) m.setzIndex(hit ? 600 : m._baseZ);
    if (hit) target = m;
  });
  if (target) {
    const pos = target.getPosition();
    // 只平移视野到该点，不改变缩放级别（避免点击点后地图突然放大、破坏用户调好的视野）
    if (!suppressAutoPan) map.panTo(pos);
  }
}

// 清除全部 marker 高亮

// ---------- 相邻轨迹段高亮（来时路/即将去） ----------
// 选中点后：前一段（来时路）绿色、后一段（即将去）橙色，其余段淡化
function shareHighlightAdjacentSegments(pointId) {
  const points = shareData ? shareData.points : [];
  if (!points || points.length < 2) return;
  const idx = points.findIndex(p => String(p.id) === String(pointId));
  if (idx < 0) return;
  shareEnsureSegmentsForHighlight(points);
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
  shareBuildRouteOverlay();
}

// 保证分段折线可用：真实路线（segPolylines）未就绪时，隐藏整条折线并用分段重建
// （有 routeSegs 用真实段路径，否则直线兜底）
function shareEnsureSegmentsForHighlight(points) {
  if (segPolylines.length > 0) return;
  if (!polyline || points.length < 2) return;
  polyline.setMap(null);
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const path = (routeSegs && routeSegs[i] && routeSegs[i].length >= 2)
      ? routeSegs[i] : [[a.lng, a.lat], [b.lng, b.lat]];
    const seg = new AMap.Polyline({
      path: path,
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
  shareRenderSegLabels();
}

// 清除相邻段高亮，恢复默认蓝色整条折线
function shareUnhighlightSegments() {
  shareStyleSegLabels(null);        // 标签常驻，只把颜色恢复成默认
  if (fallbackSegs) {
    segPolylines.forEach(p => p.setMap(null));
    segPolylines = [];
    fallbackSegs = false;
    shareRenderSegLabels();         // 换回整条折线，中点按 routeSegs 重算
    if (polyline) polyline.setMap(map);
    shareBuildRouteOverlay();
    return;
  }
  segPolylines.forEach(seg => {
    seg.setOptions({ strokeColor: '#2563eb', strokeWeight: 4, strokeOpacity: 1 });
  });
  shareBuildRouteOverlay();       // 同上：不透明度被改回 1 了，重新压住
}

// ---------- 每段路线的里程标签（与主页面一致） ----------
// 每一段都常驻一个里程数字，贴在**沿路线走一半**的位置（按累计长度算，不是取中间那个顶点——
// 真实路线顶点疏密不均，中间顶点常常离视觉中点很远）。
// 选中某个点时来时路变绿、即将去变橙，其余淡化；颜色已说明归属，所以只留数字。
function shareSegMidPoint(i) {
  let path = null;
  if (routeSegs && routeSegs[i] && routeSegs[i].length >= 2) {
    path = routeSegs[i].map(v => (Array.isArray(v) ? v : [v.lng, v.lat]));
  } else if (segPolylines[i] && segPolylines[i].getPath) {
    const p = segPolylines[i].getPath() || [];
    path = p.map(v => (Array.isArray(v) ? v : [v.lng, v.lat]));
  }
  if (!path || path.length < 2) {
    const P = shareData ? shareData.points : [];
    const a = P[i], b = P[i + 1];
    if (!a || !b) return null;
    path = [[a.lng, a.lat], [b.lng, b.lat]];
  }
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
function shareRenderSegLabels() {
  clearSegLabels();
  const points = shareData ? shareData.points : [];
  const n = (points || []).length - 1;
  if (n < 1) return;
  for (let i = 0; i < n; i++) {
    const pos = shareSegMidPoint(i);
    const km = fmtKm(shareSegDistanceMeters(i));
    if (!pos || !km) continue;
    // 零尺寸锚点 + 内部 translate(-50%,-50%)：标签宽度随文字变，只有自己居中才骑得准
    const anchor = el('div', 'seg-label-anchor');
    const pill = el('span', 'seg-label', km);
    anchor.appendChild(pill);
    const m = new AMap.Marker({
      position: pos, content: anchor,
      offset: new AMap.Pixel(0, 0),
      zIndex: 700, bubble: true,
    });
    m._pill = pill;
    m.setMap(map);
    segLabels.push(m);
  }
  applySegLabelsVisibility();   // 关着里程时，这批新标签建出来就不该露面
  renderRoadLabels(segRoads || []);   // 公路编号另起一批，骑在各自那截路的中点上
  shareStyleSegLabels(currentPointId);
}
function shareStyleSegLabels(pointId) {
  shareScheduleRouteDraw();          // 高亮变了，自绘线的配色也要跟着变
  const points = shareData ? shareData.points : [];
  const idx = pointId ? points.findIndex(p => String(p.id) === String(pointId)) : -1;
  segLabels.forEach((m, i) => {
    const pill = m._pill;
    if (!pill) return;
    // 只给相邻两段上色，其余保持默认蓝——不淡化，理由同 shareHighlightAdjacentSegments
    pill.classList.remove('in', 'out');
    if (idx < 0) return;
    if (i === idx - 1) pill.classList.add('in');
    else if (i === idx) pill.classList.add('out');
  });
}

// 获取第 i 段里程（米）：真实路线取 segDists，直线/兜底用球面距离
function shareSegDistanceMeters(i) {
  const d = segDists && segDists[i] ? segDists[i] : 0;
  if (d) return d;
  const points = shareData ? shareData.points : [];
  if (points && points[i] && points[i + 1]) {
    return distKm(points[i], points[i + 1]) * 1000;
  }
  return 0;
}

function shareDrawPolyline(path) {
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
  shareBuildRouteOverlay();     // 自绘路线层跟着重建
}

// 判断坐标是否在中国大陆大致范围内（高德驾车规划仅支持国内）

function shareGetDriving() {
  return new Promise((resolve) => {
    if (drivingLoaded) return resolve();
    let settled = false;
    const finish = () => { if (!settled) { settled = true; drivingLoaded = true; resolve(); } };
    try {
      AMap.plugin('AMap.Driving', finish);
    } catch (e) {
      finish();
    }
    setTimeout(finish, 4000); // 插件加载失败也不阻塞
  });
}

// 海外线路：高德不支持，走 OSRM 全球路线（后端 /api/route），取最快一条
async function shareRouteViaOsm(a, b) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const res = await fetch(
      `/api/route?from_lng=${a.lng}&from_lat=${a.lat}&to_lng=${b.lng}&to_lat=${b.lat}`,
      { signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const data = await res.json();
    const routes = data.routes || [];
    if (!routes.length) return null;
    const path = routes[0].path;
    return (path && path.length >= 2)
      ? { path, distance: routes[0].distance || 0,
          roadRuns: segRoadRuns(routes[0].steps, routes[0].distance) }
      : null;
  } catch (e) {
    clearTimeout(timer);
    return null;
  }
}

function shareDriveOnce(policy, from, to) {
  return new Promise((resolve) => {
    const driving = new AMap.Driving({ policy, hideMarkers: true });
    driving.search(from, to, (status, result) => {
      if (status === 'complete' && result.routes && result.routes.length) {
        const route = result.routes[0];
        const path = [];
        (route.steps || []).forEach(step => {
          (step.path || []).forEach(p => path.push([p.lng, p.lat]));
        });
        // steps 里顺手捞公路编号连同它覆盖的那截路，见 common.js 的 segRoadRuns
        const roadRuns = segRoadRuns(route.steps, route.distance);
        resolve(path.length >= 2 ? { path, roadRuns, distance: route.distance || 0 } : null);
      } else {
        resolve(null);
      }
    });
  });
}

// 串行规划相邻点最快驾车路线（只读页不弹选择，直接取最快）
// 规划单段：驾车（国内高德 / 海外 OSRM 兜底）或直线；返回 { path, distance }
async function planSegment(a, b) {
  const mode = a.route_mode || 'drive';
  if (mode !== 'drive' || distKm(a, b) <= 1) {
    return { path: [[a.lng, a.lat], [b.lng, b.lat]], roadRuns: [], distance: distKm(a, b) * 1000 };
  }
  let seg = null;
  if (isInChina(a.lng, a.lat) && isInChina(b.lng, b.lat)) {
    seg = await shareDriveOnce(AMap.DrivingPolicy.LEAST_TIME, new AMap.LngLat(a.lng, a.lat), new AMap.LngLat(b.lng, b.lat));
    // 高德有些路段只给路名不给编号（独库公路那段 328 公里就是这样），
    // 路径仍用高德的，编号跟 OSRM 要。10km 以下不值得为标签多发一次请求。
    if (seg && !(seg.roadRuns || []).length && distKm(a, b) > 10) {
      const alt = await shareRouteViaOsm(a, b);
      if (alt && (alt.roadRuns || []).length) seg.roadRuns = alt.roadRuns;
    }
  }
  if (!seg) seg = await shareRouteViaOsm(a, b);
  return (seg && seg.path && seg.path.length >= 2)
    ? seg
    : { path: [[a.lng, a.lat], [b.lng, b.lat]], roadRuns: [], distance: distKm(a, b) * 1000 };
}

// 并行规划所有段（串行 16 段 OSRM 太慢，导致曲线迟迟不出现）
async function shareDrawRoute(points, seq) {
  if (points.length < 2) return;
  try {
    await shareGetDriving();
    const tasks = points.slice(0, -1).map((a, i) => planSegment(a, points[i + 1]));
    const segs = await Promise.all(tasks);
    if (seq !== renderSeq) return;
    routeSegs = segs.map(s => s.path);
    segDists = segs.map(s => s.distance || 0);
    segRoads = segs.map(s => s.roadRuns || []);
    let routePath = [];
    segs.forEach((seg) => {
      // seg 结构为 { path, distance }；旧代码误用 seg.length 导致 routePath 恒为空 → 主路线永远是直线
      if (seg && seg.path && seg.path.length) {
        if (routePath.length === 0) routePath = routePath.concat(seg.path);
        else routePath = routePath.concat(seg.path.slice(1));
      }
    });
    if (seq !== renderSeq) return;
    if (routePath.length >= 2) {
      shareDrawPolyline(routePath);
      // 真实路线就绪后重新适配视野到完整路线（无选中点时），避免全景下曲线被误认为直线。
      // 但如果这次恢复了上次保存的视野，就不要自适应——否则用户存的视野会被顶掉。
      if (!currentPointId && !viewRestored && typeof map.setFitView === 'function') {
        try { map.setFitView([polyline], false, [60, 60, 60, 60], 300); } catch (e) {}
      }
    }
    // 真实路线就绪后重建里程标签（位置按真实路线算，不能再用直线中点）
    shareRenderSegLabels();
    shareBuildRouteOverlay();       // 自绘路线层换成真实路线几何
    // 真实路线就绪后，若有点处于选中态则恢复相邻段高亮
    if (currentPointId) shareHighlightAdjacentSegments(currentPointId);
    // 真实路线就绪后刷新统计中的总里程
    shareUpdateStatsDistance();
  } catch (e) {
    console.error('shareDrawRoute 失败：', e);
  }
}

// ---------- 详情 ----------
function shareShowDetail(pointId) {
  // 面板收起时点地图上的点 → 自动展开面板显示详情
  shareSetPanelCollapsed(false);
  currentPointId = pointId;
  saveShareState({ pointId });
  document.querySelectorAll('#point-list li').forEach(li => {
    li.classList.toggle('active', String(li.dataset.id) === String(pointId));
  });
  shareSwitchTab('detail');
  shareHighlightMapPoint(pointId);
  shareHighlightAdjacentSegments(pointId);
  shareStyleSegLabels(pointId);
  const p = shareData.points.find(x => x.id === pointId);
  if (!p) return;

  $('#detail-empty').classList.add('hidden');
  $('#detail-body').classList.remove('hidden');
  const nameEl = $('#detail-name');
  nameEl.textContent = p.name;
  // 经纬度不再单独占一行，收进 title
  nameEl.title = `${p.name}（${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}）`;
  $('#detail-date').textContent = shareFmtDate(p.start_date) +
    (p.end_date && p.end_date !== p.start_date ? ' ~ ' + shareFmtDate(p.end_date) : '');
  $('#detail-desc').textContent = p.description || '';
  shareRenderMedia(p.media || []);
}

// 多次经过同一地点（环线）：点击合并 marker 后弹出选择，查看某一次经过
function shareShowPointPicker(group) {
  const ov = document.createElement('div');
  ov.className = 'modal';
  const items = group.map(g => `
    <button type="button" class="btn point-pick" data-id="${g.p.id}" style="text-align:left">
      <span class="pick-num">${g.idx + 1}</span>
      <span class="pick-name">${esc(g.p.name)}</span>
      <span class="pick-date">${shareFmtDate(g.p.start_date)}</span>
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
    shareShowDetail(parseInt(btn.getAttribute('data-id'), 10));
  });
}

// ---------- 轨迹回放动画 ----------
// 沿轨迹匀速移动一个小圆点，模拟旅程回放；速度 1x/2x/4x
// 路径优先取真实路线（routeSegs 逐段拼接），未就绪时用直线兜底
function shareBuildReplayPath() {
  if (routeSegs && routeSegs.length) {
    const path = [];
    const wayIdx = [0];
    routeSegs.forEach(seg => {
      if (seg && seg.length) {
        if (!path.length) path.push(...seg);
        else path.push(...seg.slice(1));
        wayIdx.push(path.length - 1);
      }
    });
    if (path.length >= 2) return { path, wayIdx };
  }
  const pts = shareData ? shareData.points : [];
  if (pts.length >= 2) {
    return {
      path: pts.map(p => [p.lng, p.lat]),
      wayIdx: pts.map((_, i) => i),
    };
  }
  return { path: [], wayIdx: [] };
}

function shareStartReplay() {
  const { path, wayIdx } = shareBuildReplayPath();
  if (path.length < 2) return;
  shareStopReplay(false);
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
  $('#replay-toggle').textContent = '⏸';
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
  sharePlayLoop();
}

function sharePlayLoop() {
  if (!replayActive) return;
  replayRaf = requestAnimationFrame((now) => {
    if (!replayLast) replayLast = now;
    const dt = now - replayLast;
    replayLast = now;
    replayT += replaySpeed * replayTotal * dt / REPLAY_BASE_MS;
    if (replayT >= replayTotal) {
      replayT = replayTotal;
      shareFinishReplay();
      return;
    }
    let i = 1;
    while (i < replayCum.length - 1 && replayCum[i] < replayT) i++;
    const segLen = (replayCum[i] - replayCum[i - 1]) || 1;
    const t = Math.min(Math.max((replayT - replayCum[i - 1]) / segLen, 0), 1);
    const a = replayPath[i - 1], b = replayPath[i];
    replayMarker.setPosition([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    updateReplayLines();
    $('#replay-fill').style.width = (replayT / replayTotal * 100).toFixed(2) + '%';
    shareUpdateReplayName(replayT);
    sharePlayLoop();
  });
}

// 进度条下方显示当前已到达的行程点
function shareUpdateReplayName(t) {
  let wi = 0;
  for (let i = 1; i < replayWayIdx.length; i++) {
    if (replayCum[replayWayIdx[i]] <= t + 1) wi = i;
    else break;
  }
  const pts = shareData ? shareData.points : [];
  const p = pts[wi];
  if (p) $('#replay-name').textContent = `${wi + 1}/${pts.length} · ${p.name}`;
}

function sharePauseReplay() {
  replayActive = false;
  if (replayRaf) { cancelAnimationFrame(replayRaf); replayRaf = null; }
  $('#replay-toggle').textContent = '▶';
}

function shareFinishReplay() {
  sharePauseReplay();
  updateReplayLines(); // 播完：整条路线变绿（todo 缩为终点）
  $('#replay-fill').style.width = '100%';
  shareUpdateReplayName(replayTotal); // 播完：进度条落到最后一个行程点（修复 16/17 错觉）
  $('#replay-toggle').textContent = '↻'; // 播完：点击重播
}

function shareStopReplay(hide = true) {
  replayActive = false;
  if (replayRaf) { cancelAnimationFrame(replayRaf); replayRaf = null; }
  if (replayMarker) replayMarker.setMap(null);
  clearReplayLines();
  if (hide) {
    $('#replay-bar').classList.add('hidden');
    // 恢复回放入口按钮（点数足够时）
    const pts = (shareData && shareData.points) || [];
    $('#replay-launch').classList.toggle('hidden', !pts || pts.length < 2);
  }
  replayT = 0;              // 停止 = 复位，别让残留进度把下次播放判成「暂停中」
  $('#replay-fill').style.width = '0%';
  $('#replay-name').textContent = '';
  $('#replay-toggle').textContent = '▶';
}

function shareToggleReplay() {
  if (replayActive) { sharePauseReplay(); return; }
  if (replayT >= replayTotal) { shareStartReplay(); return; } // 播完或首次 → 从头播
  if (replayTotal > 0 && replayPath.length >= 2 && replayMarker) {
    replayActive = true; // 续播
    replayLast = 0;
    $('#replay-toggle').textContent = '⏸';
    replayMarker.setMap(map);   // 停过就被摘下来了，续播前补挂回去
    ensureReplayLines();
    updateReplayLines();
    sharePlayLoop();
  } else {
    shareStartReplay();
  }
}

function shareRenderMedia(media) {
  const grid = $('#media-grid');
  grid.innerHTML = '';
  applyMediaCols();      // 重建后把当前缩放档位重新落到网格上
  lightboxMedia = media;
  media.forEach((m, idx) => {
    const item = el('div', 'media-item');
    const img = el('img');
    img.src = mediaSrc(m, 'thumb');
    img.classList.add('thumb-loading');
    img.onload = () => img.classList.remove('thumb-loading');
    mediaFallbackToLocal(img, '/uploads/thumbs/' + m.thumb_filename);
    img.loading = 'lazy';
    img.onclick = () => shareOpenLightbox(idx);
    item.appendChild(img);
    if (m.type === 'video') item.appendChild(el('span', 'video-badge', '▶'));
    if (m.exif_time) item.appendChild(el('span', 'media-date', m.exif_time.slice(0, 10)));
    grid.appendChild(item);
  });
}

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
function shareSetMediaZoom(delta) {
  const next = Math.min(MEDIA_COLS.length - 1, Math.max(0, mediaZoom + delta));
  if (next === mediaZoom) return;
  mediaZoom = next;
  try { localStorage.setItem(MEDIA_ZOOM_KEY, String(next)); } catch (e) {}
  const grid = document.getElementById('media-grid');
  shareFlipGrid(grid, applyMediaCols);
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
    if (e.touches.length !== 2) return;
    // 抢在浏览器把双指判成整页缩放之前接管；晚一步 touchmove 就不可取消了
    if (e.cancelable) e.preventDefault();
    start = d2(e);
  }, { passive: false });
  view.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 2 || !start) return;
    if (e.cancelable) e.preventDefault();
    const d = d2(e), r = d / start;
    if (r > 1.18) { shareSetMediaZoom(1); start = d; }
    else if (r < 0.85) { shareSetMediaZoom(-1); start = d; }
  }, { passive: false });
  view.addEventListener('touchend', () => { start = 0; }, { passive: true });
  view.addEventListener('touchcancel', () => { start = 0; }, { passive: true });
  // 桌面：Ctrl/⌘ + 滚轮，和相册一致
  view.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    shareSetMediaZoom(e.deltaY < 0 ? 1 : -1);
  }, { passive: false });
})();

// ---------- 灯箱 ----------
function shareOpenLightbox(idx) {
  lightboxIndex = idx;
  shareRenderLightbox();
  $('#lightbox').classList.remove('hidden');
}
let lightboxTrack = null;   // 当前的三格轨道（prev/cur/next）
let lbAnimating = false;    // 翻页动画进行中，期间忽略新的翻页请求

// ---------- 极简 Markdown（只用于图片说明） ----------
// 说明文字是用户输入，而且会原样呈现给分享页的访客，所以顺序是**先转义、再解析**：
// 进入解析前所有 < > & " 已经变成实体，之后往里加的标签全是我们自己生成的，
// 不存在把用户写的 <script>/onerror 放进 DOM 的路径。链接另外限制协议白名单。
// 只做说明文字用得上的那几种语法，不追求完整 CommonMark：
//   **粗** *斜* ~~删除~~ `代码` [文字](链接) # 标题 - 列表 1. 列表 > 引用 ---

function shareRenderLightbox() {
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
      // 分享页只读。相册是整个行程的，所以不管有没有描述都先给出「地点 · 时间」，
      // 让人知道这张图属于哪个轨迹点；有描述的再接在下面。
      const metaText = [item.point_name,
        (item.taken_at || item.exif_time || '').slice(0, 16).replace('T', ' ')]
        .filter(Boolean).join(' · ');
      const hasCap = !!(item.caption && String(item.caption).trim());
      if (metaText || hasCap) {
        const box = el('div', 'lb-caption readonly');
        if (metaText) box.appendChild(el('div', 'lb-caption-meta', metaText));
        const txt = el('div', 'lb-caption-text');
        // 同主页面：说明支持 Markdown，renderMarkdown 内部已先做 HTML 转义
        if (hasCap) { txt.innerHTML = renderMarkdown(item.caption); txt.classList.add('md'); }
        else txt.style.display = 'none';
        const more = el('button', 'lb-caption-more', '展开');
        more.type = 'button';
        more.hidden = true;
        const row = el('div', 'lb-caption-row');
        row.appendChild(txt);
        row.appendChild(more);
        box.appendChild(row);
        // 同主页面：测量依赖布局，多测几次避免量到 0
        const syncMore = () => {
          if (!hasCap || !txt.clientHeight) { more.hidden = true; return; }
          more.hidden = txt.scrollHeight <= txt.clientHeight + 1;
        };
        requestAnimationFrame(syncMore);
        setTimeout(syncMore, 120);
        setTimeout(syncMore, 400);
        const toggle = (e) => {
          e.stopPropagation();
          if (e.target.closest && e.target.closest('a')) return;   // 点链接就只打开链接
          if (more.hidden) return;
          more.textContent = box.classList.toggle('expanded') ? '收起' : '展开';
        };
        txt.onclick = toggle;
        more.onclick = toggle;
        slide.appendChild(box);
      }
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
}
function shareCloseLightbox() {
  if (typeof lbExitZoom === 'function') lbExitZoom(false);
  stopLightboxVideos();
  $('#lightbox').classList.add('hidden');
  $('#lightbox-stage').innerHTML = '';
}
// 翻页：让轨道继续滑满一屏，滑完再切索引重建。axis 决定横滑还是竖滑。
function shareLightboxNav(dir, axis) {
  if (lbAnimating) return false;
  const next = lightboxIndex + dir;
  if (next < 0 || next >= lightboxMedia.length) return false;   // 到头了，没翻成
  const track = lightboxTrack;
  if (!track) { lightboxIndex = next; shareRenderLightbox(); return true; }
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
    shareRenderLightbox();
  };
  track.addEventListener('transitionend', finish);
  setTimeout(finish, 500);   // transitionend 偶尔不触发时的兜底
  return true;
}
window.addEventListener('resize', syncTabIndicator);

function shareSwitchTab(name, slideFrom) {
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
  if (name === 'albums') shareLoadAlbums();
  saveShareState({ tab: name });
}

// 恢复地图视野：优先用上次保存的，没有再按全部点自适应
function shareRestoreMapView() {
  const st = getShareState();
  if (st.zoom != null && st.center && st.center.length === 2) {
    // 第三个参数 immediately=true：直接落位，不做从默认视野飞过去的动画
    try { map.setZoomAndCenter(st.zoom, st.center, true); viewRestored = true; return; } catch (e) {}
  }
  viewRestored = false;
  map.setFitView(null, false, [60, 60, 60, 60]);
}

// ---------- 事件 ----------
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => shareSwitchTab(t.dataset.tab);
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
function shareRestoredMapHeight(full) {
  const st = (typeof getShareState === 'function') ? getShareState() : {};
  const saved = Number(st && st.panelHeight);
  const dflt = Math.round(window.innerHeight * 0.55);
  const h = (saved > 0 && saved < full - 40) ? saved : dflt;
  return Math.max(0, Math.min(full - 40, h));
}
// 把最终状态一次性落到 DOM 上（不含动画）
function shareApplyPanelCollapsed(collapsed) {
  $('.panel').classList.toggle('collapsed', collapsed);
  $('.layout').classList.toggle('panel-collapsed', collapsed);
  $('#btn-panel-open').classList.toggle('hidden', !collapsed);
  if (!collapsed) setTimeout(syncTabIndicator, 20);
  if (map && typeof map.resize === "function") setTimeout(() => map.resize(), 80);
  saveShareState({ collapsed });
}

function shareSetPanelCollapsed(collapsed, animate) {
  const layout = $('.layout'), panel = $('.panel');
  const mapEl = document.getElementById('map');
  const already = panel.classList.contains('collapsed');

  clearTimeout(panelAnimTimer);
  layout.classList.remove('panel-animating');
  panel.classList.remove('sliding-out');

  // 状态没变 / 不要动画 / 系统要求减弱动效 → 直接落状态
  if (animate === false || panelReducedMotion() || !mapEl || already === collapsed) {
    if (!collapsed && isMobileLayout() && mapEl && !mapEl.style.height) {
      mapEl.style.height = shareRestoredMapHeight(layout.getBoundingClientRect().height) + 'px';
    }
    shareApplyPanelCollapsed(collapsed);
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
        shareApplyPanelCollapsed(true);
      }, PANEL_ANIM_MS);
    } else {
      // 展开：先把面板放回 DOM，但地图仍占满，再过渡回上次的高度
      const target = shareRestoredMapHeight(full);
      shareApplyPanelCollapsed(false);
      mapEl.style.height = Math.round(full) + 'px';
      void mapEl.offsetHeight;
      layout.classList.add('panel-animating');
      mapEl.style.height = Math.round(target) + 'px';
      pumpMapResize();
      panelAnimTimer = setTimeout(() => {
        layout.classList.remove('panel-animating');
        syncTabIndicator();
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
      shareApplyPanelCollapsed(true);
    }, PANEL_ANIM_MS);
  } else {
    shareApplyPanelCollapsed(false);
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
  b.addEventListener('click', () => shareSetPanelCollapsed(true)));
$('#btn-panel-open').addEventListener('click', () => shareSetPanelCollapsed(false));
function shareRestorePanelState() {
  // 分享页首次打开默认折叠面板（地图全屏展示，点 marker/列表时自动展开看详情）；
  // 来过的话尊重上次的折叠状态。
  const st = getShareState();
  // 首屏恢复不做动画：页面刚出来就滑一下反而像卡了一下
  shareSetPanelCollapsed(st.collapsed === undefined ? true : !!st.collapsed, false);
}

// 全览：把整条行程（所有轨迹点 + 路线）收进当前视野。
function shareFitWholeTrip() {
  if (!map || typeof map.setFitView !== 'function') return;
  const overlays = markers.concat(
    segPolylines.length ? segPolylines : (polyline ? [polyline] : []));
  if (!overlays.length) { tmToast('这个行程还没有轨迹点'); return; }
  try {
    map.setFitView(overlays, false, [60, 60, 60, 60], 300);
    viewRestored = false;
  } catch (e) {}
}
$('#btn-fit-view').addEventListener('click', shareFitWholeTrip);

// ---------- 地图全屏 ----------
// 与主页面 app.js 的实现保持一致：给 #map 加 .map-fullscreen（fixed 铺满视口），
// 而不是对 #map 调元素级 Fullscreen API —— 后者会让灯箱（在 #map 外面）直接不可见。
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

  // 用户用浏览器自己的方式退出原生全屏（Esc / 手势）时，CSS 全屏也跟着退出
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(ev => {
    document.addEventListener(ev, () => {
      if (usedNative && !nativeOn() && isOn()) exit();
    });
  });

  // Esc 退出（原生全屏不可用时的兜底；灯箱开着时优先让灯箱吃掉这个键）
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !isOn()) return;
    const lb = $('#lightbox');
    if (lb && !lb.classList.contains('hidden')) return;
    exit();
  });

  sync();
})();

// ---------- 网格重排的 FLIP 过渡 ----------
// 缩放换列数时，直接改 CSS 变量会让所有格子瞬间跳到新位置，很生硬。
// FLIP：先量旧位置(First) → 改列数(Last) → 给每个格子套上「回到旧位置」的
// 逆变换(Invert) → 下一帧放开让它过渡回原位(Play)。
// 关键是**不重建 DOM**：重建会让缩略图重走一遍加载，中间闪一下白。
// 只给视口附近的格子做动画——列表可能上百张，全做会掉帧，看不见的直接归位。
function shareFlipGrid(grid, mutate) {
  if (!grid) return;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const cells = Array.from(grid.children).filter(c => c.nodeType === 1 && !c.classList.contains('empty-hint'));
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

// ---------- 相册：扁平缩略图网格（与主页面一致） ----------
// 数据直接从 shareData.points[].media 摊平，分享接口已经带回全部媒体，不需要额外请求。
const PHOTO_COLS = [8, 6, 5, 4, 3, 2];
const PHOTO_VIEW_KEY = 'tm_photo_view';   // 与主页面共用同一份缩放/筛选偏好
let photoItems = [];
let photoShown = [];
let photoView = loadPhotoView();

function shareLoadAlbums() {
  if (!shareData) return;
  photoItems = [];
  shareData.points.forEach(p => (p.media || []).forEach(m => {
    photoItems.push({
      id: m.id, type: m.type, point_id: p.id, point_name: p.name,
      filename: m.filename, thumb_filename: m.thumb_filename,
      url: mediaSrc(m, 'full'),
      thumb_url: m.thumb_filename ? mediaSrc(m, 'thumb') : null,
      taken_at: m.exif_time || m.created_at || '',
      caption: m.caption || '',
      qualities: m.qualities,        // 视频清晰度档位，相册页点开也要能选
    });
  }));
  // 统一按拍摄时间升序，和主页面后端返回的顺序一致
  photoItems.sort((a, b) => (a.taken_at || '').localeCompare(b.taken_at || '') || (a.id - b.id));
  syncPhotoToolbar();
  shareRenderPhotoGrid();
}

function shareRenderPhotoGrid() {
  const grid = $('#photo-grid');
  const list = photoItems.filter(m => photoView.filter === 'all' || m.type === photoView.filter);
  photoShown = photoView.sort === 'desc' ? list.slice().reverse() : list;

  const n = photoShown.length, total = photoItems.length;
  // 写明范围：这里是「整个行程」的全部媒体，不是某个轨迹点的
  $('#photo-count').textContent = total === 0 ? ''
    : (n === total ? `本行程 · ${total} 项` : `本行程 · ${n} / ${total} 项`);

  grid.style.setProperty('--photo-cols', PHOTO_COLS[photoView.zoom]);
  grid.innerHTML = '';
  if (!n) {
    grid.innerHTML = total === 0
      ? '<div class="empty-hint">这个行程还没有照片/视频</div>'
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
      img.onerror = () => { if (img.src !== location.origin + m.url) img.src = m.url; };
      cell.appendChild(img);
    } else {
      cell.appendChild(el('div', 'photo-cell-blank', '🎬'));
    }
    if (m.type === 'video') cell.appendChild(el('span', 'photo-video-badge', '▶'));
    if (m.caption && m.caption.trim()) cell.appendChild(el('span', 'photo-cap-badge', '✎'));
    cell.onclick = () => {
      lightboxMedia = photoShown;
      lightboxIndex = idx;
      shareRenderLightbox();
      $('#lightbox').classList.remove('hidden');
    };
    grid.appendChild(cell);
  });
}

function shareSetPhotoZoom(delta) {
  const next = Math.min(PHOTO_COLS.length - 1, Math.max(0, photoView.zoom + delta));
  if (next === photoView.zoom) return;
  photoView.zoom = next;
  savePhotoView();
  syncPhotoToolbar();
  // 只改列数、不重建 DOM——重建会让每张缩略图重走一遍加载，中间闪白。
  // 位置变化交给 FLIP 过渡。
  const grid = $('#photo-grid');
  shareFlipGrid(grid, function () {
    grid.style.setProperty('--photo-cols', PHOTO_COLS[next]);
  });
}

document.querySelectorAll('#photo-filter .seg-btn').forEach(b => {
  b.addEventListener('click', () => {
    photoView.filter = b.dataset.filter;
    savePhotoView();
    syncPhotoToolbar();
    shareRenderPhotoGrid();
  });
});
$('#btn-photo-sort').addEventListener('click', () => {
  photoView.sort = photoView.sort === 'desc' ? 'asc' : 'desc';
  savePhotoView();
  syncPhotoToolbar();
  shareRenderPhotoGrid();
});
$('#btn-photo-zoom-in').addEventListener('click', () => shareSetPhotoZoom(1));
$('#btn-photo-zoom-out').addEventListener('click', () => shareSetPhotoZoom(-1));

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
  if (e.cancelable) e.preventDefault();
  const d = pinchDist(e);
  const ratio = d / pinchStart;
  if (ratio > 1.18) { shareSetPhotoZoom(1); pinchStart = d; }
  else if (ratio < 0.85) { shareSetPhotoZoom(-1); pinchStart = d; }
}, { passive: false });
albumsView.addEventListener('touchend', () => { pinchStart = 0; }, { passive: true });
albumsView.addEventListener('touchcancel', () => { pinchStart = 0; }, { passive: true });

// Ctrl/⌘ + 滚轮缩放（桌面）
albumsView.addEventListener('wheel', e => {
  if (!e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  shareSetPhotoZoom(e.deltaY < 0 ? 1 : -1);
}, { passive: false });

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
// 实现与主页面 app.js 完全一致。刻意**不**搬「足迹热力图」和「照片图层」——
// 那两层走 /api/footprint 和 /api/photo-layer，返回的是该用户**所有行程**的
// 坐标与照片，放在公开分享页上等于泄露其他行程。
let layerSat = null, layerRoad = null, layerTraffic = null;
let poiMarkers = [], poiSearch = null, poiTimer = null;

// ---------- 照片视频图层（分享页版）----------
// 主页面那版走 /api/photo-layer，返回的是该用户**所有行程**的照片，分享页绝不能用。
// 这里改成只用本次分享的 shareData：同一批数据，观感一致，但不会泄露别的行程。
// 照片堆不再走 AMap.Marker，改由 photoStackItems + 自建图层承载（见 ensurePhotoStackLayer）

// 照片图层开着时，轨迹点旁边的 📷 角标是重复信息，用 CSS 类整体藏掉
function shareSyncPhotoMarkersVisibility() {
  const on = !!($('#lay-photos') && $('#lay-photos').checked);
  const el = document.getElementById('map');
  if (el) el.classList.toggle('photo-layer-on', on);
}

// 照片堆随地图缩放级别变大变小（与主页面同一套公式）

// ---------- 自绘路线层（方案 B，与主页面同一套实现）----------
// 背景：照片堆必须压在路线之下，但高德把 DOM marker 全塞进同一个 z-index=300 的
// 容器里，marker 自己的 zIndex 只在容器内部排序，永远盖着 z=110 的折线层。
// 把照片堆挪进自建层(z=100)后仍有环境会挡线，于是干脆不再依赖高德的层级：
// 路线由自己画在 z=105 的 SVG 上（照片堆 100 < 路线 105 < 高德 marker 300），
// 高德原来那条线压成近乎透明。
//
// 性能是关键：逐点调 map.lngLatToContainer 每帧要十几毫秒，必卡。两个优化：
//   1) 顶点先算好 Web Mercator 坐标（与缩放/平移无关，只算一次）；
//      重画时用 3 个参考点解出仿射变换，每个点只剩两次乘加。
//   2) 抽稀：投影后与上一个点不足 1.2px 的直接丢掉。
let routeSvgLayer = null;
let routeSegGeom = [];      // [{i, pts:[[mx,my],...]}]，mx/my 是 Web Mercator
let routePathEls = [];
let routeDrawRaf = 0;
let routeOverlayOn = false;

// 用 3 个参考点解 Mercator -> 屏幕的仿射变换。
// 地图有俯仰角时投影不再是仿射的，这时返回 null，调用方退回高德自己的线。
function shareRouteAffine() {
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

// 几何来源。分享页和主页面不同：主页面渲染完就是一组分段折线，
// 分享页平时只有一条整线（polyline），选中点时才拆成 segPolylines。
// 三级取值：分段折线 > 真实路线段(routeSegs) > 点到点直线兜底。
function routeGeomSource() {
  if (segPolylines.length) {
    return segPolylines.map((sp, i) => ({ i: i, path: sp.getPath() || [] }));
  }
  if (routeSegs && routeSegs.length) {
    return routeSegs.map((path, i) => ({ i: i, path: path || [] }));
  }
  const pts = (shareData && shareData.points) ? shareData.points : [];
  const out = [];
  for (let i = 0; i < pts.length - 1; i++) {
    out.push({ i: i, path: [[pts[i].lng, pts[i].lat], [pts[i + 1].lng, pts[i + 1].lat]] });
  }
  return out;
}

// 取出几何转成 Mercator 存好；同时把高德那条线隐藏
function shareBuildRouteOverlay() {
  routeSegGeom = [];
  const src = routeGeomSource();
  if (!src.length) { shareDrawRouteOverlay(); return; }
  src.forEach(s => {
    const pts = [];
    for (const v of s.path) {
      const lng = Array.isArray(v) ? v[0] : v.lng;
      const lat = Array.isArray(v) ? v[1] : v.lat;
      pts.push(mercatorXY(lng, lat));
    }
    if (pts.length >= 2) routeSegGeom.push({ i: s.i, pts: pts });
  });
  shareSetAmapRouteVisible(false);
  shareDrawRouteOverlay();
}

// 高德原线的显隐。隐藏时保留极低的不透明度而不是 0 —— 完全透明后点击热区在
// 部分版本上会失效。分享页整线和分段线都要一起处理。
function shareSetAmapRouteVisible(vis) {
  routeOverlayOn = !vis;
  const all = segPolylines.concat(polyline ? [polyline] : []);
  all.forEach(sp => {
    try { sp.setOptions({ strokeOpacity: vis ? 1 : 0.01 }); } catch (e) {}
  });
  const el = document.getElementById('map');
  if (el) el.classList.toggle('route-overlay-on', !vis);
}

// 当前每段该用什么颜色/粗细：与高亮状态保持一致
function shareRouteSegStyle(i) {
  const pts = (shareData && shareData.points) ? shareData.points : [];
  const idx = currentPointId ? pts.findIndex(p => String(p.id) === String(currentPointId)) : -1;
  if (idx >= 0 && i === idx - 1) return ['#16a34a', 6];   // 来时路
  if (idx >= 0 && i === idx) return ['#f97316', 6];       // 即将去
  return ['#2563eb', 4];
}

function shareDrawRouteOverlay() {
  const svg = ensureRouteSvgLayer();
  if (!svg) return;
  if (!routeSegGeom.length) { svg.innerHTML = ''; routePathEls = []; return; }
  const aff = shareRouteAffine();
  if (!aff) {                    // 俯仰状态：交还给高德自己画
    svg.innerHTML = ''; routePathEls = [];
    if (routeOverlayOn) shareSetAmapRouteVisible(true);
    return;
  }
  if (!routeOverlayOn) shareSetAmapRouteVisible(false);
  const { A, B } = aff;
  const W = svg.clientWidth || 400, H = svg.clientHeight || 800;
  const MARGIN = 200;            // 视野外留一点余量，避免线头在边缘断掉
  while (routePathEls.length < routeSegGeom.length) {
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke-linejoin', 'round');
    p.setAttribute('stroke-linecap', 'round');
    svg.appendChild(p);
    routePathEls.push(p);
  }
  while (routePathEls.length > routeSegGeom.length) routePathEls.pop().remove();

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
    const [color, width] = shareRouteSegStyle(seg.i);
    const el = routePathEls[s];
    el.setAttribute('d', d);
    el.setAttribute('stroke', color);
    el.setAttribute('stroke-width', String(width));
    el._segIndex = seg.i;
  }
}

// 地图动就重画。用 rAF 合并，一帧最多画一次。
function shareScheduleRouteDraw() {
  if (routeDrawRaf) return;
  routeDrawRaf = requestAnimationFrame(() => {
    routeDrawRaf = 0;
    shareDrawRouteOverlay();
    // 照片堆/角标也并进这一帧，理由同主页面 static/app.js 的 scheduleRouteDraw：
    // 各自单独挂 mapmove 会让每次地图移动多跑几趟，而且落在不同的帧上。
    // 角标排在照片堆后面，换行程时才盖得住（见 clearMap 之后那次重排的注释）。
    positionPhotoStacks();
    positionPhotoBadges();
  });
}
(function initRouteOverlaySync() {
  const bind = () => {
    if (!map || !map.on) return setTimeout(bind, 200);
    ['mapmove', 'zoomchange', 'movend', 'zoomend', 'resize', 'rotatechange', 'pitchchange']
      .forEach(ev => { try { map.on(ev, shareScheduleRouteDraw); } catch (e) {} });
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
})();

// 照片堆现在在折线层**之下**，而高德的矢量覆盖物层（SVG，pointer-events: auto）
// 整片盖在上面，命中测试永远落不到照片堆上 —— 实测在堆的范围里取样 64 个点，
// 命中堆 0 个、命中 SVG 层 40 个。所以点击要自己接：在 #map 的捕获阶段做一次
// 矩形命中判定，落在某个堆里就当成点了这个堆。
(function initPhotoStackClick() {
  // 命中判定 + 触摸兜底都在 common.js 的 tmBindMapPhotoTap 里（和主界面共用一份）。
  // 分享页没有路段面板，没命中就什么都不做。
  tmBindMapPhotoTap(document.getElementById('map'));
})();

// 照片堆/角标的重排已经并进 shareScheduleRouteDraw 那个 rAF 里了，这里不再单独挂 mapmove。

function shareToggleLayerPhotos(on, silent) {
  const cb = $('#lay-photos');
  if (on) {
    if (photoStackItems.length) { positionPhotoStacks(); shareSyncPhotoMarkersVisibility(); syncPhotoStackScale(); return; }
    const pts = (shareData && shareData.points) || [];
    const withMedia = pts.filter(p => p.media && p.media.length);
    if (!withMedia.length) {
      if (!silent) tmToast('这个行程还没有照片或视频');   // 默认开启时不打扰
      cb.checked = false;
      shareSyncPhotoMarkersVisibility();
      return;
    }
    const layerBox = ensurePhotoStackLayer();
    // 地图图层树还没建好：现在画会挂错地方，等一下重试（最多等 ~6 秒）
    if (!layerBox) {
      photoLayerRetry = (photoLayerRetry || 0) + 1;
      if (photoLayerRetry <= 12) setTimeout(() => shareToggleLayerPhotos(true, true), 500);
      return;
    }
    photoLayerRetry = 0;
    clearPhotoStackLayer();
    const seen = {};
    withMedia.forEach(p => {
      // 坐标非常接近的点做小幅对角错开，避免完全重叠
      const key = p.lng.toFixed(4) + ',' + p.lat.toFixed(4);
      const k = seen[key] = (seen[key] || 0) + 1;
      let lng = p.lng, lat = p.lat;
      if (k > 1) { lng += (k - 1) * 0.00018; lat -= (k - 1) * 0.00012; }

      const media = p.media;
      const stackN = Math.min(media.length, 3);
      let inner = '';
      for (let i = stackN - 1; i >= 0; i--) {
        const rot = (i - (stackN - 1)) * 7;    // 最上层 0deg，越往下越斜
        const off = (i - (stackN - 1)) * -9;   // 向下堆叠偏移
        const thumb = media[i].thumb_filename || media[i].filename;
        inner += '<img src="' + mediaUrl('/uploads/thumbs/' + thumb) + '" ' +
                 'style="transform: translate(' + off + 'px,' + (-off) + 'px) rotate(' + rot + 'deg); z-index:' + (10 + i) + ';">';
      }
      const badge = media.length > 1
        ? '<span class="photo-stack-badge">' +
          (media.length > 99 ? '99+' : media.length) + '</span>'
        : '';
      const stackEl = document.createElement('div');
      stackEl.className = 'photo-stack';
      stackEl.innerHTML = inner + badge;
      const openStack = function () {
        const now = Date.now();
        if (stackEl.__last && now - stackEl.__last < 500) return;
        stackEl.__last = now;
        lightboxMedia = p.media.map(x => Object.assign({ point_name: p.name }, x));
        lightboxIndex = 0;
        shareRenderLightbox();
        $('#lightbox').classList.remove('hidden');
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
  shareSyncPhotoMarkersVisibility();
  syncPhotoStackScale();
}

// 图层勾选状态跟着分享 token 一起存，刷新后保留
function shareSaveLayerState(patch) {
  const cur = getShareState().layers || {};
  saveShareState({ layers: Object.assign(cur, patch) });
}
function shareRestoreLayerState() {
  const s = getShareState().layers || {};
  const apply = (sel, key, fn) => {
    const cb = $(sel);
    if (!cb || s[key] === undefined) return;
    cb.checked = !!s[key];
    if (cb.checked) { try { fn(); } catch (e) {} }
  };
  apply('#lay-poi', 'poi', () => toggleLayerPoi(true));
  apply('#lay-satellite', 'satellite', () => toggleLayerSatellite(true));
  apply('#lay-traffic', 'traffic', () => toggleLayerTraffic(true));
  // 「照片视频」默认开：收到分享链接的人一进来就能在地图上看到照片
  const photoCb = $('#lay-photos');
  if (photoCb) {
    const want = s.photos === undefined ? true : !!s.photos;
    photoCb.checked = want;
    if (want) { try { shareToggleLayerPhotos(true, true); } catch (e) {} }
  }
  // 「里程标签」默认开，理由同主页面：加开关前它就是常驻的
  const mileageCb = $('#lay-mileage');
  if (mileageCb) {
    mileageCb.checked = s.mileage === undefined ? true : !!s.mileage;
    applySegLabelsVisibility();
  }
  // 「公路编号」同理：默认开
  const roadCb = $('#lay-road');
  if (roadCb) {
    roadCb.checked = s.road === undefined ? true : !!s.road;
    applyRoadLabelsVisibility();
  }
  shareSyncPhotoMarkersVisibility();
}

function shareInitLayerControls() {
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
  $('#lay-poi').addEventListener('change', function () { toggleLayerPoi(this.checked); shareSaveLayerState({ poi: this.checked }); });
  $('#lay-satellite').addEventListener('change', function () { toggleLayerSatellite(this.checked); shareSaveLayerState({ satellite: this.checked }); });
  $('#lay-traffic').addEventListener('change', function () { toggleLayerTraffic(this.checked); shareSaveLayerState({ traffic: this.checked }); });
  $('#lay-photos').addEventListener('change', function () { shareToggleLayerPhotos(this.checked); shareSaveLayerState({ photos: this.checked }); });
  $('#lay-mileage').addEventListener('change', function () { applySegLabelsVisibility(); shareSaveLayerState({ mileage: this.checked }); });
  $('#lay-road').addEventListener('change', function () { applyRoadLabelsVisibility(); shareSaveLayerState({ road: this.checked }); });
}

// ---------- 移动端：左右滑动切换 tab ----------
// 与主页面同一套判定：只认明确的横向手势，纵向一律放行给滚动；双指忽略；不循环。
const TAB_ORDER = ['points', 'detail', 'albums'];
(function initTabSwipe() {
  const panel = document.querySelector('.panel');
  if (!panel) return;
  const H_MIN = 55;      // 触发切换的最小横向位移(px)
  const LOCK = 10;       // 超过这个位移才判定方向
  let sx = 0, sy = 0, axis = '', active = false;

  panel.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { active = false; return; }
    active = true; axis = '';
    sx = e.touches[0].clientX;
    sy = e.touches[0].clientY;
  }, { passive: true });

  panel.addEventListener('touchmove', (e) => {
    if (!active) return;
    if (e.touches.length !== 1) { active = false; return; }
    const dx = e.touches[0].clientX - sx;
    const dy = e.touches[0].clientY - sy;
    if (!axis) {
      if (Math.abs(dx) < LOCK && Math.abs(dy) < LOCK) return;
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (axis === 'y') { active = false; return; }
      const tk = document.querySelector('.view-track');
      if (tk) tk.classList.remove('animating');               // 跟手期间不要过渡，否则发黏
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
    const tk = document.querySelector('.view-track');
    if (tk) tk.classList.add('animating');                     // 松手后的吸附要带过渡
    const i = TAB_ORDER.indexOf(currentTab);
    const next = Math.abs(dx) >= H_MIN ? TAB_ORDER[i + (dx < 0 ? 1 : -1)] : null;
    if (!next) { positionViews(currentTab); return; }           // 位移不够或已在两端 → 弹回
    shareSwitchTab(next);
  }, { passive: true });

  panel.addEventListener('touchcancel', () => {
    active = false;
    const tk = document.querySelector('.view-track');
    if (tk) tk.classList.add('animating');
    positionViews(currentTab);
  }, { passive: true });
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

  // 与主页面一致：地图高度下限 0（面板可吃掉整个地图区），单击手柄三档循环
  const MAP_MIN_VH = 0;
  const MAP_MAX_VH = 80;
  const MAP_SNAP_VH = [0, 45, 72];

  function setMapHeight(h) {
    mapEl.style.height = Math.round(h) + 'px';
    saveShareState({ panelHeight: Math.round(h) });
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
$('#lightbox-close').onclick = shareCloseLightbox;
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
    if (Math.abs(d) >= need) turned = shareLightboxNav(d < 0 ? 1 : -1, axis);
    if (!turned) springBack();
    mode = '';
  }, { passive: false });

  box.addEventListener('touchcancel', () => {
    active = false; mode = '';
    if (!lbZoom.on) springBack();
  }, { passive: true });
})();

$('#lightbox-prev').onclick = () => shareLightboxNav(-1);
$('#lightbox-next').onclick = () => shareLightboxNav(1);
$('#lightbox').addEventListener('click', (e) => {
  if (e.target === $('#lightbox')) shareCloseLightbox();
});
document.addEventListener('keydown', (e) => {
  if ($('#lightbox').classList.contains('hidden')) return;
  if (e.key === 'Escape') shareCloseLightbox();
  else if (e.key === 'ArrowLeft') shareLightboxNav(-1);
  else if (e.key === 'ArrowRight') shareLightboxNav(1);
});

// ---------- 轨迹回放事件 ----------
$('#replay-toggle').onclick = shareToggleReplay;
$('#replay-launch').onclick = () => shareStartReplay();      // 按需出现：点入口才开始回放
$('#replay-close').onclick = () => shareStopReplay(true);    // 关闭回放条，恢复入口按钮
const _spdBtn = $('#replay-speed');
if (_spdBtn) {
  _spdBtn.onclick = () => {
    const speeds = [1, 2, 4];
    const cur = speeds.indexOf(replaySpeed);
    replaySpeed = speeds[(cur + 1) % speeds.length];
    _spdBtn.textContent = replaySpeed + 'x';
  };
}

// 按「最上层优先」依次尝试关闭一层，关掉了返回 true。
function shareCloseTopLayer() {
  const q = (s) => document.querySelector(s);
  const shown = (el) => el && !el.classList.contains('hidden');

  const dlg = q('.tm-dialog-mask');
  if (dlg) {
    const btn = dlg.querySelector('.tm-dialog-cancel') || dlg.querySelector('.tm-dialog-ok');
    if (btn) btn.click(); else dlg.remove();
    return true;
  }
  if (shown(q('#lightbox'))) {
    if (typeof lbZoom !== 'undefined' && lbZoom.on) { lbExitZoom(true); return true; }
    shareCloseLightbox();
    return true;
  }
  if (shown(q('#stats-modal'))) { q('#stats-modal').classList.add('hidden'); return true; }
  const mapEl = q('#map');
  if (mapEl && mapEl.classList.contains('map-fullscreen')) {
    const b = q('#btn-map-fullscreen');
    if (b) b.click();
    return true;
  }
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
    if (shareCloseTopLayer()) { fill(); return; }      // 关掉一层，这次返回就当成「关闭当前层」
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
shareInitMap();
shareInitLayerControls();
shareRestorePanelState();
$('#points-view').addEventListener('scroll', shareSavePointListScroll, { passive: true });
positionViews(currentTab);   // 首屏先把三页在轨道上摆好（不带过渡）
syncTabIndicator();
loadShare();
