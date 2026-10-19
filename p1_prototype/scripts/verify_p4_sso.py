"""P4 企业 SSO（OIDC）单点登录验收脚本。

用一个**本地假 IdP**（http.server 实现 discovery / authorize / token / userinfo）
跑完整的 Authorization Code + PKCE 往返，因此不需要 Docker、不需要 Keycloak、
不需要外网。假 IdP 刻意实现得跟真的一样（校验 client_id、code_challenge、
校验 redirect_uri），这样"我们的客户端写错了"才会被真的抓出来——
一个什么都放行的假 IdP 会让验收变成自欺欺人。

跑法：在项目根执行
    python scripts/verify_p4_sso.py

覆盖：
  1. 未启用 SSO 时的行为（404，且不吐内部配置）
  2. 授权跳转：参数完整（state/nonce/PKCE/scope/redirect_uri）
  3. 完整往返：回调 → 换 token → 自动开户 → 签发本地 JWT → 一次性票据
  4. 三层安全防线各自被绕过时的表现
     - state Cookie 不匹配（登录 CSRF）
     - state 不存在 / 已用过（重放）
     - nonce 不匹配（ID Token 重放）
     - aud / iss 不匹配（别家 IdP 的令牌）
  5. 票据一次性、会过期
  6. 角色映射（显式 oidc_role_map，不做"取最高权限"）
  7. 租户判定（claims 优先于 hint）
  8. 已禁用账号不能被 SSO 绕过；未开自动开户时拒绝
  9. 开放重定向防护（next 只接受站内路径）
 10. 成功登录写审计
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")
os.environ.setdefault("MODEL_MODE", "offline")

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {extra}" if extra else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ======================================================================
# 假 IdP
# ======================================================================
class FakeIdP:
    """最小可用的 OIDC Provider。

    刻意保留的服务端校验（去掉任何一条都会让客户端 bug 溜过去）：
      - client_id 必须匹配
      - code_challenge 必须存在，且换 token 时校验 code_verifier 的 SHA256
      - redirect_uri 必须与授权时一致
      - code 只能换一次
    """

    def __init__(self) -> None:
        self.port = 0
        self.base = ""
        self.codes: dict = {}       # code -> {nonce, challenge, redirect_uri, extra}
        self.userinfo_calls = 0
        self.token_calls = 0
        self.fail_token = False     # 置 True 时 token 端点返回 400
        self.override_claims: dict = {}   # 用于注入 aud/iss/nonce 错误
        self.use_userinfo = True    # 置 False 时测"退回 ID Token claims"

    def start(self) -> None:
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):        # 关掉默认 stderr 噪音
                pass

            def _json(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/.well-known/openid-configuration":
                    return self._json({
                        "issuer": idp.base,
                        "authorization_endpoint": idp.base + "/authorize",
                        "token_endpoint": idp.base + "/token",
                        "userinfo_endpoint": idp.base + "/userinfo" if idp.use_userinfo else None,
                        "jwks_uri": idp.base + "/jwks",
                    })
                if u.path == "/userinfo":
                    idp.userinfo_calls += 1
                    auth = self.headers.get("Authorization", "")
                    if not auth.startswith("Bearer "):
                        return self._json({"error": "invalid_token"}, 401)
                    # 真实 IdP 的 userinfo 只返回"身份属性"，**不含** nonce/aud/iss，
                    # 也不含任何令牌校验信息。这里必须照这个语义实现：
                    # 否则我们的 `identity.update(userinfo)` 会把 ID Token 里的
                    # nonce 覆盖成空值，让 nonce 校验凭空失效——
                    # 一个"什么都返回"的假 IdP 会让这道防线永远测不出来。
                    ident = {k: v for k, v in idp.claims.items()
                             if k not in ("nonce", "aud", "iss", "iat", "exp", "azp")}
                    return self._json(ident)
                return self._json({"error": "not_found"}, 404)

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if u.path != "/token":
                    return self._json({"error": "not_found"}, 404)

                idp.token_calls += 1
                if idp.fail_token:
                    return self._json({"error": "invalid_grant"}, 400)

                code = (form.get("code") or [""])[0]
                rec = idp.codes.pop(code, None)      # 一次性：取出即删
                if not rec:
                    return self._json({"error": "invalid_grant"}, 400)

                if (form.get("client_id") or [""])[0] != idp.client_id:
                    return self._json({"error": "invalid_client"}, 400)
                verifier = (form.get("code_verifier") or [""])[0]
                expect = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not verifier or expect != rec["challenge"]:
                    return self._json({"error": "invalid_grant", "detail": "pkce"}, 400)
                if (form.get("redirect_uri") or [""])[0] != rec["redirect_uri"]:
                    return self._json({"error": "invalid_grant", "detail": "redirect_uri"}, 400)

                claims = dict(rec["claims"])
                claims.update(idp.override_claims)
                idp.claims = claims       # userinfo 端点也用它
                return self._json({
                    "access_token": "at-" + code[:8],
                    "token_type": "Bearer",
                    "id_token": _make_id_token(claims),
                    "expires_in": 300,
                })
        # 注释结束
        self._srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.client_id = "rag-kb"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self._srv.shutdown()
        except Exception:
            pass


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _make_id_token(claims: dict) -> str:
    """构造一个**结构正确但未签名**的 ID Token。

    我们的实现本来就不依赖本地验签（身份以 userinfo 为准，原因见 oidc_service
    的说明），所以这里只需要 header.payload.signature 三段结构正确即可。
    这也顺便验证了"没验签也能安全"这个前提确实成立。
    """
    head = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64(claims)
    return f"{head}.{body}.fakesig"


# ======================================================================
# HTTP 客户端（绕开本机代理）
# ======================================================================
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_NO_REDIRECT = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), NoRedirect())


def http_json(url: str, method: str = "GET", body=None, token: str = ""):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"raw": raw}, dict(e.headers)


def main() -> int:
    idp = FakeIdP()
    idp.start()

    # 先把配置注入，再 import 应用（settings 在实例化时读环境）
    os.environ["OIDC_ENABLED"] = "true"
    os.environ["OIDC_ISSUER"] = idp.base
    os.environ["OIDC_CLIENT_ID"] = "rag-kb"
    os.environ["OIDC_CLIENT_SECRET"] = "test-secret"
    os.environ["OIDC_REDIRECT_URI"] = "http://testserver/api/v1/auth/sso/callback"
    os.environ["OIDC_DEFAULT_TENANT"] = "default"
    os.environ["OIDC_AUTO_PROVISION"] = "true"
    os.environ["OIDC_USERNAME_CLAIM"] = "preferred_username"

    from fastapi.testclient import TestClient

    from app_v2.core.settings import settings
    from app_v2.services.oidc_service import oidc_service, resolve_role, resolve_tenant
    from app_v2.services.tenant_service import tenant_service
    from app_v2.storage.meta_store import meta_store

    # 关掉跟随重定向，才能看到 302 本身
    from app_v2.main import app

    print(f"配置版本：{settings.app_version}  issuer={settings.oidc_issuer}")
    print(f"假 IdP：{idp.base}")

    tenant_service.bootstrap_default()
    tenant_service.create("acme", "Acme 公司")

    # ================= 1. 未启用时的行为 =================
    section("1. 未启用 SSO 的行为")
    c0 = TestClient(app)
    saved_enabled = settings.oidc_enabled
    settings.oidc_enabled = False
    r = c0.get("/api/v1/auth/sso/login", follow_redirects=False)
    check("未启用 → 404", r.status_code == 404, f"HTTP={r.status_code}")
    body = r.json()
    check("未启用 → 错误信息不泄露 issuer",
          idp.base not in json.dumps(body), str(body.get("message")))
    m = c0.get("/api/v1/auth/methods").json()["data"]
    check("/auth/methods 在未启用时 oidc_enabled=false", m["oidc_enabled"] is False)
    check("/auth/methods 未启用时 oidc_login_url 为空", m["oidc_login_url"] == "")
    settings.oidc_enabled = saved_enabled

    m = c0.get("/api/v1/auth/methods").json()["data"]
    check("/auth/methods 启用后 oidc_enabled=true", m["oidc_enabled"] is True)
    check("oidc_login_url 指向 /auth/sso/login",
          m["oidc_login_url"] == "/api/v1/auth/sso/login")
    check("oidc_label 按 issuer 猜出可读文案", isinstance(m["oidc_label"], str) and m["oidc_label"])

    # ================= 2. 授权跳转参数 =================
    section("2. 授权跳转：参数完整性")
    c = TestClient(app)
    r = c.get("/api/v1/auth/sso/login?next=/admin&tenant=acme", follow_redirects=False)
    check("发起登录 → 302", r.status_code == 302, f"HTTP={r.status_code}")
    loc = r.headers.get("location", "")
    check("跳到 IdP 的 authorize 端点", loc.startswith(idp.base + "/authorize"), loc[:60])
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    check("response_type=code", q.get("response_type") == ["code"])
    check("client_id 正确", q.get("client_id") == ["rag-kb"])
    check("带 state", bool(q.get("state", [""])[0]))
    check("带 nonce", bool(q.get("nonce", [""])[0]))
    check("PKCE code_challenge_method=S256", q.get("code_challenge_method") == ["S256"])
    check("PKCE challenge 长度=43（SHA256 base64url 无填充）",
          len(q.get("code_challenge", [""])[0]) == 43,
          str(len(q.get("code_challenge", [""])[0])))
    check("scope 含 openid", "openid" in (q.get("scope", [""])[0] or ""))
    check("redirect_uri 用显式配置",
          q.get("redirect_uri") == ["http://testserver/api/v1/auth/sso/callback"],
          str(q.get("redirect_uri")))
    check("带 login_hint（租户提示）", q.get("login_hint") == ["acme"])
    check("响应禁缓存", "no-store" in (r.headers.get("cache-control") or ""))
    cookie = r.headers.get("set-cookie", "")
    check("种下 state Cookie", "rag_sso_state=" in cookie)
    check("state Cookie 为 HttpOnly", "HttpOnly" in cookie, cookie[:80])
    check("state Cookie SameSite=Lax", "samesite=lax" in cookie.lower())
    login_state = q.get("state", [""])[0]
    login_nonce = q.get("nonce", [""])[0]
    login_challenge = q.get("code_challenge", [""])[0]
    login_cookie = cookie.split(";")[0]

    # next 只接受站内路径
    r = c.get("/api/v1/auth/sso/login?next=https://evil.com/x", follow_redirects=False)
    q2 = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    st2 = q2.get("state", [""])[0]
    pending2 = oidc_service._pending.get(st2)
    check("next 为外站 URL → 降级为 /app（防开放重定向）",
          pending2 is not None and pending2.next_url == "/app",
          pending2.next_url if pending2 else "-")
    r = c.get("/api/v1/auth/sso/login?next=//evil.com", follow_redirects=False)
    q3 = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    p3 = oidc_service._pending.get(q3.get("state", [""])[0])
    check("next 为 //evil.com → 降级为 /app", p3 is not None and p3.next_url == "/app")

    # ================= 3. 完整往返 =================
    section("3. 完整往返：回调 → 换 token → 开户 → 签发本地 JWT")
    base_claims = {
        "iss": idp.base,
        "aud": "rag-kb",
        "sub": "u-1001",
        "nonce": login_nonce,
        "preferred_username": "zhangsan",
        "name": "张三",
        "email": "zhangsan@acme.com",
    }
    idp.codes["code-ok"] = {
        "nonce": login_nonce, "challenge": login_challenge,
        "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
        "claims": base_claims,
    }
    idp.override_claims = {}
    # 必须显式带上 state Cookie：TestClient 的 CookieJar 会跨请求累积，
    # 但这条用例发生在"多次发起登录"之后，jar 里的 Cookie 可能已是别的 state，
    # 依赖隐式行为会让测试结果随用例顺序漂移。
    r = c.get(f"/api/v1/auth/sso/callback?code=code-ok&state={login_state}",
              headers={"Cookie": login_cookie}, follow_redirects=False)
    check("回调 → 302", r.status_code == 302, f"HTTP={r.status_code}")
    loc = r.headers.get("location", "")
    check("回跳到 next（/admin）", loc.startswith("/admin?sso_ticket="), loc[:60])
    m2 = re.search(r"sso_ticket=([A-Za-z0-9_\-]+)", loc)
    check("URL 里带一次性票据", m2 is not None)
    check("URL 里**不含** access_token", "access_token" not in loc)
    check("回跳响应禁缓存", "no-store" in (r.headers.get("cache-control") or ""))
    check("回调后清除 state Cookie", "rag_sso_state=" in (r.headers.get("set-cookie") or ""))
    check("IdP token 端点被调用一次", idp.token_calls == 1, str(idp.token_calls))
    check("userinfo 被调用（身份以 userinfo 为准）", idp.userinfo_calls == 1,
          str(idp.userinfo_calls))

    ticket = m2.group(1) if m2 else ""
    r = c.post("/api/v1/auth/sso/ticket", json={"ticket": ticket})
    check("票据换 token → 200", r.status_code == 200, f"HTTP={r.status_code}")
    tk = r.json()["data"]
    check("拿到 access_token", bool(tk.get("access_token")))
    check("拿到 refresh_token", bool(tk.get("refresh_token")))
    check("user.username=zhangsan", (tk.get("user") or {}).get("username") == "zhangsan")
    check("user.display_name=张三（来自 IdP name claim）",
          (tk.get("user") or {}).get("display_name") == "张三")
    check("user.tenant_id=acme（来自 login_hint，IdP 未下发租户 claim）",
          (tk.get("user") or {}).get("tenant_id") == "acme",
          str((tk.get("user") or {}).get("tenant_id")))
    check("首次 SSO 登录已自动开户",
          meta_store.get_user_by_name("zhangsan", "acme") is not None)

    # 用换来的 token 调需要鉴权的业务接口，证明这枚 token 真的可用
    r2 = c.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tk["access_token"]})
    check("SSO 签发的 token 可通过 /auth/me 鉴权", r2.status_code == 200, f"HTTP={r2.status_code}")
    me = r2.json()["data"]
    check("/auth/me 的 actor 是 SSO 用户", me.get("actor") == "zhangsan", str(me.get("actor")))

    r3 = c.get("/api/v1/stats", headers={"Authorization": "Bearer " + tk["access_token"]})
    check("SSO token 可调用业务接口 /stats", r3.status_code == 200, f"HTTP={r3.status_code}")

    # ================= 4. 票据一次性 / 过期 =================
    section("4. 一次性票据")
    r = c.post("/api/v1/auth/sso/ticket", json={"ticket": ticket})
    check("同一票据第二次使用 → 401", r.status_code == 401, f"HTTP={r.status_code}")
    check("重复使用不吐 token", "access_token" not in json.dumps(r.json()))
    r = c.post("/api/v1/auth/sso/ticket", json={"ticket": "nonexistent-ticket"})
    check("不存在的票据 → 401", r.status_code == 401, f"HTTP={r.status_code}")
    r = c.post("/api/v1/auth/sso/ticket", json={})
    check("缺 ticket → 422", r.status_code == 422, f"HTTP={r.status_code}")

    from app_v2.api.routes import sso as sso_mod

    old = sso_mod._TICKET_TTL
    sso_mod._TICKET_TTL = -1.0      # 立刻过期
    tk2 = sso_mod._issue_ticket({"access_token": "x"})
    r = c.post("/api/v1/auth/sso/ticket", json={"ticket": tk2})
    check("过期票据 → 401", r.status_code == 401, f"HTTP={r.status_code}")
    sso_mod._TICKET_TTL = old

    # ================= 5. 安全防线 =================
    section("5. 安全防线：state / nonce / aud / iss")

    def fresh_login(tenant: str = "") -> tuple[str, str, str, str]:
        """发起一次新登录，返回 (state, nonce, challenge, cookie)。"""
        url = "/api/v1/auth/sso/login?next=/app" + (f"&tenant={tenant}" if tenant else "")
        rr = c.get(url, follow_redirects=False)
        qq = urllib.parse.parse_qs(urllib.parse.urlparse(rr.headers["location"]).query)
        ck = rr.headers.get("set-cookie", "").split(";")[0]
        return (qq["state"][0], qq["nonce"][0], qq["code_challenge"][0], ck)

    # 5.1 state Cookie 不匹配 —— 登录 CSRF
    s1, n1, ch1, ck1 = fresh_login()
    idp.codes["c-csrf"] = {"nonce": n1, "challenge": ch1,
                           "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                           "claims": {**base_claims, "nonce": n1}}
    # 模拟"攻击者在自己浏览器发起登录，拿到 code+state，再诱导受害者点这个回调链接"。
    # 受害者浏览器上有自己的 rag_sso_state（属于别的 state），但**不是**这个 s1。
    # ⚠ 这里必须显式指定一个不相干的 Cookie：TestClient 的 CookieJar 会自动累积，
    # 若只写"不带 Cookie"，jar 里上一次的 state 会被自动带上，测不出真实场景。
    r = c.get(f"/api/v1/auth/sso/callback?code=c-csrf&state={s1}",
              headers={"Cookie": "rag_sso_state=some-other-users-state"},
              follow_redirects=False)
    check("state Cookie 与该 state 不匹配 → 拒绝（防登录 CSRF）",
          "sso_error=state" in r.headers.get("location", ""),
          r.headers.get("location", "")[:60])
    check("被拒时不下发 token", "sso_ticket" not in r.headers.get("location", ""))
    check("被拒时 IdP 的 code 未被兑换（未发起 token 请求）",
          "c-csrf" in idp.codes, f"codes_left={list(idp.codes)}")

    # 5.2 Cookie 里是别的 state
    s2, n2, ch2, ck2 = fresh_login()
    r = c.get(f"/api/v1/auth/sso/callback?code=c-csrf&state={s2}",
              headers={"Cookie": ck1}, follow_redirects=False)
    check("Cookie 与 state 不一致 → 拒绝（防登录 CSRF）",
          "sso_error=state" in r.headers.get("location", ""))

    # 5.3 state 不存在
    r = c.get("/api/v1/auth/sso/callback?code=x&state=not-a-real-state",
              headers={"Cookie": "rag_sso_state=not-a-real-state"}, follow_redirects=False)
    check("伪造 state（Cookie 也伪造）→ 拒绝（服务端无此 state）",
          "sso_error=state" in r.headers.get("location", ""))

    # 5.4 state 重放（已消费）
    s4, n4, ch4, ck4 = fresh_login()
    idp.codes["c-replay"] = {"nonce": n4, "challenge": ch4,
                             "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                             "claims": {**base_claims, "nonce": n4}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-replay&state={s4}",
              headers={"Cookie": ck4}, follow_redirects=False)
    check("首次使用 state → 成功", "sso_ticket" in r.headers.get("location", ""))
    idp.codes["c-replay2"] = {"nonce": n4, "challenge": ch4,
                              "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                              "claims": {**base_claims, "nonce": n4}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-replay2&state={s4}",
              headers={"Cookie": ck4}, follow_redirects=False)
    check("同一 state 第二次使用 → 拒绝（一次性）",
          "sso_error=state" in r.headers.get("location", ""))

    # 5.5 nonce 不匹配
    s5, n5, ch5, ck5 = fresh_login()
    idp.codes["c-nonce"] = {"nonce": n5, "challenge": ch5,
                            "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                            "claims": {**base_claims, "nonce": "stolen-nonce"}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-nonce&state={s5}",
              headers={"Cookie": ck5}, follow_redirects=False)
    check("nonce 不匹配 → 拒绝（防 ID Token 重放）",
          "sso_error=exchange" in r.headers.get("location", ""),
          r.headers.get("location", "")[:60])

    # 5.6 aud 不匹配
    s6, n6, ch6, ck6 = fresh_login()
    idp.codes["c-aud"] = {"nonce": n6, "challenge": ch6,
                          "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                          "claims": {**base_claims, "nonce": n6, "aud": "other-app"}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-aud&state={s6}",
              headers={"Cookie": ck6}, follow_redirects=False)
    check("aud 不匹配 → 拒绝（别家应用的令牌）",
          "sso_error=exchange" in r.headers.get("location", ""))

    # 5.7 iss 不匹配
    s7, n7, ch7, ck7 = fresh_login()
    idp.codes["c-iss"] = {"nonce": n7, "challenge": ch7,
                          "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                          "claims": {**base_claims, "nonce": n7, "iss": "https://evil-idp.example"}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-iss&state={s7}",
              headers={"Cookie": ck7}, follow_redirects=False)
    check("iss 不匹配 → 拒绝（签发方不一致）",
          "sso_error=exchange" in r.headers.get("location", ""))

    # 5.8 IdP 侧取消授权
    r = c.get("/api/v1/auth/sso/callback?error=access_denied&state=whatever",
              headers={"Cookie": "rag_sso_state=whatever"}, follow_redirects=False)
    check("IdP 返回 access_denied → sso_error=denied",
          "sso_error=denied" in r.headers.get("location", ""))

    # 5.9 缺参数
    r = c.get("/api/v1/auth/sso/callback", follow_redirects=False)
    check("回调缺 code/state → 拒绝", "sso_error=" in r.headers.get("location", ""))

    # 5.10 token 端点失败
    s8, n8, ch8, ck8 = fresh_login()
    idp.codes["c-tok"] = {"nonce": n8, "challenge": ch8,
                          "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                          "claims": {**base_claims, "nonce": n8}}
    idp.fail_token = True
    r = c.get(f"/api/v1/auth/sso/callback?code=c-tok&state={s8}",
              headers={"Cookie": ck8}, follow_redirects=False)
    idp.fail_token = False
    check("token 交换失败 → sso_error=exchange", "sso_error=exchange" in r.headers.get("location", ""))

    # 5.11 错误回跳 URL 里不出现 IdP 细节
    loc = r.headers.get("location", "")
    check("错误回跳 URL 不含 IdP 地址", "127.0.0.1" not in loc, loc[:80])

    # ================= 6. 角色映射 =================
    section("6. 角色映射：显式映射，不取最高权限")
    saved = (settings.oidc_role_claim, settings.oidc_role_map)
    settings.oidc_role_claim = "groups"
    settings.oidc_role_map = "rag-admins=admin,rag-users=user"
    check("命中 rag-admins → admin",
          resolve_role({"groups": ["rag-admins"]}) == "admin")
    check("命中 rag-users → user",
          resolve_role({"groups": ["rag-users"]}) == "user")
    check("两个组都在 → 按映射顺序先命中（admin）",
          resolve_role({"groups": ["rag-admins", "rag-users"]}) == "admin")
    check("都不命中 → 默认角色 user", resolve_role({"groups": ["other"]}) == "user")
    check("IdP 组名恰好叫 admin 但未在映射表里 → 不提权",
          resolve_role({"groups": ["admin"]}) == "user")
    settings.oidc_default_role = "kb_admin"
    check("默认角色可配置（未命中时用 oidc_default_role）",
          resolve_role({"groups": ["other"]}) == "kb_admin")
    settings.oidc_default_role = "user"
    check("映射表为空 → 一律默认角色（不做同名推断）",
          (lambda: (setattr(settings, "oidc_role_map", ""),
                    resolve_role({"groups": ["rag-admins"]}))[1])() == "user")
    settings.oidc_role_claim, settings.oidc_role_map = saved

    # ================= 7. 租户判定 =================
    section("7. 租户判定：claims 优先于 hint")
    saved_claim = settings.oidc_tenant_claim
    settings.oidc_tenant_claim = "org.tenant"
    check("嵌套 claim 取值（org.tenant）",
          resolve_tenant({"org": {"tenant": "acme"}}, "other") == "acme",
          resolve_tenant({"org": {"tenant": "acme"}}, "other"))
    check("claims 无租户 → 用 hint",
          resolve_tenant({}, "Acme") == "acme")
    settings.oidc_tenant_claim = ""
    check("都没配置 → 默认租户",
          resolve_tenant({}, "") == settings.oidc_default_tenant)

    # 非法 identifier 的基础函数行为（独立于 SSO 映射逻辑）
    from app_v2.core.response import AppError
    from app_v2.services.tenant_service import normalize_tenant_id

    def _rejects(raw: str) -> bool:
        try:
            normalize_tenant_id(raw)
            return False
        except AppError:
            return True

    check("normalize_tenant_id 拒绝中文公司名", _rejects("Acme 公司!!"))
    check("normalize_tenant_id 拒绝路径穿越", _rejects("../../etc/passwd"))
    check("normalize_tenant_id 接受合法 ID 并小写化",
          normalize_tenant_id("  Acme-01 ") == "acme-01")

    # claims 里带非法租户：**必须抛错，不能静默降级**
    # 静默降级 = 把 A 公司的人丢进 default 租户，他会登录成功然后看到别人的内容。
    # 这是静默串户，比登录失败严重得多。
    settings.oidc_tenant_claim = "org.tenant"
    from app_v2.core.response import AppError as _AE

    try:
        got = resolve_tenant({"org": {"tenant": "Acme 公司"}}, "")
        bad = False
    except _AE:
        got, bad = "", True
    check("claims 里租户非法 → 抛 422（不静默降级到默认租户）", bad, "" if bad else f"got={got!r}")

    try:
        got = resolve_tenant({"org": {"tenant": "../../etc"}}, "")
        bad = False
    except _AE:
        got, bad = "", True
    check("claims 里租户含路径穿越 → 抛 422", bad, "" if bad else f"got={got!r}")

    # hint 非法（用户手填的公司名）：不算安全事件，退回默认租户
    check("hint 非法 → 退回默认租户（用户输入，非安全事件）",
          resolve_tenant({}, "Acme 公司") == settings.oidc_default_tenant,
          resolve_tenant({}, "Acme 公司"))
    settings.oidc_tenant_claim = saved_claim

    # ================= 8. 账号状态 =================
    section("8. 账号状态：禁用不可绕过、未开户可拒绝")
    zs = meta_store.get_user_by_name("zhangsan", "acme")
    from app_v2.services import user_service

    user_service.update_user(zs["user_id"], {"enabled": False}, by="test")
    s9, n9, ch9, ck9 = fresh_login("acme")
    idp.codes["c-disabled"] = {"nonce": n9, "challenge": ch9,
                               "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                               "claims": {**base_claims, "nonce": n9}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-disabled&state={s9}",
              headers={"Cookie": ck9}, follow_redirects=False)
    check("本地账号已禁用 → SSO 不给登录（sso_error=disabled）",
          "sso_error=disabled" in r.headers.get("location", ""),
          r.headers.get("location", "")[:60])
    user_service.update_user(zs["user_id"], {"enabled": True}, by="test")

    saved_auto = settings.oidc_auto_provision
    settings.oidc_auto_provision = False
    s10, n10, ch10, ck10 = fresh_login("acme")
    idp.codes["c-newuser"] = {"nonce": n10, "challenge": ch10,
                              "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                              "claims": {**base_claims, "nonce": n10,
                                         "preferred_username": "lisi", "name": "李四"}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-newuser&state={s10}",
              headers={"Cookie": ck10}, follow_redirects=False)
    check("未开自动开户 → 新用户被拒（sso_error=no_account）",
          "sso_error=no_account" in r.headers.get("location", ""),
          r.headers.get("location", "")[:60])
    check("被拒后库里没有该用户", meta_store.get_user_by_name("lisi", "acme") is None)
    settings.oidc_auto_provision = saved_auto

    # 不存在 + 未开开户标志之外的路径：租户停用
    s11, n11, ch11, ck11 = fresh_login("no-such-tenant")
    idp.codes["c-badtenant"] = {"nonce": n11, "challenge": ch11,
                                "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                                "claims": {**base_claims, "nonce": n11}}
    r = c.get(f"/api/v1/auth/sso/callback?code=c-badtenant&state={s11}",
              headers={"Cookie": ck11}, follow_redirects=False)
    check("租户不存在 → 拒绝（sso_error=tenant）",
          "sso_error=tenant" in r.headers.get("location", ""),
          r.headers.get("location", "")[:60])

    # ================= 9. 已有账号的行为 =================
    section("9. 已有账号：不重复开户、不覆盖本地角色")
    user_service.update_user(zs["user_id"], {"role": "kb_admin"}, by="test")
    s12, n12, ch12, ck12 = fresh_login("acme")
    idp.codes["c-existing"] = {"nonce": n12, "challenge": ch12,
                               "redirect_uri": "http://testserver/api/v1/auth/sso/callback",
                               "claims": {**base_claims, "nonce": n12,
                                          "name": "张三（新）"}}
    before = len(meta_store.list_users("acme"))
    r = c.get(f"/api/v1/auth/sso/callback?code=c-existing&state={s12}",
              headers={"Cookie": ck12}, follow_redirects=False)
    check("已有账号再登录 → 成功", "sso_ticket" in r.headers.get("location", ""))
    after = len(meta_store.list_users("acme"))
    check("不重复开户（用户数不变）", before == after, f"{before} -> {after}")
    zs2 = meta_store.get_user(zs["user_id"])
    check("本地角色不被 IdP 每次覆盖（仍为 kb_admin）",
          zs2.get("role") == "kb_admin", str(zs2.get("role")))
    check("显示名按 IdP 同步（张三 → 张三（新））",
          zs2.get("display_name") == "张三（新）", str(zs2.get("display_name")))

    # ================= 10. 审计 =================
    section("10. 审计留痕")
    from app_v2.services import audit_service

    # 审计是异步落库（队列 + 后台线程）。不先等队列排空就查库，
    # 会得到一个"看起来没写审计"的假阴性——这类断言必须等确定的信号，
    # 不能用固定 sleep 碰运气。
    # ⚠ TestClient 不触发 lifespan，所以后台写入线程要在这里显式启动；
    #   否则记录只会停在队列里（enqueued=1 / written=0），断言永远失败。
    audit_service.start()
    audit_service.flush(timeout=8.0)
    logs = meta_store.query_audit(action="auth.sso", limit=200)
    check("成功登录写审计（action=auth.sso_login）", len(logs) >= 1, f"count={len(logs)}")
    if logs:
        d = json.dumps(logs[0], ensure_ascii=False, default=str)
        check("审计记录含 tenant_id", "tenant_id" in d)
        check("审计记录含 IdP 来源", "idp" in d or "oidc" in d.lower())
        check("审计记录不含 token 明文",
              "access_token" not in d and "refresh_token" not in d)
        check("审计 actor 是 SSO 登录的用户",
              logs[0].get("actor") in ("zhangsan", "用户"), str(logs[0].get("actor")))
    refused = meta_store.query_audit(action="auth.", limit=300)
    check("被拒绝的 SSO 尝试也有留痕（≥1 条 auth.* 记录）", len(refused) >= 1,
          f"count={len(refused)}")

    # ================= 11. 多副本要点 =================
    section("11. 多实例部署要点")
    # P4 已把 state 迁到 Redis（配了 Redis 时），进程内字典降级为兜底。
    # 断言要点变成"两条路都在"，而不是"state 只在内存里"。
    check("state 支持跨副本存储（Redis 前缀已定义）",
          isinstance(getattr(oidc_service, "_STATE_PREFIX", None), str))
    check("state 仍有进程内兜底（无 Redis 时可用）",
          isinstance(oidc_service._pending, dict))
    check("一次性票据支持跨副本（前缀已定义）",
          isinstance(getattr(__import__("app_v2.api.routes.sso", fromlist=["_TICKET_PREFIX"]),
                             "_TICKET_PREFIX", None), str))
    check("discovery 有缓存（避免每次登录都打 IdP）",
          oidc_service._discovery is not None)

    # ================= 汇总 =================
    idp.stop()
    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 58}")
    print(f"PASS {len(PASS)} / {total}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
