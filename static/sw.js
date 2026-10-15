/* 旅行记忆 —— Service Worker
   ==========================================================================
   它存在的唯一理由是**可安装性**：Chrome / Edge 把「注册了带 fetch 监听的
   Service Worker」当成 PWA 可安装的条件之一（iOS 的「添加到主屏幕」不看这条，
   它只认 manifest 和 apple-touch-icon）。

   它**故意不缓存任何东西**，这是刻意的设计：
     - 本站静态文件已经用 `?v=<mtime+size>` 做了指纹（见 routers/deps.py），
       浏览器缓存 + 版本号已经足够，再加一层 SW 只会让「改了看不到」更难查；
     - 业务数据（/api、/uploads、高德瓦片、OSS 直链）一旦进 SW 缓存，就会出现
       「照片删了还在」「行程改了不刷新」这类经典事故；
     - 原图/原片还会被 transcode 原地重封装（见 transcode.ensure_faststart），
       SW 缓存住旧字节基本等于制造一个说不清的播放 bug。

   所以 fetch 监听只做「筛掉不该管的」，其余一律不调用 respondWith ——
   交回浏览器默认行为：零缓存、零滞后、零解释成本。

   以后真要做离线壳（比如把 /static 下的外壳缓存起来做秒开），在这里加缓存层，
   并且只缓存带 ?v= 的那几个文件。 */
'use strict';

const SCOPE_VERSION = 'v1';

self.addEventListener('install', () => {
  // 新版本立刻进入 waiting，不必等旧页面全部关掉（本 SW 不持有状态，安全）
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;              // 写请求（上传/表单）不碰

  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;      // 高德 SDK / OSS / CDN 不碰
  if (url.pathname === '/mcp' || url.pathname.startsWith('/mcp/')) return;
  if (url.pathname.startsWith('/api/')) return;         // 业务接口不碰
  if (url.pathname.startsWith('/uploads/')) return;     // 媒体不碰

  // 其余（HTML / JS / CSS / 图标）也不缓存：交给浏览器。
  // 注意这里刻意没有 event.respondWith(...) —— 监听器存在即满足可安装性，
  // 但请求仍然完全走网络，SW 不参与加载。
  void SCOPE_VERSION;
});
