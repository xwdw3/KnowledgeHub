/* =====================================================================
   聊天主界面
   ---------------------------------------------------------------------
   关键设计决策：

   1. **会话只存在 localStorage，不落后端**。
      后端 /sessions/{id}/history 是 Redis 里的 TTL 上下文缓存（用于多轮
      指代消解），不是"聊天记录数据库"。把它当记录列表来用会在 TTL 到期后
      凭空消失。所以侧栏的会话列表是纯前端的，消息正文也存本地。
      代价：换设备看不到历史。这是 P4 有意识的取舍，要跨设备需另建会话表。

   2. **流式是"假流式"（逐字打字机）**。
      后端 /chat 是一次性返回整段答案，没有 SSE。与其假装支持流式，
      不如明确：整段拿到后逐字显示，让等待期间有反馈。
      代码里保留了 stream 分支的位置，将来后端上 SSE 只需替换 displayAnswer。

   3. **请求可中止**。用 AbortController。用户连续追问时必须能停掉上一条，
      否则慢请求会一直占着，新消息插不进来，且后端 LLM 配额被浪费。
   ===================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  // ---------------- SSO 回跳：先换票，再做任何鉴权判断 ----------------
  // 后端 SSO 回调 302 到 /app?sso_ticket=xxx。票据是一次性的（60 秒、换过即废），
  // 所以必须在 requireAuth 之前消费掉——否则用户会被当成"未登录"踢回 /login，
  // 而在 /login 上刷新一下票据就过期了，表现为"点了 SSO 一直登不进去"。
  // 为了不引入异步启动流程，这里用**同步 XHR**：票换完才继续往下走，
  // 保证后续所有初始化逻辑读到的都是已登录状态。只此一处同步请求。
  var ssoTicket = (function () {
    var m = /[?&]sso_ticket=([^&]+)/.exec(location.search);
    return m ? decodeURIComponent(m[1]) : '';
  })();

  if (ssoTicket) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', App.api.base + '/auth/sso/ticket', false);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.send(JSON.stringify({ ticket: ssoTicket }));
      if (xhr.status >= 200 && xhr.status < 300) {
        var body = JSON.parse(xhr.responseText);
        App.store.saveLogin(body.data || body);
      }
    } catch (e) {
      // 换票失败就走正常流程：requireAuth 会把用户送到 /login 并显示错误
    }
    // 把票据从地址栏抹掉：它已经用过，留在历史记录里只会让人困惑
    try { history.replaceState(null, '', location.pathname); } catch (e2) { /* 忽略 */ }
  }

  if (!App.requireAuth()) return;

  // ---------------- 状态 ----------------
  var state = {
    sessionId: null,
    mode: 'rag',
    spaceId: '',
    spaces: [],
    pending: null,          // AbortController
    sessions: [],           // [{id, title, at, messages:[...]}]
    messages: []            // 当前会话的消息 [{role, content, meta}]
  };

  var LS_SESSIONS = 'rag.sessions.v1';
  var LS_MODE = 'rag.mode';
  var LS_SPACE = 'rag.space';

  // ---------------- DOM ----------------
  var chatScroll = $('chatScroll');
  var messagesEl = $('messages');
  var welcomeEl = $('welcome');
  var inputEl = $('questionInput');
  var sendBtn = $('btnSend');
  var stopBtn = $('btnStop');
  var charCount = $('charCount');
  var scopePill = $('scopePill');
  var spaceSelect = $('spaceSelect');
  var sidebar = $('sidebar');

  // ---------------- 初始化 ----------------
  var user = App.store.user() || {};
  var displayName = user.display_name || user.username || '用户';
  $('userName').textContent = displayName;
  $('avatarText').textContent = (displayName.charAt(0) || 'U').toUpperCase();
  $('ddName').textContent = displayName;
  $('ddMeta').textContent = (user.username || '') +
    (user.tenant_id ? ' · 租户 ' + user.tenant_id : '') +
    (user.role ? ' · ' + (user.role_label || user.role) : '');
  if (user.tenant_id) $('tenantChip').textContent = '租户 ' + user.tenant_id;

  state.mode = localStorage.getItem(LS_MODE) || 'rag';
  state.spaceId = localStorage.getItem(LS_SPACE) || '';
  setMode(state.mode, true);

  loadSessions();
  loadSpaces();
  loadStats();
  checkHealth();
  setInterval(checkHealth, 30000);

  // ---------------- 服务状态 ----------------
  function checkHealth() {
    var dot = $('healthDot');
    App.api.get('/health')
      .then(function (h) {
        dot.className = 'status-dot ' + (h.status === 'ok' ? 'ok' : 'warn');
        dot.querySelector('.status-text').textContent =
          h.status === 'ok' ? '服务正常' : '部分降级';
        dot.title = '模型：' + (h.model_mode || '-') +
                    '｜向量库：' + ((h.storage && h.storage.vector && h.storage.vector.backend) || '-') +
                    '｜业务库：' + ((h.storage && h.storage.meta && h.storage.meta.backend) || '-');
      })
      .catch(function () {
        dot.className = 'status-dot err';
        dot.querySelector('.status-text').textContent = '连接异常';
      });
  }

  // ---------------- 知识库统计 ----------------
  function loadStats() {
    App.api.get('/stats')
      .then(function (s) {
        $('statDocs').textContent = s.documents || s.doc_count || 0;
        $('statChunks').textContent = s.chunks || s.chunk_count || 0;
      })
      .catch(function () { /* 统计失败不打扰用户 */ });
  }

  // ---------------- 空间 ----------------
  function loadSpaces() {
    App.api.get('/spaces')
      .then(function (data) {
        state.spaces = (data && data.items) || [];
        renderSpaces();
      })
      .catch(function () { renderSpaces(); });
  }

  function renderSpaces() {
    var html = '<option value="">全部空间</option>';
    state.spaces.forEach(function (s) {
      html += '<option value="' + App.escapeHtml(s.space_id) + '"' +
              (s.space_id === state.spaceId ? ' selected' : '') + '>' +
              App.escapeHtml(s.name || s.space_id) +
              (s.doc_count != null ? '（' + s.doc_count + '）' : '') + '</option>';
    });
    spaceSelect.innerHTML = html;

    var cur = state.spaces.filter(function (s) { return s.space_id === state.spaceId; })[0];
    var label = cur ? (cur.name || cur.space_id) : '全部空间';
    scopePill.textContent = label;
    if (state.spaces.length === 0) {
      $('spaceHint').textContent = '暂无可用空间，文档将归入默认空间';
    } else if (state.spaces.length === 1) {
      $('spaceHint').textContent = '当前只有 1 个空间';
    } else {
      $('spaceHint').textContent = '可检索 ' + state.spaces.length + ' 个空间';
    }
  }

  spaceSelect.addEventListener('change', function () {
    state.spaceId = spaceSelect.value;
    localStorage.setItem(LS_SPACE, state.spaceId);
    renderSpaces();
  });

  // ---------------- 模式 ----------------
  function setMode(mode, silent) {
    state.mode = mode;
    localStorage.setItem(LS_MODE, mode);
    Array.prototype.forEach.call(document.querySelectorAll('.mode-opt'), function (b) {
      var on = b.getAttribute('data-mode') === mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', String(on));
    });
    if (!silent) App.toast(mode === 'agent' ? '已切换到智能体模式（可多步检索）' : '已切换到快速问答', '', 1800);
  }

  Array.prototype.forEach.call(document.querySelectorAll('.mode-opt'), function (b) {
    b.addEventListener('click', function () { setMode(b.getAttribute('data-mode')); });
  });

  // ---------------- 本地会话 ----------------
  function loadSessions() {
    try {
      state.sessions = JSON.parse(localStorage.getItem(LS_SESSIONS) || '[]');
    } catch (e) { state.sessions = []; }
    renderSessionList();

    // 打开最近一次会话：用户刷新页面不该回到空白欢迎屏
    if (state.sessions.length && !state.sessionId) {
      openSession(state.sessions[0].id);
    }
  }

  function persistSessions() {
    // 只保留最近 40 个，避免 localStorage 爆掉（5MB 上限，
    // 每条消息含引用片段正文，几十轮长对话就能吃掉几 MB）
    state.sessions = state.sessions.slice(0, 40);
    try {
      localStorage.setItem(LS_SESSIONS, JSON.stringify(state.sessions));
    } catch (e) {
      // 配额满了就丢掉最老的再试一次，仍失败则放弃（不影响本次对话）
      state.sessions = state.sessions.slice(0, 10);
      try { localStorage.setItem(LS_SESSIONS, JSON.stringify(state.sessions)); } catch (e2) {}
    }
  }

  function renderSessionList() {
    var host = $('sessionList');
    if (!state.sessions.length) {
      host.innerHTML = '<div class="side-hint" style="padding:8px 10px">还没有对话记录</div>';
      return;
    }
    host.innerHTML = state.sessions.map(function (s) {
      return '<button class="session-item' + (s.id === state.sessionId ? ' active' : '') +
             '" data-id="' + App.escapeHtml(s.id) + '" type="button">' +
             '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
             'stroke-width="2" stroke-linecap="round" style="flex-shrink:0;opacity:.6">' +
             '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>' +
             '<span class="si-title">' + App.escapeHtml(s.title) + '</span>' +
             '<span class="si-del" data-del="' + App.escapeHtml(s.id) + '" role="button" ' +
             'aria-label="删除对话">' +
             '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
             'stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>' +
             '</span></button>';
    }).join('');
  }

  $('sessionList').addEventListener('click', function (e) {
    var del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      var id = del.getAttribute('data-del');
      state.sessions = state.sessions.filter(function (s) { return s.id !== id; });
      persistSessions();
      if (state.sessionId === id) newChat();
      renderSessionList();
      return;
    }
    var item = e.target.closest('.session-item');
    if (item) openSession(item.getAttribute('data-id'));
  });

  function openSession(id) {
    var s = state.sessions.filter(function (x) { return x.id === id; })[0];
    if (!s) return;
    state.sessionId = s.id;
    state.messages = s.messages || [];
    renderMessages();
    renderSessionList();
  }

  function saveCurrentSession() {
    if (!state.sessionId || !state.messages.length) return;
    var first = state.messages.filter(function (m) { return m.role === 'user'; })[0];
    var title = first ? first.content.slice(0, 28) : '新对话';
    var rec = {
      id: state.sessionId,
      title: title + (first && first.content.length > 28 ? '…' : ''),
      at: Date.now(),
      messages: state.messages
    };
    var idx = -1;
    state.sessions.forEach(function (x, i) { if (x.id === rec.id) idx = i; });
    if (idx >= 0) state.sessions[idx] = rec;
    else state.sessions.unshift(rec);
    persistSessions();
    renderSessionList();
  }

  // ---------------- 新对话 ----------------
  function newChat() {
    abortPending();
    state.sessionId = null;
    state.messages = [];
    messagesEl.innerHTML = '';
    welcomeEl.classList.remove('hidden');
    renderSessionList();
    inputEl.value = '';
    autoGrow();
    updateSendState();
    inputEl.focus();
  }

  $('btnNewChat').addEventListener('click', newChat);

  $('btnClearSessions').addEventListener('click', function () {
    if (!state.sessions.length) return App.toast('没有需要清空的记录', '', 1600);
    App.confirm({
      title: '清空本地对话记录',
      message: '将删除本浏览器保存的全部对话（共 ' + state.sessions.length +
               ' 条）。此操作不影响服务端的知识库内容，且无法撤销。',
      okText: '清空', danger: true
    }).then(function (ok) {
      if (!ok) return;
      state.sessions = [];
      persistSessions();
      newChat();
      App.toast('已清空本地记录', 'ok');
    });
  });

  // ---------------- 渲染消息 ----------------
  function renderMessages() {
    if (!state.messages.length) {
      messagesEl.innerHTML = '';
      welcomeEl.classList.remove('hidden');
      return;
    }
    welcomeEl.classList.add('hidden');
    messagesEl.innerHTML = '';
    state.messages.forEach(function (m) { appendMessage(m, false); });
    scrollToBottom(true);
  }

  function appendMessage(m, animate) {
    var wrap = document.createElement('div');
    wrap.className = 'msg ' + (m.role === 'user' ? 'msg-user' : 'msg-bot');
    if (!animate) wrap.style.animation = 'none';

    if (m.role === 'user') {
      wrap.innerHTML = '<div class="msg-avatar">' +
        App.escapeHtml((displayName.charAt(0) || 'U').toUpperCase()) + '</div>' +
        '<div class="msg-body"><div class="bubble-user"></div></div>';
      wrap.querySelector('.bubble-user').textContent = m.content;
    } else {
      wrap.innerHTML =
        '<div class="msg-avatar" aria-hidden="true">' +
        '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="#fff" ' +
        'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M12 3l2.4 5.2 5.6.7-4.1 3.9 1.1 5.6L12 15.8 6.9 18.4l1.1-5.6L4 8.9l5.6-.7z"/></svg></div>' +
        '<div class="msg-body"><div class="answer"></div></div>';

      var answerEl = wrap.querySelector('.answer');
      if (m.rejected) {
        answerEl.innerHTML = '';
        var box = document.createElement('div');
        box.className = 'reject-box';
        box.innerHTML = '<span class="rb-icon">🔍</span><div>' +
          App.escapeHtml(m.content) + '</div>';
        answerEl.appendChild(box);
      } else {
        // Markdown 渲染器内部先转义再生成标签，不会把模型输出当 HTML 解析
        answerEl.innerHTML = MD.render(m.content);
      }

      if (m.meta) answerEl.appendChild(buildMeta(m.meta));
      if (m.sources && m.sources.length) answerEl.appendChild(buildSources(m.sources));
    }

    messagesEl.appendChild(wrap);
    if (animate) scrollToBottom();
    return wrap;
  }

  function buildMeta(meta) {
    var el = document.createElement('div');
    el.className = 'msg-meta';
    var html = '';

    if (meta.confidence != null) {
      var pct = Math.round((Number(meta.confidence) || 0) * 100);
      var cls = pct >= 60 ? 'badge-ok' : (pct >= 30 ? 'badge-warn' : 'badge-plain');
      html += '<span class="badge ' + cls + '" title="相关度越高，答案越可能来自知识库原文">' +
              '相关度 ' + pct + '%</span>';
    }
    if (meta.mode) {
      var modeText = meta.mode.indexOf('agent') === 0 ? '智能体' : '快速问答';
      html += '<span class="meta-chip">' + modeText + '</span>';
    }
    if (meta.elapsed_ms) {
      html += '<span class="meta-chip">' + (meta.elapsed_ms / 1000).toFixed(2) + ' 秒</span>';
    }
    if (meta.sources > 0) {
      html += '<span class="meta-chip">引用 ' + meta.sources + ' 段</span>';
    }
    html += '<button class="meta-chip btn-copy" type="button" style="border:none;' +
            'background:transparent;color:inherit;padding:0;cursor:pointer">复制</button>';
    el.innerHTML = html;

    var copyBtn = el.querySelector('.btn-copy');
    if (copyBtn) {
      copyBtn.addEventListener('click', function () {
        navigator.clipboard.writeText(meta._raw || '').then(function () {
          App.toast('已复制到剪贴板', 'ok', 1500);
        }).catch(function () { App.toast('复制失败，请手动选择文本', 'err'); });
      });
    }
    return el;
  }

  function buildSources(sources) {
    var wrap = document.createElement('div');
    wrap.className = 'sources';

    var head = document.createElement('div');
    head.className = 'sources-head';
    head.innerHTML = '<svg class="arrow" viewBox="0 0 24 24" width="13" height="13" fill="none" ' +
      'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M6 9l6 6 6-6"/></svg>' +
      '<span>引用来源 ' + sources.length + ' 段</span>' +
      '<span class="muted" style="font-weight:400;margin-left:auto;font-size:11.5px">点击展开原文</span>';
    head.addEventListener('click', function () { wrap.classList.toggle('collapsed'); });

    var body = document.createElement('div');
    body.className = 'sources-body';

    sources.forEach(function (s, i) {
      var item = document.createElement('div');
      item.className = 'src-item';

      var btn = document.createElement('div');
      btn.className = 'src-btn';
      btn.setAttribute('role', 'button');
      btn.setAttribute('tabindex', '0');

      var scores = [];
      if (s.rerank_score != null) scores.push('rerank ' + Number(s.rerank_score).toFixed(3));
      if (s.rrf_score != null) scores.push('rrf ' + Number(s.rrf_score).toFixed(3));
      if (s.vec_score) scores.push('向量 ' + Number(s.vec_score).toFixed(3));
      if (s.bm25_score) scores.push('关键词 ' + Number(s.bm25_score).toFixed(3));

      btn.innerHTML =
        '<span class="src-idx">' + (i + 1) + '</span>' +
        '<span class="src-main">' +
          '<span class="src-name">' + App.escapeHtml(s.file_name || '未命名') +
            '<span class="badge badge-plain" style="font-size:10.5px">第 ' + (Number(s.seq) + 1) + ' 段</span>' +
            (s.space_id && s.space_id !== 'default'
              ? '<span class="badge badge-brand" style="font-size:10.5px">' +
                App.escapeHtml(s.space_id) + '</span>' : '') +
          '</span>' +
          '<span class="src-text">' + App.escapeHtml(s.text || '') + '</span>' +
          (scores.length ? '<span class="src-scores">' +
            scores.map(function (x) { return '<span>' + App.escapeHtml(x) + '</span>'; }).join('') +
            '</span>' : '') +
        '</span>';

      function toggle() {
        item.classList.toggle('expanded');
        var t = item.querySelector('.src-text');
        t.style.webkitLineClamp = item.classList.contains('expanded') ? 'unset' : '2';
      }
      btn.addEventListener('click', toggle);
      btn.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });

      item.appendChild(btn);
      body.appendChild(item);
    });

    wrap.appendChild(head);
    wrap.appendChild(body);
    // 超过 3 段默认折叠：答案才是主体，引用是补充
    if (sources.length > 3) wrap.classList.add('collapsed');
    return wrap;
  }

  function scrollToBottom(instant) {
    if (instant) chatScroll.scrollTop = chatScroll.scrollHeight;
    else chatScroll.scrollTo({ top: chatScroll.scrollHeight, behavior: 'smooth' });
  }

  // ---------------- 发送 ----------------
  function updateSendState() {
    var v = inputEl.value.trim();
    sendBtn.disabled = !v || !!state.pending;
    var len = inputEl.value.length;
    charCount.textContent = len > 800 ? len + ' / 1000' : '';
    charCount.classList.toggle('warn', len > 900);
  }

  function autoGrow() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
  }

  inputEl.addEventListener('input', function () { autoGrow(); updateSendState(); });

  inputEl.addEventListener('keydown', function (e) {
    // Enter 发送 / Shift+Enter 换行。中文输入法组合期间必须放行，
    // 否则拼音输入时按回车选词会把半截拼音发出去（最常见的输入 bug）。
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  sendBtn.addEventListener('click', send);

  function abortPending() {
    if (state.pending) {
      state.pending.abort();
      state.pending = null;
      stopBtn.hidden = true;
      sendBtn.disabled = false;
    }
  }

  stopBtn.addEventListener('click', function () {
    abortPending();
    App.toast('已停止', '', 1400);
  });

  function send() {
    var q = inputEl.value.trim();
    if (!q || state.pending) return;

    if (!state.sessionId) state.sessionId = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

    // 用户消息上屏
    var um = { role: 'user', content: q };
    state.messages.push(um);
    appendMessage(um, true);
    inputEl.value = '';
    autoGrow();
    updateSendState();
    welcomeEl.classList.add('hidden');

    // 加载态
    var loading = appendThinking();

    var controller = new AbortController();
    state.pending = controller;
    stopBtn.hidden = false;
    sendBtn.disabled = true;
    var t0 = Date.now();

    var payload = { question: q, session_id: state.sessionId, mode: state.mode };
    if (state.spaceId) payload.space_id = state.spaceId;

    App.api.post('/chat', payload, { signal: controller.signal })
      .then(function (data) {
        loading.remove();
        state.pending = null;
        stopBtn.hidden = true;

        var botMsg = {
          role: 'assistant',
          content: data.answer || '',
          rejected: !!data.rejected,
          sources: data.sources || [],
          meta: {
            confidence: data.confidence,
            mode: data.mode,
            elapsed_ms: Date.now() - t0,
            sources: (data.sources || []).length,
            _raw: data.answer || ''
          }
        };
        if (data.session_id) state.sessionId = data.session_id;
        state.messages.push(botMsg);

        // 逐字显示让等待有反馈；长答案用更快的节奏，否则用户等得不耐烦
        typeOut(botMsg);
        saveCurrentSession();
      })
      .catch(function (err) {
        loading.remove();
        state.pending = null;
        stopBtn.hidden = true;
        updateSendState();

        if (err && err.name === 'AbortError') return;

        var msg = App.errText(err);
        if (err && err.status === 429) msg = '提问太频繁了，请稍等几秒再试。';
        else if (err && err.status === 403) msg = '您没有在该空间提问的权限。';
        else if (err && err.status === 0) msg = '网络连接失败，请检查后端服务是否正常。';

        var em = { role: 'assistant', content: '⚠️ ' + msg, rejected: true };
        state.messages.push(em);
        appendMessage(em, true);
      });
  }

  function appendThinking() {
    var wrap = document.createElement('div');
    wrap.className = 'msg msg-bot';
    wrap.innerHTML =
      '<div class="msg-avatar" aria-hidden="true">' +
      '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="#fff" ' +
      'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M12 3l2.4 5.2 5.6.7-4.1 3.9 1.1 5.6L12 15.8 6.9 18.4l1.1-5.6L4 8.9l5.6-.7z"/></svg></div>' +
      '<div class="msg-body"><div class="thinking">' +
      '<span>' + (state.mode === 'agent' ? '正在调用工具检索…' : '正在检索知识库…') + '</span>' +
      '<span class="dots"><i></i><i></i><i></i></span></div></div>';
    messagesEl.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  // 逐字显示。按答案长度动态调整每帧字数：
  // 200 字的答案逐字显示约 3 秒，2000 字的答案逐字要 30 秒——不可接受。
  function typeOut(msg) {
    var wrap = document.createElement('div');
    wrap.className = 'msg msg-bot';
    wrap.innerHTML =
      '<div class="msg-avatar" aria-hidden="true">' +
      '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="#fff" ' +
      'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M12 3l2.4 5.2 5.6.7-4.1 3.9 1.1 5.6L12 15.8 6.9 18.4l1.1-5.6L4 8.9l5.6-.7z"/></svg></div>' +
      '<div class="msg-body"><div class="answer caret"></div></div>';
    messagesEl.appendChild(wrap);

    var answerEl = wrap.querySelector('.answer');
    var full = msg.content || '';

    if (msg.rejected) {
      answerEl.classList.remove('caret');
      var box = document.createElement('div');
      box.className = 'reject-box';
      box.innerHTML = '<span class="rb-icon">🔍</span><div></div>';
      box.querySelector('div').textContent = full;
      answerEl.appendChild(box);
      finish();
      return;
    }

    // 一次性渲染完整 Markdown，再用 CSS 控制可见部分？
    // 不行——截断的 Markdown 会产生残缺标签。
    // 改为：分片重新渲染"已显示的前缀"（前缀总是合法 Markdown 的概率高，
    // 残缺标签由浏览器自动修复），最后一帧渲染完整内容保证正确。
    var total = full.length;
    var step = Math.max(6, Math.ceil(total / 60));
    var shown = 0;
    var timer = null;

    function tick() {
      shown = Math.min(total, shown + step);
      if (shown < total) {
        answerEl.innerHTML = MD.render(full.slice(0, shown));
      } else {
        clearInterval(timer);
        answerEl.innerHTML = MD.render(full);
        finish();
        return;
      }
      // 用户上滚查看历史时不要强制拉回底部
      var nearBottom = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 180;
      if (nearBottom) scrollToBottom();
    }

    timer = setInterval(tick, 28);

    function finish() {
      answerEl.classList.remove('caret');
      // 引用与元信息在打字结束后一次性补上，避免逐帧重建导致闪烁
      if (msg.meta) answerEl.appendChild(buildMeta(msg.meta));
      if (msg.sources && msg.sources.length) answerEl.appendChild(buildSources(msg.sources));
      scrollToBottom();
    }
  }

  // ---------------- 建议问题 ----------------
  $('suggestGrid').addEventListener('click', function (e) {
    var card = e.target.closest('.suggest-card');
    if (!card) return;
    inputEl.value = card.getAttribute('data-q');
    autoGrow();
    updateSendState();
    inputEl.focus();
    send();
  });

  // ---------------- 顶栏交互 ----------------
  var userMenu = $('userMenu');
  $('avatarBtn').addEventListener('click', function (e) {
    e.stopPropagation();
    var open = !$('userDropdown').classList.contains('hidden');
    $('userDropdown').classList.toggle('hidden', open);
    $('avatarBtn').setAttribute('aria-expanded', String(!open));
  });
  document.addEventListener('click', function () {
    $('userDropdown').classList.add('hidden');
    $('avatarBtn').setAttribute('aria-expanded', 'false');
  });
  $('userDropdown').addEventListener('click', function (e) { e.stopPropagation(); });

  $('btnLogout').addEventListener('click', function () {
    App.confirm({
      title: '退出登录',
      message: '退出后需要重新输入账号密码。本地保存的对话记录仍会保留。',
      okText: '退出'
    }).then(function (ok) {
      if (!ok) return;
      // 先请求后端注销（把 token 拉黑），失败也继续本地清理——
      // 本地清不干净等于没退出，比后端黑名单更关键。
      App.api.post('/auth/logout', { refresh_token: App.store.refreshToken() })
        .catch(function () {})
        .then(function () {
          App.store.clear();
          location.replace('/login');
        });
    });
  });

  $('btnApiDocs').addEventListener('click', function () {
    window.open('/docs', '_blank', 'noopener');
  });

  $('btnShortcuts').addEventListener('click', function () {
    $('shortcutMask').classList.remove('hidden');
  });
  $('scClose').addEventListener('click', function () {
    $('shortcutMask').classList.add('hidden');
  });
  $('shortcutMask').addEventListener('click', function (e) {
    if (e.target === $('shortcutMask')) $('shortcutMask').classList.add('hidden');
  });

  // ---------------- 侧栏收起 ----------------
  $('sidebarToggle').addEventListener('click', function () {
    sidebar.classList.toggle('collapsed');
    $('sidebarToggle').setAttribute('aria-expanded',
      String(!sidebar.classList.contains('collapsed')));
  });

  // ---------------- 全局快捷键 ----------------
  document.addEventListener('keydown', function (e) {
    var mod = e.ctrlKey || e.metaKey;

    if (mod && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      newChat();
      return;
    }
    if (mod && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      sidebar.classList.toggle('collapsed');
      return;
    }
    if (e.key === 'Escape') {
      $('shortcutMask').classList.add('hidden');
      $('userDropdown').classList.add('hidden');
      if (state.pending) abortPending();
      return;
    }
    // "/" 聚焦输入框：只在不在输入状态下生效，
    // 否则用户想在输入框里打 "/" 会被夺走焦点。
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA') {
      e.preventDefault();
      inputEl.focus();
    }
  });

  // 页面卸载时中止未完成请求，避免浏览器报 "request aborted" 噪音
  window.addEventListener('beforeunload', function () {
    if (state.pending) state.pending.abort();
  });

  updateSendState();
})();
