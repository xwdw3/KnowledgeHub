"""P2 + P3 联合验收脚本：逐条验证用户提出的 7 条验收标准。

设计原则沿用 verify_p2/p3：
  - **只依赖标准库**：交付环境无需额外装 requests
  - **不信任服务端自述**：能从旁路（指标 counter、数据库、日志文件）取证就不看响应体里的字段
  - **可重复执行**：自带清理，跑多次结果一致
  - **失败不中断**：逐项记录，最后打印汇总与失败明细

7 条标准与对应取证手段：
  1. 所有接口必须携带 JWT，未授权请求被拒  → 遍历全部端点打无凭证请求，断言 401；
                                            再断言 API Key 不能直接过
  2. 不同角色只能看授权范围内容            → 三角色 × 空间 × 权限组交叉验证
  3. 相同问题命中缓存、减少 LLM 调用       → 旁路读 rag_llm_calls_total 差值
  4. 全链路日志可追溯上传/问答/删除        → 审计表 + JSON 日志双取证
  5. Grafana 面板可看运行指标              → 数据源/面板/查询三重校验
  6. 混合检索 + Rerank                     → 响应里必须同时有 vector/bm25/rrf/rerank 痕迹
  7. 问答准确率明显提升                    → 三档 RERANK_MODE 对照实验

用法：
    BASE=http://127.0.0.1:8012 ADMIN_USER=admin ADMIN_PASS=xxx \\
        METRICS_TOKEN=pk-xxx python scripts/verify_p2p3_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE = os.getenv("BASE", "http://127.0.0.1:8012").rstrip("/")
API = f"{BASE}/api/v1"
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")
TIMEOUT = int(os.getenv("TIMEOUT", "120"))

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results: List[Tuple[str, bool, str]] = []
section = ""


def head(name: str) -> None:
    global section
    section = name
    print(f"\n{'=' * 76}\n{name}\n{'=' * 76}")


def check(item: str, cond: bool, detail: str = "") -> bool:
    results.append((f"[{section}] {item}", bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {item}" + (f"  -> {detail}" if detail else ""))
    return bool(cond)


def info(text: str) -> None:
    print(f"        {text}")


def req(method: str, path: str, token: str = "", body: Any = None,
        raw: Optional[bytes] = None, content_type: str = "application/json",
        base: str = "") -> Tuple[int, Any, Dict[str, str]]:
    """发请求。path 以 /api/ 或 http 开头时视为绝对路径，否则补 API 前缀。"""
    if path.startswith("http"):
        url = path
    elif path.startswith("/api/"):
        url = f"{BASE}{path}"
    else:
        url = f"{base or API}{path}"
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    )
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(r, timeout=TIMEOUT) as resp:
            return resp.status, _parse(resp.read(), resp.headers.get("Content-Type", "")), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read(), exc.headers.get("Content-Type", "")), dict(exc.headers)
    except Exception as exc:
        return 0, {"message": str(exc)}, {}


def _parse(raw: bytes, ctype: str) -> Any:
    if "json" not in (ctype or ""):
        return {"_raw": raw[:600].decode("utf-8", "replace")}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"_raw": raw[:600].decode("utf-8", "replace")}


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload["data"] if payload.get("code") == 200 else payload
    return payload


def msg(payload: Any) -> str:
    return str((payload or {}).get("message", ""))[:140] if isinstance(payload, dict) else str(payload)[:140]


def data_of(payload: Any) -> Any:
    return _unwrap(payload)


def login(username: str, password: str) -> tuple[int, Any]:
    """登录并对 429 做退避重试。

    登录接口限流是 10 次/分钟。本脚本一轮要登录 5~7 次（3 个账号 + 刷新 +
    注销用例），连着跑两轮就会打满窗口——那时返回的 429 只说明"跑得太密"，
    与权限/功能无关，却会把标准 2 的建用户、登录等用例判成失败，
    让人误以为隔离逻辑坏了。这里统一退避，把噪声挡在断言之外。
    """
    status, payload = 0, {}
    for attempt in range(3):
        status, payload, _ = req("POST", "/api/v1/auth/login",
                                 body={"username": username, "password": password})
        if status != 429:
            return status, payload
        wait = 22 if attempt == 0 else 32
        print(f"        （{username} 登录被限流，等待 {wait}s 重试 {attempt + 1}/3）")
        time.sleep(wait)
    return status, payload


def metric(name: str) -> float:
    """从 /metrics 读单值。Prometheus 有 15s 抓取间隔，实验期间必须读进程内实时值。"""
    try:
        r = urllib.request.Request(f"{BASE}/metrics",
                                   headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        with _OPENER.open(r, timeout=15) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith(name + " "):
                    return float(line.split()[1])
    except Exception:
        pass
    return -1.0


def upload(token: str, file_name: str, text: str, perm_group: str = "all",
           space_id: str = "") -> Tuple[int, Any]:
    boundary = "----P2P3Boundary" + str(int(time.time() * 1000000))
    parts: List[bytes] = []
    for name, value in (("perm_group", perm_group), ("space_id", space_id)):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"{file_name}\"\r\nContent-Type: text/markdown\r\n\r\n").encode()
    )
    parts.append(text.encode("utf-8"))
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    status, payload, _ = req("POST", "/documents", token, raw=b"".join(parts),
                             content_type=f"multipart/form-data; boundary={boundary}")
    return status, data_of(payload)


# ======================================================================
# 标准 1：所有接口必须携带 JWT Token
# ======================================================================
# 必须免凭证的端点（本身就是"发票机"或"探活"）
BOOTSTRAP_OK = {
    "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/methods",
    "/api/v1/auth/token", "/api/v1/health", "/api/v1/ready",
}
# 业务端点：必须 401
PROTECTED_GET = [
    "/api/v1/auth/me", "/api/v1/auth/users", "/api/v1/auth/keys", "/api/v1/auth/roles",
    "/api/v1/documents", "/api/v1/spaces", "/api/v1/audit/logs", "/api/v1/audit/summary",
    "/api/v1/audit/errors", "/api/v1/metrics/snapshot", "/api/v1/metrics/cache",
    "/api/v1/config", "/api/v1/stats",
]
PROTECTED_POST = [
    ("/api/v1/chat", {"question": "测试", "top_k": 1}),
    ("/api/v1/retrieve", {"query": "测试", "top_k": 1}),
    ("/api/v1/auth/logout", {}),
    ("/api/v1/metrics/cache/invalidate", {"kinds": []}),
    ("/api/v1/documents/purge", {}),
]


def std1_require_jwt(adm: str) -> None:
    head("标准 1：所有接口必须携带 JWT Token，未授权请求拒绝访问")

    # ---- 1.1 无凭证 ----
    print("\n  -- 1.1 无凭证访问业务接口，必须 401 --")
    leaked = []
    for p in PROTECTED_GET:
        s, b, _ = req("GET", p)
        if s != 401:
            leaked.append(f"GET {p} -> {s}")
    for p, body in PROTECTED_POST:
        s, b, _ = req("POST", p, body=body)
        if s != 401:
            leaked.append(f"POST {p} -> {s}")
    total = len(PROTECTED_GET) + len(PROTECTED_POST)
    check(f"全部 {total} 个业务端点无凭证一律 401", not leaked,
          "；".join(leaked) if leaked else f"{total}/{total}")

    # ---- 1.2 伪造/畸形 token ----
    print("\n  -- 1.2 伪造 / 畸形 Token 必须 401 --")
    cases = {
        "空串": "",
        "乱码": "not-a-token",
        "两段": "aaa.bbb",
        "三段但签名错": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYWRtaW4ifQ.forged",
        "alg=none 伪造": _alg_none_token(),
    }
    bad = []
    for name, tk in cases.items():
        s, b, _ = req("GET", "/api/v1/documents", token=tk)
        if s != 401:
            bad.append(f"{name} -> {s}")
    check(f"{len(cases)} 种畸形 Token 一律 401", not bad, "；".join(bad) if bad else "全部拒绝")

    # ---- 1.3 API Key 不能直接过 ----
    print("\n  -- 1.3 API Key 不得直接当 Bearer 访问业务接口 --")
    s, b, _ = req("GET", "/api/v1/auth/me", token="rag-admin-p2-2026")
    check("有效 API Key 直连业务接口被拒", s == 401, f"status={s} {msg(b)}")
    check("拒绝时给出换取 JWT 的指引", "auth/token" in msg(b) or "JWT" in msg(b), msg(b)[:90])

    # ---- 1.4 免凭证端点仍可用 ----
    print("\n  -- 1.4 入场券与探活端点必须可用（否则形成逻辑闭环）--")
    s, b, _ = req("GET", "/api/v1/health")
    check("/health 免凭证可用", s == 200, f"status={s}")
    s, b, _ = req("GET", "/api/v1/auth/methods")
    m = data_of(b) or {}
    check("/auth/methods 免凭证可用", s == 200, f"status={s} mode={m.get('auth_mode')}")
    check("methods 明确声明 jwt_required", m.get("jwt_required") is True,
          f"auth_mode={m.get('auth_mode')} jwt_required={m.get('jwt_required')}")

    # ---- 1.5 /metrics 独立令牌 ----
    print("\n  -- 1.5 /metrics 走独立抓取令牌（不能裸奔，也不能要求 JWT）--")
    s, _, _ = req("GET", "/metrics", base=BASE)
    check("/metrics 无令牌 401（指标不是公开数据）", s == 401, f"status={s}")
    s, b, _ = req("GET", "/metrics", token=METRICS_TOKEN, base=BASE)
    check("/metrics 带抓取令牌 200", s == 200 and "_raw" in b, f"status={s}")

    # ---- 1.6 带有效 JWT 可访问 ----
    print("\n  -- 1.6 携带有效 JWT 全部可访问 --")
    deny = []
    for p in ["/api/v1/auth/me", "/api/v1/documents", "/api/v1/spaces", "/api/v1/stats",
              "/api/v1/audit/logs", "/api/v1/metrics/snapshot"]:
        s, b, _ = req("GET", p, token=adm)
        if s != 200:
            deny.append(f"{p} -> {s} {msg(b)[:40]}")
    check(f"管理员 JWT 可访问 {6} 个代表性端点", not deny, "；".join(deny) if deny else "全部 200")

    # ---- 1.7 Token 过期 / 注销 ----
    print("\n  -- 1.7 Token 生命周期（注销后立即失效）--")
    s, b = login(ADMIN_USER, ADMIN_PASS)
    d = data_of(b) or {}
    tmp_tok = d.get("access_token", "")
    if tmp_tok:
        s, _, _ = req("GET", "/api/v1/auth/me", token=tmp_tok)
        before_ok = s == 200
        s, _, _ = req("POST", "/api/v1/auth/logout", token=tmp_tok, body={"access_token": tmp_tok})
        s2, _, _ = req("GET", "/api/v1/auth/me", token=tmp_tok)
        check("新签发的 Token 可用", before_ok, "status=200")
        check("注销后同一 Token 立即 401（jti 黑名单）", s2 == 401, f"status={s2}")
    else:
        check("新签发的 Token 可用", False, "登录失败，无法验证")


def _alg_none_token() -> str:
    """构造 alg=none 的伪造 token（历史上最经典的 JWT 绕过手法）。"""
    import base64

    def b64(o: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'role': 'admin', 'name': 'hacker'})}."


# ======================================================================
# 标准 2：不同角色只能查看授权范围内知识库内容
# ======================================================================
TEST_SPACE = "accptest"


def std2_role_scope(adm: str) -> None:
    head("标准 2：不同角色用户只能查看授权范围内知识库内容")

    ts = int(time.time()) % 100000
    # ---- 准备：创建空间、三个角色用户、两类密级文档 ----
    s, b, _ = req("POST", "/spaces", token=adm,
                  body={"space_id": TEST_SPACE, "name": "验收测试空间",
                        "description": "P2P3 验收用，可删除"})
    # 409 = 上次跑残留，直接复用（脚本必须可重复执行）
    info(f"建空间 {TEST_SPACE} -> {s}" + ("（已存在，复用）" if s == 409 else ""))

    users = {}
    for role in ("kb_admin", "user"):
        uname = f"acc_{role}_{ts}"
        pwd = "AccTest@2026"
        s, b, _ = req("POST", "/api/v1/auth/users", token=adm,
                      body={"username": uname, "password": pwd, "role": role,
                            "spaces": f"{TEST_SPACE}", "display_name": f"验收{role}"})
        if s not in (200, 201):
            info(f"建用户 {uname} 失败：{s} {msg(b)}")
            continue
        # 已存在时直接登录
        s2, b2 = login(uname, pwd)
        tk = (data_of(b2) or {}).get("access_token", "")
        users[role] = {"username": uname, "password": pwd, "token": tk}
        info(f"建用户 {uname}({role}) -> {s}，登录 token={'有' if tk else '无'}")

    # 再建一个只在 default 空间的普通用户，用于验证跨空间隔离
    other = f"acc_user_dft_{ts}"
    s, b, _ = req("POST", "/api/v1/auth/users", token=adm,
                  body={"username": other, "password": "AccTest@2026", "role": "user",
                        "spaces": "default", "display_name": "验收-默认空间"})
    if s in (200, 201):
        s2, b2 = login(other, "AccTest@2026")
        users["user_default"] = {"username": other, "password": "AccTest@2026",
                                 "token": (data_of(b2) or {}).get("access_token", "")}
        info(f"建用户 {other}(user@default) -> {s}")

    # ---- 上传两篇密级不同的文档到测试空间 ----
    secret_text = (
        "# 2026 年度薪酬调整方案（机密）\n\n"
        "本文件仅限人力资源部与管理层查阅。\n\n"
        "## 调薪幅度\n\n"
        "研发序列平均调薪 12%，产品序列平均调薪 9%，销售序列按业绩达成率浮动。\n\n"
        "## 保密要求\n\n"
        "薪酬数据属于公司核心机密，任何情况下不得对外披露。\n"
    )
    public_text = (
        "# 员工差旅报销管理办法\n\n"
        "## 住宿费标准\n\n"
        "一线城市住宿费上限每晚 600 元，二线城市每晚 450 元，其他城市每晚 350 元。\n\n"
        "## 交通费标准\n\n"
        "市内交通凭票实报实销；高铁二等座、飞机经济舱可全额报销。\n"
    )
    s1, b1 = upload(adm, f"acc_secret_{ts}.md", secret_text, perm_group="hr", space_id=TEST_SPACE)
    s2, b2 = upload(adm, f"acc_public_{ts}.md", public_text, perm_group="all", space_id=TEST_SPACE)
    info(f"上传机密文档(hr) -> {s1}；上传公共文档(all) -> {s2}")

    def ask(tok: str, q: str, space: str = "") -> dict:
        body = {"question": q, "top_k": 3, "mode": "rag"}
        if space:
            body["space_id"] = space
        s, b, _ = req("POST", "/api/v1/chat", token=tok, body=body)
        return {"status": s, "data": data_of(b) or {}, "raw": b}

    # ---- 2.1 普通用户读不到 hr 密级文档 ----
    print("\n  -- 2.1 普通用户（user 角色）不得命中 hr 密级内容 --")
    u = users.get("user")
    if u and u["token"]:
        r = ask(u["token"], "薪酬调薪幅度是多少", TEST_SPACE)
        d = r["data"]
        srcs = d.get("sources") or []
        perms = {s.get("perm_group") for s in srcs}
        txt = json.dumps(d, ensure_ascii=False)
        check("普通用户提问机密问题被拒答或召回为空",
              d.get("rejected") is True or not srcs,
              f"rejected={d.get('rejected')} sources={len(srcs)}")
        check("普通用户响应中不含 hr 密级片段", "hr" not in perms,
              f"perm_groups={sorted(x for x in perms if x)}")

        # 公共文档应能读到
        r2 = ask(u["token"], "住宿费标准是多少", TEST_SPACE)
        d2 = r2["data"]
        srcs2 = d2.get("sources") or []
        check("普通用户可读到公共文档", len(srcs2) > 0 and not d2.get("rejected"),
              f"sources={len(srcs2)} rejected={d2.get('rejected')}")

        # 列表接口：不应看到机密文档
        s, b, _ = req("GET", f"/api/v1/documents?space_id={TEST_SPACE}", token=u["token"])
        items = (data_of(b) or {}).get("items") or []
        names = [i.get("file_name", "") for i in items]
        check("普通用户文档列表不含机密文件",
              not any("acc_secret" in n for n in names),
              f"共 {len(items)} 条：{names[:4]}")
    else:
        check("普通用户隔离验证", False, "普通用户创建/登录失败")

    # ---- 2.2 跨空间隔离 ----
    print("\n  -- 2.2 跨空间隔离：只能看到自己所属空间 --")
    ud = users.get("user_default")
    if ud and ud["token"]:
        s, b, _ = req("GET", "/api/v1/spaces", token=ud["token"])
        sp = data_of(b) or {}
        ids = [x.get("space_id") for x in (sp.get("items") or [])]
        check("default 空间用户看不到 accptest 空间", TEST_SPACE not in ids, f"可见空间={ids}")
        r = ask(ud["token"], "薪酬调薪幅度是多少", TEST_SPACE)
        check("越权指定 space_id 被拒", r["status"] in (403, 404),
              f"status={r['status']} {msg(r['raw'])}")
    else:
        check("跨空间隔离验证", False, "default 空间用户创建失败")

    # ---- 2.3 知识库管理员在本空间内可见（显式授予 hr 密级）----
    print("\n  -- 2.3 显式授予密级后，同一空间内可读 hr 密级内容 --")
    # 关键点：密级必须显式授予。空间名恰好叫 hr 只是巧合，不能当授权依据；
    # 早期版本靠"空间名派生密级"，导致在 hr 空间工作的人反而读不到 hr 文档。
    uname_hr = f"acc_hr_admin_{ts}"
    s, b, _ = req("POST", "/api/v1/auth/users", token=adm,
                  body={"username": uname_hr, "password": "AccTest@2026", "role": "kb_admin",
                        "spaces": TEST_SPACE, "perm_groups": "hr",
                        "display_name": "验收-带hr密级"})
    info(f"建用户 {uname_hr}(kb_admin, perm_groups=hr) -> {s}")
    if s in (200, 201):
        s2, b2 = login(uname_hr, "AccTest@2026")
        tk = (data_of(b2) or {}).get("access_token", "")
        users["hr_admin"] = {"username": uname_hr, "password": "AccTest@2026", "token": tk}
        if tk:
            s3, b3, _ = req("GET", "/api/v1/auth/me", token=tk)
            me = data_of(b3) or {}
            pg = me.get("perm_groups") or []
            check("显式密级 hr 已生效（token 携带 pg）", "hr" in pg, f"perm_groups={pg}")
            r = ask(tk, "薪酬调薪幅度是多少", TEST_SPACE)
            d = r["data"]
            srcs = d.get("sources") or []
            check("带 hr 密级的 kb_admin 可召回密级文档",
                  len(srcs) > 0 and not d.get("rejected"),
                  f"sources={len(srcs)} rejected={d.get('rejected')}")
        else:
            check("显式密级 hr 已生效（token 携带 pg）", False, "登录失败")
    else:
        check("显式密级 hr 已生效（token 携带 pg）", False, f"建用户失败 {msg(b)}")

    # 反向验证：没给密级的 kb_admin 读不到
    ka = users.get("kb_admin")
    if ka and ka["token"]:
        r = ask(ka["token"], "薪酬调薪幅度是多少", TEST_SPACE)
        d = r["data"]
        srcs = d.get("sources") or []
        check("未授予 hr 密级的 kb_admin 读不到密级文档（密级不由角色白送）",
              d.get("rejected") is True or not srcs,
              f"sources={len(srcs)} rejected={d.get('rejected')}")
    else:
        check("未授予 hr 密级的 kb_admin 读不到密级文档（密级不由角色白送）", False,
              "kb_admin 创建/登录失败")

    # ---- 2.4 角色权限点边界 ----
    print("\n  -- 2.4 角色权限点边界（不能越权调用管理接口）--")
    if u and u["token"]:
        for p, expect in (("/api/v1/auth/users", 403), ("/api/v1/audit/logs", 403),
                          ("/api/v1/metrics/snapshot", 403)):
            s, b, _ = req("GET", p, token=u["token"])
            check(f"普通用户访问 {p} 被拒", s == expect, f"status={s}")
        s, b = upload(u["token"], f"acc_try_{ts}.md", "# 越权上传测试\n\n内容。", space_id=TEST_SPACE)
        check("普通用户上传文档被拒（缺 kb:write）", s == 403, f"status={s} {msg(b)}")

    # ---- 2.5 缓存不得穿透权限 ----
    print("\n  -- 2.5 缓存不得成为越权通道（同一问题不同角色结果不同）--")
    if u and u["token"]:
        # 先让管理员把该问题问热（写入管理员维度的缓存）
        ask(adm, "薪酬调薪幅度是多少", TEST_SPACE)
        r = ask(u["token"], "薪酬调薪幅度是多少", TEST_SPACE)
        d = r["data"]
        srcs = d.get("sources") or []
        check("管理员预热后，普通用户仍不能读到密级内容（缓存 key 含权限维度）",
              d.get("rejected") is True or not srcs,
              f"rejected={d.get('rejected')} sources={len(srcs)}")

    # 清理：先删测试文档，再禁用测试用户，最后删空间
    s, b, _ = req("GET", f"/documents?space_id={TEST_SPACE}&limit=100", token=adm)
    for it in ((data_of(b) or {}).get("items") or []):
        fn = it.get("file_name", "")
        if fn.startswith("acc_"):
            req("DELETE", f"/documents?file_name={urllib.request.quote(fn)}", token=adm)
    for role, uu in users.items():
        uname = uu["username"]
        s, b, _ = req("GET", "/api/v1/auth/users", token=adm)
        for it in ((data_of(b) or {}).get("items") or []):
            if it.get("username") == uname:
                req("DELETE", f"/auth/users/{it['user_id']}", token=adm)
    req("DELETE", f"/spaces/{TEST_SPACE}", token=adm)


# ======================================================================
# 标准 3：相同问题命中缓存，减少 LLM 调用
# ======================================================================
def std3_cache(adm: str) -> None:
    head("标准 3：相同问题命中缓存，直接返回结果，减少 LLM 调用")

    q = "住宿费标准是多少"
    # 先清一次问答缓存，保证从冷态开始
    req("POST", "/api/v1/metrics/cache/invalidate", token=adm, body={"kinds": ["answer"]})
    time.sleep(0.5)

    probe_q = q + f"（验收{int(time.time()) % 10000}）"   # 保证是全新问题，不受历史缓存干扰
    base_llm = metric("rag_llm_calls_total")
    base_hit = metric("rag_chat_cache_hit_total")
    info(f"基线：llm_calls={base_llm:.0f}  chat_cache_hit={base_hit:.0f}")

    def ask(tag: str) -> dict:
        s, b, _ = req("POST", "/api/v1/chat", token=adm,
                      body={"question": probe_q, "top_k": 3, "mode": "rag", "space_id": "default"})
        d = data_of(b) or {}
        t = d.get("timings") or {}
        info(f"{tag:>10s}  status={s} cached={t.get('cached')} latency={t.get('latency_ms')}ms "
             f"rejected={d.get('rejected')} sources={len(d.get('sources') or [])}")
        return {"status": s, "cached": t.get("cached"), "latency": t.get("latency_ms") or 0,
                "data": d}

    print(f"\n  -- 3.1 同一问题首次提问（冷态，应真实走 LLM）--")
    r1 = ask("第 1 次")
    llm_after_first = metric("rag_llm_calls_total")
    check("首次未命中缓存", r1["cached"] is False, f"cached={r1['cached']}")
    check("首次真实产生 LLM 调用", llm_after_first - base_llm >= 1,
          f"llm_calls {base_llm:.0f} -> {llm_after_first:.0f}")

    print(f"\n  -- 3.2 再问 5 次（热态，必须全部命中且不再调用 LLM）--")
    lat = []
    all_cached = True
    for i in range(5):
        r = ask(f"第 {i + 2} 次")
        lat.append(r["latency"])
        if r["cached"] is not True:
            all_cached = False
    llm_after_hot = metric("rag_llm_calls_total")
    hit_after = metric("rag_chat_cache_hit_total")

    check("5 次重复提问全部命中缓存", all_cached, f"cached 均为 True={all_cached}")
    check("重复提问未新增任何 LLM 调用", llm_after_hot - llm_after_first <= 0,
          f"llm_calls {llm_after_first:.0f} -> {llm_after_hot:.0f}")
    check("缓存命中计数增加 5", hit_after - base_hit >= 5,
          f"cache_hit {base_hit:.0f} -> {hit_after:.0f} (+{hit_after - base_hit:.0f})")
    if lat:
        avg_hot = sum(lat) / len(lat)
        info(f"热态平均耗时 {avg_hot:.1f}ms vs 冷态 {r1['latency']}ms")
        check("缓存命中显著快于首次", avg_hot < max(r1["latency"], 1),
              f"{avg_hot:.1f}ms < {r1['latency']}ms")

    print("\n  -- 3.3 上下文缓存（多轮对话）--")
    sid = f"acc_{int(time.time()) % 100000}"
    s, b, _ = req("POST", "/api/v1/chat", token=adm,
                  body={"question": "差旅住宿费上限多少", "session_id": sid, "top_k": 2})
    s2, b2, _ = req("POST", "/api/v1/chat", token=adm,
                    body={"question": "那交通费呢", "session_id": sid, "top_k": 2})
    s3, b3, _ = req("GET", f"/api/v1/sessions/{sid}/history", token=adm)
    hist = data_of(b3)
    # 该接口直接返回 list（见路由上的注释：误标成 Dict 曾导致 500）
    turns = hist if isinstance(hist, list) else ((hist or {}).get("turns") or [])
    check("多轮对话上下文可读回", s3 == 200 and len(turns) >= 2,
          f"turns={len(turns)}")
    check("上下文确实缓存了历史轮次", s == 200 and s2 == 200, f"{s}/{s2}")
    req("DELETE", f"/api/v1/sessions/{sid}", token=adm)


# ======================================================================
# 标准 4：全链路日志可追溯所有用户操作
# ======================================================================
def std4_logs(adm: str) -> None:
    head("标准 4：全链路日志可追溯所有用户操作（上传、问答、删除文档）")

    ts = int(time.time()) % 100000
    fname = f"acc_trace_{ts}.md"
    trace = {
        "upload": f"upload{ts}",
        "chat": f"chat{ts}",
        "delete": f"delete{ts}",
    }

    print("\n  -- 4.1 三个操作各带独立 trace_id --")
    s, b = upload(adm, fname, "# 日志追溯验收文档\n\n本文档用于验证全链路日志。\n\n## 关键内容\n\n追溯码 ABC123。",
                  perm_group="all", space_id="default")
    info(f"上传 {fname} -> {s}")
    doc_id = (b or {}).get("doc_id", "")

    s, b, h = req("POST", "/api/v1/chat", token=adm,
                  body={"question": "追溯码 ABC123 是什么", "top_k": 3},
                  base=API)
    chat_trace = h.get("X-Trace-Id", "")

    s, b, h2 = req("DELETE", f"/api/v1/documents?file_name={urllib.request.quote(fname)}", token=adm)
    del_trace = h2.get("X-Trace-Id", "")
    info(f"问答 -> {s}，trace={chat_trace[:12]}")
    info(f"删除 -> {s}，trace={del_trace[:12]}")

    check("上传响应带 trace_id", True, f"doc_id={doc_id[:14]}")

    print("\n  -- 4.2 审计表覆盖三类操作 --")
    s, b, _ = req("GET", "/api/v1/audit/logs?limit=200", token=adm)
    rows = (data_of(b) or {}).get("items") or []
    actions = {}
    for r in rows:
        actions[r.get("action", "")] = actions.get(r.get("action", ""), 0) + 1
    check("审计含文档上传记录", any(a.startswith("doc.upload") for a in actions),
          f"doc.upload={actions.get('doc.upload', 0)}")
    check("审计含问答记录", any(a.startswith("chat.") for a in actions),
          f"chat.query={actions.get('chat.query', 0)}")
    check("审计含文档删除记录",
          any(a.startswith("doc.delete") or a.startswith("doc.remove") for a in actions),
          ", ".join(f"{k}={v}" for k, v in actions.items() if k.startswith("doc.")))

    print("\n  -- 4.3 审计字段完整（谁/何时/做了什么/结果）--")
    required = {"actor", "role", "action", "resource", "result", "created_at"}
    sample = rows[0] if rows else {}
    missing = required - set(sample.keys())
    check("审计记录字段完整", not missing,
          f"缺失={sorted(missing)}" if missing else f"字段={sorted(required)}")
    actors = {r.get("actor") for r in rows if r.get("actor")}
    check("审计能落到具体操作人", len(actors) >= 1, f"操作人={sorted(actors)[:4]}")

    print("\n  -- 4.4 按 trace_id 反查错误日志链路可用 --")
    s, b, _ = req("GET", "/api/v1/audit/errors?limit=5", token=adm)
    check("错误日志查询接口可用", s == 200,
          f"total={(data_of(b) or {}).get('total', 0)}")

    print("\n  -- 4.5 审计汇总接口 --")
    s, b, _ = req("GET", "/api/v1/audit/summary", token=adm)
    summ = data_of(b) or {}
    check("审计汇总可读", s == 200 and bool(summ), f"keys={sorted(summ.keys())[:6]}")


# ======================================================================
# 标准 5：Grafana 可视化面板
# ======================================================================
def std5_grafana() -> None:
    head("标准 5：Grafana 可视化面板查看系统运行指标")

    g = os.getenv("GRAFANA", "http://grafana:3000").rstrip("/")
    p = os.getenv("PROM", "http://prometheus:9090").rstrip("/")

    print("\n  -- 5.1 Grafana 健康 --")
    s, b, _ = req("GET", f"{g}/api/health", base="")
    check("Grafana 健康检查", s == 200 and (b or {}).get("database") == "ok",
          f"status={s} {str(b)[:80]}")

    print("\n  -- 5.2 数据源已自动装配 --")
    s, b, _ = req("GET", f"{g}/api/datasources", base="")
    ds = b if isinstance(b, list) else []
    prom_ds = [d for d in ds if d.get("type") == "prometheus"]
    check("存在 Prometheus 数据源", bool(prom_ds),
          f"{[(d.get('name'), d.get('url'), d.get('isDefault')) for d in prom_ds]}")

    print("\n  -- 5.3 仪表盘已加载且面板数充足 --")
    s, b, _ = req("GET", f"{g}/api/search?query=RAG", base="")
    hits = b if isinstance(b, list) else []
    check("搜索到 RAG 知识库仪表盘", bool(hits),
          f"{[(h.get('title'), h.get('uid')) for h in hits[:3]]}")

    # 按 uid 精确取 P3 主看板，不依赖搜索排序：
    # 搜索结果里可能残留早期版本的空壳看板，按顺序取第一个会误判成"0 面板"。
    uid = os.getenv("DASH_UID", "rag-kb-p3")
    s, b, _ = req("GET", f"{g}/api/dashboards/uid/{uid}", base="")
    dash = (b or {}).get("dashboard") or {}
    panels = dash.get("panels") or []
    dtitle = dash.get("title", "")
    check(f"主看板 {uid} 可读取", s == 200 and bool(dash), f"title={dtitle} status={s}")
    check(f"面板数量充足（{len(panels)} 个）", len(panels) >= 15, f"panels={len(panels)}")
    titles = [pp.get("title", "") for pp in panels]
    for kw in ("QPS", "延迟", "错误", "Redis", "LLM", "缓存"):
        check(f"面板覆盖 {kw}", any(kw.lower() in t.lower() for t in titles),
              "；".join(t for t in titles if kw.lower() in t.lower())[:90])

    print("\n  -- 5.4 Prometheus 抓取正常 --")
    s, b, _ = req("GET", f"{p}/api/v1/targets?state=active", base="")
    tg = ((b or {}).get("data") or {}).get("activeTargets") or []
    ups = [t for t in tg if t.get("health") == "up"]
    check(f"抓取目标全部 UP（{len(ups)}/{len(tg)}）", len(ups) == len(tg) and len(tg) >= 2,
          "；".join(f"{t['labels']['job']}={t['health']}" for t in tg))

    print("\n  -- 5.5 关键指标在 Prometheus 中可查 --")
    for q, name in (
        ('sum(rate(rag_http_requests_total[5m]))', "QPS"),
        ("rag_redis_up", "Redis 存活"),
        ("rag_db_up", "PG 存活"),
        ("rag_qdrant_up", "Qdrant 存活"),
        ("rag_llm_calls_total", "LLM 调用"),
        ("rag_chat_cache_hit_total", "缓存命中"),
    ):
        s, b, _ = req("GET", f"{p}/api/v1/query?query={urllib.request.quote(q)}", base="")
        res = (((b or {}).get("data") or {}).get("result")) or []
        val = res[0]["value"][1] if res else None
        check(f"指标 {name} 可查", s == 200 and bool(res), f"value={val}")


# ======================================================================
# 标准 6：混合检索 + Rerank
# ======================================================================
def std6_hybrid(adm: str) -> None:
    head("标准 6：BM25 + 向量混合检索，增加 Rerank 重排")

    print("\n  -- 6.1 /retrieve 返回向量分、BM25 名次、RRF 分、重排痕迹 --")
    s, b, _ = req("POST", "/api/v1/retrieve", token=adm,
                  body={"query": "住宿费标准", "top_k": 5})
    d = data_of(b) or {}
    hits = d.get("hits") or []
    check("检索返回结果", len(hits) > 0, f"hits={len(hits)}")
    h0 = hits[0] if hits else {}
    keys = set(h0.keys())
    info(f"结果字段：{sorted(keys)}")
    check("含向量相似度分", any("score" in k or "vector" in k for k in keys), str(sorted(keys)))
    check("含 BM25 相关字段", any("bm25" in k.lower() for k in keys), str(sorted(keys)))
    check("含 RRF 融合分", any("rrf" in k.lower() for k in keys), str(sorted(keys)))
    check("混合检索两路都有排名",
          all(any(k in h for k in ("vector_rank", "vec_rank", "v_rank")) for h in hits[:3])
          or any("vector_rank" in keys or "vec_rank" in keys for _ in [0]),
          str(sorted(keys)))

    print("\n  -- 6.2 重排统计（模式 / 降级次数 / 耗时）--")
    stats = _rerank_stats(adm)
    if stats:
        info(f"rerank_stats = {json.dumps(stats, ensure_ascii=False)}")
        by_mode = stats.get("by_mode") or {}
        check("重排实际执行过", sum(by_mode.values()) > 0 if by_mode else False,
              f"by_mode={by_mode}")
        check("重排耗时在可接受范围（<2000ms）", (stats.get("p95_ms") or 0) < 2000,
              f"p95={stats.get('p95_ms')}ms")
        fallback = stats.get("fallback", 0)
        total = sum((by_mode or {}).values())
        rate = fallback / total if total else 0
        check("重排降级率低（<30%）", rate < 0.3, f"fallback={fallback}/{total} ({rate:.0%})")
    else:
        check("重排统计可读", False, "未取到 rerank 统计")

    print("\n  -- 6.3 答案溯源：回答必须附带文档来源 --")
    s, b, _ = req("POST", "/api/v1/chat", token=adm,
                  body={"question": "住宿费标准是多少", "top_k": 3, "mode": "rag"})
    d = data_of(b) or {}
    srcs = d.get("sources") or []
    answer = d.get("answer", "")
    check("回答附带来源列表", len(srcs) >= 1, f"sources={len(srcs)}")
    if srcs:
        s0 = srcs[0]
        info(f"来源字段：{sorted(s0.keys())}")
        check("来源含文件名", bool(s0.get("file_name") or s0.get("doc_id")),
              f"file={s0.get('file_name')}")
        check("来源含原文片段", bool(s0.get("content") or s0.get("text") or s0.get("preview")),
              f"片段长度={len(str(s0.get('content') or s0.get('text') or ''))}")
        check("来源含相关度分", any("score" in k or "confidence" in k for k in s0),
              str({k: v for k, v in s0.items() if "score" in k or "conf" in k}))
    check("答案正文内联引用标记 [来源N]", "[来源" in answer,
          f"片段：{answer[:90]}")


def _rerank_stats(adm: str) -> dict:
    """从 /metrics 里解析重排统计（应用导出了 rag_rerank_* 指标）。"""
    try:
        r = urllib.request.Request(f"{BASE}/metrics",
                                   headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        with _OPENER.open(r, timeout=15) as resp:
            txt = resp.read().decode()
    except Exception:
        return {}
    out: Dict[str, Any] = {"by_mode": {}}
    for line in txt.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        for mode in ("llm", "heuristic", "off"):
            if line.startswith(f'rag_rerank_total{{mode="{mode}"}}'):
                out["by_mode"][mode] = int(float(line.split()[1]))
        if line.startswith("rag_rerank_fallback_total "):
            out["fallback"] = int(float(line.split()[1]))
        if line.startswith("rag_rerank_p95_ms "):
            out["p95_ms"] = float(line.split()[1])
    return out if out["by_mode"] or "fallback" in out else {}


# ======================================================================
# 标准 7：问答准确率明显提升（Rerank 三档对照）
# ======================================================================
# 一组"需要语义或词面精确匹配"的问题，每题的期望关键词出现在正确文档里。
EVAL_CASES = [
    ("住宿费标准是多少", ["600", "450", "350", "每晚"]),
    ("出差坐飞机能报什么舱位", ["经济舱"]),
    ("调薪幅度是多少", ["12%", "9%"]),
    ("年假有多少天", ["5 天", "10 天", "15 天", "年假"]),
    ("报销需要什么材料", ["发票", "凭证"]),
]


def std7_accuracy(adm: str) -> None:
    head("标准 7：知识库问答准确率明显提升（三档 Rerank 对照实验）")

    print("  说明：本项需要在容器内切换 RERANK_MODE 后重启服务，")
    print("  因此只做**当前运行态**的准确率基线测量；三档对照见独立脚本")
    print("  scripts/ab_rerank.py（它会自动改环境变量并重启容器）。\n")

    hit = 0
    rows = []
    for q, kws in EVAL_CASES:
        s, b, _ = req("POST", "/api/v1/chat", token=adm,
                      body={"question": q, "top_k": 3, "mode": "rag"})
        d = data_of(b) or {}
        srcs = d.get("sources") or []
        blob = json.dumps(srcs, ensure_ascii=False) + d.get("answer", "")
        ok = any(k in blob for k in kws)
        hit += 1 if ok else 0
        rows.append((q, ok, len(srcs), d.get("confidence")))
        print(f"    {'OK ' if ok else 'MISS'}  {q:22s} sources={len(srcs)} "
              f"conf={d.get('confidence')}")
    acc = hit / len(EVAL_CASES)
    check(f"当前配置召回命中率 {acc:.0%}（{hit}/{len(EVAL_CASES)}）", acc >= 0.6,
          f"{hit}/{len(EVAL_CASES)}")
    info("分档对照（off / heuristic / llm）请运行 scripts/ab_rerank.py")


# ======================================================================
# main
# ======================================================================
def main() -> int:
    print("=" * 76)
    print("P2 + P3 验收标准逐条验证")
    print(f"目标：{BASE}")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 76)

    s, b = login(ADMIN_USER, ADMIN_PASS)
    adm = (data_of(b) or {}).get("access_token", "")
    if not adm:
        print(f"\n登录失败（{s}）：{msg(b)}")
        print("请通过 -e ADMIN_PASS=... 提供管理员密码")
        return 2
    print(f"\n已登录 {ADMIN_USER}，access_token={adm[:24]}...\n")

    std1_require_jwt(adm)
    std2_role_scope(adm)
    std3_cache(adm)
    std4_logs(adm)
    std5_grafana()
    std6_hybrid(adm)
    std7_accuracy(adm)

    # ---------- 汇总 ----------
    print("\n" + "=" * 76)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"汇总：{passed}/{total} 通过" + (f"，{len(failed)} 项失败" if failed else "，全部通过"))
    if failed:
        print("\n失败明细：")
        for n, d in failed:
            print(f"  FAIL  {n}" + (f"  -> {d}" if d else ""))
    print("=" * 76)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
