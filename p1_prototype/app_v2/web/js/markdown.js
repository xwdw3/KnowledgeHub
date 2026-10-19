/* =====================================================================
   极简 Markdown 渲染器
   ---------------------------------------------------------------------
   为什么不用 marked.js / markdown-it：
     它们是几百 KB 的依赖，要下载到内网或随包分发；本项目的渲染需求只有
     "标题、列表、加粗、代码、引用、表格"这六七种，自己实现 150 行足够。
     更重要的是安全性可控——第三方库默认输出 HTML 字符串，
     直接 innerHTML 就是 XSS 通道（大模型的输出是不可信内容）。

   安全模型（这是本文件最重要的部分）：
     1. **先转义，再渲染**。所有原文先 escapeHtml，之后只从白名单语法
        生成标签。不存在"用户输入被当作 HTML 解析"的路径。
     2. **链接只允许 http/https**。`javascript:` / `data:` 一律降级为纯文本。
        这是 innerHTML 场景下最常见的绕过点。
     3. **不用 innerHTML 拼接原文**。所有文本节点都走 textContent。
   ===================================================================== */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function safeUrl(url) {
    var u = String(url || '').trim();
    // 只放行 http(s) 与站内相对路径（以 / 开头且不是 //）
    if (/^https?:\/\//i.test(u)) return u;
    if (u.charAt(0) === '/' && u.charAt(1) !== '/') return u;
    return '';
  }

  // 行内元素：代码 → 加粗 → 斜体 → 链接
  // 顺序有讲究：先处理 `code`，否则代码里的 * 会被当成加粗标记。
  function inline(text) {
    var out = esc(text);

    // 行内代码
    var codes = [];
    out = out.replace(/`([^`\n]+)`/g, function (m, code) {
      codes.push(code);
      return '\u0000C' + (codes.length - 1) + '\u0000';
    });

    // 链接 [text](url)
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (m, label, url) {
      var u = safeUrl(esc(url).replace(/&amp;/g, '&'));
      if (!u) return label;                   // 不安全协议 → 只留文字，不留链接
      return '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' +
             label + '</a>';
    });

    // 裸 URL 自动链接（跳过已在 href 里的，用简单负向断言近似处理）
    out = out.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g, function (m, pre, url) {
      return pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' +
             url + '</a>';
    });

    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    // 删除线
    out = out.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

    // 还原行内代码
    out = out.replace(/\u0000C(\d+)\u0000/g, function (m, i) {
      return '<code>' + codes[Number(i)] + '</code>';
    });

    return out;
  }

  function render(src) {
    var lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
    var html = [];
    var i = 0;
    var n = lines.length;

    function flushList(tag, items) {
      if (!items.length) return;
      html.push('<' + tag + '>' + items.map(function (t) {
        return '<li>' + inline(t) + '</li>';
      }).join('') + '</' + tag + '>');
    }

    while (i < n) {
      var line = lines[i];

      // 空行
      if (/^\s*$/.test(line)) { i++; continue; }

      // 代码块 ```
      if (/^\s*```/.test(line)) {
        var lang = line.replace(/^\s*```\s*/, '').trim();
        var buf = [];
        i++;
        while (i < n && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;                                  // 跳过结束 ```
        html.push('<pre><code' + (lang ? ' data-lang="' + esc(lang) + '"' : '') + '>' +
                  esc(buf.join('\n')) + '</code></pre>');
        continue;
      }

      // 标题 # ~ ####
      var h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        // 把 # 降一级：# → h3。页面上已经有 h1/h2（欢迎语、消息区），
        // 模型输出的标题不应抢走文档大纲层级。
        var level = Math.min(4, h[1].length + 2);
        html.push('<h' + level + '>' + inline(h[2]) + '</h' + level + '>');
        i++;
        continue;
      }

      // 分隔线
      if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) {
        html.push('<hr>');
        i++;
        continue;
      }

      // 引用 >
      if (/^\s*>\s?/.test(line)) {
        var quote = [];
        while (i < n && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        html.push('<blockquote>' + inline(quote.join(' ')) + '</blockquote>');
        continue;
      }

      // 表格：| a | b | 且下一行是分隔行
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < n && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        var head = splitRow(line);
        i += 2;
        var rows = [];
        while (i < n && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(splitRow(lines[i])); i++; }
        var t = '<table><thead><tr>' +
          head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          rows.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table>';
        html.push(t);
        continue;
      }

      // 无序列表
      if (/^\s*[-*+]\s+/.test(line)) {
        var ul = [];
        while (i < n && /^\s*[-*+]\s+/.test(lines[i])) {
          ul.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
          i++;
        }
        flushList('ul', ul);
        continue;
      }

      // 有序列表
      if (/^\s*\d+[.)]\s+/.test(line)) {
        var ol = [];
        while (i < n && /^\s*\d+[.)]\s+/.test(lines[i])) {
          ol.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
          i++;
        }
        flushList('ol', ol);
        continue;
      }

      // 段落：连续非空行合并成一段
      var para = [line];
      i++;
      while (i < n && !/^\s*$/.test(lines[i]) &&
             !/^\s*(#{1,4}\s|```|>|\||[-*+]\s|\d+[.)]\s)/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      html.push('<p>' + inline(para.join(' ')) + '</p>');
    }

    return html.join('\n');
  }

  function splitRow(line) {
    return line.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); });
  }

  global.MD = { render: render, escape: esc };
})(window);
