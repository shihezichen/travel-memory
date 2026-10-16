/* 照片 → 轨迹：前端交互
   ============================================================================
   入口（两端都有）：
     · 桌面顶栏「照片行程」按钮
     · 手机「更多」菜单里的「照片行程」（顶栏按钮在窄屏会全部收进这个菜单）

   两种模式：
     新建行程 —— 填行程名 + 选照片 → 预览 → 生成
     追加到既有行程 —— 选行程 + 选照片 → 预览 → 追加（轨迹按时间插到对应位置）

   流程是「选照片 → 服务端解析 EXIF → 预览轨迹点 → 确认生成」。
   预览这一步刻意放在写库之前：逆地理编码要打网络，用户看到点名列得不好
   可以直接改行程名或取消，不会留下一堆垃圾行程。

   照片有两类，UI 上必须说清楚：
     · 可用 = 同时有拍摄时间和 GPS → 会变成轨迹点
     · 待分配 = 缺时间或缺 GPS（截图、微信转存）→ 挂到「📷 待分配照片」，
       用户之后可以手工拖到别的点上。不说清楚的话用户会以为照片丢了。
   ========================================================================== */
'use strict';

(function () {
  const API_PREVIEW = '/api/photo-trip/preview';
  const API_CREATE = '/api/photo-trip/create';

  let state = {
    mode: 'create',      // create | append
    files: [],           // File[]
    plan: null,          // 服务端返回的 plan
    tripId: null,        // append 模式下的目标行程
    trips: [],           // 既有行程列表
    busy: false,
  };

  // ---------- 小工具 ----------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // 提示条。本项目的提示函数叫 tmToast（定义在 common.js），**不是** toast。
  // 这里做一层兜底：万一将来改名，也不至于让「生成成功」这种时刻直接抛错。
  function notify(text) {
    if (typeof window.tmToast === 'function') {
      window.tmToast(text);
    } else if (typeof window.toast === 'function') {
      window.toast(text);
    } else {
      setStatus(text);
    }
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(0) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  async function loadTrips() {
    if (state.trips.length) return state.trips;
    try {
      const r = await fetch('/api/trips', { credentials: 'same-origin' });
      if (r.ok) state.trips = await r.json();
    } catch (e) { /* 拉不到就让用户走新建 */ }
    return state.trips;
  }

  // ---------- 弹窗 DOM ----------
  function buildModal() {
    const wrap = el('div', 'pt-modal hidden');
    wrap.id = 'photo-trip-modal';
    wrap.innerHTML = `
      <div class="pt-box" role="dialog" aria-modal="true" aria-labelledby="pt-title">
        <h2 id="pt-title">照片生成轨迹</h2>

        <div class="pt-mode-row">
          <div class="pt-segmented" id="pt-seg" role="tablist">
            <button type="button" class="pt-seg-btn active" data-mode="create"
                    role="tab" aria-selected="true">新建行程</button>
            <button type="button" class="pt-seg-btn" data-mode="append"
                    role="tab" aria-selected="false">追加到既有行程</button>
          </div>
        </div>

        <div class="pt-field" id="pt-field-new">
          <label class="pt-label" for="pt-trip-name">行程名称</label>
          <input type="text" id="pt-trip-name" class="pt-input"
                 placeholder="例如：2026 杭州周末" maxlength="60">
        </div>

        <div class="pt-field hidden" id="pt-field-pick">
          <label class="pt-label" for="pt-trip-pick">选择行程</label>
          <select id="pt-trip-pick" class="pt-input"></select>
          <p class="pt-hint">照片会追加到这个行程，轨迹按拍摄时间插到对应位置。</p>
        </div>

        <div class="pt-drop" id="pt-drop" tabindex="0"
             role="button" aria-label="选择或拖入照片">
          <div class="pt-drop-icon">🖼️</div>
          <div class="pt-drop-main">点击选择照片，或拖到这里</div>
          <div class="pt-drop-sub">支持一次选多张 · JPG / HEIC / PNG · 读取拍摄时间与 GPS</div>
          <input type="file" id="pt-file-input" accept="image/*" multiple hidden>
        </div>

        <div class="pt-status" id="pt-status"></div>
        <div class="pt-preview" id="pt-preview"></div>

        <div class="modal-actions">
          <button type="button" class="btn" id="pt-cancel">取消</button>
          <button type="button" class="btn btn-primary" id="pt-ok" disabled>生成轨迹</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    return wrap;
  }

  let modal = null;

  function ensureModal() {
    if (!modal) {
      modal = buildModal();
      const drop = modal.querySelector('#pt-drop');
      const input = modal.querySelector('#pt-file-input');

      drop.addEventListener('click', () => input.click());
      drop.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
      });
      input.addEventListener('change', () => {
        if (input.files && input.files.length) setFiles(Array.from(input.files));
        input.value = '';
      });

      // 拖拽
      ['dragenter', 'dragover'].forEach(ev =>
        drop.addEventListener(ev, (e) => {
          e.preventDefault(); drop.classList.add('dragging');
        }));
      ['dragleave', 'drop'].forEach(ev =>
        drop.addEventListener(ev, (e) => {
          e.preventDefault(); drop.classList.remove('dragging');
        }));
      drop.addEventListener('drop', (e) => {
        const fs = e.dataTransfer && e.dataTransfer.files;
        if (fs && fs.length) setFiles(Array.from(fs));
      });

      // 模式切换
      modal.querySelectorAll('.pt-seg-btn').forEach(b => {
        b.addEventListener('click', () => switchMode(b.dataset.mode));
      });

      // 点遮罩关闭
      modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
      });
      modal.querySelector('#pt-cancel').addEventListener('click', close);
      modal.querySelector('#pt-ok').addEventListener('click', submit);
    }
    return modal;
  }

  function switchMode(mode) {
    state.mode = mode === 'append' ? 'append' : 'create';
    modal.querySelectorAll('.pt-seg-btn').forEach(b => {
      const on = b.dataset.mode === state.mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    modal.querySelector('#pt-field-new').classList.toggle('hidden', state.mode !== 'create');
    modal.querySelector('#pt-field-pick').classList.toggle('hidden', state.mode !== 'append');
    modal.querySelector('#pt-ok').textContent =
      state.mode === 'append' ? '追加到行程' : '生成轨迹';
    if (state.mode === 'append') fillTripSelect();
    setStatus('');
  }

  async function fillTripSelect() {
    const sel = modal.querySelector('#pt-trip-pick');
    if (sel.dataset.filled) return;
    setStatus('正在读取行程列表…');
    const trips = await loadTrips();
    sel.innerHTML = '';
    if (!trips.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = '（还没有行程，请先新建）';
      sel.appendChild(o);
      sel.disabled = true;
      setStatus('你还没有任何行程，请切到「新建行程」模式。', 'err');
      return;
    }
    sel.disabled = false;
    trips.forEach(t => {
      const o = document.createElement('option');
      o.value = String(t.id);
      const d = t.start_date ? ` · ${t.start_date}` : '';
      o.textContent = (t.name || '未命名') + d;
      sel.appendChild(o);
    });
    // 默认选中当前行程（若在列表里）
    const cur = window.currentTripId;
    if (cur && trips.some(t => t.id === Number(cur))) sel.value = String(cur);
    sel.dataset.filled = '1';
    setStatus('');
    // 换行程 =换了一批「已存在」的基准，照片若已选就重新预检一次
    sel.addEventListener('change', () => {
      if (state.files.length) setFiles(state.files);
    });
  }

  function close() {
    if (!modal) return;
    modal.classList.add('hidden');
    state.files = [];
    state.plan = null;
    state.busy = false;
    const st = modal.querySelector('#pt-status');
    if (st) { st.textContent = ''; st.className = 'pt-status'; }
    const pv = modal.querySelector('#pt-preview');
    if (pv) pv.innerHTML = '';
    const ok = modal.querySelector('#pt-ok');
    if (ok) { ok.disabled = true; ok.textContent = '生成轨迹'; }
    const nm = modal.querySelector('#pt-trip-name');
    if (nm) nm.value = '';
  }

  // ---------- 选照片 → 调preview ----------
  async function setFiles(files) {
    const photos = files.filter(f => f.type.startsWith('image/'));
    if (!photos.length) {
      setStatus('没识别到图片文件', 'err');
      return;
    }
    state.files = photos;
    setStatus(`已选 ${photos.length} 张（${fmtSize(
      photos.reduce((s, f) => s + f.size, 0))}），正在读取拍摄时间与位置…`);

    const fd = new FormData();
    photos.forEach(f => fd.append('files', f, f.name));
    // 追加模式下把目标行程告诉后端，让它顺手比对「哪些已经存在」——
    // 在用户点确认之前就把这件事说出来，比事后补救友好得多。
    if (state.mode === 'append') {
      const tid = modal.querySelector('#pt-trip-pick').value;
      if (tid) fd.append('trip_id', tid);
    }
    try {
      const r = await fetch(API_PREVIEW, {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.plan = data.plan;
      state.previewDupCount = data.duplicate_count;
      state.previewDupPhotos = data.duplicate_photos || [];
      renderPreview(data);
    } catch (err) {
      setStatus('解析失败：' + err.message, 'err');
      state.plan = null;
      const pv = modal.querySelector('#pt-preview');
      if (pv) pv.innerHTML = '';
      const ok = modal.querySelector('#pt-ok');
      if (ok) ok.disabled = true;
    }
  }

  function setStatus(text, cls) {
    if (!modal) return;
    const st = modal.querySelector('#pt-status');
    st.textContent = text;
    st.className = 'pt-status' + (cls ? ' ' + cls : '');
  }

function renderPreview(data) {
    const plan = data.plan || {};
    const s = plan.stats || {};
    const pv = modal.querySelector('#pt-preview');
    const ok = modal.querySelector('#pt-ok');
    pv.innerHTML = '';

    const verb = state.mode === 'append' ? '将追加' : '将生成';

    if (!s.usable) {
      setStatus(`读了 ${s.photo_total || 0} 张，但没有一张同时有拍摄时间和 GPS。`
        + '截图、微信保存的图通常没有 GPS，这类照片会进「待分配」。', 'err');
      ok.disabled = !state.files.length;
      ok.textContent = state.mode === 'append' ? '仅导入照片' : '仅导入照片';
      return;
    }

    const summary = el('div', 'pt-summary');
    summary.appendChild(el('span', null, `${verb} ${s.point_count} 个轨迹点`));
    summary.appendChild(el('span', null, ` · ${s.usable} 张可用照片`));
    if (s.date_from) {
      summary.appendChild(el('span', null,
        ` · ${s.date_from}${s.date_from !== s.date_to ? ' ~ ' + s.date_to : ''}`));
    }
    pv.appendChild(summary);

    // 轨迹点预览
    const list = el('div', 'pt-points');
    (plan.points || []).forEach((p, i) => {
      const row = el('div', 'pt-point');
      row.appendChild(el('span', 'pt-idx', String(i + 1)));
      row.appendChild(el('span', 'pt-pname', p.name));
      row.appendChild(el('span', 'pt-pdate', `${p.date} ${p.time_label}`));
      row.appendChild(el('span', 'pt-pcount', `${p.photo_count} 张`));
      list.appendChild(row);
    });
    pv.appendChild(list);

    // 无 GPS / 无时间的照片，明确告知去向
    if (s.orphan) {
      pv.appendChild(el('div', 'pt-orphan-note',
        `另有 ${s.orphan} 张照片缺少拍摄时间或 GPS（多为截图），`
        + `会放到「📷 待分配照片」，你可以稍后手工分配到具体地点。`));
    }

    // 重复照片（追加模式）：在确认按钮之前就说清楚，别让用户点了才知道白点。
    const dupN = state.previewDupCount;
    if (dupN != null && dupN > 0) {
      const box = el('div', 'pt-dup-warn');
      box.appendChild(el('strong', null,
        `其中 ${dupN} 张照片在这个行程里已经有了`));
      const sample = (state.previewDupPhotos || []).slice(0, 3)
        .map((d) => d.filename).join('、');
      box.appendChild(el('div', 'pt-dup-warn-sub',
        dupN === 1 ? '重复的照片不会被再次上传。'
                   : `重复的照片不会被再次上传${sample ? `（如 ${sample}${dupN > 3 ? ' 等' : ''}）` : ''}`));
      pv.appendChild(box);
    }

    const unnamed = data.named_count != null
      ? (plan.points || []).length - data.named_count : 0;
    if (unnamed > 0) {
      pv.appendChild(el('div', 'pt-orphan-note',
        `其中 ${unnamed} 个点没查到地名，暂时用拍摄时间命名。`));
    }

    ok.disabled = false;
    ok.textContent = state.mode === 'append' ? '追加到行程' : '生成轨迹';
    setStatus('');
  }

  // ---------- 追加结果：把「重复」这件事说清楚 ----------
  //
  // 为什么值得单独写一段（2026-10-16 用户反馈）：原先这里只 toast 一句
  // 「已追加 2 个轨迹点」，但用户拖进来 9 张、结果一张没进去 —— 他完全
  // 不知道发生了什么，只能怀疑功能坏了。实际上是 9 张全都在这个行程里了，
  // 服务端按 (拍摄时间, 坐标) 去重后全部跳过，这是**正确行为**，
  // 但从界面看就是「说添加了却没反应」。
  //
  // 现在分三种情况，每种都把账算清楚：
  //   · 全部重复   → 明确说「9 张都已存在，一张都没有上传」，并列出前几张
  //   · 部分重复   → 分开报「新增 N 张 / 跳过 M 张」，重复的列出来
  //   · 无重复     → 正常报新增
  function reportAppendResult(data) {
    const added = data.added_photos || 0;
    const dupes = data.duplicate_photos || [];
    const dupeCount = dupes.length;
    const orphanAdded = data.orphan_count || 0;

    const parts = [];
    if (added > 0) parts.push(`新增 ${added} 张照片`);
    if (orphanAdded > 0) parts.push(`${orphanAdded} 张放入待分配`);
    if (dupeCount > 0) parts.push(`${dupeCount} 张已存在、未重复上传`);

    if (!parts.length) {
      notify('没有可添加的照片');
      return;
    }

    notify(parts.join(' · '));

    // 全部被跳过时，光靠 toast 太短（而且 toast 会自动消失）—— 再补一个
    // 长驻的结果面板，把重复的照片逐条列出来，用户可以核对是不是同一批。
    if (data.nothing_added && dupeCount > 0) {
      showDupReport(dupes);
    } else if (dupeCount > 0) {
      showDupReport(dupes, false);
    }
  }

  function showDupReport(dupes, replace) {
    // 挂在页面上而不是弹窗里：弹窗已经close了，重新打开反而绕。
    let box = document.getElementById('pt-dup-report');
    if (!box) {
      box = el('div', 'pt-dup-report');
      box.id = 'pt-dup-report';
      document.body.appendChild(box);
    }
    box.innerHTML = '';
    const head = el('div', 'pt-dup-head',
      `以下 ${dupes.length} 张照片在这个行程里已经有了，本次没有重复上传：`);
    box.appendChild(head);
    const list = el('div', 'pt-dup-list');
    // 一次最多列 12 条，剩下折叠成一行 —— 有 60 张重复时不至于刷屏
    dupes.slice(0, 12).forEach((d) => {
      const row = el('div', 'pt-dup-item');
      row.appendChild(el('span', 'pt-dup-name', d.filename || '(未命名)'));
      if (d.time) row.appendChild(el('span', 'pt-dup-time', d.time));
      list.appendChild(row);
    });
    box.appendChild(list);
    if (dupes.length > 12) {
      box.appendChild(el('div', 'pt-dup-more', `…另有 ${dupes.length - 12} 张`));
    }
    box.appendChild(el('button', 'pt-dup-close', '知道了'));

    const closeBtn = box.querySelector('.pt-dup-close');
    closeBtn.addEventListener('click', () => box.remove());

    box.classList.remove('hidden');
    // 过一会儿淡出，但别自动消失得那么快——用户要能读完
    void replace;
  }

  // ---------- 确认生成 ----------
  async function submit() {
    if (state.busy || !state.files.length) return;
    const ok = modal.querySelector('#pt-ok');
    state.busy = true;
    ok.disabled = true;
    const old = ok.textContent;
    ok.textContent = '处理中…';

    try {
      const fd = new FormData();
      state.files.forEach(f => fd.append('files', f, f.name));

      let url;
      if (state.mode === 'append') {
        const tid = modal.querySelector('#pt-trip-pick').value;
        if (!tid) {
          setStatus('请先选择要追加到的行程', 'err');
          state.busy = false; ok.disabled = false; ok.textContent = old;
          return;
        }
        url = `/api/trips/${tid}/photo-trip/append`;
        state.tripId = tid;
      } else {
        const name = (modal.querySelector('#pt-trip-name').value || '').trim();
        if (!name) {
          setStatus('请先给行程起个名字', 'err');
          state.busy = false; ok.disabled = false; ok.textContent = old;
          return;
        }
        url = API_CREATE;
        fd.append('trip_name', name);
      }

      const r = await fetch(url, {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();

      const s = data.stats || {};
      if (state.mode === 'append') {
        reportAppendResult(data);
      } else {
        notify(`已生成行程：${s.point_count} 个轨迹点 / ${s.usable} 张照片`);
      }

      const tid = state.mode === 'append' ? state.tripId : data.trip_id;
      close();
      state.trips = [];   // 行程列表可能变了，下次进弹窗重新拉

      // 切到新行程让用户直接看到结果。selectTrip 是 app.js 的顶层 function，
      // 运行时能拿到；仍做存在性判断，失败就退回刷新。
      if (tid && typeof window.selectTrip === 'function') {
        try { await window.selectTrip(tid); return; } catch (e) { /* 落下去刷新 */ }
      }
      location.reload();
    } catch (err) {
      setStatus('生成失败：' + err.message, 'err');
      state.busy = false;
      ok.disabled = false; ok.textContent = old;
    }
  }

  // ---------- 对外入口 ----------
  function open(mode, tripId) {
    const m = ensureModal();
    state.files = [];
    state.plan = null;
    state.busy = false;
    if (tripId) state.tripId = tripId;

    m.querySelector('#pt-title').textContent = '照片生成轨迹';
    m.querySelector('#pt-preview').innerHTML = '';
    m.querySelector('#pt-status').textContent = '';
    m.querySelector('#pt-ok').disabled = true;
    m.classList.remove('hidden');

    // 默认模式：传了 tripId 就直接进追加，否则新建
    switchMode(mode === 'append' || tripId ? 'append' : 'create');

    if (state.mode === 'create') {
      const d = new Date();
      m.querySelector('#pt-trip-name').value = `${d.getFullYear()} 照片行程`;
    }
  }

  window.PhotoTrip = { open };
})();