/* ============================================================================
   起步引导面板 —— 新用户注册后弹在屏幕正中的「挑一个开始」
   ============================================================================
   为什么单独一个模块而不是塞进 app.js：app.js 已经 8000 行，这个面板只在
   注册成功那一瞬间出现一次，逻辑很短却要占位；独立出来好读、好复用，也
   避免动 app.js 里那些要靠行号定位的段落。

   两张卡对应两条完全不同的起步路径：
     · 照片行程 —— 已经有手机相册里的照片？丢进来自动生成轨迹
     · 手工行程 —— 从零开始自己划地点

   命名与顶栏 / 更多菜单保持一致（都叫「照片行程」「手工行程」），第一次
   看到的东西和之后找得到的东西是同一个词，不用来回对照。

   逃逸方式：点遮罩、右上角 ×、或按 Esc 都能关 —— 新用户不该被一个弹窗
   困住，关掉之后顶栏和「更多」里都有同样的入口。
   ========================================================================== */
'use strict';

(function () {
  let overlay = null;

  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function close() {
    if (overlay) overlay.remove();
    overlay = null;
    document.removeEventListener('keydown', onKey);
    // 用户主动关掉（而不是从卡片进去）时，把本次会话的引导标记也置上，
    // 免得他接下来切个行程、点个按钮，面板又糊回来一次。
    if (window.__tmWelcomeDismissed) window.__tmWelcomeDismissed();
  }

  function onKey(e) {
    if (e.key === 'Escape') close();
  }

  function openWelcomePanel() {
    // 重复调用不叠加
    if (overlay) return;

    overlay = el('div', 'wc-overlay');
    overlay.id = 'welcome-panel';
    overlay.innerHTML = `
      <div class="wc-box" role="dialog" aria-modal="true"
           aria-labelledby="wc-title">
        <button type="button" class="wc-close" aria-label="关闭">&times;</button>
        <h2 id="wc-title">欢迎使用旅行记忆</h2>
        <p class="wc-sub">先挑一个开始，两个入口随时可以在顶栏或「更多」里找到</p>

        <div class="wc-cards">
          <button type="button" class="wc-card" data-act="photo">
            <span class="wc-ico" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 7l4-2 5 2 5-2 4 2v10l-4 2-5-2-5 2-4-2z"/>
                <path d="M7 5v10"/><path d="M17 5v10"/>
              </svg>
            </span>
            <span class="wc-t">照片行程</span>
            <span class="wc-d">
              根据导入的照片和视频，自动按时间和位置，聚成一条行程轨迹
            </span>
          </button>

          <button type="button" class="wc-card" data-act="manual">
            <span class="wc-ico" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
            </span>
            <span class="wc-t">手工行程</span>
            <span class="wc-d">
              自己新建一个行程，然后在地图上逐个添加想去的地点
            </span>
          </button>
        </div>

        <button type="button" class="wc-skip">先逛逛，稍后再说</button>
      </div>`;

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) { close(); return; }
      const card = e.target.closest('.wc-card');
      if (card) {
        close();
        if (card.dataset.act === 'photo') {
          if (window.PhotoTrip) window.PhotoTrip.open('create');
        } else {
          if (typeof openNewTrip === 'function') openNewTrip();
        }
        return;
      }
      if (e.target.closest('.wc-close') || e.target.closest('.wc-skip')) close();
    });

    document.body.appendChild(overlay);
    document.addEventListener('keydown', onKey);
    // 让键盘用户能直接落到第一张卡上
    const first = overlay.querySelector('.wc-card');
    if (first) first.focus();
  }

  window.openWelcomePanel = openWelcomePanel;
})();