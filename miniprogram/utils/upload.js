/**
 * 上传一个本地文件到某个点。
 *
 * 两条路，优先走第一条：
 *
 *   1. **直传 OSS**（`/api/points/{id}/oss-uploads`）—— 分片，逐片 PUT 到后端
 *      签好名的临时 URL，文件字节根本不经过我们的服务器。网页端 2026-09-11 就
 *      改成这样了，小程序一直落在后面。
 *   2. **服务器中转**（`/api/points/{id}/media`）—— `wx.uploadFile` 单请求。
 *      直传没开（后端回 503）或者建会话失败时兜底。
 *
 * 为什么非改不可：`wx.uploadFile` 是**单请求、不可分片、不可续传**的。一个
 * 两百兆的视频在弱网下传到 90% 断一次，就得从头再来，而且整条流量还挤在
 * 那条 4Mbps 的隧道里。分片之后断的只是一片，重试也只重试那一片。
 *
 * 断点续传不需要客户端记什么：后端按 (点, 属主, 文件指纹) 找回上次的
 * uploadId，`received` 里会告诉我们哪些片已经在云上了，跳过就是。
 */
const config = require('../config');
const api = require('./api');

// 同时在飞的分片数。压到 2 不是因为并发上限（小程序默认能开 10 条），是因为
// **内存**：一片默认 8MB，读进来是完整的 ArrayBuffer，开几路就占几倍。
// 手机上行本来也跑不满更多路，2 路已经能把带宽吃住。
const CONCURRENCY = 2;
// 一次向后端要多少张分片 URL。和后端 OSS_URL_BATCH 对齐，要多了它也只给这么多。
const URL_BATCH = 64;
// 单片最多重试几次。403 单独算（签名过期，换张 URL 再来，不占重试次数）。
const MAX_RETRY = 2;

/**
 * 文件指纹。后端只拿它当「同一个文件」的标识来找回上次的会话，不校验内容，
 * 所以不需要真去算哈希 —— 小程序里读整个文件算 MD5 既慢又占内存。
 *
 * 用 (大小, 临时路径) 组合：同一次选择产生的 tempFilePath 在这次上传的生命周期
 * 里是稳定的，足够支撑「传到一半失败，用户再点一次继续」这个场景。
 */
function fingerprint(filePath, filesize) {
  const name = String(filePath || '').split('/').pop() || 'unknown';
  return filesize + '-' + name;
}

/** tempFilePath 里的文件名。后端靠扩展名判断照片还是视频，不能丢。 */
function basename(filePath) {
  return String(filePath || '').split('/').pop() || 'unknown';
}

/** 第 n 片（从 1 数）在文件里的位置和长度。最后一片通常不满。 */
function partRange(n, partSize, filesize) {
  const position = (n - 1) * partSize;
  return { position, length: Math.min(partSize, filesize - position) };
}

/** 还没传上去的片号。received 是后端 ListParts 的结果。 */
function missingParts(total, received) {
  const got = {};
  (received || []).forEach((p) => { got[p.number] = true; });
  const out = [];
  for (let n = 1; n <= total; n++) if (!got[n]) out.push(n);
  return out;
}

/** 读文件的一段，拿到 ArrayBuffer。position/length 要基础库 ≥ 2.10.0。 */
function readChunk(filePath, position, length) {
  return new Promise((resolve, reject) => {
    wx.getFileSystemManager().readFile({
      filePath, position, length,
      success: (res) => resolve(res.data),
      fail: (e) => reject(new Error(e.errMsg || '读取文件失败')),
    });
  });
}

/**
 * PUT 一片到 OSS。
 *
 * Content-Type 必须和后端签名时用的**一字不差**（见 oss.sign_part_url 里的
 * `content_type="application/octet-stream"`），差一个字符 OSS 就判签名不符回 403。
 *
 * 刻意不读 ETag：后端 complete 时现查 ListParts，客户端报的一概不信 ——
 * 于是也就不需要桶去 ExposeHeader。
 */
function putPart(url, buf) {
  return new Promise((resolve) => {
    wx.request({
      url,
      method: 'PUT',
      data: buf,
      header: { 'content-type': 'application/octet-stream' },
      timeout: 120000,
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) return resolve({ ok: true });
        // 403 基本都是签名过期（分片 URL 只活 1 小时），换一张就好，不算失败
        resolve({ ok: false, expired: res.statusCode === 403, status: res.statusCode });
      },
      fail: (e) => resolve({ ok: false, status: 0, msg: e.errMsg || '网络中断' }),
    });
  });
}

/** 再要一批分片 URL（第一批用完了，或者旧的过期了）。 */
function signMore(sessionId, numbers) {
  return api.post('/api/oss-uploads/' + sessionId + '/sign', { parts: numbers.join(',') })
    .then((r) => (r && r.urls) || {});
}

/**
 * 走直传。成功返回后端 complete 的结果；直传不可用时返回 `{ fallback: true }`，
 * 由 uploadMedia 回落到服务器中转。
 *
 * onProgress(0..100) 可选。
 */
