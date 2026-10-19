"""调试：打印 SSO 每一步的请求/响应头（含 Cookie 收发）。

    python scripts/debug_sso_flow.py
"""
from __future__ import annotations

import http.cookiejar
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8021"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, *a, **kw):
        return None


def main() -> int:
    jar = http.cookiejar.CookieJar()
    forward = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
    )
    stop = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
        _NoRedirect,
    )

    def step(name, url, follow=False):
        op = forward if follow else stop
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        try:
            r = op.open(req, timeout=20)
            code, hdr = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            code, hdr = e.code, dict(e.headers)
        loc = hdr.get("Location") or hdr.get("location") or ""
        print(f"\n[{name}] HTTP {code}")
        print(f"  url   : {url[:110]}")
        print(f"  loc   : {loc[:110]}")
        print(f"  setck : {(hdr.get('Set-Cookie') or hdr.get('set-cookie') or '-')[:90]}")
        print(f"  jar   : {[(c.name, c.value[:12]) for c in jar]}")
        return code, loc

    _, loc = step("1 发起登录", BASE + "/api/v1/auth/sso/login?next=/app&tenant=acme")
    if not loc:
        return 1
    _, cb = step("2 IdP authorize", loc)
    if not cb:
        return 1
    step("3 回调", cb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
