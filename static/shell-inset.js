/* ============================================================================
   原生壳（Android WebView / 鸿蒙 ArkWeb）安全区适配
   ============================================================================
   与两端壳的分工（这套协议是 2026-10-16 定的，两端壳都按它实现）：

     网页侧声明 `<meta name="tm-safe-area" content="none">`
        → 网页自己会处理 inset，壳**不要**注入（避免双重留白）
     网页侧没有这个标记，或值不是 "none"
        → 网页自己处理不了，壳**注入** padding

   历史包袱：这三个页面原本写的是 `<meta name="tm-safe-area" content="1">`，
   两端壳看到 meta 存在就都跳过了。但网页「自己处理」靠的是纯 CSS
   `env(safe-area-inset-top)` —— 而它在原生 WebView 壳里**恒为 0**。
   于是谁都不补，状态栏直接压在顶栏上（用户反馈的「顶部重叠」）。

   所以这里改成 content="none"：明确告诉壳「网页处理不了，请你来补」。
   CSS 那边保持用 env()，因为 iOS Safari / Android Chrome 的 PWA 场景
   env() 是真能取到值的，两条路不能互相拆台。

   壳注入的是 !important 的 padding-top，会盖掉 style.css 里的 calc()；
   本文件的 env() 兜底在壳里等于 0，两者相加不会翻倍。
   ========================================================================== */
(function () {
  'use strict';

  // 探测 env() 到底给不给值：造个探针量高度，0 就是不支持。
  function cssEnvInsetTop() {
    try {
      const probe = document.createElement('div');
      probe.style.cssText =
        'position:fixed;top:0;left:0;width:0;visibility:hidden;pointer-events:none;' +
        'height:env(safe-area-inset-top,0px);';
      document.body.appendChild(probe);
      const v = parseFloat(getComputedStyle(probe).height) || 0;
      probe.remove();
      return v;
    } catch (e) {
      return 0;
    }
  }

  function apply() {
    // 浏览器原生支持 env()（iOS Safari / Chrome PWA）——什么都不用做，
    // style.css 里的 calc(10px + env(...)) 已经对了。
    if (cssEnvInsetTop() > 0) return;

    // 到这里说明 env() 给不了值：要么是原生壳，要么是老的 WebView。
    // 壳如果已经注入了 padding（它会写 #tm-safe-area 这个 <style>），就别再插手。
    if (document.getElementById('tm-safe-area')) return;
    // 两端壳在注入前都会先置 window.__tmSafe（幂等标记），并回读明暗。
    // 见到它说明壳接管了，网页侧一律不补。
    if (window.__tmSafe) return;

    // 谁都还没管：用一个保守的估计兜底，宁可稍多留白也不要压字。
    // 状态栏在竖屏手机上通常 24~32px；宽屏（≥360，主流手机）取 32。
    const portrait = window.innerHeight >= window.innerWidth;
    if (!portrait) return;               // 横屏刘海在侧边，顶部不该顶

    const guess = window.innerWidth >= 360 ? 32 : 26;
    const st = document.createElement('style');
    st.id = 'tm-safe-area-fallback';
    st.textContent =
      `header{padding-top:calc(10px + ${guess}px) !important}` +
      `.topbar{padding-top:calc(10px + ${guess}px) !important}`;
    document.head.appendChild(st);
  }

  // 壳的注入时机比 DOMContentLoaded 晚：Android 要等 WindowInsets 回调
  // （onPageFinished 时通常还没测出来），鸿蒙也要等 ArkWeb 控制器就绪。
  // 所以兜底先上、壳后到，必须留一个足够长的观察窗口，等壳补上就撤掉自己那份，
  // 否则就是双重留白 —— 用户在 Android 上看到的「两倍空白」正是这个成因。
  var probes = [0, 200, 600, 1200, 2500, 5000];

  function reconcile() {
    // 壳已接管（注入了自己的 <style>，或置了幂等标记）→ 撤掉兜底
    if (document.getElementById('tm-safe-area') || window.__tmSafe) {
      const fb = document.getElementById('tm-safe-area-fallback');
      if (fb) fb.remove();
      return true;
    }
    return false;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }

  window.addEventListener('load', function () {
    probes.forEach(function (ms) {
      setTimeout(function () {
        if (reconcile()) return;
        // 到 1.2s 还没等到壳，说明真的没有壳接管（比如老 WebView），
        // 这时兜底就正式生效，不再撤销。
        if (ms === 1200) apply();
      }, ms);
    });
  });
})();