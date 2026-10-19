/* =====================================================================
   前端共用运行时：API 封装、鉴权存储、界面小工具
   ---------------------------------------------------------------------
   三条硬性约定（前端最容易在这里出错）：

   1. **统一响应体**：后端所有 JSON 都是 `{code, message, data, trace_id}`。
      非 2xx 时也是这个结构（由全局异常处理器保证）。因此判断"成功"要看
      HTTP 状态码，而不是"有没有 data"——业务数据本身可能就是 null。
   2. **401 只有一个处理路径**：access token 过期 → 用 refresh token 换一次
      → 失败则清空并跳登录页。refresh 是**一次性**的（后端轮换），
      所以多个请求同时 401 时必须串行化，否则第二个请求会拿已被轮换作废的
      refresh token 去换，refreshing 一旦并发就会互相把对方挤下线。
   3. **错误提示必须带 trace_id**：用户报障时客服只看截图，没有 trace_id
      就等于无法在 ELK 里定位那一条请求。
   ===================================================================== */
(function (global) {
  'use strict';

  // 与后端 main.API_PREFIX 保持一致
  var API_BASE = '/api/v1';
  var TOKEN_KEY = 'rag.access_token';
  var REFRESH_KEY = 'rag.refresh_token';
  var USER_KEY = 'rag.user';
  var TENANT_KEY = 'rag.tenant_id';

  // ---------------- 存储 ----------------
  // 用 localStorage 而不是内存：刷新页面不该掉登录态。
  // 代价是 XSS 可读——本项目的页面全部自有、无用户可注入 HTML（见 escapeHtml 与
  // 消息渲染处一律 textContent），这是可接受的权衡；若有第三方脚本嵌入则须改 httpOnly cookie。
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} },

    token: function () { return store.get(TOKEN_KEY); },
    refreshToken: function () { return store.get(REFRESH_KEY); },
    tenant: function () { return store.get(TENANT_KEY) || ''; },

    saveLogin: function (payload) {
      store.set(TOKEN_KEY, payload.access_token);
      if (payload.refresh_token) store.set(REFRESH_KEY, payload.refresh_token);
      if (payload.user) {
        store.set(USER_KEY, JSON.stringify(payload.user));
        store.set(TENANT_KEY, payload.user.tenant_id || '');
      }
    },
    user: function () {
      try { return JSON.parse(store.get(USER_KEY) || 'null'); } catch (e) { return null; }
    },
    clear: function () {
      store.del(TOKEN_KEY);
      store.del(REFRESH_KEY);
      store.del(USER_KEY);
      // 刻意**不**清 TENANT_KEY：租户是登录前就要填的信息，
      // 清掉会让用户每次退出后都要重新输一遍公司标识。
    }
  };

  // ---------------- Toast ----------------
  function toastHost() {
    var host = document.getElementById('toastHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'toastHost';
      document.body.appendChild(host);
    }
    return host;
  }

  function toast(msg, type, ms) {
    var el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = msg;
    toastHost().appendChild(el);
    var ttl = ms || (type === 'err' ? 6000 : 3000);
    setTimeout(function () {
      el.classList.add('leaving');
      setTimeout(function () { el.remove(); }, 250);
    }, ttl);
    return el;
  }

  // ---------------- 错误类型 ----------------
  function ApiError(status, message, traceId, payload) {
    this.name = 'ApiError';
    this.status = status;
    this.message = message || ('请求失败（HTTP ' + status + '）');
    this.traceId = traceId || '';
    this.payload = payload;
  }
  ApiError.prototype = Object.create(Error.prototype);

  // ---------------- Token 刷新（串行化） ----------------
  var refreshing = null;   // Promise | null

  function doRefresh() {
    // 单飞：并发 401 全部挂到同一个 Promise 上。
    // 不做这一步的话，三个并发请求会同时去轮换 refresh token，
    // 后端"一次性使用"的语义会让后两个判定为已作废，直接把用户踢下线。
    if (refreshing) return refreshing;

    var rt = store.refreshToken();
    if (!rt) return Promise.reject(new ApiError(401, '登录已过期，请重新登录'));

    refreshing = fetch(API_BASE + '/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt })
    })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) throw new ApiError(res.status, body.message || '刷新登录失败', body.trace_id, body);
          return body.data;
        });
      })
      .then(function (data) {
        store.set(TOKEN_KEY, data.access_token);
        if (data.refresh_token) store.set(REFRESH_KEY, data.refresh_token);
        return data.access_token;
      })
      .catch(function (err) {
        store.clear();
        throw err;
      })
      .finally(function () { refreshing = null; });

    return refreshing;
  }

  // ---------------- 请求 ----------------
  function request(method, path, options) {
    options = options || {};
    var url = path.indexOf('http') === 0 ? path : (API_BASE + path);
    var headers = Object.assign({}, options.headers || {});
    var body = options.body;

    // FormData 不能手写 Content-Type：浏览器要自己加 boundary，
    // 覆盖掉会导致后端解析不出文件字段，表现为"上传成功但文件为空"。
    if (body !== undefined && body !== null && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      if (typeof body !== 'string') body = JSON.stringify(body);
    }

    var token = store.token();
    if (token) headers['Authorization'] = 'Bearer ' + token;

    var init = { method: method, headers: headers, body: body };
    if (options.signal) init.signal = options.signal;

    return fetch(url, init).then(function (res) {
      // 401 且不是登录/刷新本身 → 尝试续期一次后重放原请求
      var isAuthCall = path.indexOf('/auth/login') === 0 || path.indexOf('/auth/refresh') === 0;
      if (res.status === 401 && !isAuthCall && !options._retried) {
        return doRefresh().then(function () {
          options._retried = true;
          return request(method, path, options);
        }).catch(function () {
          if (options.onAuthFail) options.onAuthFail();
          else redirectToLogin();
          throw new ApiError(401, '登录已过期，请重新登录');
        });
      }

      var ct = res.headers.get('Content-Type') || '';
      var parse = ct.indexOf('application/json') >= 0 ? res.json() : res.text();

      return parse.then(function (payload) {
        if (!res.ok) {
          var msg = (payload && payload.message) || ('HTTP ' + res.status);
          var tid = (payload && payload.trace_id) || res.headers.get('X-Trace-Id') || '';
          if (res.status === 403 && options.onForbidden) options.onForbidden(msg);
          throw new ApiError(res.status, msg, tid, payload);
        }
        // 统一响应体：取 data；若后端某接口未包装（如 /health 白名单）则原样返回
        if (payload && typeof payload === 'object' && 'code' in payload && 'data' in payload) {
          return payload.data;
        }
        return payload;
      });
    });
  }

  function redirectToLogin() {
    // 已经不在登录页才跳，否则登录页自身的 401 会造成无限重定向
    if (location.pathname.indexOf('login') < 0) {
      var next = location.pathname + location.search;
      location.replace('/login?next=' + encodeURIComponent(next));
    }
  }

  var api = {
    get: function (p, o) { return request('GET', p, o); },
    post: function (p, b, o) { return request('POST', p, Object.assign({ body: b }, o || {})); },
    patch: function (p, b, o) { return request('PATCH', p, Object.assign({ body: b }, o || {})); },
    del: function (p, b, o) { return request('DELETE', p, Object.assign({ body: b }, o || {})); },
    base: API_BASE
  };

  // ---------------- 格式化 ----------------
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    // GB 档位是 P4 加的：异步上传上限 4096MB，不换算的话提示会写成
    // "4096.00 MB"，用户读不出这是 4GB，也就无法判断自己的文件是否超限。
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function fmtTime(raw) {
    if (!raw) return '-';
    var d = new Date(raw);
    if (isNaN(d.getTime())) return String(raw);
    var pad = function (x) { return x < 10 ? '0' + x : '' + x; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
           ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function fmtAgo(raw) {
    if (!raw) return '-';
    var d = new Date(raw);
    if (isNaN(d.getTime())) return String(raw);
    var sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 0) return '刚刚';
    if (sec < 60) return sec + ' 秒前';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
    if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
    if (sec < 86400 * 30) return Math.floor(sec / 86400) + ' 天前';
    return fmtTime(raw);
  }

  function fmtPct(v, digits) {
    return ((Number(v) || 0) * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  }

  // ---------------- 模态框 ----------------
  function confirmDialog(opts) {
    return new Promise(function (resolve) {
      var mask = document.createElement('div');
      mask.className = 'modal-mask';
      mask.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="cfmTitle">' +
          '<div class="modal-head" id="cfmTitle"></div>' +
          '<div class="modal-body"><p class="muted" style="font-size:13.5px"></p></div>' +
          '<div class="modal-foot">' +
            '<button class="btn btn-ghost" data-act="cancel">取消</button>' +
            '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" data-act="ok"></button>' +
          '</div>' +
        '</div>';
      mask.querySelector('.modal-head').textContent = opts.title || '确认操作';
      mask.querySelector('.modal-body p').textContent = opts.message || '';
      mask.querySelector('[data-act="ok"]').textContent = opts.okText || '确定';
      mask.querySelector('.btn-ghost').textContent = opts.cancelText || '取消';

      function close(v) { mask.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
      function onKey(e) { if (e.key === 'Escape') close(false); }

      mask.addEventListener('click', function (e) {
        if (e.target === mask) return close(false);
        var act = e.target.getAttribute && e.target.getAttribute('data-act');
        if (act === 'ok') close(true);
        else if (act === 'cancel') close(false);
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(mask);
      mask.querySelector('[data-act="ok"]').focus();
    });
  }

  // ---------------- 权限判断 ----------------
  function has(perm) {
    var u = store.user();
    if (!u) return false;
    var perms = u.permissions || [];
    return perms.indexOf('*') >= 0 || perms.indexOf(perm) >= 0;
  }

  function requireAuth() {
    if (!store.token()) {
      redirectToLogin();
      return false;
    }
    return true;
  }

  // ---------------- 其它 ----------------
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms || 300);
    };
  }

  function errText(err) {
    if (!err) return '未知错误';
    var s = err.message || String(err);
    if (err.traceId) s += '（trace_id: ' + err.traceId + '）';
    return s;
  }

  global.App = {
    api: api,
    store: store,
    toast: toast,
    ApiError: ApiError,
    escapeHtml: escapeHtml,
    fmtBytes: fmtBytes,
    fmtTime: fmtTime,
    fmtAgo: fmtAgo,
    fmtPct: fmtPct,
    confirm: confirmDialog,
    has: has,
    requireAuth: requireAuth,
    debounce: debounce,
    errText: errText,
    redirectToLogin: redirectToLogin
  };
})(window);
