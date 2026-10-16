/* ============================================================================
   「📷 待分配照片」池的专用能力
   ============================================================================
   这个点是个特例，不是普通的轨迹点：
     · 不能改名 —— 前端/后端都靠名字识别它，改名会让整个识别逻辑失效
       （db.is_pending_pool / 前端地图过滤都按名字判）
     · 没有位置，也不参与轨迹
     · 里面装的是「缺时间或缺位置」的照片，需要人工决定：移到某个真地点，
       或者干脆删掉

   所以详情页里它多两样东西：
     1. 编辑按钮置灰 —— 不是 disabled，是视觉上就表明「这个不能改」，
        并且 title 说清原因，免得用户反复点以为是坏了
     2. 多选模式 —— 勾选若干张后批量「移动到别的点」或「删除」

   多选只在待分配池里出现：普通轨迹点的照片是「已经安置好的」，没有
   批量搬迁的必要，多加一套 UI 只会让人以为照片能被随便搬走。
   ========================================================================== */
'use strict';

(function () {
  let selected = new Set();     // 选中的 media id
  let poolMode = false;         // 多选模式是否开着

  // ---------- 进入/退出多选 ----------
  function enterSelectMode() {
    poolMode = true;
    selected = new Set();
    document.body.classList.add('pt-pool-select');
    syncBar();
    refreshChecks();
  }

  function exitSelectMode() {
    poolMode = false;
    selected = new Set();
    document.body.classList.remove('pt-pool-select');
    // 撤掉每格上的对勾痕迹，回到「点开看大图」的常态
    document.querySelectorAll('.media-item.is-picked').forEach((el) => {
      el.classList.remove('is-picked');
    });
    const bar = document.getElementById('pt-pool-bar');
    if (bar) bar.remove();
  }

  function togglePick(mid) {
    const id = Number(mid);
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    refreshChecks();
    syncBar();
  }

  function refreshChecks() {
    document.querySelectorAll('#media-grid .media-item').forEach((item) => {
      const on = selected.has(Number(item.dataset.mid));
      item.classList.toggle('is-picked', on);
      const box = item.querySelector('.pt-pick-box');
      if (box) box.textContent = on ? '✓' : '';
    });
  }

  function syncBar() {
    let bar = document.getElementById('pt-pool-bar');
    const grid = $('#media-grid');
    if (!grid || !grid.parentElement) return;

    if (!poolMode) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'pt-pool-bar';
      bar.className = 'pt-pool-bar';
      bar.innerHTML =
        '<span class="pt-pool-count"><b>0</b> 张已选</span>' +
        '<button type="button" class="btn pt-pool-move">移动到…</button>' +
        '<button type="button" class="btn btn-danger pt-pool-del">删除</button>' +
        '<button type="button" class="btn pt-pool-cancel">退出多选</button>';
      grid.parentElement.appendChild(bar);

      bar.querySelector('.pt-pool-move').onclick = openMoveSheet;
      bar.querySelector('.pt-pool-del').onclick = doDelete;
      bar.querySelector('.pt-pool-cancel').onclick = exitSelectMode;
    }
    const n = selected.size;
    bar.querySelector('.pt-pool-count b').textContent = String(n);
    // 没选中就禁用，避免点出一个「对 0 张照片做操作」的尴尬态
    bar.querySelector('.pt-pool-move').disabled = n === 0;
    bar.querySelector('.pt-pool-del').disabled = n === 0;
  }

  // ---------- 选目标点 ----------
  async function openMoveSheet() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    let pts = [];
    try {
      pts = (await api(`/api/trips/${currentTripId}/points`)) || [];
    } catch (e) { return; }
    // 池子自己不在候选里（后端也会挡一道，这里是为了不让用户点到它）
    pts = pts.filter((p) => !p.is_pending_pool);
    if (!pts.length) {
      tmToast('这个行程还没有别的地点，先新建一个');
      return;
    }

    const body = '<div class="pt-move-list">' + pts.map((p, i) => {
      const when = p.start_date ? ` · ${p.start_date}` : '';
      return `<button type="button" class="pt-move-item" data-i="${i}">
                <span class="pt-move-name">${p.name}</span>
                <span class="pt-move-sub">${when}</span>
              </button>`;
    }).join('') + '</div>';

    openModal('移动到哪个地点', body);
    document.querySelectorAll('.pt-move-item').forEach((btn) => {
      btn.onclick = () => {
        const p = pts[Number(btn.dataset.i)];
        closeModal();
        doMove(ids, p.id, p.name);
      };
    });
  }

  async function doMove(ids, targetId, targetName) {
    try {
      const fd = new FormData();
      fd.append('media_ids', ids.join(','));
      fd.append('target_point_id', String(targetId));
      const r = await api('/api/media/move', { method: 'POST', body: fd });
      const moved = (r && r.moved) || 0;
      const skipped = (r && r.skipped) || 0;
      tmToast(moved
        ? `已移动 ${moved} 张到「${targetName}」` + (skipped ? `，${skipped} 张失败` : '')
        : '没有移动任何照片');
      exitSelectMode();
      await loadDetail(currentPointId);
      await loadTrips();
    } catch (e) {
      tmToast('移动失败：' + (e && e.message ? e.message : '未知错误'));
    }
  }

  async function doDelete() {
    const ids = Array.from(selected);
    if (!ids.length) return;
    const ok = await tmConfirm(
      `删除选中的 ${ids.length} 张照片？此操作不可恢复。`,
      '删除照片', { danger: true, okText: '删除' });
    if (!ok) return;

    let okCount = 0;
    for (const id of ids) {
      try { await api(`/api/media/${id}`, { method: 'DELETE' }); okCount += 1; }
      catch (e) { /* 单张失败不中断后面的 */ }
    }
    tmToast(`已删除 ${okCount} 张照片`);
    exitSelectMode();
    await loadDetail(currentPointId);
    await loadTrips();
  }

  // ---------- 对外 ----------
  window.PendingPool = {
    isSelectMode: () => poolMode,
    enter: enterSelectMode,
    exit: exitSelectMode,
    toggle: togglePick,
    isPicked: (mid) => selected.has(Number(mid)),
  };
})();