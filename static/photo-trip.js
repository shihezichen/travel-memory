/* 照片 → 轨迹：前端交互
   ============================================================================
   两个入口，一个共用弹窗：
     · 顶栏「照片生成轨迹」→ 新建行程（mode=create）
     · 面板悬浮加号的长按/双击 → 往当前行程追加（mode=append）

   流程是「选照片 → 服务端解析 EXIF → 预览轨迹点 → 确认生成」三步走。
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
    }
    // 都没有就退回弹窗自己的状态行，保证用户至少看得到结果
    if (typeof window.tmToast !== 'function' && typeof window.toast !== 'function') {
      setStatus(text);
    }
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(0) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  // ---------- 弹窗 DOM ----------
  function buildModal() {
    const wrap = el('div', 'pt-modal hidden');
    wrap.id = 'photo-trip-modal';
    wrap.innerHTML = `
      <div class="pt-box" role="dialog" aria-modal="true" aria-labelledby="pt-title">
        <h2 id="pt-title">照片生成轨迹</h2>

        <div class="pt-mode" id="pt-mode-row">
          <label class="pt-label">行程名称</label>
          <input type="text" id="pt-trip-name" class="pt-input"
                 placeholder="例如：2026 杭州周末" maxlength="60">
          <p class="pt-hint" id="pt-append-hint" style="display:none">
            照片会追加到当前行程，轨迹按时间自动插到对应位置。
          </p>
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

      //点遮罩关闭
      modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
      });
      modal.querySelector('#pt-cancel').addEventListener('click', close);
      modal.querySelector('#pt-ok').addEventListener('click', submit);
    }
    return modal;
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
    try {
      const r = await fetch(API_PREVIEW, {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      state.plan = data.plan;
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

    if (!s.usable) {
      setStatus(`读了 ${s.photo_total || 0} 张，但没有一张同时有拍摄时间和 GPS。`
        + '截图、微信保存的图通常没有 GPS，这类照片会进「待分配」。', 'err');
      ok.disabled = !state.files.length;
      ok.textContent = '仅导入照片';
      return;
    }

    const summary = el('div', 'pt-summary');
    summary.appendChild(el('span', null,
      `${s.usable} 张可用照片`));
    summary.appendChild(el('span', null, ` → ${s.point_count} 个轨迹点`));
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
      const warn = el('div', 'pt-orphan-note',
        `另有 ${s.orphan} 张照片缺少拍摄时间或 GPS（多为截图），`
        + `会放到「📷 待分配照片」，你可以稍后手工分配到具体地点。`);
      pv.appendChild(warn);
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

      let url, name;
      if (state.mode === 'append' && state.tripId) {
        url = `/api/trips/${state.tripId}/photo-trip/append`;
        name = '';
      } else {
        url = API_CREATE;
        name = (modal.querySelector('#pt-trip-name').value || '').trim();
        if (!name) {
          setStatus('请先给行程起个名字', 'err');
          state.busy = false; ok.disabled = false; ok.textContent = old;
          return;
        }
        fd.append('trip_name', name);
      }

      const r = await fetch(url, {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();

      const s = data.stats || {};
      const msg = state.mode === 'append'
        ? `已追加 ${data.added_points || 0} 个轨迹点`
          + (data.skipped_photos ? `，跳过 ${data.skipped_photos} 张重复照片` : '')
        : `已生成行程：${s.point_count} 个轨迹点 / ${s.usable} 张照片`;
      notify(msg);

      const tid = state.mode === 'append' ? state.tripId : data.trip_id;
      close();

      // 切到新行程让用户直接看到结果。
      // selectTrip / currentTripId 都定义在 app.js 里，而 app.js 的 <script>
      // 排在 photo-trip.js **之后**（index.html 405~407 行），但那时 app.js
      // 已经加载完毕，所以运行时能拿到。仍然做存在性判断：万一加载顺序变了，
      // 退化成刷新页面，至少结果不会丢。
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
    state.mode = mode === 'append' ? 'append' : 'create';
    state.tripId = tripId || null;
    state.plan = null;
    state.files = [];

    m.querySelector('#pt-title').textContent =
      state.mode === 'append' ? '照片追加到当前行程' : '照片生成轨迹';
    const nameRow = m.querySelector('#pt-mode-row');
    const nameInput = m.querySelector('#pt-trip-name');
    const hint = m.querySelector('#pt-append-hint');
    if (state.mode === 'append') {
      nameInput.style.display = 'none';
      hint.style.display = '';
      nameRow.querySelector('.pt-label').textContent = '';
    } else {
      nameInput.style.display = '';
      hint.style.display = 'none';
      nameRow.querySelector('.pt-label').textContent = '行程名称';
      if (!nameInput.value) {
        const d = new Date();
        nameInput.value = `${d.getFullYear()} 照片轨迹`;
      }
    }
    m.querySelector('#pt-preview').innerHTML = '';
    m.querySelector('#pt-status').textContent = '';
    m.querySelector('#pt-ok').disabled = true;
    m.classList.remove('hidden');
  }

  window.PhotoTrip = { open };
})();