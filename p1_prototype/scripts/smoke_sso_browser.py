"""浏览器式 SSO 全链路联调（真实 HTTP 往返，非 TestClient）。

与 verify_p4_sso.py 的分工：
  - verify_p4_sso.py 用 TestClient 覆盖**分支与安全边界**（快、可注入错误）；
  - 本脚本用真实 HTTP + CookieJar 模拟**浏览器**走一遍，验证"真的能登进去"
    ——包括 302 链路、Cookie 传递、票据换票、拿到的 token 能调业务接口。
    分支测试通过但真实链路走不通（比如反代改了 Host、Cookie 属性写错）
    是 SSO 最常见的翻车方式。

前置：本机已启动
    python scripts/dev_idp.py 9999
    OIDC_ENABLED=true OIDC_ISSUER=http://127.0.0.1:9999 ... uvicorn app_v2.main:app --port 8021

跑法：
    python scripts/smoke_sso_browser.py
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8021"
# IdP 的 issuer 可能带 realm 前缀（Keycloak 风格）。
# 比对跳转目标时只看 host:port，否则改个 realm 名就要改脚本。
IDP = "http://127.0.0.1:9999"
IDP_ORIGIN = "http://127.0.0.1:9999"

jar = http.cookiejar.CookieJar()


class _StopAt3xx(urllib.request.HTTPRedirectHandler):
    """只劫持 3xx：原样返回响应对象，不跟跳转。

    关键点是**继承** HTTPRedirectHandler 并只覆盖 http_error_30x。
    自己从零写一个 handler 会让 opener 的 handle_error['http'] 指向它，
    连带把 4xx/5xx 也路由过来（返回 fp 当响应 = 假 200）。
    """

    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, *a, **kw):
        return None


def _build(follow: bool):
    """构造带 CookieJar 的 opener。

    用 build_opener（而不是手工 add_handler）才能拿到完整的协议/错误处理器表；
    拦截重定向只需把自定义 handler 的 order 调到比默认 500 更小。
    """
    handlers = [urllib.request.ProxyHandler({}),
                urllib.request.HTTPCookieProcessor(jar)]
    if not follow:
        stop = _StopAt3xx()
        stop.order = 400          # 先于默认 HTTPRedirectHandler(500) 被调用
        handlers.append(stop)
    return urllib.request.build_opener(*handlers)


op_follow = _build(True)
op_stop = _build(False)


PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {extra}" if extra else ""))


def get_json(path, token=""):
    """GET 并返回 (status, json)。4xx/5xx 不抛异常，交给调用方断言。"""
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        with op_follow.open(urllib.request.Request(BASE + path, headers=h),
                            timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def post_json(path, body, token=""):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        with op_follow.open(urllib.request.Request(
                BASE + path, data=json.dumps(body).encode(), headers=h,
                method="POST"), timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get(url, follow=True):
    op = op_follow if follow else op_stop
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with op.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main() -> int:
    print(f"目标：{BASE}   IdP：{IDP}")
    print(f"{'=' * 60}")

    # 1. 登录页能打开
    st, _, html = get(BASE + "/login")
    check("GET /login → 200", st == 200, f"HTTP={st}")
    check("登录页含 SSO 按钮容器", 'id="ssoBtn"' in html)

    # 2. 认证方式探测
    st, _, body = get(BASE + "/api/v1/auth/methods")
    m = json.loads(body)["data"] if st == 200 else {}
    check("/auth/methods → oidc_enabled=true", m.get("oidc_enabled") is True)
    check("oidc_label 可读", bool(m.get("oidc_label")), str(m.get("oidc_label")))

    # 3. 发起登录 → 302 到 IdP
    st, hdr, _ = get(BASE + "/api/v1/auth/sso/login?next=/app&tenant=acme", follow=False)
    check("SSO 发起 → 302", st == 302, f"HTTP={st}")
    loc = hdr.get("Location") or hdr.get("location") or ""
    check("跳转目标是 IdP 的 authorize", loc.startswith(IDP_ORIGIN) and "/authorize?" in loc, loc[:80])
    cookies = {c.name: c.value for c in jar}
    check("浏览器已持有 state Cookie", "rag_sso_state" in cookies,
          str(list(cookies)))
    if not loc.startswith(IDP_ORIGIN):
        print("\nIdP 未就绪，终止。请先启动 scripts/dev_idp.py 9999")
        return 1

    # 4. 跟着跳到 IdP（IdP 直接回 code）
    st, hdr, _ = get(loc, follow=False)
    check("IdP /authorize → 302 回回调地址", st == 302, f"HTTP={st}")
    cb = hdr.get("Location") or hdr.get("location") or ""
    check("回调地址指向本系统", cb.startswith(BASE + "/api/v1/auth/sso/callback"), cb[:80])

    # 5. 访问回调 → 应换成票据并 302 到 /app
    st, hdr, _ = get(cb, follow=False)
    check("回调 → 302", st == 302, f"HTTP={st}")
    back = hdr.get("Location") or hdr.get("location") or ""
    check("回跳带票据", "sso_ticket=" in back, back[:80])
    check("回跳 URL 无 token 明文",
          "access_token" not in back and "refresh_token" not in back)
    mm = re.search(r"sso_ticket=([A-Za-z0-9_\-]+)", back)
    check("票据已生成", mm is not None)
    if not mm:
        return 1
    ticket = mm.group(1)

    # 6. 换票
    st, payload = post_json("/api/v1/auth/sso/ticket", {"ticket": ticket})
    check("换票请求 → 200", st == 200, f"HTTP={st}")
    data = (payload or {}).get("data") or {}
    check("票据换 token → 拿到 access_token", bool(data.get("access_token")))
    check("拿到 refresh_token", bool(data.get("refresh_token")))
    user = data.get("user") or {}
    check("SSO 用户 = zhangsan", user.get("username") == "zhangsan", str(user.get("username")))
    check("租户由 IdP claim 判定 = acme", user.get("tenant_id") == "acme",
          str(user.get("tenant_id")))
    check("角色由 groups 映射（rag-users → user）", user.get("role") == "user",
          str(user.get("role")))

    # 7. 用这枚 token 调业务接口
    tok = data.get("access_token", "")
    st, body = get_json("/api/v1/auth/me", tok)
    me = (body or {}).get("data") or {}
    check("/auth/me 可用", me.get("actor") == "zhangsan", str(me.get("actor")))
    check("权限集合非空", len(me.get("permissions") or []) > 0,
          str(me.get("permissions")))

    st, body = get_json("/api/v1/spaces", tok)
    sp = (body or {}).get("data") or {}
    check("/spaces 可用（SSO token 可读业务数据）", isinstance(sp.get("items"), list),
          f"{len(sp.get('items') or [])} 个空间")

    # 8. 票据已作废
    st, payload = post_json("/api/v1/auth/sso/ticket", {"ticket": ticket})
    check("票据复用被拒（一次性）", st == 401, f"HTTP={st}")
    check("复用失败也无 token 泄露",
          "access_token" not in json.dumps(payload or {}))

    print(f"{'=' * 60}")
    total = len(PASS) + len(FAIL)
    print(f"PASS {len(PASS)} / {total}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        return 1
    print("真实链路全通 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
