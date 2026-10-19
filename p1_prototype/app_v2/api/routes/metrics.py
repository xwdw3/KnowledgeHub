"""监控接口：Prometheus 抓取端点、JSON 快照、缓存管理、内置看板。

安全说明（P3 收敛后）：
  /metrics **不要求 JWT**，但要求独立的抓取令牌（METRICS_TOKEN）：
  抓取器只会带静态 header，没法先登录再刷新 token。
  用独立令牌而不是复用 JWT，是因为抓取令牌只读、可单独轮换、
  泄露后影响面仅限"能看指标"，不会顺带拿到业务数据权限。
  纯内网调试可设 METRICS_PROTECTED=false 完全放开。

  /metrics/snapshot 属敏感数据（含接口路径、错误分布），**必须 JWT + metric:read**。

  /dashboard 是空壳 HTML（不含任何数据），数据由浏览器携带 Bearer 自行拉取；
  令牌用 fragment（#key=...）传入，fragment 不会进入服务器日志与 Referer。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app_v2.api.deps import require_permission
from app_v2.core.metrics import metrics_registry
from app_v2.core.security import PERM_KB_ADMIN, PERM_METRIC_READ
from app_v2.schemas.models import CacheInvalidateRequest, MetricSnapshotData
from app_v2.services import audit_service, cache_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["监控"])


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=True,
            summary="Prometheus 指标",
            description="标准 Prometheus 文本格式，供抓取器采集。\n\n"
                        "**不走 JWT**（抓取器无法执行登录流程），改用独立抓取令牌："
                        "`Authorization: Bearer $METRICS_TOKEN`。"
                        "若 METRICS_PROTECTED=false 则完全免认证（仅建议纯内网调试用）。")
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(metrics_registry.render_prometheus(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/metrics/snapshot", response_model=MetricSnapshotData, summary="指标快照（JSON）",
            description="结构化指标，供前端看板或外部系统读取。需要 `metric:read` 权限。")
def metrics_snapshot(principal: Principal = Depends(require_permission(PERM_METRIC_READ))) -> MetricSnapshotData:
    return MetricSnapshotData(**metrics_registry.snapshot())


@router.get("/metrics/cache", summary="缓存健康与命中率",
            description="返回 L1/L2 缓存容量、Redis 状态与分层命中率。需要 `metric:read` 权限。")
def cache_health(principal: Principal = Depends(require_permission(PERM_METRIC_READ))) -> Dict[str, Any]:
    return cache_service.cache.health()


@router.post("/metrics/cache/invalidate",
             summary="主动失效缓存",
             description="按类别清除检索/问答缓存；为空表示全清。文档变更后系统会自动失效，"
                         "此接口用于人工干预（如外部数据同步）。需要 `kb:admin` 权限。")
def invalidate_cache(req: CacheInvalidateRequest, request: Request,
                     principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    result = cache_service.cache.invalidate(req.kinds)
    audit_service.record(
        action="cache.invalidate",
        actor=principal.actor, role=principal.role,
        resource="cache",
        ip=getattr(request.state, "client_ip", ""),
        detail={"kinds": req.kinds, **result},
    )
    return result


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    """内置轻量看板：单文件、无外部依赖，断网环境也能看。"""
    return HTMLResponse(content=DASHBOARD_HTML)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>企业知识库 · 运行看板</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#262b35; --text:#e6e9ef;
    --muted:#8b93a1; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4a9eff;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f7f9; --panel:#fff; --line:#e3e6ea; --text:#1b1f24;
            --muted:#656d76; --accent:#0969da; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  h1 { font-size:20px; margin:0 0 4px; font-weight:600; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .card .k { color:var(--muted); font-size:12px; margin-bottom:8px; letter-spacing:.02em; }
  .card .v { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; }
  .card .h { color:var(--muted); font-size:11px; margin-top:6px; }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
  .section { margin-top:26px; }
  .section h2 { font-size:14px; font-weight:600; margin:0 0 12px; color:var(--muted);
                text-transform:uppercase; letter-spacing:.06em; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  td.n { font-variant-numeric:tabular-nums; }
  .bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  .pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
          background:var(--line); color:var(--muted); }
  .err { color:var(--bad); font-size:13px; }
</style>
</head>
<body>
<h1>企业知识库 · 运行看板</h1>
<div class="sub">数据每 3 秒自动刷新 <span id="ts"></span></div>

<div class="grid" id="cards"></div>

<div class="section">
  <h2>延迟分布（毫秒）</h2>
  <div class="grid" id="lat"></div>
</div>

<div class="section">
  <h2>接口调用 TOP</h2>
  <table><thead><tr><th>方法</th><th>路径</th><th>状态</th><th class="n">次数</th></tr></thead>
  <tbody id="paths"></tbody></table>
</div>

<script>
const fmt = n => (n===undefined||n===null) ? '-' : Number(n).toLocaleString();
const pct = n => (n===undefined||n===null) ? '-' : (Number(n)*100).toFixed(1)+'%';

// 密钥通过 URL fragment 传入：/dashboard#key=rag_xxx
// fragment 不会随请求发送到服务器，也不会出现在 Referer 与访问日志里。
function readKey() {
  const m = /(?:^|[#&])key=([^&]+)/.exec(location.hash || '');
  if (m) { try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; } }
  return localStorage.getItem('rag_key') || '';
}
function saveKey(k) { try { localStorage.setItem('rag_key', k); } catch (e) {} }

function needAuth(msg) {
  document.getElementById('cards').innerHTML =
    `<div class="card" style="grid-column:1/-1">
       <div class="k">需要授权</div>
       <div class="err" style="margin-bottom:12px">${msg}</div>
       <input id="k" placeholder="粘贴 API Key（rag_ 开头）"
              style="width:100%;padding:9px 11px;border-radius:7px;border:1px solid var(--line);
                     background:var(--bg);color:var(--text);font-size:13px"/>
       <button onclick="const v=document.getElementById('k').value.trim();
                        if(v){saveKey(v);location.reload();}"
               style="margin-top:10px;padding:8px 16px;border-radius:7px;border:1px solid var(--accent);
                      background:transparent;color:var(--accent);cursor:pointer;font-size:13px">
         保存并加载</button>
     </div>`;
}

function card(k, v, hint, cls) {
  return `<div class="card"><div class="k">${k}</div>
          <div class="v ${cls||''}">${v}</div>
          ${hint?`<div class="h">${hint}</div>`:''}</div>`;
}

async function load() {
  const key = readKey();
  if (!key) { needAuth('看板数据需要 metric:read 权限，请先提供 API Key。'); return; }
  try {
    const r = await fetch('/api/v1/metrics/snapshot', {
      headers: { 'Authorization': 'Bearer ' + key }
    });
    if (r.status === 401) { needAuth('API Key 无效或已吊销，请重新提供。'); return; }
    if (r.status === 403) { needAuth('该密钥缺少 metric:read 权限，请换用管理员或运维密钥。'); return; }
    if (!r.ok) { throw new Error('HTTP ' + r.status); }
    const m = (await r.json()).data || await r.json();
    const l = m.latency_ms || {};

    document.getElementById('cards').innerHTML =
      card('运行时长', Math.floor((m.uptime_sec||0)/3600)+'h',
           ((m.uptime_sec||0)%3600/60).toFixed(0)+' 分钟') +
      card('总请求', fmt(m.requests_total), '累计（进程启动至今）') +
      card('错误率', pct(m.error_rate), fmt(m.errors_total)+' 个 4xx/5xx',
           (m.error_rate||0) > 0.05 ? 'bad' : 'ok') +
      card('处理中', fmt(m.inflight), '当前并发') +
      card('问答总数', fmt((m.chat||{}).total), '拒答 '+fmt((m.chat||{}).rejected)) +
      card('问答缓存命中', pct((m.chat||{}).cache_hit_rate), '命中 '+fmt((m.chat||{}).cache_hit)) +
      card('检索缓存命中', pct((m.retrieve||{}).cache_hit_rate), '命中 '+fmt((m.retrieve||{}).cache_hit)) +
      card('LLM 调用', fmt((m.llm||{}).calls), '失败 '+fmt((m.llm||{}).failures),
           ((m.llm||{}).failure_rate||0) > 0.1 ? 'warn' : '') +
      card('认证失败', fmt((m.security||{}).auth_failures), '可能的密钥爆破',
           ((m.security||{}).auth_failures||0) > 0 ? 'warn' : '') +
      card('越权拦截', fmt((m.security||{}).forbidden), '403 次数') +
      card('慢请求', fmt(m.slow_requests), '超过阈值的请求',
           (m.slow_requests||0) > 0 ? 'warn' : '');

    document.getElementById('lat').innerHTML =
      card('P50', (l.p50||0)+'', '中位数') +
      card('P90', (l.p90||0)+'', '') +
      card('P95', (l.p95||0)+'', '常用 SLA 指标') +
      card('P99', (l.p99||0)+'', '长尾') +
      card('平均', (l.avg||0)+'', '样本 '+fmt(l.samples));

    const rows = Object.entries(m.by_path || {})
      .sort((a,b) => b[1].count - a[1].count).slice(0, 12)
      .map(([k,v]) => {
        const [method, path] = k.split(' ');
        const cls = v.status.startsWith('2') ? 'ok' : (v.status.startsWith('4') ? 'warn' : 'bad');
        return `<tr><td><span class="pill">${method}</span></td>
                <td><code>${path}</code></td>
                <td class="${cls}">${v.status}</td>
                <td class="n">${fmt(v.count)}</td></tr>`;
      }).join('');
    document.getElementById('paths').innerHTML = rows || '<tr><td colspan="4">暂无数据</td></tr>';

    document.getElementById('ts').textContent = '· ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('cards').innerHTML =
      `<div class="card"><div class="k">加载失败</div><div class="err">${e}</div></div>`;
  }
}
load();
setInterval(load, 3000);
</script>
</body>
</html>"""