function uploadViaOss(pointId, filePath, filesize, onProgress) {
  const fp = fingerprint(filePath, filesize);
  return api.post('/api/points/' + pointId + '/oss-uploads', {
    fingerprint: fp, filename: basename(filePath), filesize,
  }).then(
    (sess) => runOssSession(sess, filePath, filesize, onProgress),
    (err) => {
      // 503 = 后端没开直传；其它错也一样回落，别让用户因为直传的问题传不上去。
      console.warn('[upload] 直传建会话失败，回落到服务器中转：' + err.message);
      return { fallback: true };
    }
  );
}

function runOssSession(sess, filePath, filesize, onProgress) {
  const total = sess.total_parts;
  const partSize = sess.part_size;
  const urls = Object.assign({}, sess.urls || {});
  const todo = missingParts(total, sess.received);
  // 续传时已经在云上的片也算进进度，否则进度条会从 0 开始往上爬，像是白传了
  const resumedBytes = (total - todo.length) * partSize;
  let uploaded = resumedBytes;
  const report = () => {
    if (onProgress) onProgress(Math.min(99, Math.floor(uploaded * 100 / filesize)));
  };
  report();

  let cursor = 0;
  let failure = null;

  function next() {
    if (failure || cursor >= todo.length) return Promise.resolve();
    const i = cursor++;
    return onePart(todo[i], i).then(next);
  }

  /**
   * i 是这一片在 todo 里的下标。补签 URL 时要从它自己开始切一批 ——
   * 不能用共享的 cursor 去切：几路并发跑着，cursor 早跑到别处了，
   * 切出来的窗口可能根本不含 n，于是这一片永远等不到地址。
   */
  function onePart(n, i, attempt) {
    attempt = attempt || 0;
    const ensureUrl = urls[n]
      ? Promise.resolve(urls[n])
      : signMore(sess.session_id, todo.slice(i, i + URL_BATCH))
          .then((got) => { Object.assign(urls, got); return urls[n]; });

    return ensureUrl.then((url) => {
      if (!url) throw new Error('拿不到第 ' + n + ' 片的上传地址');
      const { position, length } = partRange(n, partSize, filesize);
      return readChunk(filePath, position, length)
        .then((buf) => putPart(url, buf).then((r) => ({ r, length })));
    }).then(({ r, length }) => {
      if (r.ok) { uploaded += length; report(); return; }
      if (r.expired) {
        // 签名过期不占重试次数：换一张新 URL，这一片重来
        delete urls[n];
        if (attempt >= MAX_RETRY + 2) throw new Error('第 ' + n + ' 片的地址一直签不对');
        return onePart(n, i, attempt + 1);
      }
      if (attempt < MAX_RETRY) return onePart(n, i, attempt + 1);
      throw new Error(r.msg || ('第 ' + n + ' 片上传失败（HTTP ' + r.status + '）'));
    }).catch((e) => { failure = failure || e; });
  }

  const lanes = [];
  for (let i = 0; i < Math.min(CONCURRENCY, todo.length || 1); i++) lanes.push(next());

  return Promise.all(lanes).then(() => {
    // 一个字节都没出去就失败 = 这条路根本不通（最常见的是 OSS 域名没登记进
    // request 合法域名）。这种情况回落到服务器中转，让用户先能传上去；
    // 传到一半才断的不回落 —— 那多半是网络问题，重来一次比换条更慢的路好。
    if (failure && uploaded === resumedBytes) {
      console.warn('[upload] 直传一片都没成功，本次回落到服务器中转：' + failure.message);
      return { fallback: true };
    }
    if (failure) throw failure;
    if (onProgress) onProgress(99);
    // complete 是幂等的（后端按 session 记了 media_id），超时重试一次是安全的
    return api.post('/api/oss-uploads/' + sess.session_id + '/complete', {}, { timeout: 60000 });
  }).then((m) => {
    if (m && m.fallback) return m;
    if (onProgress) onProgress(100);
    return m;
  });
}

/** 服务器中转：单请求 multipart，直传不可用时的兜底。 */
function uploadViaServer(pointId, filePath) {
  return api.uploadMedia(pointId, filePath);
}

/**
 * 对外只有这一个：先试直传，不行就中转。
 *
 * filesize 拿不到（老基础库的 chooseMedia 不给 size）时直接走中转 ——
 * 建直传会话必须先报大小，猜一个只会让后端按错的片数签 URL。
 */
function uploadMedia(pointId, filePath, filesize, onProgress) {
  if (!filesize || filesize <= 0) return uploadViaServer(pointId, filePath);
  return uploadViaOss(pointId, filePath, filesize, onProgress)
    .then((r) => (r && r.fallback ? uploadViaServer(pointId, filePath) : r));
}

module.exports = {
  uploadMedia,
  // 导出给单元测试的纯函数
  fingerprint, basename, partRange, missingParts,
};
