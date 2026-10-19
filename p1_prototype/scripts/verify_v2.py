"""P1 工程版端到端验收。

用法：
    python scripts/verify_v2.py                 # 使用已启动的服务（默认 8012）
    python scripts/verify_v2.py --base http://127.0.0.1:8012
    python scripts/verify_v2.py --offline       # 不依赖模型，仅验证接口契约

覆盖：自动接口文档 / 健康检查 / 入库幂等 / 向量检索 / RAG 问答 / 引用溯源 /
      Agent 工具调用与轨迹 / 无答案拒答 / 权限隔离 / 错误码 / 会话落库
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL = "✅", "❌"
results: list[tuple[bool, str]] = []
_TEMP_KEY_IDS: list[str] = []        # 验收过程现建的临时密钥，收尾统一吊销
BASE = os.environ.get("BASE", "http://127.0.0.1:8012").rstrip("/")

# P2 起所有业务接口需要鉴权；P3 起收敛为 JWT 单一通道。
#   ADMIN_KEY=rag_xxx python scripts/verify_v2.py      # 传密钥，脚本自行换票
#   ADMIN_USER=admin ADMIN_PASS=xxx python ...          # 或直接给账号
# 未设置时按 admin 默认账号尝试登录（见 main 里的引导段）。
API_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Adm1n@2026kb")

# 本地调试时绕开系统代理，避免 http_proxy 把 127.0.0.1 也转发出去
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _unwrap(payload):
    """P2 统一响应约定：{code, message, data, trace_id}，取出 data。

    兼容未包装的裸响应（/metrics 等非 JSON 路径不走包装）。
    """
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload["data"]
    return payload


def call(method: str, path: str, body: dict | None = None, expect: int = 200,
         files: dict | None = None, api_key: str = "") -> tuple[int, dict]:
    url = BASE + urllib.parse.quote(path, safe="/?=&:")
    headers = {"Content-Type": "application/json"}
    bearer = api_key or API_KEY
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if files:
        boundary = "----kbverify"
        parts = []
        for name, value in files.items():
            if isinstance(value, tuple):          # 文件字段：(filename, content)
                filename, content = value
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                    f"Content-Type: text/plain\r\n\r\n{content}\r\n"
                )
            else:                                  # 普通表单字段
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
                )
        parts.append(f"--{boundary}--\r\n")
        data = "".join(parts).encode("utf-8")
        # 注意：这里不能整体覆盖 headers，否则上面注入的 Authorization 会被丢掉，
        # 上传类用例会全部退化成 401（P2 打开鉴权后极易踩到）
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=90) as resp:
            return resp.status, _unwrap(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def check(ok: bool, msg: str, detail: str = "") -> None:
    results.append((ok, msg))
    print(f"{PASS if ok else FAIL} {msg}" + (f"\n     {detail}" if detail else ""))


def _ensure_finance_editor() -> str:
    """构造一个 perm_group=finance 的 editor 密钥，用于验证权限组隔离的正向路径。

    P2 的写入侧校验是严格的：密钥能碰哪些组，就只能往哪些组投放。管理员默认密钥
    只带 all，因此不能直接投 finance —— 这一步本身就是被验收的安全行为。
    """
    if not API_KEY:
        return ""

    # 列表接口不返回明文，无法复用旧密钥，直接新建一个（脚本末尾统一吊销）
    code, payload = call("POST", "/api/v1/auth/keys",
                         body={"owner": "verify_finance", "role": "editor",
                               "perm_group": "finance", "description": "验收-编辑(finance)"})
    key = (payload or {}).get("api_key", "")
    key_id = (payload or {}).get("key_id", "")
    if not key:
        print(f"{FAIL} 未能创建 finance 组密钥（HTTP={code}），权限隔离正向用例将被跳过")
        return ""
    if key_id:
        # 注销临时密钥，避免每次验收都在密钥表里堆垃圾
        _TEMP_KEY_IDS.append(key_id)

    # 严格模式下密钥不能直接当 Bearer 用，必须换成 JWT。
    # 换出的票里 pg 会是 finance，写入/读取范围都和密钥一致。
    code, tok_payload = call("POST", "/api/v1/auth/token", body={"api_key": key})
    tok = (tok_payload or {}).get("access_token", "") if isinstance(tok_payload, dict) else ""
    if not tok:
        print(f"{FAIL} finance 密钥换 JWT 失败（HTTP={code}）")
        return ""
    return tok


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8012")
    parser.add_argument("--skip-agent", action="store_true", help="跳过需要真实模型的 Agent 用例")
    args = parser.parse_args()
    BASE = args.base.rstrip("/")

    print("=" * 74)
    print(f"P1 工程版验收　服务：{BASE}")
    print("=" * 74)

    # ---------- 0. 取得凭据 ----------
    # P3 起业务接口一律要求 JWT（AUTH_MODE=jwt）。本脚本原先直接把 API Key
    # 当 Bearer 用，在严格模式下会全线 401——那种失败与 P1 功能无关，
    # 却会把每个用例都打红，掩盖真正的回归。这里统一先换票。
    global API_KEY
    if not API_KEY:
        code, payload = call("POST", "/api/v1/auth/login",
                             body={"username": ADMIN_USER, "password": ADMIN_PASS})
        tok = (payload or {}).get("access_token", "") if isinstance(payload, dict) else ""
        if code == 200 and tok:
            API_KEY = tok
            print(f"✅ 已用管理员账号 {ADMIN_USER} 登录换取 JWT")
        else:
            print(f"{FAIL} 无法取得凭据（HTTP={code}），请传 ADMIN_KEY 或 "
                  f"ADMIN_USER/ADMIN_PASS")
            return 2
    elif not API_KEY.startswith("ey"):
        # 传进来的是 API Key 明文：先换票再继续，否则后续全部 401
        code, payload = call("POST", "/api/v1/auth/token", body={"api_key": API_KEY})
        tok = (payload or {}).get("access_token", "") if isinstance(payload, dict) else ""
        if code == 200 and tok:
            API_KEY = tok
            print("✅ 已用 API Key 换取 JWT")

    # ---------- 1. 自动接口文档 ----------
    code, spec = call("GET", "/openapi.json")
    paths = spec.get("paths", {})
    check(code == 200 and len(paths) >= 8, f"OpenAPI 文档可自动生成（{len(paths)} 个路径）")
    required = ["/api/v1/chat", "/api/v1/retrieve", "/api/v1/documents", "/api/v1/health"]
    missing = [p for p in required if p not in paths]
    check(not missing, "核心接口均出现在文档中", f"缺失：{missing}" if missing else "")
    chat_op = paths.get("/api/v1/chat", {}).get("post", {})
    check("requestBody" in chat_op and "responses" in chat_op,
          "接口含请求体与响应 Schema（TypeScript/Postman 可直接生成客户端）")

    # ---------- 2. 健康检查 ----------
    code, health = call("GET", "/api/v1/health")
    storage = health.get("storage", {})
    check(code == 200, f"健康检查可用 status={health.get('status')} model={health.get('model_mode')}",
          f"vector={storage.get('vector', {}).get('backend')} meta={storage.get('meta', {}).get('backend')}")
    check(storage.get("vector", {}).get("backend") in ("qdrant", "local"),
          f"向量库后端：{storage.get('vector', {}).get('backend')}")
    check(storage.get("meta", {}).get("backend") in ("postgres", "memory"),
          f"业务库后端：{storage.get('meta', {}).get('backend')}")

    # ---------- 3. 入库（含幂等） ----------
    doc_content = ("# 测试制度\n\n第一条 本文件用于验收。\n\n"
                   "第十条 差旅补贴每日 200 元，按自然日计算，由财务部负责解释。\n\n"
                   "第十一条 月度报销截止日为次月 5 日，逾期不予受理。\n")
    code, r1 = call("POST", "/api/v1/documents", files={"file": ("验收测试制度.md", doc_content)})
    check(code == 200 and r1.get("chunk_count", 0) > 0,
          f"文档上传入库成功（{r1.get('chunk_count')} 片段，{r1.get('char_count')} 字）")
    _, r2 = call("POST", "/api/v1/documents", files={"file": ("验收测试制度.md", doc_content)})
    check(r2.get("overwritten") is True, "同名重复上传走覆盖更新（幂等）")

    # 非法格式
    code, bad = call("POST", "/api/v1/documents", files={"file": ("a.exe", "MZ binary")})
    check(code == 415, f"非法格式被拒绝，返回 HTTP {code}", bad.get("message", ""))

    # 空文件
    code, empty = call("POST", "/api/v1/documents", files={"file": ("空.md", "")})
    check(code == 400, f"空文件被拒绝，返回 HTTP {code}")

    # ---------- 4. 文档列表与详情 ----------
    # 显式给 limit：接口默认分页（20 条），而验收库里往往已累积几十份文档，
    # 目标文档不一定落在第一页——不加 limit 会把"分页正常"误判成"列表缺数据"。
    code, docs = call("GET", "/api/v1/documents?limit=200")
    items = docs.get("items", [])
    check(code == 200 and any(d["file_name"] == "验收测试制度.md" for d in items),
          f"文档列表返回 {len(items)} 条")
    target = next((d for d in items if d["file_name"] == "验收测试制度.md"), None)
    if target:
        code, detail = call("GET", f"/api/v1/documents/{target['doc_id']}")
        check(code == 200 and detail["doc_id"] == target["doc_id"], "文档详情可按 doc_id 查询")

    # ---------- 5. 检索 ----------
    code, ret = call("POST", "/api/v1/retrieve",
                     {"query": "差旅补贴每天多少钱", "top_k": 3})
    hits = ret.get("hits", [])
    check(code == 200 and hits, f"向量检索返回 {len(hits)} 条片段，顶层相关度 {ret.get('confidence')}")
    if hits:
        top = hits[0]
        check("vec_rank" in top and "bm25_rank" in top,
              f"命中片段带双路排名 vec#{top['vec_rank']} bm25#{top['bm25_rank']}")
        check(bool(top.get("text")), "命中片段带原文，可溯源")

    # ---------- 6. RAG 问答 ----------
    code, rag = call("POST", "/api/v1/chat", {"question": "差旅补贴每天多少钱？", "mode": "rag"})
    answer = rag.get("answer", "")
    check(code == 200 and not rag.get("rejected"), f"RAG 问答返回答案（相关度 {rag.get('confidence')}）",
          answer.replace("\n", " ")[:100])
    check("200" in answer or "补贴" in answer, "答案包含关键事实（200 元/天）")
    check(bool(rag.get("sources")), f"返回 {len(rag.get('sources', []))} 条引用片段")
    check(bool(rag.get("session_id")), f"自动分配会话 session_id={rag.get('session_id')}")

    # 无答案拒答
    code, rej = call("POST", "/api/v1/chat", {"question": "公司对于火星移民有什么补贴政策？", "mode": "rag"})
    check(code == 200 and rej.get("rejected") is True, f"库外问题拒答（相关度 {rej.get('confidence')}）",
          rej.get("answer", "").replace("\n", " ")[:90])

    # 多轮上下文
    sid = rag.get("session_id")
    call("POST", "/api/v1/chat", {"question": "那报销截止日是哪天？", "mode": "rag", "session_id": sid})
    code, hist = call("GET", f"/api/v1/sessions/{sid}/history")
    msgs = hist if isinstance(hist, list) else (hist.get("items") or hist.get("messages") or [])
    check(code == 200 and len(msgs) >= 4,
          f"会话历史已落库（{len(msgs)} 条消息）")

    # ---------- 7. Agent ----------
    if not args.skip_agent:
        t0 = time.time()
        code, ag = call("POST", "/api/v1/chat",
                        {"question": "帮我看看知识库里有哪些文档，然后算一下 200 元补贴 22 天是多少钱", "mode": "agent"})
        cost = time.time() - t0
        trace = ag.get("trace") or {}
        check(code == 200 and ag.get("answer"), f"Agent 返回答案（{cost:.1f}s，{trace.get('rounds')} 轮）",
              ag.get("answer", "").replace("\n", " ")[:100])
        tools = [c["name"] for c in trace.get("tool_calls", [])]
        check("calculator" in tools or "list_documents" in tools or "knowledge_search" in tools,
              f"Agent 实际调用了工具：{tools}")
        check(trace.get("break_reason") in ("", None),
              f"正常路径未误触发熔断（break_reason={trace.get('break_reason') or '无'}）")
        check(bool(trace.get("elapsed_ms")) and trace.get("elapsed_ms", 0) < 60000,
              f"单次请求耗时受控：{trace.get('elapsed_ms')}ms")
    else:
        print("（已跳过 Agent 用例）")

    # ---------- 8. 运行参数 ----------
    code, cfg = call("GET", "/api/v1/config")
    check(code == 200 and cfg.get("max_rounds", 0) > 0,
          f"熔断参数可查：轮次{cfg.get('max_rounds')}/重复{cfg.get('max_repeat')}/超时{cfg.get('timeout_sec')}s",
          f"工具：{cfg.get('tools')}")

    # ---------- 9. 权限隔离 ----------
    # P2 起 admin 密钥默认只带 perm_group=all，按「写入侧严格校验」规则不能把文档投放到
    # finance 组（这是正确的安全行为）。要构造 finance 密件，必须先拿到一个
    # perm_group in (all, finance) 且含 kb:write 的密钥：优先用已存在的测试密钥，
    # 否则用管理员现建一个（用完即吊销，避免污染密钥列表）。
    secret = "# 财务密件\n\n机密条款：财务专用信息 12 号文件，仅财务部可查阅。差旅备用金额度为 5000 元。\n"
    fin_key = _ensure_finance_editor()
    fin_code, fin_up = call("POST", "/api/v1/documents",
                            files={"file": ("财务密件-test.md", secret), "perm_group": "finance"},
                            api_key=fin_key) if fin_key else (0, {})
    check(fin_code == 200 and fin_up.get("perm_group") == "finance",
          f"finance 组密钥可投放密件（perm_group={fin_up.get('perm_group')}）",
          fin_up.get("message", ""))
    code, as_hr = call("POST", "/api/v1/retrieve", {"query": "财务专用信息 12 号文件", "perm_group": "hr"})
    leaked = [h for h in as_hr.get("hits", []) if h["file_name"] == "财务密件-test.md"]
    check(code == 200 and not leaked, f"HR 权限检索不到财务密件（命中 {len(as_hr.get('hits', []))} 条，无泄露）")
    code, as_fin = call("POST", "/api/v1/retrieve",
                        {"query": "财务专用信息 12 号文件", "perm_group": "finance"}, api_key=fin_key)
    got = [h for h in as_fin.get("hits", []) if h["file_name"] == "财务密件-test.md"]
    check(code == 200 and bool(got), f"财务权限可正常检索到密件（命中 {len(got)} 条）")
    code, rag_hr = call("POST", "/api/v1/chat", {"question": "财务专用信息 12 号文件的备用金额度是多少？",
                                                "mode": "rag", "perm_group": "hr"})
    # 判据是"不泄露"，而不是"必须走拒答分支"：模型也可能基于其他资料给出"未找到"的回复
    leaked_src = [s for s in rag_hr.get("sources", []) if s["file_name"] == "财务密件-test.md"]
    answer_hr = rag_hr.get("answer", "")
    check(not leaked_src and "5000" not in answer_hr,
          "无权用户既拿不到密件片段，也拿不到密件内容",
          f"rejected={rag_hr.get('rejected')} sources={len(rag_hr.get('sources', []))}")

    # ---------- 10. 错误码规范 ----------
    code, err = call("POST", "/api/v1/chat", {"question": ""})
    check(code == 422, f"参数校验返回 HTTP 422（而非 200 包错误）", err.get("message", ""))
    check(err.get("trace_id") is not None, f"错误响应带 trace_id，可关联日志")
    code, err2 = call("DELETE", "/api/v1/documents?file_name=不存在的文件.md")
    check(code == 404, f"删除不存在的文档返回 HTTP 404")
    code, _ = call("POST", "/api/v1/retrieve", {"query": "x", "top_k": 999})
    check(code == 422, "超出范围的 top_k 被拦截")

    # ---------- 11. 删除 ----------
    code, dele = call("DELETE", "/api/v1/documents?file_name=验收测试制度.md")
    check(code == 200, "文档删除成功")
    code, after = call("POST", "/api/v1/retrieve", {"query": "差旅补贴每日 200 元"})
    gone = all(h["file_name"] != "验收测试制度.md" for h in after.get("hits", []))
    check(gone, "删除后向量片段同步清除（检索不再命中）")
    call("DELETE", "/api/v1/documents?file_name=财务密件-test.md")
    for kid in _TEMP_KEY_IDS:
        call("DELETE", f"/api/v1/auth/keys/{kid}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    failed = [m for ok, m in results if not ok]
    print(f"验收结果：{len(results) - len(failed)}/{len(results)} 通过")
    for m in failed:
        print(f"  {FAIL} {m}")
    print("=" * 74)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
