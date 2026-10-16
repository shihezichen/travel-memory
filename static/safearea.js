/* ============================================================================
   原生壳（Android WebView / 鸿蒙 ArkWeb）沉浸式适配
   ============================================================================
   问题：纯 CSS 的 env(safe-area-inset-top) 在**原生 WebView 壳里恒为 0**。
   iOS Safari / Android Chrome 会把系统栏的 inset 通过 viewport-fit=cover 传给网页，
   但原生壳（Activity + WebView，或鸿蒙 Ability + ArkWeb）不会——它是自己的窗口，
   系统栏要么盖在 WebView 上方，要么由壳自己 padding。实测表现就是：状态栏的
   时间/电量压在网页顶栏的「体验账号 / 行程名」上，两层文字叠在一起。

   这里的做法：**让壳把真实 inset 用 JS 注进来**，网页按它补白。
   约定（两端壳都按这个来）：

     Android（Kotlin）：
       import androidx.core.view.ViewCompat
       import androidx.core.view.WindowInsetsCompat
       ViewCompat.setOnApplyWindowInsetsListener(webView) { v, insets ->
         val bars = insets.getInsets(
             WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
         val top = bars.top / resources.displayMetrics.density
         val bottom = bars.bottom / resources.displayMetrics.density
         webView.evaluateJavascript(
           "window.__tmSetInsets($top, $bottom);", null)
         insets   // 注意：返回原 insets，**不要consume**，否则 WebView 内部也拿不到
       }

     鸿蒙 ArkWeb（ArkTS）：
       import { window } from '@kit.ArkUI';
       window.getLastWindow(getContext(this)).then(win => {
         let top = 0, bottom = 0;
         try {
           const area = win.getWindowAvoidArea(window.AvoidAreaType.TYPE_SYSTEM);
           top = area.topRect.height;   // px
           bottom = area.bottomRect.height;
         } catch (e) {}
         this.arkWebViewController.runJavaScript(
           `window.__tmSetInsets(${top}, ${bottom});`);
       });
       // px → CSS px 的换算由网页自己按 devicePixelRatio 做，壳里不用管

   壳拿不到 inset（或者压根没接这个）时，网页会用下面的启发式兜底：
     · 独立显示（display-mode: standalone）→ 假定有刘海/挖孔
     · 横屏→ 假定左右有刘海
     · 都不满足 → 不动，交给壳自己处理

   为什么还要 CSS env() 那一份：iOS Safari、Chrome「添加到主屏幕」这些**确实会**
   传 env() 的场景，两条路都要在，所以 CSS 那份保留，本JS 只在 env() 为 0 时补位。
   ========================================================================== */
(function () {
  'use strict';

  const MIN_TOP = 24;      // 状态栏最小合理高度（CSS px）
  const MIN_BOTTOM = 8;

  let insetTop = 0, insetBottom = 0;
  let applied = false;

  function px(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // env() 实际生效时返回非 0，此时不该再补，否则双重留白
  function cssEnvTop() {
    // 造一个探针元素量env() 的真实值：CSS 里读不到时它就是 0
    let v = 0;
    try {
      const probe = document.createElement('div');
      probe.style.cssText =
        'position:fixed;top:0;left:0;height:env(safe-area-inset-top,0px);' +
        'width:0;pointer-events:none;visibility:hidden;';
      document.body.appendChild(probe);
      v = parseFloat(getComputedStyle(probe).height) || 0;
      probe.remove();
    } catch (e) { /* ignore */ }
    return v;
  }

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches
        || window.navigator.standalone === true;
  }

  function isFullscreen() {
    return window.matchMedia('(display-mode: fullscreen)').matches
        || window.matchMedia('(display-mode: minimal-ui)').matches;
  }

  function apply() {
    if (applied) return;
    applied = true;

    let top = insetTop;
    let bottom = insetBottom;

    if (!top) {
      // 壳没给值 → 启发式兜底，只在「独立窗口」这类确实是 App 的场景下补
      if (isStandalone() || isFullscreen()) {
        const portrait = window.innerHeight >= window.innerWidth;
        // 经验值：状态栏 ≈ 24~32px，刘海屏 ≈ 44~59px。
        // 取一个偏保守的下限，宁可稍多留白也不要压字。
        top = window.innerWidth >= 360 ? 32 : 28;
        // 横屏时刘海在左右，顶部不用让；这里不处理横向 inset，
        // 因为壳自己做左右 padding 更靠谱。
        if (!portrait) top = 0;
      }
    }
    if (!bottom) {
      if (isStandalone() || isFullscreen()) {
        // 手势导航条通常 8~24px，键盘弹出时不该顶，键盘收起后自然回落
        bottom = window.innerWidth >= 360 ? 16 : 12;
      }
    }

    top = Math.max(0, Math.round(top));
    bottom = Math.max(0, Math.round(bottom));

    if (!top && !bottom) return;

    const root = document.documentElement;
    root.style.setProperty('--tm-inset-top', top + 'px');
    root.style.setProperty('--tm-inset-bottom', bottom + 'px');
    root.classList.add('tm-native-inset');

    // 顶栏把状态栏那一片也涂上自己的底色，否则状态栏后面会漏出下层内容，
    // 视觉上像是「网页从状态栏底下钻出来」。
    const bar = document.querySelector('.topbar');
    if (bar && top) {
      // topbar 的 padding 已经含了 env()，这里只补壳给的差值
      const envTop = cssEnvTop();
      const extra = Math.max(0, top - envTop);
      if (extra > 0) {
        bar.style.paddingTop = `calc(10px + env(safe-area-inset-top, 0px) + ${extra}px)`;
        bar.style.background = getComputedStyle(bar).backgroundColor
          || getComputedStyle(document.body).backgroundColor || '#fff';
      }
    }
  }

  // 壳调用的入口
  window.__tmSetInsets = function (top, bottom) {
    const dpr = window.devicePixelRatio || 1;
    // 壳传的通常是 dp/px；给 dp 时按 dpr 放大到 CSS px。
    // 这里不盲目换算 —— 约定是壳直接传 **CSS px**，Android 侧已在
    // 上面那份注释里除过 density 了。此处只做负数保护。
    insetTop = Number.isFinite(+top) ? Math.max(0, +top) : 0;
    insetBottom = Number.isFinite(+bottom) ? Math.max(0, +bottom) : 0;
    void dpr;
    applied = false;
    apply();
  };

  // 壳也可以直接告诉网页「我是原生壳，别用启发式猜」
  window.__tmNativeShell = function (opts) {
    if (opts && opts.top != null) insetTop = +opts.top || 0;
    if (opts && opts.bottom != null) insetBottom = +opts.bottom || 0;
    applied = false;
    apply();
  };

  // 转屏 / 尺寸变化后重算（横竖屏切换时启发式结论会变）
  let rt = 0;
  const onResize = function () {
    clearTimeout(rt);
    rt = setTimeout(function () {
      // 壳给的 inset 优先保留；只有启发式那条路才重算
      if (!insetTop && !insetBottom) { applied = false; apply(); }
    }, 180);
  };
  window.addEventListener('resize', onResize);
  window.addEventListener('orientationchange', onResize);

  // DOM 就绪后应用一次；网页结构还没出来时退到 load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
  window.addEventListener('load', function () { applied = false; apply(); });
})();