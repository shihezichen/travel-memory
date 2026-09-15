/**
 * 工程结构的一致性检查。
 *
 * 小程序在这台机器上没法用开发者工具打开，所以「页面漏了个文件」「app.json
 * 里写了个不存在的路径」这类错误没有任何东西会提醒 —— 只会在别人导入项目
 * 时炸出来。这些恰好是**静态就能查**的，拿测试钉住。
 *
 * 它不保证小程序能跑（见 README 开头），只保证不会因为文件缺失、
 * 引用错路径这种低级问题在导入的第一秒就失败。
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const appJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'app.json'), 'utf8'));

test('app.json 里的每个页面，四件套都要齐（js/json/wxml/wxss）', () => {
  for (const page of appJson.pages) {
    for (const ext of ['js', 'json', 'wxml', 'wxss']) {
      const f = path.join(ROOT, page + '.' + ext);
      assert.ok(fs.existsSync(f), '缺文件：' + page + '.' + ext);
    }
  }
});

test('pages/ 下的页面都登记进了 app.json，没有孤儿页面', () => {
  const declared = new Set(appJson.pages);
  const dirs = fs.readdirSync(path.join(ROOT, 'pages'));
  for (const d of dirs) {
    const entry = path.join(ROOT, 'pages', d, d + '.js');
    if (!fs.existsSync(entry)) continue;
    assert.ok(declared.has('pages/' + d + '/' + d),
      'pages/' + d + ' 没写进 app.json，它永远不会被打包');
  }
});

test('首页是行程列表 —— 小程序的第一个 page 就是启动页', () => {
  assert.strictEqual(appJson.pages[0], 'pages/trips/trips');
});

test('代码里引用的本地资源都真实存在', () => {
  const files = [];
  (function walk(dir) {
    for (const n of fs.readdirSync(dir)) {
      if (n === 'node_modules') continue;
      const p = path.join(dir, n);
      if (fs.statSync(p).isDirectory()) walk(p);
      else if (/\.(js|wxml|wxss)$/.test(n)) files.push(p);
    }
  })(ROOT);

  const missing = [];
  for (const f of files) {
    const src = fs.readFileSync(f, 'utf8');
    // 形如 '/assets/pin.png' 的站内绝对路径
    for (const m of src.matchAll(/['"](\/assets\/[^'"]+)['"]/g)) {
      if (!fs.existsSync(path.join(ROOT, m[1]))) {
        missing.push(path.relative(ROOT, f) + ' -> ' + m[1]);
      }
    }
  }
  assert.deepStrictEqual(missing, []);
});

test('页面里的 require 路径都解析得到', () => {
  const missing = [];
  for (const page of appJson.pages) {
    const f = path.join(ROOT, page + '.js');
    const src = fs.readFileSync(f, 'utf8');
    for (const m of src.matchAll(/require\(['"](\.[^'"]+)['"]\)/g)) {
      const target = path.resolve(path.dirname(f), m[1]);
      const ok = fs.existsSync(target) || fs.existsSync(target + '.js');
      if (!ok) missing.push(page + ' -> ' + m[1]);
    }
  }
  assert.deepStrictEqual(missing, []);
});

test('config 里的域名都是 https —— 微信只认 https，http 真机直接失败', () => {
  const config = require('../config');
  assert.match(config.BASE_URL, /^https:\/\//);
  assert.match(config.MEDIA_HOST, /^https:\/\//);
  assert.ok(!config.BASE_URL.endsWith('/'), 'BASE_URL 末尾不要带斜杠，拼出来会变双斜杠');
});

test('wxml 里用到的 data 字段，页面 js 里有定义或会 setData', () => {
  // 只做粗筛：抓 {{xxx}} 里的**顶层**标识符，看 js 源码里出没出现过。
  // 目的是抓「data 字段改了名、wxml 那边漏改」，不是做真正的类型检查。
  //
  // 关键是要先把属性访问剥掉：{{item.description}} 里只有 item 是顶层字段，
  // description 是对象上的属性，js 源码里当然搜不到（它从接口来）。
  // 不剥的话这条测试全是误报。
  const skip = new Set(['true', 'false', 'null', 'undefined', 'index', 'item']);
  const problems = [];
  for (const page of appJson.pages) {
    const wxml = fs.readFileSync(path.join(ROOT, page + '.wxml'), 'utf8');
    const js = fs.readFileSync(path.join(ROOT, page + '.js'), 'utf8');
    const names = new Set();
    for (const m of wxml.matchAll(/\{\{([^}]+)\}\}/g)) {
      const expr = m[1]
        .replace(/'[^']*'|"[^"]*"/g, ' ')     // 字符串字面量里的词不算引用
        .replace(/\.[A-Za-z_$][A-Za-z0-9_$]*/g, '');   // 剥掉 .prop
      for (const id of expr.matchAll(/[A-Za-z_$][A-Za-z0-9_$]*/g)) names.add(id[0]);
    }
    for (const n of names) {
      if (skip.has(n)) continue;
      if (!js.includes(n)) problems.push(page + ' 的 {{' + n + '}} 在 js 里找不到');
    }
  }
  assert.deepStrictEqual(problems, []);
});
