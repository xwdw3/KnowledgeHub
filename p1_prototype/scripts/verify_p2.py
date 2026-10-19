"""P2 阶段端到端验收脚本（Python 标准库实现，无第三方依赖）。

用法：
    python scripts/verify_p2.py                     # 默认 http://127.0.0.1:8022
    BASE=http://127.0.0.1:8012 python scripts/verify_p2.py
    ADMIN_KEY=rag_xxx python scripts/verify_p2.py   # 跳过引导，直接用已有管理员密钥

覆盖四条 P2 主链路：
    A. 安全（API Key + RBAC）：401 / 403 / 权限组隔离 / 密钥轮换与吊销
    B. 缓存（L1 LRU + L2 Redis）：命中、分层、写后失效
    C. 审计（异步落库）：成功/失败/越权三类留痕，flush 后可见
    D. 监控（Prometheus + 看板）：文本格式、快照、缓存健康
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("BASE", "http://127.0.0.1:8012").rstrip("/")
API = f"{BASE}/api/v1"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Admin@12345")

# 本地调试时避免系统代理干扰
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

passed: list[str] = []
failed: list[str] = []


def call(method: str, path: str, key: str | None = None, body=None,
         raw: bool = False, extra_headers: dict | None = None):
    """返回 (status, payload)。payload 为解析后的 JSON 或原始文本。"""
    url = path if path.startswith("http") else API + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # 连接失败
        return 0, str(exc)
    if raw:
        return status, text
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
        print(f"  [PASS] {name}" + (f"  |  {detail}" if detail else ""))
    else:
        failed.append(name)
        print(f"  [FAIL] {name}" + (f"  |  {detail}" if detail else ""))


def unwrap(payload):
    """统一响应约定：成功响应为 {code, message, data, trace_id}，取出 data。

    兼容未包装的裸响应（/metrics 等非 JSON 接口不走包装）。
    """
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        return payload["data"]
    return payload


def _to_jwt(api_key: str | None) -> str:
    """用 API Key 换取 JWT。AUTH_MODE=any 时返回空串，调用方回退用密钥。

    P3 收敛后密钥不能直接访问业务接口，但它仍是**唯一凭据来源**：
    吊销密钥即无法再换票，所以密钥生命周期管理依然有效。
    """
    if not api_key:
        return ""
    status, payload = call("POST", "/auth/token", body={"api_key": api_key})
    data = unwrap(payload) or {}
    return data.get("access_token", "") if status == 200 else ""


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def upload(key: str, text: str, perm_group: str = "all", uploader: str = "") -> tuple[int, dict]:
    """手工构造 multipart（urllib 不支持 files 参数）。

    只提交接口声明过的表单字段（file / perm_group / uploader）。
    多传未声明的字段会被 FastAPI 直接忽略，容易让人误判为业务逻辑问题。
    """
    boundary = "----p2verify" + uuid.uuid4().hex
    filename = f"p2_verify_{uuid.uuid4().hex[:8]}.txt"
    fields = [("perm_group", perm_group)]
    if uploader:
        fields.append(("uploader", uploader))
    parts = []
    for field, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n"
            .encode("utf-8")
        )
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
         f"Content-Type: text/plain\r\n\r\n").encode("utf-8")
        + text.encode("utf-8") + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    data = b"".join(parts)

    req = urllib.request.Request(
        API + "/documents", data=data, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace") or "{}")


SAMPLE = """差旅与费用报销管理办法

第一条 住宿费标准
一线城市住宿费上限为每人每晚 650 元，二线城市为 450 元，其他城市为 350 元。
超标部分由个人承担。

第二条 交通费
市内交通费每日上限 100 元，需提供行程单与发票。

