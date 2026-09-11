/* 高德地图 JS API 的离线替身。
 *
 * 为什么需要：index.html / share.html 用一个阻塞 <script> 从 webapi.amap.com 拉
 * AMap，而 app.js 顶层直接 `initMap()`（`new AMap.Map(...)`）。测试环境无外网时
 * 这句会抛异常，之后的 loadTrips() 等业务逻辑一行都跑不到 —— 整页等于死掉。
 *
 * 于是在 Playwright 里把那个 <script> 请求拦下来，改喂这份桩：它不画地图，
 * 但保证所有 AMap.* 调用都不抛异常，从而让「地图之外」的业务链路（登录、行程
 * 列表、点列表、灯箱、分享页）在离线环境下可测。
 *
 * 兜底策略：所有对象都套一层 Proxy，未显式实现的方法一律返回一个 no-op 函数，
 * 这样 app.js 以后新用了哪个 AMap 方法也不会把测试打挂。
 */
(function () {
  'use strict';

  function noop() { return undefined; }

  /* 把一个普通对象包成「未知属性 = no-op 函数」的宽容对象 */
  function tolerant(obj) {
    return new Proxy(obj, {
      get: function (t, k) {
        if (k in t) return t[k];
        if (typeof k === 'symbol') return undefined;
        return noop;
      },
      has: function () { return true; },
    });
  }

  /* 简易事件源，map.on('moveend', fn) 之类要能存下来供测试主动触发 */
  function withEvents(o) {
    o.__ev = {};
    o.on = function (t, fn) { (o.__ev[t] = o.__ev[t] || []).push(fn); return o; };
    o.off = function (t, fn) {
      if (o.__ev[t]) o.__ev[t] = o.__ev[t].filter(function (f) { return f !== fn; });
      return o;
    };
    o.emit = function (t, e) {
      (o.__ev[t] || []).forEach(function (f) { try { f(e || {}); } catch (_) {} });
      return o;
    };
    return o;
  }

  function LngLat(lng, lat) {
    if (!(this instanceof LngLat)) return new LngLat(lng, lat);
    this.lng = Number(lng); this.lat = Number(lat);
    this.KL = this.lng; this.kT = this.lat;
  }
  LngLat.prototype.getLng = function () { return this.lng; };
  LngLat.prototype.getLat = function () { return this.lat; };
  LngLat.prototype.toString = function () { return this.lng + ',' + this.lat; };
  LngLat.prototype.distance = function (o) {
    if (!o) return 0;
    var b = Array.isArray(o) ? { lng: o[0], lat: o[1] } : o;
    var dx = (this.lng - b.lng) * 85000, dy = (this.lat - b.lat) * 111000;
    return Math.sqrt(dx * dx + dy * dy);
  };

  function Pixel(x, y) { this.x = x; this.y = y; }
  Pixel.prototype.getX = function () { return this.x; };
  Pixel.prototype.getY = function () { return this.y; };

  /* ---- 覆盖物基类（Marker / Polyline / CircleMarker / Polygon / Circle / Text）---- */
  function Overlay(opts) {
    var self = withEvents({
      __stub: 'overlay',
      __opts: opts || {},
      __map: null,
      __visible: true,
      setMap: function (m) { self.__map = m; if (m && m.__add) m.__add(self); return self; },
      getMap: function () { return self.__map; },
      setPosition: function (p) { self.__opts.position = p; return self; },
      getPosition: function () {
        var p = self.__opts.position;
        if (!p) return new LngLat(0, 0);
        return Array.isArray(p) ? new LngLat(p[0], p[1]) : p;
      },
      setContent: function (c) { self.__opts.content = c; return self; },
      getContent: function () { return self.__opts.content; },
      setPath: function (p) { self.__opts.path = p; return self; },
      getPath: function () { return self.__opts.path || []; },
      setOptions: function (o) { Object.assign(self.__opts, o || {}); return self; },
      getOptions: function () { return self.__opts; },
      setExtData: function (d) { self.__opts.extData = d; return self; },
      getExtData: function () { return self.__opts.extData; },
      setzIndex: function (z) { self.__opts.zIndex = z; return self; },
      show: function () { self.__visible = true; return self; },
      hide: function () { self.__visible = false; return self; },
      getBounds: function () { return null; },
      dom: null,
    });
    return tolerant(self);
  }
  function overlayCtor() {
    return function (opts) { return Overlay(opts); };
  }

  /* ---- 地图 ---- */
  function AMapMap(container, opts) {
    opts = opts || {};
    var el = typeof container === 'string'
      ? document.getElementById(container) : container;
    var overlays = [];
    var zoom = opts.zoom == null ? 10 : opts.zoom;
    var center = opts.center || [104.0, 35.0];

    /* 真高德会在容器里建一棵图层树，其中 .amap-layers 是我们自己插「照片堆 /
       📷 角标层」和「自绘路线层」的宿主（见 common.js 的 ensurePhotoStackLayer）。
       桩里必须有它，否则那两层永远建不出来，相关交互在 E2E 里是彻底的盲区。
       pointer-events:none —— 桩不该抢走地图按钮的点击，角标自己是 auto。 */
    if (el && !el.querySelector('.amap-layers')) {
      var layerHost = document.createElement('div');
      layerHost.className = 'amap-layers';
      layerHost.style.cssText =
        'position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:1';
      el.insertBefore(layerHost, el.firstChild);
    }

    var self = withEvents({
      __stub: 'map',
      __el: el,
      __overlays: overlays,
      __add: function (o) { if (overlays.indexOf(o) < 0) overlays.push(o); },
      getContainer: function () { return el; },
      getZoom: function () { return zoom; },
      setZoom: function (z) { zoom = z; self.emit('zoomend', {}); return self; },
      getCenter: function () {
        return Array.isArray(center) ? new LngLat(center[0], center[1]) : center;
      },
      setCenter: function (c) { center = c; self.emit('moveend', {}); return self; },
      setZoomAndCenter: function (z, c) { zoom = z; center = c; return self; },
      panTo: function (c) { center = c; self.emit('moveend', {}); return self; },
      setFitView: noop,
      /* 真高德一定返回可视范围的包围盒，桩以前直接给 null —— 于是任何
         「按视野取数」的逻辑在 E2E 里都走的是降级分支，等于没测。
         按容器尺寸和缩放级别算一个近似的墨卡托视野出来。 */
      getBounds: function () {
        var w = (el && el.clientWidth) || 1024;
        var h = (el && el.clientHeight) || 768;
        var worldPx = 256 * Math.pow(2, zoom);
        var halfLng = 180 * (w / worldPx);
        var halfLat = 180 * (h / worldPx);
        var cLng = Array.isArray(center) ? center[0] : center.lng;
        var cLat = Array.isArray(center) ? center[1] : center.lat;
        var sw = new LngLat(Math.max(cLng - halfLng, -180),
                            Math.max(cLat - halfLat, -90));
        var ne = new LngLat(Math.min(cLng + halfLng, 180),
                            Math.min(cLat + halfLat, 90));
        return {
          getSouthWest: function () { return sw; },
          getNorthEast: function () { return ne; },
          contains: function (p) {
            return p.lng >= sw.lng && p.lng <= ne.lng &&
                   p.lat >= sw.lat && p.lat <= ne.lat;
          },
        };
      },
      resize: noop,
      clearMap: function () { overlays.length = 0; return self; },
      add: function (o) {
        (Array.isArray(o) ? o : [o]).forEach(self.__add);
        return self;
      },
      remove: function (o) {
        (Array.isArray(o) ? o : [o]).forEach(function (x) {
          var i = overlays.indexOf(x); if (i >= 0) overlays.splice(i, 1);
        });
        return self;
      },
      getAllOverlays: function () { return overlays.slice(); },
      /* 经纬度 → 容器像素：线性映射就够了，测试只关心「有没有坐标、变不变」 */
      lngLatToContainer: function (ll) {
        var lng = ll && (ll.lng != null ? ll.lng : ll[0]) || 0;
        var lat = ll && (ll.lat != null ? ll.lat : ll[1]) || 0;
        var w = (el && el.clientWidth) || 800, h = (el && el.clientHeight) || 600;
        return new Pixel(w / 2 + (lng - 104) * 4, h / 2 - (lat - 35) * 4);
      },
      containerToLngLat: function (px) { return new LngLat(104, 35); },
      setMapStyle: noop,
      setLayers: noop,
      addLayer: noop,
      removeLayer: noop,
    });
    var proxied = tolerant(self);
    window.__amapStub = window.__amapStub || {};
    window.__amapStub.maps = (window.__amapStub.maps || []).concat([proxied]);
    return proxied;
  }

  /* ---- 插件 ---- */
  function Autocomplete() {
    return tolerant({
      search: function (kw, cb) { if (cb) cb('complete', { tips: [] }); },
    });
  }
  function Geocoder() {
    return tolerant({
      getAddress: function (ll, cb) { if (cb) cb('error', null); },
      getLocation: function (addr, cb) { if (cb) cb('error', null); },
    });
  }
  function PlaceSearch() {
    return tolerant({
      search: function (kw, cb) { if (cb) cb('error', null, null); },
    });
  }
  function Driving() {
    return tolerant({
      search: function () {
        var cb = arguments[arguments.length - 1];
        if (typeof cb === 'function') cb('error', null);
      },
      clear: noop, setPolicy: noop,
    });
  }
  function Geolocation() {
    return tolerant({
      getCurrentPosition: function (cb) { if (cb) cb('error', null); },
    });
  }
  function TileLayer(o) { return Overlay(o); }
  TileLayer.Satellite = function (o) { return Overlay(o); };
  TileLayer.RoadNet = function (o) { return Overlay(o); };

  var AMap = {
    version: '2.0-stub',
    v: '2.0-stub',
    Map: AMapMap,
    LngLat: LngLat,
    Pixel: Pixel,
    Size: function (w, h) { this.width = w; this.height = h; },
    Bounds: function () { return tolerant({}); },
    Icon: function (o) { return tolerant({ __opts: o || {} }); },
    Marker: overlayCtor(),
    Text: overlayCtor(),
    Polyline: overlayCtor(),
    Polygon: overlayCtor(),
    Circle: overlayCtor(),
    CircleMarker: overlayCtor(),
    InfoWindow: overlayCtor(),
    Heatmap: function () { return tolerant({ setDataSet: noop, show: noop, hide: noop }); },
    TileLayer: TileLayer,
    Autocomplete: Autocomplete,
    Geocoder: Geocoder,
    PlaceSearch: PlaceSearch,
    Driving: Driving,
    Geolocation: Geolocation,
    DrivingPolicy: { LEAST_TIME: 0, LEAST_FEE: 1, LEAST_DISTANCE: 2, REAL_TRAFFIC: 4 },
    GeometryUtil: tolerant({
      distance: function (a, b) { return new LngLat(a[0], a[1]).distance({ lng: b[0], lat: b[1] }); },
      distanceOfLine: function () { return 0; },
    }),
    plugin: function (names, cb) { if (typeof cb === 'function') cb(); },
    convertFrom: function (lnglat, type, cb) { if (cb) cb('complete', { locations: [] }); },
  };

  window.AMap = tolerant(AMap);
  window.__AMAP_STUB_LOADED__ = true;
})();
