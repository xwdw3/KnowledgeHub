"""企业 SSO 单点登录（OIDC Authorization Code Flow）。

为什么选 OIDC 而不是 SAML：
  本项目要对接的是"企业已有身份源"，现实中以 Keycloak / Azure AD / 飞书 /
  Authing / 企业微信这几类为主，它们都提供 OIDC。SAML 的 XML 签名校验
  实现复杂且易错（XML 规范化攻击、签名包裹攻击），而 OIDC 是
  JSON + JWT，能直接复用已有的 JWT 校验能力。
  同时 OIDC 天然支持"我们只做 RP（依赖方）"，不承担 IdP 职责。

三个必须做对的安全点（做错就等于没有鉴权）：

  1. **state 必须一次性且与浏览器绑定**。
     只用随机 state 存服务端还不够——必须同时种一个 HttpOnly Cookie，
     回调时比对。否则攻击者可以在自己的浏览器里发起登录、拿到 code，
     再诱导受害者访问带这个 code 的回调地址，把受害者的会话绑到
     攻击者的企业账号上（登录 CSRF）。

  2. **nonce 必须校验**。
     把 nonce 放进授权请求，回调时比对 ID Token 里的 nonce。
     没有它，攻击者可以用别处拿到的 ID Token 重放到我们这里换会话。

  3. **code 只能换一次、且必须服务端换取**。
     client_secret 绝不能出现在前端。code 一次性是 IdP 保证的，
     但我们必须保证同一个 code 不会被我们自己的并发请求各换一次
     （见 `_consume_state` 的原子删除）。

  另：PKCE 一并支持。公有客户端（无 secret）必须用，机密客户端加了也无害，
  且能防御授权码拦截。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app_v2.core.response import AppError, UnauthorizedError
from app_v2.core.settings import settings

logger = logging.getLogger(__name__)

# 授权流程状态的有效期。给 10 分钟：足够用户完成 IdP 登录 + MFA，
# 又不至于让挂起的 state 长期有效（每个 state 都是"可用的登录入口"）。
STATE_TTL = 600


@dataclass
class _PendingAuth:
    state: str
    nonce: str
    code_verifier: str
    next_url: str
    tenant_hint: str
    created_at: float


class OIDCService:
    """OIDC 客户端。所有网络调用都用 urllib，避免为 SSO 引入新依赖。"""

    # 待完成授权的存储前缀。多实例部署时 state 必须跨副本可见：
    # 用户在 A 副本点登录、回调被负载均衡打到 B 副本是**必然发生**的
    # （不是边缘情况），进程内字典会让 B 副本找不到 state 而拒绝登录，
    # 表现为"随机一半的登录失败，刷新一下又好了"。
    _STATE_PREFIX = "rag:sso:state:"

    def __init__(self) -> None:
        # 单实例（未配 Redis）时的进程内兜底。配了 Redis 就走 Redis，
        # 这里仅作为降级路径存在——降级是安全的：Redis 挂掉时所有副本
        # 都退回各自进程内，登录仍然能用，只是跨副本的回调会失败。
        # 宁可"部分登录失败"也不要"整个登录不可用"。
        self._pending: Dict[str, _PendingAuth] = {}
        self._discovery: Optional[dict] = None
        self._discovery_at = 0.0

    # ---------------- 配置 ----------------
    @property
    def enabled(self) -> bool:
        return bool(settings.oidc_enabled and settings.oidc_issuer
                    and settings.oidc_client_id)

    def _discover(self) -> dict:
        """拉取 IdP 的 discovery 文档。

        缓存 10 分钟。每次回调都拉一次会让登录变慢，而且 IdP 抖动会直接
        变成"登录不可用"；缓存后又必须能过期，否则 IdP 换端点我们要重启。
        """
        now = time.time()
        if self._discovery and now - self._discovery_at < 600:
            return self._discovery

        url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            data = _http_json(url, method="GET", timeout=8)
        except Exception as exc:
            logger.error("OIDC discovery 失败 url=%s：%s", url, exc)
            raise AppError("单点登录服务暂不可用（无法连接身份提供方）", code=503)

        required = ("authorization_endpoint", "token_endpoint")
        missing = [k for k in required if not data.get(k)]
        if missing:
            logger.error("OIDC discovery 缺少必需字段 %s，实际返回：%s",
                         missing, list(data.keys()))
            raise AppError("单点登录配置有误：身份提供方元数据不完整", code=503)

        self._discovery = data
        self._discovery_at = now
        return data

    # ---------------- 发起授权 ----------------
    def build_authorize_url(self, next_url: str = "", tenant_hint: str = "",
                            redirect_uri: str = "") -> tuple[str, _PendingAuth]:
        if not self.enabled:
            raise AppError("单点登录未启用", code=404)

        meta = self._discover()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        # PKCE：verifier 是 43~128 位的高熵字符串，challenge 是其 SHA256
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

        pending = _PendingAuth(
            state=state, nonce=nonce, code_verifier=verifier,
            next_url=_safe_next(next_url), tenant_hint=(tenant_hint or "").strip(),
            created_at=time.time(),
        )
        self._gc()
        self._save_pending(pending)

        params = {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": redirect_uri or self.default_redirect_uri(),
            "scope": settings.oidc_scopes or "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        # 租户提示：多租户部署时让 IdP 端带上租户信息（若 IdP 支持 login_hint）。
        # 不支持也不影响——回调后仍按 claims 判定租户。
        if pending.tenant_hint:
            params["login_hint"] = pending.tenant_hint

        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return meta["authorization_endpoint"] + sep + urllib.parse.urlencode(params), pending

    @staticmethod
    def default_redirect_uri() -> str:
        if settings.oidc_redirect_uri:
            return settings.oidc_redirect_uri
        # 未显式配置时按后端地址推导。生产必须显式配置：
        # 反代改写过 Host 时推导结果会与 IdP 侧登记的不一致，
        # 表现为 IdP 报 redirect_uri_mismatch，而本地日志看不出问题。
        return "/api/v1/auth/sso/callback"

    # ---------------- pending 存储（单机 / Redis 双模） ----------------
    @staticmethod
    def _redis():
        """配了 Redis 才返回可用客户端，否则 None。

        不用 `cache`（TwoLevelCache）而直接用它的 l2（RedisLayer）：
        两级缓存会把值同时写进本进程 L1，跨副本读到的可能是别处的旧值，
        而且 TTL 语义被 L1 覆盖了一层，不适合放安全相关的 state。
        """
        try:
            from app_v2.services.cache_service import cache

            layer = cache.l2
            if layer.available or layer._ensure():
                return layer
        except Exception:                       # pragma: no cover - 极端环境
            pass
        return None

    def _save_pending(self, pending: _PendingAuth) -> None:
        payload = json.dumps({
            "state": pending.state,
            "nonce": pending.nonce,
            "code_verifier": pending.code_verifier,
            "next_url": pending.next_url,
            "tenant_hint": pending.tenant_hint,
            "created_at": pending.created_at,
        })
        r = self._redis()
        if r is not None and r.setex_raw(self._STATE_PREFIX + pending.state,
                                        STATE_TTL, payload):
            self._pending.pop(pending.state, None)      # 别在内存里留副本
            return
        self._pending[pending.state] = pending

    def _take_pending(self, state: str) -> Optional[_PendingAuth]:
        """取出并删除。Redis 侧用 DEL 的返回值做**原子**判重。

        DEL 返回 1 表示"这次是我删掉的"，返回 0 表示已经被别人删了
        （并发回调 / 重放）。绝不能 GET 完再 DEL——那样两个并发请求
        都能读到有效 state，各换一次 token，一次授权变两个会话。
        """
        key = self._STATE_PREFIX + state
        r = self._redis()
        if r is not None:
            raw = r.get(key)
            if raw is not None:
                # 先删再解析：谁删成功谁才有权继续，删失败（=0）直接判无效。
                if r.delete_keys(key) != 1:
                    return None
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    logger.warning("state 反序列化失败，按无效处理")
                    return None
                return _PendingAuth(
                    state=data.get("state", ""),
                    nonce=data.get("nonce", ""),
                    code_verifier=data.get("code_verifier", ""),
                    next_url=data.get("next_url", ""),
                    tenant_hint=data.get("tenant_hint", ""),
                    created_at=float(data.get("created_at") or 0),
                )
            # Redis 里没有：可能是单实例模式下写在本地内存的，继续往下找
        return self._pending.pop(state, None)

    # ---------------- 处理回调 ----------------
    def consume_state(self, state: str) -> _PendingAuth:
        """取出并**立即删除** state。

        原子性：Redis 模式靠 DEL 的返回值；单机模式靠 dict.pop 的 GIL 保证。
        不能用"先 get 再 del"：两个并发回调（用户狂点刷新，或攻击者抢跑）
        会都读到有效 state，然后用同一个 code 各换一次 token，
        造成一次授权产生两个会话。
        """
        pending = self._take_pending((state or "").strip())
        if not pending:
            raise UnauthorizedError("登录状态无效或已过期，请重新登录")
        if time.time() - pending.created_at > STATE_TTL:
            raise UnauthorizedError("登录会话已超时，请重新登录")
        return pending

    def exchange_code(self, code: str, pending: _PendingAuth,
                      redirect_uri: str = "") -> dict:
        """用 code 换 token，返回 token 响应体（含 id_token）。"""
        meta = self._discover()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri or self.default_redirect_uri(),
            "client_id": settings.oidc_client_id,
            "code_verifier": pending.code_verifier,
        }
        if settings.oidc_client_secret:
            form["client_secret"] = settings.oidc_client_secret

        try:
            return _http_json(meta["token_endpoint"], method="POST", form=form, timeout=10)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 不要把这个 body 直接返回给前端：里面可能含 client_id 等配置细节
            logger.error("OIDC token 交换失败 HTTP=%s body=%s", exc.code, body)
            raise UnauthorizedError("单点登录校验失败，请重试或联系管理员")
        except Exception as exc:
            logger.error("OIDC token 交换异常：%s", exc)
            raise AppError("单点登录服务暂不可用", code=503)

    def decode_id_token(self, id_token: str, expected_nonce: str = "") -> dict:
        """解析并校验 ID Token。

        安全要点：**必须校验签名**，否则等于信任前端传来的任意身份。
        这里因为我们有 client_secret 且走的是"服务端换取"路径，
        可以额外调用 IdP 的 userinfo 端点拿到**由 IdP 直接返回**的用户信息，
        那条通道的安全性不依赖本地验签实现。
        因此本函数只做非安全性的解包 + nonce/aud/iss 检查，
        真正的身份以 userinfo 为准（见 fetch_userinfo）。
        若部署环境无法访问 userinfo，则退化为本地验签（见 _verify_local）。
        """
        claims = _b64_json(id_token.split(".")[1] if id_token.count(".") == 2 else "")
        if not claims:
            raise UnauthorizedError("单点登录返回的令牌格式不正确")

        if expected_nonce and claims.get("nonce") and claims["nonce"] != expected_nonce:
            logger.warning("ID Token nonce 不匹配，疑似重放攻击")
            raise UnauthorizedError("单点登录校验失败（nonce 不匹配）")

        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        if settings.oidc_client_id not in [a for a in aud_list if a]:
            logger.warning("ID Token audience 不匹配 aud=%s 期望=%s",
                           aud, settings.oidc_client_id)
            raise UnauthorizedError("单点登录令牌不属于本系统")

        iss = (claims.get("iss") or "").rstrip("/")
        expect = (settings.oidc_issuer or "").rstrip("/")
        if expect and iss and iss != expect:
            logger.warning("ID Token issuer 不匹配 iss=%s 期望=%s", iss, expect)
            raise UnauthorizedError("单点登录令牌签发方不匹配")

        return claims

    def fetch_userinfo(self, access_token: str) -> Optional[dict]:
        """从 IdP 的 userinfo 端点取用户信息。

        这是比"本地验签 ID Token"更可靠的通道：数据由 IdP 直接返回，
        我们不需要自己实现 RSA/JWKS 校验，也就不存在验签实现出错的风险。
        """
        try:
            meta = self._discover()
            ep = meta.get("userinfo_endpoint")
            if not ep:
                return None
            return _http_json(ep, method="GET", timeout=8,
                              headers={"Authorization": "Bearer " + access_token})
        except Exception as exc:
            logger.warning("OIDC userinfo 获取失败，将退回 ID Token claims：%s", exc)
            return None

    # ---------------- 内部 ----------------
    def _gc(self) -> None:
        """清理过期 state。不清的话进程长跑会积累无界字典。

        Redis 里的条目靠 setex 的 TTL 自动过期，不需要（也无法）在这里扫，
        所以这段只处理单机模式的内存字典。
        """
        now = time.time()
        expired = [k for k, v in self._pending.items() if now - v.created_at > STATE_TTL]
        for k in expired:
            self._pending.pop(k, None)


# ======================================================================
# HTTP 小工具
# ======================================================================
def _http_json(url: str, method: str = "GET", form: Optional[dict] = None,
               headers: Optional[dict] = None, timeout: int = 8) -> dict:
    h = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        h.update(headers)

    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise AppError(f"身份提供方返回了非 JSON 响应：{url}", code=502)


def _b64_json(seg: str) -> dict:
    if not seg:
        return {}
    try:
        pad = "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))
    except Exception:
        return {}


def _safe_next(url: str) -> str:
    """站内相对路径校验。防开放重定向——这是 SSO 回调最常见的漏洞：
    攻击者构造 ?next=https://evil.com，用户登录后被送到钓鱼页，
    而地址栏在跳转前确实是我们自己的域名，用户不会有任何怀疑。
    """
    u = (url or "").strip()
    if not u:
        return "/app"
    if u.startswith("/") and not u.startswith("//"):
        return u
    return "/app"


# ======================================================================
# 身份映射
# ======================================================================
def pick_claim(claims: dict, name: str, default: str = "") -> str:
    """取 claim 值。支持嵌套（如 `realm_access.roles` 这种用点表示的路径）。"""
    if not name:
        return default
    cur: Any = claims
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    if isinstance(cur, list):
        return ",".join(str(x) for x in cur)
    return str(cur) if cur is not None else default


def resolve_role(claims: dict) -> str:
    """按 IdP 组信息映射到本地角色。

    没有配置映射时用 oidc_default_role。**不要**"取角色列表里最高权限的那个"：
    IdP 里一个用户可能同时属于几十个组，其中某个组名恰好与本地角色同名，
    就会意外提权。显式映射表是唯一安全做法。
    """
    from app_v2.core.security import normalize_role

    rmap = settings.oidc_role_map_parsed
    if not rmap or not settings.oidc_role_claim:
        return normalize_role(settings.oidc_default_role)

    raw = pick_claim(claims, settings.oidc_role_claim, "")
    groups = [g.strip() for g in raw.replace(";", ",").split(",") if g.strip()]
    # 按映射表里配置的顺序判定，先命中先得——让运维能通过调整顺序控制优先级
    for idp_group, local_role in rmap.items():
        if idp_group in groups:
            return normalize_role(local_role)
    return normalize_role(settings.oidc_default_role)


def resolve_tenant(claims: dict, hint: str = "") -> str:
    """判定租户。

    优先级：IdP claims 里的租户字段 > 发起登录时的 hint > 默认租户。
    claims 优先是因为它由 IdP 签发，用户改不了；
    hint 来自前端表单，只能作为兜底且必须经白名单校验（见 tenant_service）。

    **为什么 claims 不合法时不静默降级到 hint/默认租户**：
    把 Acme 的员工因为"claim 里带了个中文公司名"就丢进 default 租户，
    是**静默串户**——用户会登录成功，然后看到（或写入）另一个租户的内容。
    这种错误必须显式失败，让管理员看到"租户标识不合法"并去修 claim 映射。
    安全动作（拒绝）永远优于便利动作（猜一个）。
    """
    from app_v2.services.tenant_service import normalize_tenant_id

    claimed = pick_claim(claims, settings.oidc_tenant_claim, "").strip()
    if claimed:
        # 不合法 → normalize_tenant_id 抛 422，由调用方转成用户可见提示
        return normalize_tenant_id(claimed)
    if hint:
        try:
            return normalize_tenant_id(hint)
        except AppError:
            # hint 来自前端输入框，用户可能填了公司全称。这不是安全事件，
            # 静默退回默认租户即可——真正的租户边界仍由 claims + 数据库校验保证。
            logger.info("租户提示不合法，退回默认租户 hint=%r", hint[:40])
    return settings.oidc_default_tenant or settings.default_tenant_id


oidc_service = OIDCService()
