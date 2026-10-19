"""P1 原型入口：FastAPI + 单页演示界面。

启动：
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8011
或直接：
    python run.py
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app import config, ingest, rag
from app.loader import ParseError
from app.store import index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("p1")

app = FastAPI(title="企业知识库 P1 原型", version="1.0", description="RAG 核心链路验证原型")


# =============================== 接口 ===============================
@app.get("/api/status")
def status():
    return {
        "mode": config.MODE,
        "demo_mode_setting": config.DEMO_MODE,
        "embed_model": config.EMBED_MODEL_NAME if config.MODE == "real" else "本地哈希向量(离线)",
        "chat_model": config.CHAT_MODEL if config.MODE == "real" else "抽取式作答(离线)",
        "index": index.stats(),
        "params": {
            "chunk_size": config.CHUNK_SIZE,
            "chunk_overlap": config.CHUNK_OVERLAP,
            "recall_k": config.RECALL_K,
            "rrf_k": config.RRF_K,
            "final_top_k": config.FINAL_TOP_K,
            "min_score": config.MIN_SCORE,
        },
    }


@app.post("/api/ask")
def ask(payload: dict):
    query = str(payload.get("query", ""))
    try:
        top_k = int(payload.get("top_k") or config.FINAL_TOP_K)
    except (TypeError, ValueError):
        top_k = config.FINAL_TOP_K
    result = rag.ask(query, top_k=top_k)
    return {"code": 200, "data": result}


@app.post("/api/retrieve")
def retrieve(payload: dict):
    """仅检索，不生成答案：用于演示"向量 vs BM25 vs 融合"的差异。"""
    from app.retriever import search

    query = str(payload.get("query", ""))
    hits = search(query, top_k=int(payload.get("top_k") or 5))
    return {"code": 200, "data": {"query": query, "hits": [h.to_dict() for h in hits]}}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), perm: str = Form("all")):
    suffix = Path(file.filename).suffix.lower()
    tmp_path = Path(tempfile.gettempdir()) / f"p1_{abs(hash(file.filename))}{suffix}"
    try:
        tmp_path.write_bytes(await file.read())
        result = ingest.ingest_path(tmp_path)
        return {"code": 200, "msg": "入库成功", "data": result}
    except ParseError as exc:
        return JSONResponse(status_code=400, content={"code": 400, "msg": str(exc)})
    except Exception as exc:
        logger.exception("上传入库失败")
        return JSONResponse(status_code=500, content={"code": 500, "msg": f"入库失败：{exc}"})
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/ingest_dir")
def ingest_dir():
    result = ingest.ingest_dir()
    return {"code": 200, "data": result}


@app.post("/api/docs/delete")
def delete_doc(payload: dict):
    file_name = str(payload.get("file_name", "")).strip()
    if not file_name:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "请提供文件名"})
    removed = ingest.delete_doc(file_name)
    return {"code": 200, "msg": f"已删除 {removed} 个片段", "removed": removed}


@app.get("/api/docs")
def list_docs():
    return {"code": 200, "data": index.list_docs()}


# =============================== 演示页面 ===============================
@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(PAGE_HTML)


PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>企业知识库 · P1 原型</title>
<style>
  :root{
    --bg:#f5f6f8; --panel:#fff; --ink:#1f2329; --sub:#646a73; --line:#e5e6eb;
    --blue:#2563eb; --blue-soft:#eff4ff; --green:#16a34a; --amber:#d97706; --red:#dc2626;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
  header{background:var(--panel);border-bottom:1px solid var(--line);padding:14px 24px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
  header h1{font-size:17px;font-weight:600}
  .tags{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
  .tag{font-size:12px;padding:3px 10px;border-radius:20px;background:var(--blue-soft);color:var(--blue);border:1px solid #dbe6ff}
  .tag.off{background:#fff7ed;color:var(--amber);border-color:#fed7aa}
  .layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px 24px 32px;max-width:1400px;margin:0 auto}
  @media(max-width:960px){.layout{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px}
  /* 对话区 */
  .chat{display:flex;flex-direction:column;height:calc(100vh - 130px);min-height:520px}
  .msgs{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:18px}
  .empty{color:var(--sub);text-align:center;margin:auto;font-size:14px}
  .empty b{display:block;font-size:16px;color:var(--ink);margin-bottom:6px}
  .sugs{display:flex;flex-direction:column;gap:8px;margin-top:18px;align-items:center}
  .sug{cursor:pointer;background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:8px 14px;font-size:13px;color:var(--sub);transition:.15s}
  .sug:hover{border-color:var(--blue);color:var(--blue);background:var(--blue-soft)}
  .row{display:flex;gap:10px}
  .row.user{justify-content:flex-end}
  .avatar{width:30px;height:30px;border-radius:8px;flex:0 0 30px;display:grid;place-items:center;font-size:14px;color:#fff}
  .avatar.u{background:#374151}
  .avatar.b{background:linear-gradient(135deg,#2563eb,#60a5fa)}
  .bubble{max-width:calc(100% - 44px);padding:12px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
  .row.user .bubble{background:var(--blue);color:#fff;border-bottom-right-radius:2px}
  .row.bot .bubble{background:#fafbfc;border:1px solid var(--line);border-bottom-left-radius:2px}
  .bubble .hint{display:block;color:var(--sub);font-size:12px;margin-top:8px}
  .src-toggle{margin-top:10px;font-size:12px;color:var(--blue);cursor:pointer;user-select:none;display:inline-block}
  .src-toggle:hover{text-decoration:underline}
  .srcs{margin-top:10px;display:none;flex-direction:column;gap:8px}
  .srcs.open{display:flex}
  .src{background:#fff;border:1px solid var(--line);border-left:3px solid var(--blue);border-radius:8px;padding:10px 12px;font-size:12.5px}
  .src-head{display:flex;gap:8px;align-items:center;color:var(--sub);margin-bottom:4px;flex-wrap:wrap}
  .src-name{color:var(--ink);font-weight:600}
  .meter{margin-left:auto;display:flex;gap:6px;font-size:11px}
  .meter span{background:#f2f3f5;border-radius:4px;padding:1px 6px}
  .src-text{color:#3f4550;white-space:pre-wrap;max-height:150px;overflow:auto;line-height:1.55}
  .composer{padding:14px 16px;border-top:1px solid var(--line);background:#fff;border-radius:0 0 12px 12px}
  .inputwrap{display:flex;gap:10px;align-items:flex-end}
  textarea{flex:1;resize:none;height:46px;max-height:140px;padding:11px 13px;border:1px solid var(--line);border-radius:10px;font:inherit;outline:none;transition:.15s}
  textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.08)}
  button{font:inherit;cursor:pointer;border:none;border-radius:10px;padding:11px 20px;background:var(--blue);color:#fff;transition:.15s}
  button:hover{filter:brightness(1.08)}
  button:disabled{opacity:.55;cursor:not-allowed}
  button.ghost{background:#f2f3f5;color:var(--ink)}
  .meta-bar{display:flex;gap:14px;font-size:12px;color:var(--sub);margin-top:8px;flex-wrap:wrap;align-items:center}
  /* 侧栏 */
  .side{display:flex;flex-direction:column;gap:16px}
  .side h3{font-size:14px;font-weight:600;padding:14px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
  .side .body{padding:14px 16px}
  .stat{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
  .stat div{background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center}
  .stat b{display:block;font-size:16px;color:var(--blue)}
  .stat span{font-size:11px;color:var(--sub)}
  .btn-line{display:flex;gap:8px}
  .btn-line button{flex:1;padding:9px 10px;font-size:13px}
  .docs{max-height:280px;overflow:auto;display:flex;flex-direction:column;gap:6px}
  .doc{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:12.5px}
  .doc .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .doc .c{color:var(--sub);font-size:11px}
  .doc .x{color:var(--red);cursor:pointer;font-size:12px;opacity:.7}
  .doc .x:hover{opacity:1}
  .params{font-size:12px;color:var(--sub);line-height:1.9}
  .params code{background:#f2f3f5;padding:1px 5px;border-radius:4px;color:var(--ink)}
  .toast{position:fixed;right:24px;bottom:24px;background:#1f2329;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none}
  .toast.show{opacity:1;transform:none}
  .toast.err{background:var(--red)}
  .loading{display:inline-flex;gap:4px;align-items:center;color:var(--sub);font-size:13px}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--blue);animation:bl 1.2s infinite}
  .dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
  @keyframes bl{0%,80%,100%{opacity:.25}40%{opacity:1}}
</style>
</head>
<body>
<header>
  <h1>企业智能知识库 <span style="color:var(--sub);font-weight:400;font-size:13px">P1 · 核心链路验证原型</span></h1>
  <div class="tags">
    <span class="tag" id="modeTag">加载中…</span>
    <span class="tag" id="idxTag">索引：—</span>
  </div>
</header>

<div class="layout">
  <section class="card chat">
    <div class="msgs" id="msgs">
      <div class="empty">
        <b>基于企业知识库作答，答案均标注出处</b>
        上传文档后即可提问，系统会先检索原文，再生成答案
        <div class="sugs" id="sugs"></div>
      </div>
    </div>
    <div class="composer">
      <div class="inputwrap">
        <textarea id="q" placeholder="输入问题，Enter 发送，Shift+Enter 换行"></textarea>
        <button id="send">发送</button>
      </div>
      <div class="meta-bar">
        <span id="latency"></span>
        <span style="margin-left:auto"><button class="ghost" style="padding:5px 12px;font-size:12px" onclick="clearChat()">清空对话</button></span>
      </div>
    </div>
  </section>

  <aside class="side">
    <div class="card">
      <h3>📚 知识库</h3>
      <div class="body">
        <div class="stat">
          <div><b id="s-doc">0</b><span>文档</span></div>
          <div><b id="s-chunk">0</b><span>片段</span></div>
          <div><b id="s-char">0</b><span>字数</span></div>
        </div>
        <div class="btn-line" style="margin-bottom:10px">
          <button onclick="document.getElementById('file').click()">上传文档</button>
          <button class="ghost" onclick="ingestDir()">导入示例</button>
        </div>
        <input type="file" id="file" accept=".txt,.md,.pdf,.docx" style="display:none">
        <div class="docs" id="docs"></div>
      </div>
    </div>
    <div class="card">
      <h3>⚙️ 当前参数</h3>
      <div class="body params" id="params"></div>
    </div>
  </aside>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const msgs = $('msgs');
let busy = false;

function toast(msg, isErr){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(() => t.className = 'toast', 2600);
}
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function loadStatus(){
  const r = await fetch('/api/status');
  const d = await r.json();
  const isReal = d.mode === 'real';
  $('modeTag').textContent = isReal ? '● 真实模型模式' : '○ 离线演示模式';
  $('modeTag').className = 'tag' + (isReal ? '' : ' off');
  $('modeTag').title = isReal ? `${d.embed_model} / ${d.chat_model}` : '未检测到模型密钥，使用本地向量 + 抽取式作答';
  $('idxTag').textContent = `索引：${d.index.doc_count} 文档 / ${d.index.chunk_count} 片段`;
  $('s-doc').textContent = d.index.doc_count;
  $('s-chunk').textContent = d.index.chunk_count;
  $('s-char').textContent = (d.index.char_count || 0).toLocaleString();
  const p = d.params;
  $('params').innerHTML = [
    `切片 <code>${p.chunk_size}</code> 字符 / 重叠 <code>${p.chunk_overlap}</code>`,
    `召回 <code>向量+BM25 各 ${p.recall_k}</code> → RRF(<code>k=${p.rrf_k}</code>)`,
    `最终上下文 <code>Top ${p.final_top_k}</code>`,
    `拒答阈值 <code>${p.min_score}</code>`,
    `模型 <code>${isReal ? d.chat_model : 'extractive'}</code>`
  ].join('<br>');
  document.querySelectorAll('.sug').forEach(e => e.remove());
  if (d.index.doc_count === 0){
    const sug = $('sugs');
    ['差旅住宿标准是多少？','请假需要提前几天申请？','报销流程要走哪几步？'].forEach(q => {
      const el = document.createElement('div');
      el.className = 'sug'; el.textContent = q;
      el.onclick = () => { $('q').value = q; ask(); };
      sug.appendChild(el);
    });
  }
  loadDocs();
}

async function loadDocs(){
  const r = await fetch('/api/docs');
  const d = await r.json();
  const box = $('docs');
  if (!d.data.length){ box.innerHTML = '<div style="color:var(--sub);font-size:12px">暂无文档，点击「上传文档」或「导入示例」</div>'; return; }
  box.innerHTML = d.data.map(x => `
    <div class="doc">
      <span class="n" title="${esc(x.file_name)}">${esc(x.file_name)}</span>
      <span class="c">${x.chunk_count}片</span>
      <span class="x" onclick="delDoc('${esc(x.file_name)}')" title="删除">✕</span>
    </div>`).join('');
}

function renderSources(srcs){
  if (!srcs || !srcs.length) return '';
  const items = srcs.map((s,i) => `
    <div class="src">
      <div class="src-head">
        <span class="src-name">[来源${i+1}] ${esc(s.file_name)}</span>
        <span>第${s.chunk_index+1}段</span>
        <span class="meter">
          <span>向量 #${s.vec_rank ?? '-'}</span>
          <span>BM25 #${s.bm25_rank ?? '-'}</span>
          <span>融合 ${s.rrf_score}</span>
        </span>
      </div>
      <div class="src-text">${esc(s.text)}</div>
    </div>`).join('');
  return `<span class="src-toggle" onclick="this.nextElementSibling.classList.toggle('open')">📎 查看 ${srcs.length} 条引用依据 ▾</span>
          <div class="srcs">${items}</div>`;
}

function addUser(text){
  const empty = msgs.querySelector('.empty'); if (empty) empty.remove();
  msgs.insertAdjacentHTML('beforeend', `<div class="row user"><div class="bubble">${esc(text)}</div><div class="avatar u">我</div></div>`);
  msgs.scrollTop = msgs.scrollHeight;
}
function addBot(text, srcs, hint){
  msgs.insertAdjacentHTML('beforeend', `
    <div class="row bot"><div class="avatar b">AI</div>
      <div class="bubble">${esc(text)}${srcs ? renderSources(srcs) : ''}${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</div>
    </div>`);
  msgs.scrollTop = msgs.scrollHeight;
}
function addLoading(){
  msgs.insertAdjacentHTML('beforeend', `<div class="row bot" id="loading"><div class="avatar b">AI</div>
    <div class="bubble"><span class="loading"><span class="dot"></span><span class="dot"></span><span class="dot"></span> 正在检索知识库…</span></div></div>`);
  msgs.scrollTop = msgs.scrollHeight;
}

async function ask(){
  const q = $('q').value.trim();
  if (!q || busy) return;
  busy = true; $('send').disabled = true; $('latency').textContent = '';
  addUser(q); $('q').value = ''; addLoading();
  const t0 = performance.now();
  try{
    const r = await fetch('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query:q})});
    const d = (await r.json()).data;
    document.getElementById('loading')?.remove();
    const hint = d.rejected ? '未达到相关度阈值，系统选择拒答而不是编造' :
                 (d.mode === 'offline' ? '离线演示模式：抽取式作答' : `模型：${d.mode} · 相关度 ${d.confidence}`);
    addBot(d.answer, d.sources, hint);
    $('latency').textContent = `耗时 ${Math.round(performance.now()-t0)} ms · 命中 ${d.sources.length} 段 · 相关度 ${d.confidence}`;
  }catch(e){
    document.getElementById('loading')?.remove();
    addBot('请求失败，请检查服务是否正常：' + e.message);
  }finally{
    busy = false; $('send').disabled = false;
  }
}

$('send').onclick = ask;
$('q').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); ask(); }
});

async function upload(file){
  const fd = new FormData(); fd.append('file', file);
  toast('正在解析并建立索引…');
  const r = await fetch('/api/upload', {method:'POST', body: fd});
  const d = await r.json();
  if (r.ok){ toast(`✅ ${d.data.file_name} 入库成功，共 ${d.data.chunk_count} 片段`); loadStatus(); }
  else { toast('❌ ' + d.msg, true); }
}
$('file').onchange = e => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ''; };

async function ingestDir(){
  toast('正在导入示例文档…');
  const r = await fetch('/api/ingest_dir', {method:'POST'});
  const d = (await r.json()).data;
  toast(`✅ 成功 ${d.success.length} 个，失败 ${d.failed.length} 个`);
  loadStatus();
}
async function delDoc(name){
  if (!confirm(`确认删除「${name}」？`)) return;
  await fetch('/api/docs/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({file_name:name})});
  toast('已删除'); loadStatus();
}
function clearChat(){
  msgs.innerHTML = `<div class="empty"><b>对话已清空</b>继续提问即可，知识库内容不受影响</div>`;
  $('latency').textContent = '';
}
loadStatus();
</script>
</body>
</html>
"""
