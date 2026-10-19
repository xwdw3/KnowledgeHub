/* =====================================================================
   登录页逻辑
   ---------------------------------------------------------------------
   要点：
   1. 先查 /auth/methods 决定 SSO 按钮是否显示，再让用户输密码。
      反过来做（先渲染表单再隐藏）会造成可见的布局跳动。
   2. 登录失败的错误提示必须是"用户能行动的信息"：
      「账号或密码错误」而不是「HTTP 401」。
      后端在账号不存在与密码错误时返回同一句话（防账号枚举），
      前端不要自作聪明区分。
   3. 回车提交、密码框回车提交、Tab 顺序，都要能正常工作——
      登录页是用户每天要过一遍的地方，任何小摩擦都会被放大。
   ===================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var form = $('loginForm');
  var tenantEl = $('tenant');
  var userEl = $('username');
  var pwdEl = $('password');
  var submitBtn = $('submitBtn');
  var formError = $('formError');
  var ssoBtn = $('ssoBtn');
  var pwdToggle = $('pwdToggle');

  // 已登录直接进主界面，不要让用户再登一次
  // （但带 sso_ticket 时要先让它被消费掉——那种情况 ticket 优先级更高）
  if (App.store.token() && !query('sso_ticket')) {
    location.replace(readNext() || '/app');
    return;
  }

  // ---------------- 回填 ----------------
  // 租户与用户名记住、密码绝不记。密码存 localStorage 是最常见的低级错误，
  // 一旦有 XSS 就等于明文泄露。
  tenantEl.value = App.store.tenant() || '';
  var remembered = App.store.get('rag.remember_user');
  if (remembered) {
    userEl.value = remembered;
    $('remember').checked = true;
  }

  function readNext() {
    var m = /[?&]next=([^&]+)/.exec(location.search);
    if (!m) return '';
    var v = decodeURIComponent(m[1]);
    // 只接受站内相对路径。允许外部 URL 就是开放重定向漏洞：
    // 攻击者可构造 /login?next=https://evil.com 把刚登录的用户送去钓鱼页。
    if (v.charAt(0) !== '/' || v.charAt(1) === '/') return '';
    return v;
  }

  function query(name) {
    var m = new RegExp('[?&]' + name + '=([^&]*)').exec(location.search);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // ---------------- SSO 回跳处理 ----------------
  // 后端回调完成后会 302 回 /app?sso_ticket=xxx 或 /login?sso_error=xxx。
  // /app 页也有同样的处理逻辑（见 app.js），这里负责"错误"和"用户本来就在登录页"的情况。
  var SSO_ERRORS = {
    denied: '您已在企业身份平台取消授权',
    state: '登录会话已失效，请重新点击单点登录',
    exchange: '单点登录校验失败，请重试或使用账号密码登录',
    unavailable: '单点登录服务暂不可用，请稍后再试',
    no_account: '您的企业账号尚未开通本系统访问权限，请联系管理员',
    disabled: '您的账号已被停用，请联系管理员',
    tenant: '所属租户不存在或已停用，请联系管理员',
    config: '单点登录配置不完整，请联系管理员'
  };

  var ssoError = query('sso_error');
  if (ssoError) {
    showError(SSO_ERRORS[ssoError] || '单点登录失败，请使用账号密码登录');
    // 把错误码从地址栏抹掉：用户刷新时不应该再看到一次同样的报错
    try {
      history.replaceState(null, '', location.pathname + (readNext() ? '?next=' + encodeURIComponent(readNext()) : ''));
    } catch (e) { /* 老浏览器忽略 */ }
  }

  // ---------------- 认证方式探测 ----------------
  App.api.get('/auth/methods')
    .then(function (m) {
      if (m && m.oidc_enabled) {
        ssoBtn.classList.remove('hidden');
        $('ssoDivider').classList.remove('hidden');
        $('ssoText').textContent = m.oidc_label || '使用企业账号登录（SSO）';
      }
      if (m && m.env) {
        $('envLine').textContent = m.env + (m.version ? ' · v' + m.version : '');
      }
      if (m && m.jwt_enabled === false) {
        showError('本实例未启用账号密码登录，请使用 API Key 或联系管理员开通。');
        submitBtn.disabled = true;
      }
    })
    .catch(function () {
      // 探测失败不阻断登录：可能是瞬时的网络抖动，
      // 而 /auth/methods 只是"锦上添花"，真正鉴权在 /auth/login。
    });

  // ---------------- 密码可见性 ----------------
  pwdToggle.addEventListener('click', function () {
    var showing = pwdEl.type === 'text';
    pwdEl.type = showing ? 'password' : 'text';
    pwdToggle.classList.toggle('on', !showing);
    pwdToggle.setAttribute('aria-pressed', String(!showing));
    pwdToggle.setAttribute('aria-label', showing ? '显示密码' : '隐藏密码');
    pwdEl.focus();
  });

  // ---------------- 错误提示 ----------------
  function showError(msg) {
    formError.textContent = msg;
    formError.classList.remove('hidden');
    // 重播抖动动画：连续两次错误时用户要能感知到"又错了"，
    // 否则相同的红块没有视觉反馈，会以为按钮没响应。
    formError.style.animation = 'none';
    void formError.offsetWidth;
    formError.style.animation = '';
  }

  function clearError() { formError.classList.add('hidden'); }

  [tenantEl, userEl, pwdEl].forEach(function (el) {
    el.addEventListener('input', clearError);
  });

  // ---------------- 提交 ----------------
  function setLoading(on) {
    submitBtn.disabled = on;
    submitBtn.querySelector('.btn-text').textContent = on ? '登录中…' : '登 录';
    submitBtn.querySelector('.spinner').classList.toggle('hidden', !on);
    [tenantEl, userEl, pwdEl].forEach(function (el) { el.disabled = on; });
  }

  function submit() {
    var username = userEl.value.trim();
    var password = pwdEl.value;
    var tenant = tenantEl.value.trim();

    clearError();
    if (!username) { userEl.focus(); return showError('请输入用户名'); }
    if (!password) { pwdEl.focus(); return showError('请输入密码'); }

    setLoading(true);
    App.api.post('/auth/login', {
      username: username,
      password: password,
      tenant_id: tenant
    })
      .then(function (data) {
        App.store.saveLogin(data);
        // 记住勾选状态
        if ($('remember').checked) App.store.set('rag.remember_user', username);
        else App.store.del('rag.remember_user');

        App.toast('登录成功，欢迎回来 ' + (data.user && data.user.display_name || username), 'ok', 1600);
        // 稍等一下让 toast 可见，直接跳会让成功反馈被"闪一下"吞掉
        setTimeout(function () { location.replace(readNext() || '/app'); }, 320);
      })
      .catch(function (err) {
        setLoading(false);
        var msg = err.message || '登录失败';
        if (err.status === 401) {
          msg = '账号或密码不正确' + (tenant ? '（租户：' + tenant + '）' : '');
        } else if (err.status === 429) {
          msg = '尝试过于频繁，请稍后再试';
        } else if (err.status === 403) {
          msg = msg || '该账号已被禁用，请联系管理员';
        }
        showError(msg);
        pwdEl.select();
      });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    submit();
  });

  // ---------------- SSO ----------------
  ssoBtn.addEventListener('click', function () {
    var next = readNext() || '/app';
    var tenant = tenantEl.value.trim();
    var url = App.api.base + '/auth/sso/login?next=' + encodeURIComponent(next);
    // 租户一起带上：IdP 支持 login_hint 时用户不用再选一次组织；
    // 不支持也没关系，回调后仍以 IdP 下发的 claims 为准。
    if (tenant) url += '&tenant=' + encodeURIComponent(tenant);
    location.href = url;
  });

  // SSO 回跳到登录页时通常带 sso_ticket（后端把用户送到了 /login 而非 /app）。
  // 这时必须立刻换票，否则刷新一下票据就废了。
  var ssoTicket = query('sso_ticket');
  if (ssoTicket) {
    submitBtn.disabled = true;
    submitBtn.querySelector('.btn-text').textContent = '正在完成单点登录…';
    App.api.post('/auth/sso/ticket', { ticket: ssoTicket })
      .then(function (data) {
        App.store.saveLogin(data);
        App.toast('单点登录成功，欢迎回来 ' +
          (data.user && data.user.display_name || ''), 'ok', 1600);
        setTimeout(function () { location.replace(readNext() || '/app'); }, 320);
      })
      .catch(function (err) {
        submitBtn.disabled = false;
        submitBtn.querySelector('.btn-text').textContent = '登 录';
        showError(err.message || '单点登录失败，请使用账号密码登录');
      });
  }

  // ---------------- 占位功能 ----------------
  $('forgotLink').addEventListener('click', function (e) {
    e.preventDefault();
    showError('本系统为内网私有化部署，密码重置请联系本单位系统管理员。');
  });
  $('policyLink').addEventListener('click', function (e) {
    e.preventDefault();
    App.toast('请向管理员索取《内部系统使用规范》全文', '', 2600);
  });
})();