第三条 报销时限
出差结束后 15 个工作日内提交报销单，逾期需部门总监书面说明。
"""


def main() -> int:
    key = ADMIN_KEY

    # ---------------- 0. 服务存活 ----------------
    section("0. 服务就绪")
    status, payload = call("GET", f"{BASE}/api/v1/health")
    check("健康检查可访问", status == 200, f"HTTP={status}")
    if status != 200:
        print(f"  !! 服务不可用：{payload}")
        return 2
    ver = (unwrap(payload) or {}).get("app_version", "?")
    # P3 起版本号升到 1.2.x，这里放宽到"1.x 且不低于 1.1"，避免每次升级都要改脚本
    check("版本为 P2 及以上", ver.startswith("1.") and int(ver.split(".")[1] or 0) >= 1,
          f"version={ver}")

    if not key:
        # 没给 ADMIN_KEY 时改用管理员账号登录换 JWT。
        # P3 严格模式下管理员密钥的明文只在签发那一刻返回过一次，
        # 之后的验收没必要（也不应该）依赖保存明文，登录拿票等价可用。
        status, payload = call("POST", "/auth/login",
                               body={"username": ADMIN_USER, "password": ADMIN_PASS})
        key = (unwrap(payload) or {}).get("access_token", "")
        if status == 200:
            print(f"  已用管理员账号 {ADMIN_USER} 登录换票")
            # 登录已换成 JWT，后续 A3 的"密钥换票"就无从测起，
            # 直接用票继续即可（A3 在此分支下跳过而非判失败）
            globals()["_LOGIN_TOKEN"] = key
        else:
            print(f"\n  !! 无法获取管理员凭据：{status} {payload}")
            print("     请用 ADMIN_KEY=rag_xxx 或 ADMIN_USER/ADMIN_PASS 环境变量传入")
            return 2

    # ---------------- A. 安全 ----------------
    section("A. 安全：API Key + RBAC/ JWT")

    status, payload = call("GET", "/auth/me")
    msg = str((payload or {}).get("message", ""))[:40]
    check("A1 无密钥访问受保护接口返回 401", status == 401,
          f"HTTP={status} msg={msg}")

    status, payload = call("GET", "/auth/me", key="rag_definitely_not_a_valid_key_000")
    check("A2 伪造密钥返回 401", status == 401, f"HTTP={status}")

    # P3 收敛后：API Key 不能直接访问业务接口，必须先用 /auth/token 换取 JWT。
    # 这一步本身就是 P2 能力在新协议下的延续——密钥仍是唯一凭据，
    # 只是不再直接当作会话令牌使用。
    raw_key = key
    if globals().get("_LOGIN_TOKEN"):
        # 本次是用账号登录拿到的票，没有密钥可测"密钥换票"。
        # 该链路已由 verify_p2p3_acceptance.py 的标准 1 专项覆盖，这里跳过。
        print("  [SKIP] A3 密钥换 JWT：本次以账号登录方式运行，换票链路见标准1专项")
    else:
        status, payload = call("POST", "/auth/token", body={"api_key": raw_key})
        tok = (unwrap(payload) or {}).get("access_token", "")
        if status == 200 and tok:
            check("A3 正确密钥可换取 JWT（P3 严格模式）", True, f"HTTP={status}")
            key = tok                                    # 后续全部用 JWT
        else:
            # 兼容 AUTH_MODE=any（P2 原模式）：密钥直接可用
            check("A3 正确密钥可使用（兼容 P2 双通道模式）", False,
                  f"HTTP={status} msg={str((payload or {}).get('message', ''))[:60]}")
            key = raw_key

    status, payload = call("GET", "/auth/me", key=key)
    me = unwrap(payload) or {}
    check("A4 有效凭据访问 /auth/me 返回 200", status == 200, f"HTTP={status}")
    check("A5 /auth/me 包含角色与权限", "permissions" in me,
          f"role={me.get('role')} perms={len(me.get('permissions', []))}")
    # P3 新增 space:admin，权限点从 6 个变 7 个；改为断言"包含 P2 的 6 个"，
    # 这样后续再加权限点时不会误判为回归
    _p2_perms = {"kb:read", "kb:write", "kb:admin", "audit:read", "user:admin", "metric:read"}
    check("A6 管理员具备全部 P2 权限点（含 P3 新增）",
          _p2_perms <= set(me.get("permissions", [])),
          str(me.get("permissions")))

    # P3 收敛后 /metrics 不再公开：它要暴露接口路径、状态码分布、依赖拓扑，
    # 裸奔等于把系统全貌交给任何能连到端口的人。改用独立抓取令牌
    # （Prometheus 只支持静态 header，无法走 JWT 登录+刷新流程）。
    _mt = os.environ.get("METRICS_TOKEN", "")
    status, _ = call("GET", f"{BASE}/metrics", key=_mt or None, raw=True)
    check("A7 /metrics 可抓取（抓取令牌）", status == 200, f"HTTP={status}")
    status, _ = call("GET", f"{BASE}/metrics", raw=True)
    check("A8 /metrics 无令牌被拒（P3 收敛）", status == 401, f"HTTP={status}")

    # 创建 viewer 与 editor
    status, payload = call("POST", "/auth/keys", key=key,
                           body={"owner": "verify_viewer", "role": "viewer",
                                 "perm_group": "all", "description": "验收-只读"})
    vkey = unwrap(payload) or {}
    viewer = vkey.get("api_key")
    viewer_id = vkey.get("key_id")
    check("A7 创建 viewer 密钥成功", status == 200 and bool(viewer), f"HTTP={status}")
    check("A8 创建响应带一次性提示", "warning" in vkey, str(vkey.get("warning"))[:30])
    check("A9 响应不含密钥哈希", "key_hash" not in vkey and "salt" not in vkey)

    status, payload = call("POST", "/auth/keys", key=key,
                           body={"owner": "verify_editor", "role": "editor",
                                 "perm_group": "hr", "description": "验收-编辑(hr)"})
    editor_hr = (unwrap(payload) or {}).get("api_key")
    check("A10 创建 editor 密钥（限制 hr 权限组）", status == 200 and bool(editor_hr), f"HTTP={status}")

    # 严格模式下，密钥本身不能开门，必须换 JWT。这里统一换票，
    # 保留"密钥 → 令牌"这条链路，也就保留了 P2 的密钥生命周期语义
    # （吊销密钥即无法再换票，令牌到期自然失效）。
    viewer = _to_jwt(viewer) or viewer
    editor_hr = _to_jwt(editor_hr) or editor_hr

    status, _ = call("GET", "/documents", key=viewer)
    check("A11 viewer 可只读访问 /documents（200）", status == 200, f"HTTP={status}")

    status, payload = call("POST", "/auth/keys", key=viewer,
                           body={"owner": "hacker", "role": "admin", "perm_group": "all"})
    check("A12 viewer 提权创建管理员密钥被拒（403）", status == 403,
          f"HTTP={status} msg={(payload or {}).get('message', '')}")

    status, _ = call("GET", "/audit/logs", key=viewer)
    check("A13 viewer 访问审计日志被拒（403）", status == 403, f"HTTP={status}")

    status, _ = call("GET", "/metrics/snapshot", key=viewer)
    check("A14 viewer 访问指标快照被拒（403）", status == 403, f"HTTP={status}")

    status, _ = call("DELETE", "/metrics/cache/invalidate", key=viewer)
    check("A15 viewer 调用缓存失效被拒（403/405）", status in (403, 405), f"HTTP={status}")

    # 权限组隔离：editor(hr) 只能投放 hr，不能投放 all（否则等于自我扩权把密件公开）
    status, payload = upload(editor_hr, SAMPLE, perm_group="all")
    check("A16 editor(hr) 越权把文档投放到 all 被拒（403）", status == 403,
          f"HTTP={status} msg={str((payload or {}).get('message', ''))[:46]}")

    status, payload = upload(editor_hr, SAMPLE.replace("650 元", "680 元"),
                             perm_group="finance")
    check("A16b editor(hr) 投放到无关组 finance 被拒（403）", status == 403,
          f"HTTP={status}")

    status, payload = upload(key, SAMPLE, perm_group="all")
    data = unwrap(payload) or {}
    check("A17 管理员上传文档成功", status == 200, f"HTTP={status}")
    doc_name = data.get("file_name") or ""
    check("A18 上传返回文档标识", bool(doc_name), f"file_name={doc_name}")
    check("A18b 管理员可投放到 all（返回 perm_group=all）",
          data.get("perm_group") == "all", f"perm_group={data.get('perm_group')}")

    status, payload = upload(editor_hr, SAMPLE.replace("650 元", "700 元"), perm_group="hr")
    hr_doc = unwrap(payload) or {}
    check("A19 editor(hr) 上传 hr 权限组文档成功", status == 200, f"HTTP={status} msg={str((payload or {}).get('message', ''))[:70]}")
    check("A19b 文档权限组未被扩写为 all,hr",
          hr_doc.get("perm_group") == "hr", f"perm_group={hr_doc.get('perm_group')}")

    # editor(hr) 只能看到 hr 与全公开文档，且不能看到其他组文档
    status, payload = call("GET", "/documents?limit=100", key=editor_hr)
    items = (payload.get("data") or {}).get("items") if isinstance(payload, dict) else []
    groups = {i.get("perm_group") for i in items or []}
    check("A20 editor(hr) 文档列表被权限组过滤", status == 200 and groups <= {"hr", "all"},
          f"可见权限组={sorted(groups)} 共 {len(items or [])} 条")
    check("A21 editor(hr) 能看到自己上传的 hr 文档",
          any((i.get("file_name") or "") == hr_doc.get("file_name") for i in items or []),
          f"查找 {hr_doc.get('file_name')}")

    # ---------------- B. 缓存 ----------------
    section("B. 缓存：L1 LRU + L2 Redis（无 Redis 时降级本地）")

    q = {"query": "住宿费标准是多少", "top_k": 3}

    status, first = call("POST", "/retrieve", key=key, body=q)
    f_t = (unwrap(first) or {}).get("timings") or {}
    check("B1 首次检索 200", status == 200, f"HTTP={status} cached={f_t.get('cached')}")

    status, second = call("POST", "/retrieve", key=key, body=q)
    s_t = (unwrap(second) or {}).get("timings") or {}
    second_hit = bool(s_t.get("cached"))
    check("B2 重复检索命中缓存（cached=true）", second_hit, f"cached={second_hit}")

    status, payload = call("GET", "/metrics/cache", key=key)
    cache_h = unwrap(payload) or {}
    m = (cache_h.get("metrics") or {})
    check("B3 缓存指标可读", status == 200 and "metrics" in cache_h, f"HTTP={status}")
    check("B4 缓存命中率大于 0", float(m.get("hit_rate") or 0) > 0,
          f"hit_rate={m.get('hit_rate')} l1_hit={m.get('l1_hit')} l2_hit={m.get('l2_hit')}")

    status, payload = call("POST", "/metrics/cache/invalidate", key=key, body={"kinds": []})
    check("B5 管理员可主动失效缓存", status == 200, f"HTTP={status}")

    status, third = call("POST", "/retrieve", key=key, body=q)
    th_t = (unwrap(third) or {}).get("timings") or {}
    third_hit = bool(th_t.get("cached"))
    check("B6 失效后再次检索不再命中（cached=false）", not third_hit, f"cached={third_hit}")

    # ---------------- C. RAG + 审计 ----------------
    section("C. RAG 问答与审计留痕")

    status, payload = call("POST", "/chat", key=key,
                           body={"question": "住宿费标准是多少", "top_k": 3})
    cdata = unwrap(payload) or {}
    check("C1 问答接口 200", status == 200, f"HTTP={status}")
    answer = cdata.get("answer") if isinstance(cdata, dict) else ""
    conf = cdata.get("confidence") if isinstance(cdata, dict) else None
    check("C2 生成答案非空", bool(answer), f"conf={conf} len={len(answer or '')}")
    check("C3 命中知识库（confidence>=0.25）", isinstance(conf, (int, float)) and conf >= 0.25,
          f"confidence={conf}")

    status, payload = call("POST", "/chat", key=key,
                           body={"question": "火星殖民地的氧气配额由谁审批", "top_k": 3})
    cdata2 = unwrap(payload) or {}
    rejected = bool(cdata2.get("rejected")) if isinstance(cdata2, dict) else False
    conf2 = cdata2.get("confidence") if isinstance(cdata2, dict) else None
    check("C4 库外问题触发拒答", rejected or (conf2 is not None and conf2 < 0.25),
          f"rejected={rejected} conf={conf2}")

    call("POST", "/audit/flush", key=key)
    status, payload = call("GET", "/audit/logs?limit=200", key=key)
    logs = (unwrap(payload) or {}).get("items") or []
    actions = {i.get("action") for i in logs}
    check("C5 审计日志可查询", status == 200, f"HTTP={status} 条数={len(logs)}")
    check("C6 记录问答动作", any("chat" in (a or "") for a in actions), str(sorted(actions))[:120])
    check("C7 记录密钥创建动作", any("key.create" in (a or "") for a in actions))
    check("C8 记录上传动作", any("doc.upload" in (a or "") for a in actions))

    denied = [i for i in logs if i.get("result") == "denied"]
    check("C9 越权与认证失败被留痕（result=denied）", len(denied) > 0, f"denied={len(denied)} 条")

    has_ip = any((i.get("ip") or "") for i in logs)
    check("C10 审计记录包含来源 IP", has_ip)

    status, payload = call("GET", "/audit/summary", key=key)
    check("C11 审计概览可读", status == 200, f"HTTP={status}")

    # ---------------- D. 监控 ----------------
    section("D. 监控：Prometheus + 看板")

    status, text = call("GET", f"{BASE}/metrics", key=os.environ.get("METRICS_TOKEN", "") or None,
                        raw=True)
    txt = text if isinstance(text, str) else ""
    check("D1 /metrics 返回 200（抓取令牌）", status == 200, f"HTTP={status}")
    check("D2 含 Prometheus HELP/TYPE 头", "# HELP" in txt and "# TYPE" in txt)
    for metric in ["rag_requests_total", "rag_chat_total", "rag_auth_failures_total",
                   "rag_cache_hit_rate", "rag_info"]:
        check(f"D3 指标存在 {metric}", metric in txt)

    status, payload = call("GET", "/metrics/snapshot", key=key)
    snap = unwrap(payload) or {}
    check("D4 指标快照可读（admin）", status == 200, f"HTTP={status}")
    check("D5 快照含分位延迟", isinstance(snap, dict) and "latency_ms" in snap,
          str((snap or {}).get("latency_ms", {}))[:90])
    check("D6 快照统计到认证失败", isinstance(snap, dict)
          and (snap.get("security") or {}).get("auth_failures", 0) >= 1,
          f"auth_failures={(snap or {}).get('security', {}).get('auth_failures')}")

    status, text = call("GET", f"{BASE}/dashboard", raw=True)
    html = text if isinstance(text, str) else ""
    check("D7 看板页面可访问", status == 200 and "运行看板" in html, f"HTTP={status}")
    check("D8 看板带密钥输入引导（生产可用）", "readKey" in html and "rag_key" in html)

    status, payload = call("GET", "/stats", key=key)
    check("D9 统计接口 200", status == 200, f"HTTP={status}")

    # ---------------- E. 密钥生命周期 ----------------
    section("E. 密钥生命周期：吊销即时生效")

    status, payload = call("GET", "/auth/keys?include_revoked=false", key=key)
    keys = (unwrap(payload) or {}).get("items") or []
    check("E1 密钥列表不含明文", status == 200 and all("api_key" not in k for k in keys),
          f"共 {len(keys)} 个密钥")

    status, _ = call("DELETE", f"/auth/keys/{me.get('key_id')}", key=key)
    check("E2 禁止吊销自身密钥（422）", status == 422, f"HTTP={status}")

    status, payload = call("DELETE", f"/auth/keys/{viewer_id}", key=key)
    check("E3 可吊销他人密钥", status == 200, f"HTTP={status}")

    status, _ = call("GET", "/auth/me", key=viewer)
    check("E4 被吊销密钥立即失效（401，无需等缓存过期）", status == 401, f"HTTP={status}")

    status, _ = call("GET", "/auth/keys?include_revoked=true", key=key)
    check("E5 已吊销密钥可查询留存", status == 200, f"HTTP={status}")

    # ---------------- 汇总 ----------------
    total = len(passed) + len(failed)
    section("验收结果")
    print(f"  通过 {len(passed)} / {total}    失败 {len(failed)}")
    if failed:
        print("\n  失败项：")
        for f in failed:
            print(f"    - {f}")
        return 1
    print("\n  ✅ P2 四条主链路全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
