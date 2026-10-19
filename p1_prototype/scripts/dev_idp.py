"""极简 OIDC Provider（仅用于本地联调）。

不是测试夹具，而是**可运行的联调服务**：起在固定端口后，
浏览器里点登录页的 SSO 按钮就能走完整条链路，方便人工验证前端。

    python scripts/dev_idp.py 9999 rag

提供：
  GET  /.well-known/openid-configuration
  GET  /authorize    —— 不渲染登录页，直接把 code 回给 redirect_uri（"已登录"语义）
  POST /token        —— 校验 PKCE 与 client_id
  GET  /userinfo     —— 返回身份属性

生产环境当然要用真的 IdP（Keycloak / Azure AD / Authing / 企业微信）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_ID = "rag-kb"
CLIENT_SECRET = "s3cret"

# 想改身份就改这里。org.tenant 与 groups 用来演示租户/角色映射。
USER = {
    "sub": "u-1001",
    "preferred_username": "zhangsan",
    "name": "张三",
    "email": "zhangsan@acme.com",
    "org": {"tenant": "acme"},
    "groups": ["rag-users"],
}

CODES: dict = {}
LOCK = threading.Lock()


def b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_id_token(claims: dict) -> str:
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.devsignature"


class Handler(BaseHTTPRequestHandler):
    base = ""     # issuer，由 main() 注入（可能带 /realms/xxx 前缀）

    def log_message(self, fmt, *args):
        sys.stderr.write("[idp] " + (fmt % args) + "\n")

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        # 兼容带 realm 前缀的 issuer（如 http://host:9999/realms/rag）。
        # 真实 IdP（Keycloak）就长这样，联调配置抄过来时不该因为多一层路径就 404。
        path = u.path
        m = re.match(r"^/realms/[^/]+(/.*)$", path)
        if m:
            path = m.group(1)

        if path == "/.well-known/openid-configuration":
            return self._json({
                "issuer": self.base,
                "authorization_endpoint": self.base + "/authorize",
                "token_endpoint": self.base + "/token",
                "userinfo_endpoint": self.base + "/userinfo",
                "jwks_uri": self.base + "/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "profile", "email"],
                "code_challenge_methods_supported": ["S256"],
            })

        if path == "/authorize":
            redirect_uri = (q.get("redirect_uri") or [""])[0]
            state = (q.get("state") or [""])[0]
            nonce = (q.get("nonce") or [""])[0]
            challenge = (q.get("code_challenge") or [""])[0]
            if (q.get("client_id") or [""])[0] != CLIENT_ID:
                return self._json({"error": "invalid_client"}, 400)
            if not redirect_uri or not state or not challenge:
                return self._json({"error": "invalid_request"}, 400)

            code = "dev-" + b64({"t": len(CODES)}).rstrip("=")
            with LOCK:
                CODES[code] = {"nonce": nonce, "challenge": challenge,
                               "redirect_uri": redirect_uri}
            sep = "&" if "?" in redirect_uri else "?"
            return self._redirect(f"{redirect_uri}{sep}code={code}&state={state}")

        if path == "/userinfo":
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._json({"error": "invalid_token"}, 401)
            # userinfo 不返回 nonce/iss/aud —— 与真实 IdP 语义一致
            return self._json({k: v for k, v in USER.items()
                               if k not in ("nonce", "iss", "aud")})

        return self._json({"error": "not_found", "path": path}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # 与 do_GET 一样剥掉 /realms/<realm> 前缀。
        # discovery 文档里的 token_endpoint 是**带前缀**的完整地址，
        # 只修 GET 会出现"discovery 通、authorize 通、token 404"的奇怪现象。
        path = u.path
        m = re.match(r"^/realms/[^/]+(/.*)$", path)
        if m:
            path = m.group(1)
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if path != "/token":
            return self._json({"error": "not_found"}, 404)

        code = (form.get("code") or [""])[0]
        with LOCK:
            rec = CODES.pop(code, None)
        if not rec:
            return self._json({"error": "invalid_grant"}, 400)
        if (form.get("client_id") or [""])[0] != CLIENT_ID:
            return self._json({"error": "invalid_client"}, 400)
        if (form.get("client_secret") or [""])[0] != CLIENT_SECRET:
            return self._json({"error": "invalid_client", "detail": "secret"}, 401)
        verifier = (form.get("code_verifier") or [""])[0]
        calc = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if calc != rec["challenge"]:
            return self._json({"error": "invalid_grant", "detail": "pkce"}, 400)

        claims = {**USER, "iss": self.base, "aud": CLIENT_ID, "nonce": rec["nonce"]}
        return self._json({
            "access_token": "dev-access-" + code,
            "token_type": "Bearer",
            "expires_in": 300,
            "id_token": make_id_token(claims),
        })


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    realm = sys.argv[2] if len(sys.argv) > 2 else "rag"
    # issuer 必须带上 realm 前缀，且与 OIDC_ISSUER 完全一致：
    # ID Token 的 iss 与 discovery 的 issuer 都要对得上，
    # 否则后端会以"签发方不匹配"为由拒绝（这正是它该做的事）。
    base = f"http://127.0.0.1:{port}/realms/{realm}"
    Handler.base = base
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"假 IdP 已启动：{base}")
    print(f"  issuer        = {base}")
    print(f"  client_id     = {CLIENT_ID}")
    print(f"  client_secret = {CLIENT_SECRET}")
    print(f"  用户          = {USER['preferred_username']} / 租户 "
          f"{USER['org']['tenant']} / 组 {USER['groups']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
