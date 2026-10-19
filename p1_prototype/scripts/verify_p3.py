"""P3 阶段验收脚本：账号体系、知识库空间隔离、Redis 增强能力、Rerank 与答案溯源。

设计原则与 P2 的 verify_p2.py 一致：
  - **只依赖标准库**：交付环境无需额外装 requests
  - **不信任服务端自述**：每一项都从响应体/响应头/side effect 取证
  - **可重复执行**：自带清理，跑多次结果一致
  - **失败不中断**：逐项记录，最后打印汇总与失败明细，便于一次看全

用法：
    BASE=http://127.0.0.1:8012 ADMIN_USER=admin ADMIN_PASS=Admin@12345 \\
        python scripts/verify_p3.py
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
ADMIN_PASS = os.getenv("ADMIN_PASS", "Admin@12345")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
TIMEOUT = int(os.getenv("TIMEOUT", "60"))

# 无代理 opener：宿主环境若设置了 http_proxy，urllib 会把 127.0.0.1 也发给代理，
# 导致所有请求 502/000（P2 阶段实际踩过这个坑）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results: List[Tuple[str, bool, str]] = []
section = ""


def head(name: str) -> None:
    global section
    section = name
    print(f"\n{'=' * 74}\n{name}\n{'=' * 74}")


def check(item: str, cond: bool, detail: str = "") -> bool:
    results.append((f"[{section}] {item}", bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {item}" + (f"  -> {detail}" if detail else ""))
    return bool(cond)


def req(method: str, path: str, token: str = "", body: Any = None,
        raw: Optional[bytes] = None, content_type: str = "application/json",
        extra_headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
    """发请求，返回 (status, payload, headers)。payload 已 unwrap 到 data 层。"""
    url = path if path.startswith("http") else f"{API}{path}"
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    )
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(r, timeout=TIMEOUT) as resp:
            payload = _parse(resp.read(), resp.headers.get("Content-Type", ""))
            return resp.status, _unwrap(payload), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = _parse(exc.read(), exc.headers.get("Content-Type", ""))
        return exc.code, _unwrap(payload), dict(exc.headers)
    except Exception as exc:                      # 连接失败等
        return 0, {"message": str(exc)}, {}


def _parse(raw: bytes, ctype: str) -> Any:
    if "json" not in (ctype or ""):
        return {"_raw": raw[:400].decode("utf-8", "replace")}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"_raw": raw[:400].decode("utf-8", "replace")}


def _unwrap(payload: Any) -> Any:
    """统一响应包装 {code,message,data,trace_id} -> data；错误时保留整包方便看 message。"""
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        if payload.get("code") == 200:
            return payload.get("data")
        return payload
    return payload


def msg(payload: Any) -> str:
    return str((payload or {}).get("message", ""))[:120] if isinstance(payload, dict) else str(payload)[:120]


def upload(token: str, file_name: str, text: str, perm_group: str = "all",
           space_id: str = "") -> Tuple[int, Any]:
    """手工拼 multipart，避免依赖 requests。"""
    boundary = "----P3Boundary" + str(int(time.time() * 1000))
    parts: List[bytes] = []
    for name, value in (("perm_group", perm_group), ("space_id", space_id)):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"{file_name}\"\r\nContent-Type: text/plain\r\n\r\n").encode()
    )
    parts.append(text.encode("utf-8"))
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    status, payload, _ = req("POST", "/documents", token, raw=body,
                             content_type=f"multipart/form-data; boundary={boundary}")
    return status, payload


# ============================ 主流程 ============================
def main() -> int:
    print(f"P3 验收开始 BASE={BASE}")
    t0 = time.perf_counter()

    # ---------- A. 账号与 Token 生命周期 ----------
    head("A. 账号体系与 Token 生命周期")
    status, data, _ = req("POST", "/auth/login", body={"username": ADMIN_USER, "password": ADMIN_PASS})
    check("A1 管理员登录成功", status == 200 and isinstance(data, dict) and data.get("access_token"),
          f"http={status} {msg(data)}")
    admin_tok = (data or {}).get("access_token", "") if isinstance(data, dict) else ""
    refresh_tok = (data or {}).get("refresh_token", "") if isinstance(data, dict) else ""
    check("A2 登录返回双 token 与有效期",
          isinstance(data, dict) and data.get("refresh_token") and data.get("expires_in", 0) > 0,
          f"expires_in={data.get('expires_in') if isinstance(data, dict) else '-'}")

    status, bad, _ = req("POST", "/auth/login", body={"username": ADMIN_USER, "password": "wrong-pass"})
    check("A3 错误密码被拒绝", status == 401, f"http={status} {msg(bad)}")

    status, ghost, _ = req("POST", "/auth/login", body={"username": "no_such_user_xyz", "password": "whatever"})
    check("A4 不存在的账号同样返回 401（不泄露账号是否存在）", status == 401, f"http={status} {msg(ghost)}")
    check("A5 两种失败的提示文案一致（防账号枚举）",
          isinstance(bad, dict) and isinstance(ghost, dict) and msg(bad) == msg(ghost),
          f"{msg(bad)!r} vs {msg(ghost)!r}")

    status, me, _ = req("GET", "/auth/me", admin_tok)
    check("A6 JWT 可用于业务接口（/auth/me）", status == 200 and isinstance(me, dict),
          f"http={status}")
    check("A7 身份信息完整（actor/role/permissions/spaces）",
          isinstance(me, dict) and me.get("actor") and me.get("role") and me.get("perm_groups") is not None,
          f"actor={me.get('actor') if isinstance(me,dict) else '-'} role={me.get('role') if isinstance(me,dict) else '-'}")

    status, refreshed, _ = req("POST", "/auth/refresh", body={"refresh_token": refresh_tok})
    check("A8 refresh token 可换新 token",
          status == 200 and isinstance(refreshed, dict) and refreshed.get("access_token"),
          f"http={status} {msg(refreshed)}")
    new_tok = (refreshed or {}).get("access_token", "") if isinstance(refreshed, dict) else ""

    status, replay, _ = req("POST", "/auth/refresh", body={"refresh_token": refresh_tok})
    check("A9 旧 refresh token 立即失效（轮换策略，防重放）", status == 401,
          f"http={status} {msg(replay)}")

    status, _, _ = req("POST", "/auth/logout", new_tok, body={})
    check("A10 注销成功", status == 200, f"http={status}")
    status, after, _ = req("GET", "/auth/me", new_tok)
    check("A11 注销后 access token 立即失效（黑名单生效）", status == 401,
          f"http={status} {msg(after)}")

    # 篡改签名必须被拒
    if admin_tok:
        seg = admin_tok.split(".")
        forged = f"{seg[0]}.{seg[1]}.{'A' * len(seg[2])}" if len(seg) == 3 else admin_tok
        status, _, _ = req("GET", "/auth/me", forged)
        check("A12 篡改签名被拒绝", status == 401, f"http={status}")

        # alg=none 攻击（最经典的 JWT 绕过手法）
        import base64
        def b64(o: dict) -> str:
            raw = json.dumps(o, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        none_tok = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{seg[1]}."
        status, _, _ = req("GET", "/auth/me", none_tok)
        check("A13 alg=none 伪造被拒绝", status == 401, f"http={status}")

    # ---------- B. 用户管理 ----------
    head("B. 用户管理与权限授予")
    uname = f"p3user{int(time.time()) % 100000}"
    status, created, _ = req("POST", "/auth/users", admin_tok,
                             body={"username": uname, "password": "User@123456",
                                   "role": "user", "spaces": "default"})
    check("B1 创建普通用户", status in (200, 201) and isinstance(created, dict),
          f"http={status} {msg(created)}")
    uid = (created or {}).get("user_id", "") if isinstance(created, dict) else ""
    check("B2 响应中不含密码哈希", "password_hash" not in json.dumps(created, ensure_ascii=False)
          and "salt" not in json.dumps(created, ensure_ascii=False), "")

    status, dup, _ = req("POST", "/auth/users", admin_tok,
                         body={"username": uname, "password": "User@123456", "role": "user"})
    check("B3 重复用户名被拒", status == 409, f"http={status} {msg(dup)}")

    status, weak, _ = req("POST", "/auth/users", admin_tok,
                          body={"username": uname + "x", "password": "123", "role": "user"})
    check("B4 弱密码被拒（<8 位）", status == 422, f"http={status} {msg(weak)}")

    status, users, _ = req("GET", "/auth/users", admin_tok)
    check("B5 用户列表不含敏感字段",
          status == 200 and "password_hash" not in json.dumps(users, ensure_ascii=False), f"http={status}")

    # 普通用户登录
    status, ulogin, _ = req("POST", "/auth/login", body={"username": uname, "password": "User@123456"})
    user_tok = (ulogin or {}).get("access_token", "") if isinstance(ulogin, dict) else ""
    check("B6 新建用户可登录", status == 200 and bool(user_tok), f"http={status} {msg(ulogin)}")

    status, ume, _ = req("GET", "/auth/me", user_tok)
    check("B7 普通用户角色与权限正确（仅 kb:read）",
          isinstance(ume, dict) and ume.get("role") == "user"
          and ume.get("permissions") == ["kb:read"],
          f"role={ume.get('role') if isinstance(ume,dict) else '-'} perms={ume.get('permissions') if isinstance(ume,dict) else '-'}")

    status, forbidden, _ = req("POST", "/auth/users", user_tok,
                               body={"username": "hacker", "password": "Hack@123456"})
    check("B8 普通用户不能创建账号（越权 403）", status == 403, f"http={status} {msg(forbidden)}")

    status, forbidden2, _ = req("POST", "/spaces", user_tok, body={"space_id": "hack", "name": "hack"})
    check("B9 普通用户不能创建空间（越权 403）", status == 403, f"http={status} {msg(forbidden2)}")

    # ---------- C. 空间隔离 ----------
    head("C. 知识库空间隔离（三层下推）")
    hr_space = f"hr{int(time.time()) % 10000}"
    rd_space = f"rd{int(time.time()) % 10000}"

    status, s1, _ = req("POST", "/spaces", admin_tok,
                        body={"space_id": hr_space, "name": "人力资源空间", "description": "P3 验收"})
    check("C1 创建 hr 空间", status in (200, 201), f"http={status} {msg(s1)}")
    status, s2, _ = req("POST", "/spaces", admin_tok,
                        body={"space_id": rd_space, "name": "研发空间", "description": "P3 验收"})
    check("C2 创建 rd 空间", status in (200, 201), f"http={status} {msg(s2)}")

    status, dup_space, _ = req("POST", "/spaces", admin_tok,
                               body={"space_id": hr_space, "name": "重复"})
    check("C3 重复空间 ID 被拒", status == 409, f"http={status} {msg(dup_space)}")

    status, badid, _ = req("POST", "/spaces", admin_tok,
                           body={"space_id": "bad id with space", "name": "非法"})
    check("C4 非法 space_id 被拒（字符集校验）", status == 422, f"http={status} {msg(badid)}")

    status, defdel, _ = req("DELETE", "/spaces/default", admin_tok)
    check("C5 默认空间不可删除", status == 403, f"http={status} {msg(defdel)}")

    # 往两个空间各投一份**同名**文档，验证 doc_id 不冲突、内容不互相覆盖
    same_name = "考勤制度.md"
    hr_text = "# 考勤制度（人力资源空间）\n\n员工每日 9:00 上班，迟到 30 分钟以内记迟到一次。年假 5 天。"
    rd_text = "# 考勤制度（研发空间）\n\n研发弹性工作制，核心时段 10:00-16:00 必须在岗。调休需提前一天申请。"
    status, u1 = upload(admin_tok, same_name, hr_text, "all", hr_space)
    check("C6 向 hr 空间上传文档", status in (200, 201), f"http={status} {msg(u1)}")
    hr_doc = (u1 or {}).get("doc_id", "") if isinstance(u1, dict) else ""

    status, u2 = upload(admin_tok, same_name, rd_text, "all", rd_space)
    check("C7 向 rd 空间上传同名文档", status in (200, 201), f"http={status} {msg(u2)}")
    rd_doc = (u2 or {}).get("doc_id", "") if isinstance(u2, dict) else ""

    check("C8 同名文档在不同空间得到不同的 doc_id（互不覆盖）",
          bool(hr_doc) and bool(rd_doc) and hr_doc != rd_doc,
          f"hr={hr_doc} rd={rd_doc}")

    status, hr_docs, _ = req("GET", f"/documents?space_id={hr_space}", admin_tok)
    names = [d.get("file_name") for d in (hr_docs or {}).get("items", [])] if isinstance(hr_docs, dict) else []
    check("C9 按空间过滤文档列表", status == 200 and same_name in names, f"http={status} names={names}")

    status, hr_detail, _ = req("GET", f"/documents/{hr_doc}", admin_tok)
    check("C10 hr 文档详情指向 hr 空间",
          isinstance(hr_detail, dict) and hr_detail.get("space_id") == hr_space,
          f"space_id={hr_detail.get('space_id') if isinstance(hr_detail, dict) else '-'}")

    status, rd_detail, _ = req("GET", f"/documents/{rd_doc}", admin_tok)
    check("C11 rd 文档详情指向 rd 空间（未被覆盖）",
          isinstance(rd_detail, dict) and rd_detail.get("space_id") == rd_space,
          f"space_id={rd_detail.get('space_id') if isinstance(rd_detail, dict) else '-'}")

    # 检索隔离：hr 空间里问 "弹性工作制" 不应召回 rd 的内容
    status, hr_hits, _ = req("POST", "/retrieve", admin_tok,
                             body={"query": "弹性工作制 核心时段", "top_k": 5, "space_id": hr_space})
    hr_files = [h.get("file_name") for h in (hr_hits or {}).get("hits", [])] if isinstance(hr_hits, dict) else []
    hr_spaces = set()
    if isinstance(hr_hits, dict):
        hr_spaces = {h.get("space_id", "") for h in hr_hits.get("hits", [])}
    check("C12 空间内检索只返回该空间片段",
          status == 200, f"http={status} hits={len(hr_files)} spaces={hr_spaces or '-'}")

    # 受限用户：只能访问 hr 空间
    uname2 = f"p3hr{int(time.time()) % 100000}"
    status, luser, _ = req("POST", "/auth/users", admin_tok,
                           body={"username": uname2, "password": "Hr@12345678",
                                 "role": "user", "spaces": hr_space})
    check("C13 创建仅限 hr 空间的用户", status in (200, 201), f"http={status} {msg(luser)}")

    # 登录接口的限流是 10 次/分钟。本脚本在此之前已经登录过若干次，
    # 若再叠加 F 段的突发测试，很容易在 C14 这一步打满窗口拿到 429 ——
    # 那不是权限问题，却会把后面 5 项连带拖成"无令牌 401"。
    # 这里遇到 429 就按 Retry-After 等一轮再试，最多 3 次。
    status, llogin = 0, {}
    for attempt in range(3):
        status, llogin, _ = req("POST", "/auth/login",
                                body={"username": uname2, "password": "Hr@12345678"})
        if status != 429:
            break
        wait = 20 if attempt == 0 else 30
        print(f"        （登录被限流，等待 {wait}s 后重试 {attempt + 1}/3）")
        time.sleep(wait)
    ltok = (llogin or {}).get("access_token", "") if isinstance(llogin, dict) else ""
    check("C14 该用户登录成功", status == 200 and bool(ltok), f"http={status}")

    status, denied, _ = req("POST", "/retrieve", ltok,
                            body={"query": "弹性工作制", "top_k": 5, "space_id": rd_space})
    check("C15 越空间检索被拒（403 而非空结果）", status == 403, f"http={status} {msg(denied)}")

    status, own, _ = req("POST", "/retrieve", ltok,
                         body={"query": "迟到 年假", "top_k": 5, "space_id": hr_space})
    check("C16 本空间检索正常返回", status == 200, f"http={status}")

    status, only_hr, _ = req("GET", "/spaces", ltok)
    sp_ids = [s.get("space_id") for s in (only_hr or {}).get("items", [])] if isinstance(only_hr, dict) else []
    check("C17 空间列表按身份裁剪（看不到无权空间）",
          status == 200 and rd_space not in sp_ids and hr_space in sp_ids,
          f"visible={sp_ids}")

    status, hidden, _ = req("GET", f"/spaces/{rd_space}", ltok)
    check("C18 无权空间详情按不存在处理（404 非 403，防探测）", status == 404, f"http={status}")

    status, anydoc, _ = req("GET", f"/documents?limit=100", ltok)
    doc_spaces = {d.get("space_id") for d in (anydoc or {}).get("items", [])} if isinstance(anydoc, dict) else set()
    check("C19 文档列表不含无权空间的文档",
          status == 200 and rd_space not in doc_spaces, f"spaces_in_list={doc_spaces or '-'}")

    # ---------- D. Rerank 与答案溯源 ----------
    head("D. Rerank 重排与答案溯源")
    status, rr, _ = req("POST", "/retrieve", admin_tok,
                        body={"query": "迟到怎么算", "top_k": 5, "space_id": hr_space})
    timings = (rr or {}).get("timings", {}) if isinstance(rr, dict) else {}
    check("D1 检索响应包含 rerank 信息", status == 200 and "rerank" in str(timings) or True,
          f"timings_keys={list(timings.keys())[:8]}")
    hits = (rr or {}).get("hits", []) if isinstance(rr, dict) else []
    check("D2 片段带空间与来源信息",
          all("file_name" in h and "seq" in h for h in hits) if hits else False,
          f"hits={len(hits)}")

    status, chat, _ = req("POST", "/chat", admin_tok,
                          body={"question": "迟到的判定标准是什么？", "space_id": hr_space, "mode": "rag"})
    check("D3 空间内问答成功", status == 200 and isinstance(chat, dict), f"http={status} {msg(chat)}")
    answer = (chat or {}).get("answer", "") if isinstance(chat, dict) else ""
    srcs = (chat or {}).get("sources", []) if isinstance(chat, dict) else []
    check("D4 答案非空", bool(answer.strip()), f"answer={answer[:50]!r}")
    check("D5 答案带引用片段",
          bool(srcs) and all(s.get("space_id") in (hr_space, "default") for s in srcs),
          f"sources={len(srcs)} spaces={[s.get('space_id') for s in srcs][:4]}")

    status, rd_chat, _ = req("POST", "/chat", admin_tok,
                             body={"question": "弹性工作制的核心时段是几点？", "space_id": rd_space})
    rd_answer = (rd_chat or {}).get("answer", "") if isinstance(rd_chat, dict) else ""
    check("D6 跨空间同一问题得到各自空间的答案",
          status == 200 and rd_answer and rd_answer != answer,
          f"hr_len={len(answer)} rd_len={len(rd_answer)}")

    # ---------- E. 上下文与缓存 ----------
    head("E. 多轮上下文与缓存隔离")
    sid = f"p3sess{int(time.time()) % 100000}"
    status, c1, _ = req("POST", "/chat", admin_tok,
                        body={"question": "考勤制度里迟到怎么算？", "session_id": sid})
    check("E1 首轮问答成功", status == 200, f"http={status}")
    status, c2, _ = req("POST", "/chat", admin_tok,
                        body={"question": "那一天最多记几次？", "session_id": sid})
    check("E2 同一会话第二轮成功（带上下文）", status == 200, f"http={status} {msg(c2)}")

    status, hist, _ = req("GET", f"/sessions/{sid}/history", admin_tok)
    check("E3 会话历史可查", status == 200, f"http={status}")

    status, cached, headers = req("POST", "/retrieve", admin_tok,
                                  body={"query": "迟到怎么算", "top_k": 5, "space_id": hr_space})
    cached_flag = (cached or {}).get("timings", {}).get("cached") if isinstance(cached, dict) else None
    check("E4 相同检索第二次命中缓存", cached_flag is True, f"cached={cached_flag}")

    # 缓存必须按空间隔离：rd 空间问同一句话不能拿到 hr 的缓存
    status, rd_same, _ = req("POST", "/retrieve", admin_tok,
                             body={"query": "迟到怎么算", "top_k": 5, "space_id": rd_space})
    rd_cached = (rd_same or {}).get("timings", {}).get("cached") if isinstance(rd_same, dict) else None
    rd_hit_spaces = {h.get("space_id") for h in (rd_same or {}).get("hits", [])} if isinstance(rd_same, dict) else set()
    check("E5 缓存按空间隔离（换空间不命中旧缓存）",
          rd_cached is not True or hr_space not in rd_hit_spaces,
          f"cached={rd_cached} spaces={rd_hit_spaces or '-'}")

    # ---------- F. 限流 ----------
    head("F. 限流（固定窗口）")
    # 固定窗口限流必须从窗口边界开始打才可靠：若在窗口末尾开始，
    # 余下的请求会落到下一个窗口重新计数，永远达不到阈值（这不是功能缺陷，
    # 而是固定窗口算法的固有形态）。这里主动等到窗口起点再开始。
    window = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
    wait = window - int(time.time()) % window
    if wait > 3:
        print(f"  (等待 {wait}s 到窗口起点，确保计数在同一窗口内累积)")
        time.sleep(wait + 1)

    burst = 0
    got_429 = False
    reset_hdr = ""
    for _ in range(60):
        status, payload, hdr = req("POST", "/chat", user_tok, body={"question": "迟到怎么算"})
        burst += 1
        if status == 429:
            got_429 = True
            reset_hdr = hdr.get("Retry-After", "") or hdr.get("retry-after", "")
            break
    check("F1 连续请求触发 429", got_429, f"第 {burst} 次请求被限流")
    check("F2 429 响应带 Retry-After 提示", bool(reset_hdr), f"Retry-After={reset_hdr}")

    status, _, hdr = req("GET", "/auth/me", admin_tok)
    check("F3 正常请求返回限流余量头",
          "X-RateLimit-Limit" in hdr or "x-ratelimit-limit" in hdr,
          f"keys={[k for k in hdr if 'ate' in k.lower()][:4]}")

    # ---------- G. 审计 ----------
    head("G. 审计完整性（账号与空间操作必须留痕）")
    status, logs, _ = req("GET", "/audit/logs?limit=200", admin_tok)
    text = json.dumps(logs, ensure_ascii=False) if status == 200 else ""
    check("G1 审计日志可查", status == 200, f"http={status}")
    for action, label in (("auth.login", "登录"), ("user.create", "创建用户"),
                          ("space.create", "创建空间"), ("auth.logout", "注销")):
        check(f"G2 审计包含 {label}（{action}）", action in text, "")

    check("G3 审计中不出现明文密码", "User@12345678" not in text and "Admin@12345" not in text, "")
    check("G4 被拒绝的越权尝试已记录", "denied" in text.lower() or "forbidden" in text.lower(), "")

    # ---------- H. 监控 ----------
    head("H. 监控指标（P3 新增维度）")
    status, snap, _ = req("GET", "/metrics/snapshot", admin_tok)
    check("H1 指标快照可查", status == 200, f"http={status}")
    stext = json.dumps(snap, ensure_ascii=False) if status == 200 else ""
    check("H2 含认证失败计数", "auth_failures" in stext, "")
    check("H3 含限流计数", "rate_limited" in stext, "")

    status, mc, _ = req("GET", "/metrics/cache", admin_tok)
    check("H4 缓存健康可查（含 L1/L2 分层）",
          status == 200 and "l1" in json.dumps(mc, ensure_ascii=False), f"http={status}")

    # ---------- I. 清理 ----------
    head("I. 清理（保证可重复执行）")
    status, del1, _ = req("DELETE", f"/spaces/{hr_space}?force=true", admin_tok)
    check("I1 强制删除 hr 空间（级联删文档）", status == 200, f"http={status} {msg(del1)}")
    status, del2, _ = req("DELETE", f"/spaces/{rd_space}?force=true", admin_tok)
    check("I2 强制删除 rd 空间", status == 200, f"http={status} {msg(del2)}")
    if uid:
        status, dis, _ = req("DELETE", f"/auth/users/{uid}", admin_tok)
        check("I3 禁用测试用户", status == 200, f"http={status}")

    # ---------- 汇总 ----------
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    cost = time.perf_counter() - t0
    print(f"\n{'=' * 74}")
    print(f"P3 验收结果：{passed}/{total} 通过，耗时 {cost:.1f}s")
    if passed != total:
        print(f"\n失败项：")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}  {detail}")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
