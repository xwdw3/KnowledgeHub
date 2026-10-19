/* =====================================================================
   管理后台
   ---------------------------------------------------------------------
   核心约束：**按权限隐藏入口，但不假装接口会成功**。
     前端隐藏按钮只是体验优化，真正的边界在 API（每个接口都校验权限点）。
     这里对每个视图按权限做可用性判断，并在视图内给出"权限不足"的说明，
     而不是让用户点进去看到一堆 403。

   轮询策略：
     任务队列是唯一需要高频刷新的视图（用户要盯上传进度）。
     只用 3 秒轮询、且**离开视图即停**，否则用户开着后台一整天
     会白消耗几千次请求，日志和指标全被这些轮询淹没。
   ===================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  if (!App.requireAuth()) return;

  var user = App.store.user() || {};
  var displayName = user.display_name || user.username || '用户';

  // ---------------- 权限 ----------------
  var PERM = {
    read: 'kb:read',
    write: 'kb:write',
    kbAdmin: 'kb:admin',
    spaceAdmin: 'space:admin',
    auditRead: 'audit:read',
    userAdmin: 'user:admin',
    metricRead: 'metric:read'
  };

  // ---------------- 状态 ----------------
  var state = {
    view: 'docs',
    docs: [],
    spaces: [],
    tasks: [],
    sessions: {},
    pollTimer: null,
    pendingFiles: []
  };

  // ---------------- 初始化 ----------------
  $('userName').textContent = displayName;
  $('avatarText').textContent = (displayName.charAt(0) || 'U').toUpperCase();
  $('ddName').textContent = displayName;
  $('ddMeta').textContent = (user.username || '') +
    (user.tenant_id ? ' · 租户 ' + user.tenant_id : '') +
    (user.role ? ' · ' + (user.role_label || user.role) : '');
  if (user.tenant_id) $('tenantChip').textContent = '租户 ' + user.tenant_id;

  // 无权限的分组直接隐藏。用 display:none 而不是 disabled：
  // 灰着的按钮会让人反复去点，期望它是"待解锁"而不是"永不可用"。
  if (!App.has(PERM.metricRead) && !App.has(PERM.auditRead)) $('groupOps').classList.add('hidden');
  if (!App.has(PERM.userAdmin)) $('groupAdmin').classList.add('hidden');
  if (!App.has(PERM.metricRead)) {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-view="alerts"],[data-view="metrics"]'),
      function (el) { el.classList.add('hidden'); });
  }
  if (!App.has(PERM.auditRead)) {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-view="audit"]'), function (el) { el.classList.add('hidden'); });
  }
  if (!App.has(PERM.write)) $('btnUpload').classList.add('hidden');
  if (!App.has(PERM.spaceAdmin)) $('btnNewSpace').classList.add('hidden');

  checkHealth();
  setInterval(checkHealth, 30000);

  // ---------------- 导航 ----------------
  $('adminNav').addEventListener('click', function (e) {
    var item = e.target.closest('.nav-item');
    if (!item || item.classList.contains('hidden')) return;
    switchView(item.getAttribute('data-view'));
  });

  function switchView(name) {
    state.view = name;
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-view') === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.view'), function (v) {
      v.classList.toggle('hidden', v.id !== 'view-' + name);
    });
    // 离开任务视图必须停轮询
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }

    var loaders = {
      docs: loadDocs,
      spaces: loadSpaces,
      tasks: loadTasks,
      alerts: loadAlerts,
      audit: loadAudit,
      metrics: loadMetrics,
      users: loadUsers
    };
    if (loaders[name]) loaders[name]();
    if (name === 'tasks') {
      state.pollTimer = setInterval(function () {
        if (state.view === 'tasks') loadTasks(true);
      }, 3000);
    }
  }

  // ---------------- 健康检查 ----------------
  function checkHealth() {
    var dot = $('healthDot');
    App.api.get('/health')
      .then(function (h) {
        dot.className = 'status-dot ' + (h.status === 'ok' ? 'ok' : 'warn');
        dot.querySelector('.status-text').textContent = h.status === 'ok' ? '服务正常' : '部分降级';
        dot.title = '模型：' + (h.model_mode || '-');
        state.health = h;
      })
      .catch(function () {
        dot.className = 'status-dot err';
        dot.querySelector('.status-text').textContent = '连接异常';
      });
  }

  // ==================================================================
  // 文档管理
  // ==================================================================
  function loadDocs() {
    var tb = $('docTbody');
    tb.innerHTML = '<tr><td colspan="8"><div class="empty"><div class="skeleton" ' +
                   'style="height:16px;width:60%;margin:0 auto"></div></div></td></tr>';

    Promise.all([App.api.get('/documents'), App.api.get('/spaces').catch(function () { return { items: [] }; })])
      .then(function (res) {
        state.docs = (res[0] && res[0].items) || [];
        state.spaces = (res[1] && res[1].items) || [];
        renderDocSpaceFilter();
        renderDocs();
      })
      .catch(function (err) {
        tb.innerHTML = '<tr><td colspan="8"><div class="empty">加载失败：' +
                       App.escapeHtml(App.errText(err)) + '</div></td></tr>';
      });
  }

  function renderDocSpaceFilter() {
    var sel = $('docSpaceFilter');
    var cur = sel.value;
    var html = '<option value="">全部空间</option>';
    state.spaces.forEach(function (s) {
      html += '<option value="' + App.escapeHtml(s.space_id) + '">' +
              App.escapeHtml(s.name || s.space_id) + '</option>';
    });
    sel.innerHTML = html;
    sel.value = cur;
  }

  function fileBadge(ext) {
    var e = String(ext || '').toLowerCase().replace(/^\./, '');
    var cls = { pdf: 'ft-pdf', docx: 'ft-docx', doc: 'ft-docx', txt: 'ft-txt', md: 'ft-md' }[e] || 'ft-other';
    var label = { pdf: 'PDF', docx: 'DOC', doc: 'DOC', txt: 'TXT', md: 'MD' }[e] || (e.slice(0, 3) || '?');
    return '<span class="ft-badge ' + cls + '">' + App.escapeHtml(label) + '</span>';
  }

  function renderDocs() {
    var tb = $('docTbody');
    var q = ($('docSearch').value || '').trim().toLowerCase();
    var sp = $('docSpaceFilter').value;

    var list = state.docs.filter(function (d) {
      if (q && (d.file_name || '').toLowerCase().indexOf(q) < 0) return false;
      if (sp && d.space_id !== sp) return false;
      return true;
    });

    $('docCount').textContent = '共 ' + list.length + ' 个文档' +
      (list.length !== state.docs.length ? '（已筛选，全部 ' + state.docs.length + '）' : '');

    if (!list.length) {
      tb.innerHTML = '<tr><td colspan="8">' +
        '<div class="empty"><div class="empty-icon">📄</div>' +
        '<div class="empty-title">' + (state.docs.length ? '没有匹配的文档' : '还没有文档') + '</div>' +
        '<div class="empty-desc">' +
        (state.docs.length ? '换个关键词或清除筛选试试' : '点击右上角「上传文档」开始构建知识库') +
        '</div></div></td></tr>';
      return;
    }

    tb.innerHTML = list.map(function (d) {
      var ext = d.file_ext || (d.file_name || '').split('.').pop();
      return '<tr>' +
        '<td><div class="file-cell">' + fileBadge(ext) +
          '<div style="min-width:0">' +
            '<div class="file-name truncate">' + App.escapeHtml(d.file_name) + '</div>' +
            '<div class="file-meta mono">' + App.escapeHtml(d.doc_id) + '</div>' +
          '</div></div></td>' +
        '<td><span class="badge badge-plain">' + App.escapeHtml(d.space_id || 'default') + '</span></td>' +
        '<td><span class="badge ' + (d.perm_group === 'all' ? 'badge-plain' : 'badge-warn') + '">' +
          App.escapeHtml(d.perm_group || 'all') + '</span></td>' +
        '<td class="mono">' + (d.chunk_count || 0) + '</td>' +
        '<td class="mono">' + App.fmtBytes(d.file_size) + '</td>' +
        '<td>' + App.escapeHtml(d.uploader || '-') + '</td>' +
        '<td class="muted" style="font-size:12.5px">' + App.fmtTime(d.updated_at || d.created_at) + '</td>' +
        '<td><div class="row" style="gap:6px">' +
          '<button class="btn btn-ghost btn-sm" data-dl="' + App.escapeHtml(d.doc_id) + '"' +
            (d.object_key ? '' : ' disabled title="该文档以无原件模式入库"') + '>原件</button>' +
          (App.has(PERM.write)
            ? '<button class="btn btn-danger btn-sm" data-del="' + App.escapeHtml(d.doc_id) +
              '" data-name="' + App.escapeHtml(d.file_name) + '">删除</button>'
            : '') +
        '</div></td>' +
      '</tr>';
    }).join('');
  }

  $('docSearch').addEventListener('input', App.debounce(renderDocs, 200));
  $('docSpaceFilter').addEventListener('change', renderDocs);
  $('btnRefreshDocs').addEventListener('click', loadDocs);

  $('docTbody').addEventListener('click', function (e) {
    var dl = e.target.closest('[data-dl]');
    if (dl) return downloadDoc(dl.getAttribute('data-dl'));

    var del = e.target.closest('[data-del]');
    if (!del) return;
    var name = del.getAttribute('data-name');
    App.confirm({
      title: '删除文档',
      message: '将删除「' + name + '」及其全部向量片段，检索将立即不再命中。此操作不可撤销。',
      okText: '删除', danger: true
    }).then(function (ok) {
      if (!ok) return;
      App.api.del('/documents', { file_name: name })
        .then(function (r) {
          App.toast('已删除 ' + name + '（移除 ' + (r.removed_chunks || 0) + ' 个片段）', 'ok');
          loadDocs();
        })
        .catch(function (err) { App.toast('删除失败：' + App.errText(err), 'err'); });
    });
  });

  function downloadDoc(docId) {
    App.api.get('/documents/' + encodeURIComponent(docId) + '/download-url')
      .then(function (r) {
        if (!r || !r.url) throw new Error('未取得下载链接');
        window.open(r.url, '_blank', 'noopener');
        if (r.expires_in) App.toast('下载链接 ' + Math.round(r.expires_in / 60) + ' 分钟内有效', '', 2200);
      })
      .catch(function (err) { App.toast('获取下载链接失败：' + App.errText(err), 'err'); });
  }

  // ---------------- 上传 ----------------
  var dropzone = $('dropzone');
  var fileInput = $('fileInput');

  $('btnUpload').addEventListener('click', openUpload);
  function openUpload() {
    state.pendingFiles = [];
    renderPendingFiles();
    // 回填空间下拉
    var html = state.spaces.map(function (s) {
      return '<option value="' + App.escapeHtml(s.space_id) + '">' +
             App.escapeHtml(s.name || s.space_id) + '</option>';
    }).join('');
    $('upSpace').innerHTML = html || '<option value="default">default</option>';
    syncLimitHint();
    $('uploadMask').classList.remove('hidden');
  }

  $('upCancel').addEventListener('click', function () {
    $('uploadMask').classList.add('hidden');
  });

  dropzone.addEventListener('click', function () { fileInput.click(); });
  dropzone.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  });

  ['dragenter', 'dragover'].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.add('drag');
    });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.remove('drag');
    });
  });
  dropzone.addEventListener('drop', function (e) {
    addFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener('change', function () {
    addFiles(fileInput.files);
    fileInput.value = '';           // 允许重复选同一个文件
  });

  // 两个上限必须分开，与后端 settings.max_upload_mb / max_async_upload_mb 对齐：
  //   同步路径要把全文读进内存且必须在一个请求内跑完，所以只到 20MB；
  //   异步路径走流式解析，内存与文件大小无关，可以到 4GB。
  // 早期版本这里写死 20MB 且不看勾选状态，等于前端把异步能力整个禁掉了——
  // 后端能收 4GB，用户却连一个 30MB 的 PDF 都传不上去。
  var MAX_SYNC = 20 * 1024 * 1024;
  var MAX_ASYNC = 4096 * 1024 * 1024;
  var ALLOWED = ['pdf', 'docx', 'txt', 'md', 'markdown'];

  function sizeLimit() {
    return $('upAsync').checked ? MAX_ASYNC : MAX_SYNC;
  }

  // 上限提示随勾选状态变化：用户不用去猜"我勾了异步到底能传多大"。
  function syncLimitHint() {
    var isAsync = $('upAsync').checked;
    var hint = $('dzHint');
    if (hint) {
      hint.textContent = '支持 PDF / DOCX / TXT / MD，' +
        (isAsync ? '异步模式单文件最大 ' + App.fmtBytes(MAX_ASYNC)
                 : '同步模式单文件最大 ' + App.fmtBytes(MAX_SYNC) + '（大文件请勾选异步）');
    }
    var tip = $('upLimitHint');
    if (tip) {
      tip.textContent = isAsync
        ? '当前走异步通道，单文件上限 ' + App.fmtBytes(MAX_ASYNC) + '，内存占用与文件大小无关'
        : '当前走同步通道，单文件上限 ' + App.fmtBytes(MAX_SYNC) +
          '；请求内跑完整条入库链路，不适合大文件';
    }
  }

  function addFiles(fileList) {
    var limit = sizeLimit();
    Array.prototype.forEach.call(fileList, function (f) {
      var ext = (f.name.split('.').pop() || '').toLowerCase();
      if (ALLOWED.indexOf(ext) < 0) {
        App.toast('不支持的文件类型：' + f.name + '（仅支持 ' + ALLOWED.join('/') + '）', 'err');
        return;
      }
      if (f.size > limit) {
        // 提示里直接给出解法，而不是只说"超限"：大文件的正确定律是勾异步，
        // 而不是让用户自己去压缩文件。
        App.toast(f.name + ' 超过 ' + App.fmtBytes(limit) + ' 限制' +
                  ($('upAsync').checked ? '' : '，大文件请勾选「异步处理」'), 'err', 6000);
        return;
      }
      if (state.pendingFiles.some(function (x) { return x.name === f.name; })) return;
      state.pendingFiles.push(f);
    });
    renderPendingFiles();
  }

  // 勾选/取消异步时重新校验已选文件：用户往往是先选文件再想到勾异步，
  // 若不在这一刻放开限制，他就得删掉重选一遍。
  $('upAsync').addEventListener('change', function () {
    var limit = sizeLimit();
    var dropped = state.pendingFiles.filter(function (f) { return f.size > limit; });
    state.pendingFiles = state.pendingFiles.filter(function (f) { return f.size <= limit; });
    if (dropped.length) {
      App.toast(dropped.length + ' 个文件超过' + App.fmtBytes(limit) +
                '，已移除（勾选「异步处理」可支持 ' + App.fmtBytes(MAX_ASYNC) + '）', 'err', 6000);
    }
    syncLimitHint();
    renderPendingFiles();
  });

  function renderPendingFiles() {
    var host = $('fileList');
    host.innerHTML = state.pendingFiles.map(function (f, i) {
      return '<div class="file-row">' + fileBadge(f.name.split('.').pop()) +
        '<span class="fr-name">' + App.escapeHtml(f.name) + '</span>' +
        '<span class="fr-size">' + App.fmtBytes(f.size) + '</span>' +
        '<button class="fr-del" data-i="' + i + '" aria-label="移除">✕</button></div>';
    }).join('');
    $('upSubmit').disabled = !state.pendingFiles.length;
    $('upSubmit').textContent = state.pendingFiles.length
      ? '开始上传（' + state.pendingFiles.length + '）' : '开始上传';
  }

  $('fileList').addEventListener('click', function (e) {
    var b = e.target.closest('.fr-del');
    if (!b) return;
    state.pendingFiles.splice(Number(b.getAttribute('data-i')), 1);
    renderPendingFiles();
  });

  $('upSubmit').addEventListener('click', function () {
    if (!state.pendingFiles.length) return;
    var files = state.pendingFiles.slice();
    var spaceId = $('upSpace').value;
    var perm = ($('upPerm').value || 'all').trim() || 'all';
    var useAsync = $('upAsync').checked;

    $('uploadMask').classList.add('hidden');
    uploadSequentially(files, 0, spaceId, perm, useAsync);
  });

  // 串行上传而不是并发：后端入库是 CPU/网络双重密集（切片 + 向量化 + 写库），
  // 并发上传几个 PDF 会把单实例的 worker 打满，反而整体更慢，
  // 而且失败时难以定位是哪个文件的问题。
  function uploadSequentially(files, idx, spaceId, perm, useAsync) {
    if (idx >= files.length) {
      App.toast('全部 ' + files.length + ' 个文件处理完毕', 'ok');
      loadDocs();
      if (useAsync) switchView('tasks');
      return;
    }
    var f = files[idx];
    var fd = new FormData();
    fd.append('file', f, f.name);
    fd.append('perm_group', perm);
    fd.append('space_id', spaceId);
    if (useAsync) fd.append('async_mode', 'true');

    var t = App.toast('正在上传 ' + f.name + '…', '', 60000);
    App.api.post('/documents', fd)
      .then(function (r) {
        t.remove();
        if (r && r.async_mode) {
          App.toast(f.name + ' 已进入队列（任务 ' + (r.task_id || '').slice(0, 8) + '…）', 'ok');
        } else {
          App.toast(f.name + ' 入库完成，' + (r.chunk_count || 0) + ' 个片段', 'ok');
        }
        uploadSequentially(files, idx + 1, spaceId, perm, useAsync);
      })
      .catch(function (err) {
        t.remove();
        App.toast(f.name + ' 上传失败：' + App.errText(err), 'err', 8000);
        // 单个失败不中断整批：用户选了 5 个文件，不该因为第 1 个失败就全丢
        uploadSequentially(files, idx + 1, spaceId, perm, useAsync);
      });
  }

  // ==================================================================
  // 空间
  // ==================================================================
  function loadSpaces() {
    var grid = $('spaceGrid');
    grid.innerHTML = '<div class="skeleton" style="height:150px"></div>'.repeat(3);
    App.api.get('/spaces')
      .then(function (data) {
        state.spaces = (data && data.items) || [];
        renderSpaces();
      })
      .catch(function (err) {
        grid.innerHTML = '<div class="empty">加载失败：' + App.escapeHtml(App.errText(err)) + '</div>';
      });
  }

  function renderSpaces() {
    var grid = $('spaceGrid');
    if (!state.spaces.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">' +
        '<div class="empty-icon">📁</div><div class="empty-title">还没有知识库空间</div>' +
        '<div class="empty-desc">空间用于按业务域隔离内容，例如「人力资源」「财务制度」</div></div>';
      return;
    }
    grid.innerHTML = state.spaces.map(function (s) {
      var isDefault = s.space_id === 'default';
      return '<div class="space-card' + (isDefault ? ' is-default' : '') + '">' +
        '<div class="sc-head">' +
          '<div style="min-width:0">' +
            '<div class="sc-name truncate">' + App.escapeHtml(s.name || s.space_id) + '</div>' +
            '<span class="sc-id">' + App.escapeHtml(s.space_id) + '</span>' +
          '</div>' +
          (isDefault ? '<span class="badge badge-brand">默认</span>' : '') +
        '</div>' +
        '<div class="sc-desc">' + App.escapeHtml(s.description || '暂无说明') + '</div>' +
        '<div class="sc-foot">' +
          '<span class="sc-count"><strong>' + (s.doc_count == null ? '-' : s.doc_count) + '</strong> 个文档</span>' +
          '<div class="row" style="gap:4px">' +
            '<button class="btn btn-ghost btn-sm" data-goto="' + App.escapeHtml(s.space_id) + '">查看文档</button>' +
            (!isDefault && App.has(PERM.spaceAdmin)
              ? '<button class="btn btn-danger btn-sm" data-delspace="' + App.escapeHtml(s.space_id) +
                '" data-sname="' + App.escapeHtml(s.name || s.space_id) + '">删除</button>'
              : '') +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  $('spaceGrid').addEventListener('click', function (e) {
    var go = e.target.closest('[data-goto]');
    if (go) {
      switchView('docs');
      $('docSpaceFilter').value = go.getAttribute('data-goto');
      renderDocs();
      return;
    }
    var del = e.target.closest('[data-delspace]');
    if (!del) return;
    var sid = del.getAttribute('data-delspace');
    var sname = del.getAttribute('data-sname');
    App.confirm({
      title: '删除空间',
      message: '将删除空间「' + sname + '」。空间内若还有文档，请先转移或删除，否则会被拒绝。',
      okText: '删除', danger: true
    }).then(function (ok) {
      if (!ok) return;
      App.api.del('/spaces/' + encodeURIComponent(sid))
        .then(function () { App.toast('已删除空间 ' + sid, 'ok'); loadSpaces(); })
        .catch(function (err) { App.toast('删除失败：' + App.errText(err), 'err'); });
    });
  });

  $('btnNewSpace').addEventListener('click', function () {
    $('spId').value = ''; $('spName').value = ''; $('spDesc').value = '';
    $('spaceMask').classList.remove('hidden');
    $('spId').focus();
  });
  $('spCancel').addEventListener('click', function () { $('spaceMask').classList.add('hidden'); });
  $('btnRefreshSpaces').addEventListener('click', loadSpaces);

  $('spSubmit').addEventListener('click', function () {
    var id = ($('spId').value || '').trim();
    var name = ($('spName').value || '').trim();
    var desc = ($('spDesc').value || '').trim();
    if (!/^[A-Za-z0-9_-]{2,64}$/.test(id)) {
      return App.toast('空间标识需为 2~64 位字母、数字、下划线或短横线', 'err');
    }
    if (!name) return App.toast('请填写空间名称', 'err');

    $('spSubmit').disabled = true;
    App.api.post('/spaces', { space_id: id, name: name, description: desc })
      .then(function () {
        $('spaceMask').classList.add('hidden');
        App.toast('空间「' + name + '」已创建', 'ok');
        loadSpaces();
      })
      .catch(function (err) { App.toast('创建失败：' + App.errText(err), 'err'); })
      .finally(function () { $('spSubmit').disabled = false; });
  });

  // ==================================================================
  // 任务队列
  // ==================================================================
  function loadTasks(silent) {
    var params = [];
    var st = $('taskStatusFilter').value;
    if (st) params.push('status=' + encodeURIComponent(st));
    var url = '/tasks' + (params.length ? '?' + params.join('&') : '');

    App.api.get(url)
      .then(function (data) {
        state.tasks = (data && data.items) || [];
        renderTaskStats(data && data.stats);
        renderTasks();
        updateTaskBadge(data && data.stats);
      })
      .catch(function (err) {
        if (!silent) {
          $('taskTbody').innerHTML = '<tr><td colspan="7"><div class="empty">加载失败：' +
            App.escapeHtml(App.errText(err)) + '</div></td></tr>';
        }
      });
  }

  function updateTaskBadge(stats) {
    var n = (stats && (stats.pending + stats.running)) || 0;
    var badge = $('taskBadge');
    badge.textContent = n;
    badge.classList.toggle('hidden', n === 0);
  }

  function renderTaskStats(stats) {
    stats = stats || {};
    var items = [
      { label: '总数', value: stats.total || 0, cls: '' },
      { label: '排队中', value: stats.pending || 0, cls: (stats.pending ? 'warn' : '') },
      { label: '执行中', value: stats.running || 0, cls: (stats.running ? 'warn' : '') },
      { label: '成功', value: stats.success || 0, cls: 'ok' },
      { label: '失败', value: stats.failed || 0, cls: (stats.failed ? 'err' : '') }
    ];
    $('taskStats').innerHTML = items.map(function (i) {
      return '<div class="stat-box"><div class="stat-label">' + i.label + '</div>' +
             '<div class="stat-value ' + i.cls + '">' + i.value + '</div></div>';
    }).join('');
  }

  var STATUS_MAP = {
    pending: ['排队中', 'badge-plain'],
    running: ['执行中', 'badge-info'],
    success: ['成功', 'badge-ok'],
    failed: ['失败', 'badge-err'],
    cancelled: ['已取消', 'badge-plain']
  };
  var TYPE_MAP = { ingest: '入库', reindex: '重建索引', webhook: '告警投递' };

  function renderTasks() {
    var tb = $('taskTbody');
    if (!state.tasks.length) {
      tb.innerHTML = '<tr><td colspan="7"><div class="empty">' +
        '<div class="empty-icon">⏱</div><div class="empty-title">暂无任务</div>' +
        '<div class="empty-desc">上传文档（勾选异步入库）后会在这里出现</div></div></td></tr>';
      return;
    }
    tb.innerHTML = state.tasks.map(function (t) {
      var s = STATUS_MAP[t.status] || [t.status, 'badge-plain'];
      var detail = '';
      if (t.status === 'failed' && t.error) {
        detail = '<span style="color:var(--err-500)">' + App.escapeHtml(t.error) + '</span>';
      } else if (t.file_name) {
        detail = App.escapeHtml(t.file_name) +
          (t.space_id ? ' <span class="badge badge-plain" style="font-size:10.5px">' +
            App.escapeHtml(t.space_id) + '</span>' : '');
      } else if (t.result && Object.keys(t.result).length) {
        detail = '<span class="muted mono">' + App.escapeHtml(JSON.stringify(t.result).slice(0, 90)) + '</span>';
      } else {
        detail = '<span class="muted">-</span>';
      }

      var progress = '';
      if (t.status === 'running') {
        progress = '<div class="skeleton" style="height:4px;width:' +
          Math.max(8, t.progress || 0) + '%;margin-top:4px;background:var(--brand-400)"></div>';
      }

      return '<tr>' +
        '<td class="mono">' + App.escapeHtml((t.task_id || '').slice(0, 12)) + '</td>' +
        '<td>' + (TYPE_MAP[t.task_type] || t.task_type) + '</td>' +
        '<td><span class="badge ' + s[1] + '">' + s[0] + '</span>' + progress + '</td>' +
        '<td style="font-size:12.5px">' + detail + '</td>' +
        '<td class="mono">' + (t.elapsed_ms ? (t.elapsed_ms / 1000).toFixed(2) + 's' : '-') + '</td>' +
        '<td class="muted" style="font-size:12px">' + App.fmtAgo(t.created_at) + '</td>' +
        '<td>' + (t.status === 'pending'
          ? '<button class="btn btn-ghost btn-sm" data-cancel="' + App.escapeHtml(t.task_id) + '">取消</button>'
          : '<span class="muted">-</span>') + '</td>' +
      '</tr>';
    }).join('');
  }

  $('taskStatusFilter').addEventListener('change', function () { loadTasks(); });
  $('btnRefreshTasks').addEventListener('click', function () { loadTasks(); });

  $('taskTbody').addEventListener('click', function (e) {
    var b = e.target.closest('[data-cancel]');
    if (!b) return;
    var id = b.getAttribute('data-cancel');
    App.api.post('/tasks/' + encodeURIComponent(id) + '/cancel')
      .then(function () { App.toast('任务已取消', 'ok'); loadTasks(); })
      .catch(function (err) { App.toast('取消失败：' + App.errText(err), 'err'); });
  });

  // ==================================================================
  // 告警
  // ==================================================================
  function loadAlerts() {
    var sev = $('alertSevFilter').value;
    var q = sev ? '?severity=' + encodeURIComponent(sev) : '';
    Promise.all([
      App.api.get('/alerts' + q),
      App.api.get('/alerts/rules')
    ]).then(function (res) {
      var a = res[0] || {};
      var r = res[1] || {};
      renderAlertStats(a.stats || r.stats);
      renderAlerts(a.items || []);
      renderRules(r.items || []);
      var firing = (a.stats && a.stats.firing) || 0;
      var badge = $('alertBadge');
      badge.textContent = firing;
      badge.classList.toggle('hidden', !firing);
    }).catch(function (err) {
      $('alertList').innerHTML = '<div class="empty">加载失败：' + App.escapeHtml(App.errText(err)) + '</div>';
    });
  }

  function renderAlertStats(s) {
    s = s || {};
    var ch = s.channels || {};
    var items = [
      { label: '24h 告警', value: s.total_24h || 0, cls: '' },
      { label: '严重', value: s.critical_24h || 0, cls: (s.critical_24h ? 'err' : '') },
      { label: '警告', value: s.warning_24h || 0, cls: (s.warning_24h ? 'warn' : '') },
      { label: '当前触发', value: s.firing || 0, cls: (s.firing ? 'err' : 'ok') },
      {
        label: '通知渠道',
        value: (ch.webhook ? 'Webhook ' : '') + (ch.email ? '邮件' : '') || '仅落库',
        cls: 'sm'
      }
    ];
    $('alertStats').innerHTML = items.map(function (i) {
      return '<div class="stat-box"><div class="stat-label">' + i.label + '</div>' +
             '<div class="stat-value ' + i.cls + '">' + i.value + '</div></div>';
    }).join('');
  }

  var SEV_ICON = { critical: '🔴', warning: '🟠', info: '🔵' };

  function renderAlerts(items) {
    var host = $('alertList');
    if (!items.length) {
      host.innerHTML = '<div class="empty"><div class="empty-icon">✅</div>' +
        '<div class="empty-title">没有告警</div>' +
        '<div class="empty-desc">系统运行正常，或告警已被静默</div></div>';
      return;
    }
    host.innerHTML = items.map(function (a) {
      return '<div class="alert-item ' + App.escapeHtml(a.severity) + '">' +
        '<span class="al-icon">' + (SEV_ICON[a.severity] || '⚪') + '</span>' +
        '<div class="al-main">' +
          '<div class="al-title">' + App.escapeHtml(a.title) + '</div>' +
          (a.message ? '<div class="al-msg">' + App.escapeHtml(a.message) + '</div>' : '') +
          '<div class="al-foot">' +
            '<span>' + App.escapeHtml(a.rule_id) + '</span>' +
            '<span>值 ' + a.value + ' / 阈值 ' + a.threshold + '</span>' +
            '<span>' + App.escapeHtml(a.channels || '-') + '</span>' +
            '<span>' + App.fmtAgo(a.created_at) + '</span>' +
          '</div>' +
        '</div></div>';
    }).join('');
  }

  function renderRules(items) {
    var host = $('ruleList');
    if (!items.length) {
      host.innerHTML = '<div class="empty">没有规则</div>';
      return;
    }
    host.innerHTML = items.map(function (r) {
      var last = r.last || {};
      var breach = last.breached;
      return '<div class="rule-row' + (r.enabled ? '' : ' off') + '">' +
        '<span class="al-icon">' + (SEV_ICON[r.severity] || '⚪') + '</span>' +
        '<div class="rr-main">' +
          '<div class="rr-title">' + App.escapeHtml(r.title) +
            (r.silenced ? ' <span class="badge badge-warn" style="font-size:10px">静默中</span>' : '') +
            (!r.enabled ? ' <span class="badge badge-plain" style="font-size:10px">已停用</span>' : '') +
          '</div>' +
          '<div class="rr-meta">' + App.escapeHtml(r.rule_id) +
            ' · 阈值 ' + r.threshold +
            ' · 连续 ' + r.for_rounds + ' 轮</div>' +
        '</div>' +
        '<span class="rr-value' + (breach ? ' breach' : '') + '">' +
          (last.value == null ? '-' : last.value) + '</span>' +
      '</div>';
    }).join('');
  }

  $('alertSevFilter').addEventListener('change', loadAlerts);
  $('btnRefreshAlerts').addEventListener('click', loadAlerts);

  $('btnDryRun').addEventListener('click', function () {
    App.api.post('/alerts/evaluate?dry_run=true')
      .then(function (r) {
        if (!r.fired) {
          App.toast('试算完成：当前没有规则越线', 'ok');
        } else {
          App.toast('试算完成：' + r.fired + ' 条规则已越线（未投递、未落库）', 'warn', 5000);
        }
        loadAlerts();
      })
      .catch(function (err) { App.toast('试算失败：' + App.errText(err), 'err'); });
  });

  // ==================================================================
  // 审计
  // ==================================================================
  function loadAudit() {
    var params = [];
    var actor = ($('auditActor').value || '').trim();
    var action = ($('auditAction').value || '').trim();
    var result = $('auditResult').value;
    if (actor) params.push('actor=' + encodeURIComponent(actor));
    if (action) params.push('action=' + encodeURIComponent(action));
    if (result) params.push('result=' + encodeURIComponent(result));
    params.push('limit=100');

    App.api.get('/audit/logs?' + params.join('&'))
      .then(function (data) {
        renderAudit((data && data.items) || []);
      })
      .catch(function (err) {
        $('auditTbody').innerHTML = '<tr><td colspan="8"><div class="empty">加载失败：' +
          App.escapeHtml(App.errText(err)) + '</div></td></tr>';
      });
  }

  var RESULT_BADGE = { success: 'badge-ok', denied: 'badge-warn', failed: 'badge-err' };
  var RESULT_LABEL = { success: '成功', denied: '被拒绝', failed: '失败' };

  function renderAudit(items) {
    var tb = $('auditTbody');
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="8"><div class="empty">' +
        '<div class="empty-icon">📋</div><div class="empty-title">没有审计记录</div>' +
        '<div class="empty-desc">换个筛选条件试试</div></div></td></tr>';
      return;
    }
    tb.innerHTML = items.map(function (a) {
      return '<tr>' +
        '<td class="muted mono" style="font-size:12px">' + App.fmtTime(a.created_at) + '</td>' +
        '<td>' + App.escapeHtml(a.actor || '-') + '</td>' +
        '<td><span class="badge badge-plain" style="font-size:11px">' +
          App.escapeHtml(a.role || '-') + '</span></td>' +
        '<td class="mono" style="font-size:12px">' + App.escapeHtml(a.action) + '</td>' +
        '<td class="truncate" style="max-width:220px;font-size:12.5px">' +
          App.escapeHtml(a.resource || '-') + '</td>' +
        '<td><span class="badge ' + (RESULT_BADGE[a.result] || 'badge-plain') + '">' +
          (RESULT_LABEL[a.result] || a.result) + '</span></td>' +
        '<td class="mono">' + (a.latency_ms || 0) + 'ms</td>' +
        '<td class="mono muted" style="font-size:11.5px">' + App.escapeHtml(a.trace_id || '-') + '</td>' +
      '</tr>';
    }).join('');
  }

  $('btnAuditSearch').addEventListener('click', loadAudit);
  $('btnRefreshAudit').addEventListener('click', loadAudit);
  ['auditActor', 'auditAction'].forEach(function (id) {
    $(id).addEventListener('keydown', function (e) { if (e.key === 'Enter') loadAudit(); });
  });
  $('auditResult').addEventListener('change', loadAudit);

  // ==================================================================
  // 指标
  // ==================================================================
  function loadMetrics() {
    Promise.all([App.api.get('/metrics/snapshot'), App.api.get('/health')])
      .then(function (res) {
        renderMetrics(res[0] || {});
        renderHealth(res[1] || {});
      })
      .catch(function (err) {
        $('metricGrid').innerHTML = '<div class="empty">加载失败：' +
          App.escapeHtml(App.errText(err)) + '</div>';
      });
  }

  function renderMetrics(m) {
    var lat = m.latency_ms || {};
    var chat = m.chat || {};
    var ret = m.retrieve || {};
    var llm = m.llm || {};
    var sec = m.security || {};

    var cards = [
      { label: '总请求数', value: m.requests_total || 0, sub: '运行 ' + Math.floor((m.uptime_sec || 0) / 60) + ' 分钟' },
      { label: '错误率', value: App.fmtPct(m.error_rate), sub: '错误 ' + (m.errors_total || 0) + ' 次',
        cls: (m.error_rate > 0.05 ? 'err' : '') },
      { label: 'P95 延迟', value: lat.p95 || 0, unit: 'ms', sub: '样本 ' + (lat.samples || 0) },
      { label: '平均延迟', value: lat.avg || 0, unit: 'ms' },
      { label: '问答次数', value: chat.total || 0, sub: '拒答 ' + (chat.rejected || 0) },
      { label: '缓存命中率', value: App.fmtPct(chat.cache_hit_rate), sub: '命中 ' + (chat.cache_hit || 0) },
      { label: '检索次数', value: ret.total || 0, sub: '命中 ' + (ret.cache_hit || 0) },
      { label: 'LLM 失败率', value: App.fmtPct(llm.failure_rate), sub: '调用 ' + (llm.calls || 0),
        cls: (llm.failure_rate > 0.3 ? 'err' : '') },
      { label: 'LLM P95', value: llm.p95_ms || 0, unit: 'ms' },
      { label: '限流拦截', value: sec.rate_limited || 0, sub: '认证失败 ' + (sec.auth_failures || 0) },
      { label: '慢请求', value: m.slow_requests || 0 },
      { label: '并发请求', value: m.inflight || 0 }
    ];

    $('metricGrid').innerHTML = cards.map(function (c) {
      return '<div class="metric-card">' +
        '<div class="mc-label">' + c.label + '</div>' +
        '<div class="mc-value ' + (c.cls || '') + '" style="' +
          (c.cls === 'err' ? 'color:var(--err-500)' : '') + '">' +
          c.value + (c.unit ? '<span class="mc-unit">' + c.unit + '</span>' : '') + '</div>' +
        (c.sub ? '<div class="mc-sub">' + App.escapeHtml(c.sub) + '</div>' : '') +
      '</div>';
    }).join('');
  }

  function renderHealth(h) {
    var st = h.storage || {};
    var rows = [
      { name: '向量库', d: st.vector },
      { name: '业务库', d: st.meta },
      { name: '缓存', d: st.cache && st.cache.l2 ? st.cache.l2 : st.cache }
    ];
    $('healthGrid').innerHTML = rows.map(function (r) {
      var d = r.d || {};
      var ok = d.status === 'ok';
      return '<div class="health-item">' +
        '<span class="hi-dot ' + (ok ? 'ok' : 'bad') + '"></span>' +
        '<div style="min-width:0">' +
          '<div class="hi-name">' + r.name + '</div>' +
          '<div class="hi-detail">' + App.escapeHtml(d.backend || '-') +
            ' · ' + App.escapeHtml(d.status || '-') + '</div>' +
        '</div></div>';
    }).join('') +
    '<div class="health-item"><span class="hi-dot ' + (h.status === 'ok' ? 'ok' : 'bad') + '"></span>' +
      '<div><div class="hi-name">模型</div><div class="hi-detail">' +
      App.escapeHtml(h.model_mode || '-') + '</div></div></div>';
  }

  $('btnRefreshMetrics').addEventListener('click', loadMetrics);

  // ==================================================================
  // 用户与密钥
  // ==================================================================
  function loadUsers() {
    Promise.all([
      App.api.get('/auth/users').catch(function () { return { items: [] }; }),
      App.api.get('/auth/keys').catch(function () { return { items: [] }; })
    ]).then(function (res) {
      renderUsers((res[0] && res[0].items) || []);
      renderKeys((res[1] && res[1].items) || []);
    });
  }

  var ROLE_LABEL = { admin: '管理员', kb_admin: '知识库管理员', user: '普通用户' };

  function renderUsers(items) {
    var tb = $('userTbody');
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="4"><div class="empty">暂无用户</div></td></tr>';
      return;
    }
    tb.innerHTML = items.map(function (u) {
      return '<tr>' +
        '<td><div style="font-weight:500">' + App.escapeHtml(u.display_name || u.username) + '</div>' +
          '<div class="file-meta mono">' + App.escapeHtml(u.username) + '</div></td>' +
        '<td><span class="badge badge-plain">' + (ROLE_LABEL[u.role] || u.role) + '</span></td>' +
        '<td><span class="badge ' + (u.enabled ? 'badge-ok' : 'badge-err') + '">' +
          (u.enabled ? '启用' : '禁用') + '</span></td>' +
        '<td class="muted" style="font-size:12px">' + App.fmtAgo(u.last_login_at) + '</td>' +
      '</tr>';
    }).join('');
  }

  var KEY_STATUS = {
    active: ['有效', 'badge-ok'],
    disabled: ['已停用', 'badge-plain'],
    expired: ['已过期', 'badge-warn'],
    revoked: ['已吊销', 'badge-err']
  };

  function renderKeys(items) {
    var tb = $('keyTbody');
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="4"><div class="empty">暂无密钥</div></td></tr>';
      return;
    }
    tb.innerHTML = items.map(function (k) {
      var s = KEY_STATUS[k.status] || [k.status, 'badge-plain'];
      return '<tr>' +
        '<td><div class="mono" style="font-size:12px">' + App.escapeHtml(k.key_id) + '</div>' +
          (k.description ? '<div class="file-meta">' + App.escapeHtml(k.description) + '</div>' : '') + '</td>' +
        '<td>' + App.escapeHtml(k.owner) + '</td>' +
        '<td><span class="badge ' + s[1] + '">' + s[0] + '</span></td>' +
        '<td>' + (k.status === 'active'
          ? '<button class="btn btn-danger btn-sm" data-revoke="' + App.escapeHtml(k.key_id) + '">吊销</button>'
          : '<span class="muted">-</span>') + '</td>' +
      '</tr>';
    }).join('');
  }

  $('btnRefreshUsers').addEventListener('click', loadUsers);

  $('keyTbody').addEventListener('click', function (e) {
    var b = e.target.closest('[data-revoke]');
    if (!b) return;
    var kid = b.getAttribute('data-revoke');
    App.confirm({
      title: '吊销密钥',
      message: '吊销后使用该密钥的系统调用将立即失败，且不可恢复。请确认相关服务已切换到新密钥。',
      okText: '吊销', danger: true
    }).then(function (ok) {
      if (!ok) return;
      App.api.del('/auth/keys/' + encodeURIComponent(kid))
        .then(function () { App.toast('已吊销密钥 ' + kid, 'ok'); loadUsers(); })
        .catch(function (err) { App.toast('吊销失败：' + App.errText(err), 'err'); });
    });
  });

  $('btnNewUser').addEventListener('click', function () {
    $('usName').value = ''; $('usPwd').value = ''; $('usSpaces').value = '*';
    $('usRole').value = 'user';
    $('userMask').classList.remove('hidden');
    $('usName').focus();
  });
  $('usCancel').addEventListener('click', function () { $('userMask').classList.add('hidden'); });

  $('usSubmit').addEventListener('click', function () {
    var name = ($('usName').value || '').trim();
    var pwd = $('usPwd').value || '';
    if (!name) return App.toast('请填写用户名', 'err');
    if (pwd.length < 8) return App.toast('密码至少 8 位', 'err');

    $('usSubmit').disabled = true;
    App.api.post('/auth/users', {
      username: name,
      password: pwd,
      role: $('usRole').value,
      spaces: ($('usSpaces').value || '*').trim(),
      perm_groups: ''
    })
      .then(function () {
        $('userMask').classList.add('hidden');
        App.toast('用户 ' + name + ' 已创建', 'ok');
        loadUsers();
      })
      .catch(function (err) { App.toast('创建失败：' + App.errText(err), 'err'); })
      .finally(function () { $('usSubmit').disabled = false; });
  });

  // ---------------- 新建密钥 ----------------
  $('btnNewKey').addEventListener('click', function () {
    App.confirm({
      title: '新建 API 密钥',
      message: '将创建一个归属当前账号、用于系统间调用的长期密钥。明文只显示一次。',
      okText: '创建'
    }).then(function (ok) {
      if (!ok) return;
      App.api.post('/auth/keys', {
        description: '管理后台创建 ' + new Date().toLocaleString('zh-CN')
      })
        .then(function (k) {
          $('secretValue').textContent = k.api_key || '';
          $('secretNote').textContent = 'key_id: ' + (k.key_id || '') +
            (k.warning ? '　' + k.warning : '');
          $('secretMask').classList.remove('hidden');
          loadUsers();
        })
        .catch(function (err) { App.toast('创建失败：' + App.errText(err), 'err'); });
    });
  });

  $('secretCopy').addEventListener('click', function () {
    var v = $('secretValue').textContent;
    navigator.clipboard.writeText(v)
      .then(function () { App.toast('已复制', 'ok', 1400); })
      .catch(function () { App.toast('复制失败，请手动选择', 'err'); });
  });
  $('secretClose').addEventListener('click', function () {
    $('secretValue').textContent = '';
    $('secretMask').classList.add('hidden');
  });

  // ---------------- 退出 ----------------
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
    App.api.post('/auth/logout', { refresh_token: App.store.refreshToken() })
      .catch(function () {})
      .then(function () {
        App.store.clear();
        location.replace('/login');
      });
  });

  // Esc 关闭所有弹窗
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    ['uploadMask', 'spaceMask', 'userMask', 'secretMask'].forEach(function (id) {
      $(id).classList.add('hidden');
    });
  });

  // ---------------- 启动 ----------------
  switchView('docs');
  // 后台首次加载顺带拉一次任务统计，让侧栏角标立刻可用
  App.api.get('/tasks?limit=1').then(function (d) {
    updateTaskBadge(d && d.stats);
  }).catch(function () {});
})();
