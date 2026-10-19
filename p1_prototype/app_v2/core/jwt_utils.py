"""JWT（HS256）签发与校验，零第三方依赖。

为什么不引 PyJWT：私有化交付希望依赖越少越好，而 HS256 的签名规则只有
「base64url(header).base64url(payload) → HMAC-SHA256 → base64url」三步，
自己实现反而能把校验策略（过期、时钟偏移、算法白名单）写死，不留配置黑洞。

安全要点：
  1. **算法白名单**：只接受 HS256。拒绝 `alg=none`（历史上有名的伪造手法），
     也拒绝 RS256 与 HS256 混用造成的公钥当密钥攻击。
  2. **常量时间比较签名**：hmac.compare_digest，避免逐字节比较泄露时序。
  3. **exp / iat / nbf 全部校验**，并允许有限时钟偏移。
  4. **jti**：每个 token 一个唯一 ID，注销时把它写进黑名单，
     在 token 自然过期前就失效（JWT 无状态带来的最大缺陷就是"注销不掉"）。
  5. payload 只放 uid/role/space，**不放敏感信息**——JWT 只签名不加密，任何人都能解码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional

ALGORITHM = "HS256"
CLOCK_SKEW_SEC = 30          # 允许的时钟偏移，避免多机部署时间不同步导致误判过期


class JWTError(Exception):
    """JWT 校验失败。message 可以直接返回给调用方（不含敏感细节）。"""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason or message


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(signing_input: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64url_encode(digest)


def encode(payload: Dict[str, Any], secret: str, expires_in: int,
           issuer: str = "rag-kb", subject: str = "") -> str:
    """签发 token。会自动补齐 iat/exp/nbf/jti/iss/sub。"""
    now = int(time.time())
    body: Dict[str, Any] = {
        **payload,
        "iat": now,
        "nbf": now,
        "exp": now + max(int(expires_in), 1),
        "jti": secrets.token_hex(8),
        "iss": issuer,
    }
    if subject:
        body["sub"] = subject
    header = {"alg": ALGORITHM, "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    p = _b64url_encode(json.dumps(body, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")
    return f"{h}.{p}.{_sign(signing_input, secret)}"


def decode(token: str, secret: str, issuer: str = "rag-kb",
           verify_exp: bool = True) -> Dict[str, Any]:
    """校验并解析 token，失败抛 JWTError。"""
    token = (token or "").strip()
    if not token:
        raise JWTError("缺少 Token", "missing")
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("Token 格式不正确", "malformed")
    h_b64, p_b64, sig = parts

    # ---- 算法白名单 ----
    try:
        header = json.loads(_b64url_decode(h_b64))
    except Exception:
        raise JWTError("Token 头部无法解析", "bad_header")
    alg = str(header.get("alg", ""))
    if alg != ALGORITHM:
        # alg=none 会走到这里；算法混淆攻击同样被挡住
        raise JWTError(f"不支持的签名算法：{alg or '空'}", "bad_alg")
    if str(header.get("typ", "JWT")).upper() != "JWT":
        raise JWTError("Token 类型不正确", "bad_typ")

    # ---- 签名（常量时间比较）----
    expected = _sign(f"{h_b64}.{p_b64}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, sig):
        raise JWTError("Token 签名无效", "bad_signature")

    try:
        payload = json.loads(_b64url_decode(p_b64))
    except Exception:
        raise JWTError("Token 载荷无法解析", "bad_payload")
    if not isinstance(payload, dict):
        raise JWTError("Token 载荷格式不正确", "bad_payload")

    now = int(time.time())
    if issuer and payload.get("iss") and payload.get("iss") != issuer:
        raise JWTError("Token 签发方不匹配", "bad_issuer")

    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and now + CLOCK_SKEW_SEC < nbf:
        raise JWTError("Token 尚未生效", "not_yet_valid")

    if verify_exp:
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            raise JWTError("Token 缺少过期时间", "no_exp")
        if now - CLOCK_SKEW_SEC >= exp:
            raise JWTError("Token 已过期，请重新登录", "expired")

    return payload


def peek_payload(token: str) -> Dict[str, Any]:
    """不校验签名地读出 payload（仅用于取 jti 做注销、或日志埋点，不可用于鉴权）。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        return json.loads(_b64url_decode(parts[1]))
    except Exception:
        return {}


def token_fingerprint(token: str) -> str:
    """token 的短指纹，用于日志与指标打标——**不记录 token 明文**。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class JWTManager:
    """持有密钥与配置的签发器。密钥来自环境变量，重启不变（否则所有 token 失效）。"""

    def __init__(self, secret: str, access_ttl: int, refresh_ttl: int,
                 issuer: str = "rag-kb") -> None:
        self.secret = secret or ""
        self.access_ttl = max(int(access_ttl), 60)
        self.refresh_ttl = max(int(refresh_ttl), self.access_ttl)
        self.issuer = issuer

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    def issue(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """按用户记录签发 access + refresh 两个 token。"""
        base = {
            "uid": user.get("user_id", ""),
            "name": user.get("username", ""),
            "role": user.get("role", "user"),
            "spaces": user.get("spaces", "*"),
            # 密级（知识权限组）。空串表示"按空间名派生"，与老 token 行为一致，
            # 因此老 token 在升级后仍可正常校验，不需要强制全员重登。
            "pg": user.get("perm_groups", "") or "",
            # P4：租户。老 token 没有 tid，校验时按默认租户补齐（见 principal_from_jwt），
            # 这样升级瞬间在线用户的 token 不会因为多了个字段就全失效。
            "tid": user.get("tenant_id", "") or "",
        }
        # 若该 token 是由 API Key 换来的，记录来源密钥，
        # 让"吊销密钥"能连带作废已签发的票据（见 user_service.principal_from_jwt）
        if user.get("src_key"):
            base["sk"] = user["src_key"]
        access = encode({**base, "typ": "access"}, self.secret, self.access_ttl,
                        issuer=self.issuer, subject=base["uid"])
        refresh = encode({**base, "typ": "refresh"}, self.secret, self.refresh_ttl,
                         issuer=self.issuer, subject=base["uid"])
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_expires_in": self.refresh_ttl,
        }

    def verify(self, token: str, expect_type: str = "access") -> Dict[str, Any]:
        payload = decode(token, self.secret, issuer=self.issuer)
        typ = payload.get("typ", "access")
        if typ != expect_type:
            raise JWTError(f"Token 类型不正确：需要 {expect_type}，实际 {typ}", "wrong_type")
        return payload

    def remaining(self, payload: Dict[str, Any]) -> int:
        """剩余有效期（秒），用于决定黑名单记录要保留多久。"""
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return self.access_ttl
        return max(int(exp - time.time()), 1)


def generate_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def load_or_create_secret(path: str) -> str:
    """从文件读取签名密钥；不存在则生成并写入（0600）。"""
    import os
    from pathlib import Path

    p = Path(path)
    try:
        if p.exists():
            value = p.read_text(encoding="utf-8").strip()
            if value:
                return value
        p.parent.mkdir(parents=True, exist_ok=True)
        value = generate_secret()
        p.write_text(value, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return value
    except Exception:
        # 文件系统不可写时退化为进程内随机密钥（重启后 token 全失效，但服务可用）
        return generate_secret()
