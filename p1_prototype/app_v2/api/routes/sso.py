"""企业 SSO 单点登录路由（OIDC Authorization Code Flow）。

两个端点构成一个完整的往返：

    GET /auth/sso/login     浏览器 → 302 → IdP 登录页
    GET /auth/sso/callback  IdP → 302 → 本系统 → 换 token → 302 → 前端页面

**为什么回调返回 302 而不是 JSON**：
SSO 是被浏览器"跳过来"的，调用方是浏览器地址栏而不是 XHR。返回 JSON 会让用户
停在一屏 JSON 上，还得手动复制 token —— 那就不叫单点登录了。
因此回调的产出是：种好 session（通过 URL 片段交给前端）+ 302 回目标页。

**token 怎么交给前端**：放在 URL 的 `?sso_ticket=` 里只能是一次性的，
放在 `#fragment` 里不会发到服务器也不进 Referer，但仍会留在浏览器历史里。
这里选择 `?sso_ticket=<一次性票据>`：票据 60 秒有效、只能用一次，
前端拿它调 `/auth/sso/ticket` 换真正的 token。这样：
  - access/refresh token 永不进入 URL、不进历史记录、不进 access log；
  - 即使票据泄露，60 秒后自毁，且已被用过一次就失效。

失败的返回方式同理：302 回 `/login?sso_error=<短码>`，短码在服务端映射成
用户可读文案，绝不把 IdP 的原始错误体拼进 URL（可能含配置细节）。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from app_v2.core.response import AppError, UnauthorizedError
from app_v2.core.settings import settings
from app_v2.services import audit_service, user_service
from app_v2.services.oidc_service import (
    oidc_service, pick_claim, resolve_role, resolve_tenant, _safe_next,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["单点登录"])

# state Cookie 名。加 `__Host-` 前缀？需要 Secure + Path=/ + 无 Domain，
# 本地 http 调试时会失效，因此这里用普通名 + 显式属性，并在生产强制 https。
STATE_COOKIE = "rag_sso_state"

# 一次性票据：ticket -> (tokens, user, created_at)
# 配了 Redis 就存 Redis（见 `_issue_ticket` / `_take_ticket`）；这里的字典是
# 单实例/Redis 不可用时的降级路径，否则"回调落在 A、换票落在 B"会找不到票据。
_TICKET_TTL = 60.0
_tickets: Dict[str, Tuple[dict, dict, float]] = {}

# 错误短码 → 用户可读文案。对外只吐短码，避免把 IdP 细节带进 URL。
_ERROR_TEXT = {
    "denied": "您已在企业身份平台取消授权",
    "state": "登录会话已失效，请重新点击单点登录",
    "exchange": "单点登录校验失败，请重试",
    "unavailable": "单点登录服务暂不可用，请稍后再试，或使用账号密码登录",
    "no_account": "您的企业账号尚未开通本系统访问权限，请联系管理员",
    "disabled": "您的账号已被停用，请联系管理员",
    "tenant": "所属租户不存在或已停用，请联系管理员",
    "config": "单点登录配置不完整，请联系管理员",
}


def _redirect_uri(request: Request) -> str:
    """回调地址。显式配置优先；未配置则按请求推导。

    生产必须显式配置：反代改写过 Host / X-Forwarded-Proto 时，
    推导出的 https 会变成 http，IdP 侧登记的却是 https，
    表现为 IdP 报 redirect_uri_mismatch —— 而这个错误在本系统日志里看不出来。
    """
    if settings.oidc_redirect_uri:
        return settings.oidc_redirect_uri
    base = str(request.base_url).rstrip("/")
    return base + "/api/v1/auth/sso/callback"


def _login_error(code: str) -> RedirectResponse:
    """把失败统一转成登录页上的提示。"""
    url = "/login?" + urllib.parse.urlencode({"sso_error": code})
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


def _seed_state_cookie(resp: RedirectResponse, state: str, request: Request) -> None:
    """种下与 state 绑定的 HttpOnly Cookie（防登录 CSRF 的另一半）。

    SameSite=Lax：回调是从 IdP 域名的顶层跳转，Lax 允许携带；
    用 Strict 会导致回调时 Cookie 不被发送，登录永远失败。
    """
    secure = _is_https(request)
    resp.set_cookie(
        STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax",
        secure=secure, path="/",
    )


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if proto:
        return proto.lower() == "https"
    return str(request.base_url).startswith("https://")


# ======================================================================
# 1. 发起登录
# ======================================================================
@router.get("/auth/sso/login", summary="发起单点登录",
            description="302 跳转到企业身份提供方（IdP）。可带 `next` 指定登录后回跳的站内路径、"
                        "`tenant` 提示租户。未启用 SSO 时返回 404。",
            include_in_schema=True)
def sso_login(request: Request,
              next: str = Query("", description="登录后回跳的站内路径，如 /admin"),
              tenant: str = Query("", description="租户提示（IdP 支持 login_hint 时生效）"),
              ) -> RedirectResponse:
    if not oidc_service.enabled:
        # 404 而不是 503：没配置 SSO 的部署里，这个地址本来就不该存在
        raise AppError("单点登录未启用", code=404)

    redirect_uri = _redirect_uri(request)
    try:
        url, pending = oidc_service.build_authorize_url(
            next_url=next, tenant_hint=tenant, redirect_uri=redirect_uri)
    except AppError:
        raise
    except Exception as exc:
        logger.exception("构造 SSO 授权地址失败：%s", exc)
        raise AppError("单点登录服务暂不可用", code=503)

    logger.info("SSO 发起授权 tenant_hint=%s next=%s ip=%s",
                pending.tenant_hint or "-", pending.next_url,
                getattr(request.state, "client_ip", ""))
    resp = RedirectResponse(url, status_code=302)
    _seed_state_cookie(resp, pending.state, request)
    # 不许缓存：这是带 state 的一次性跳转，缓存后重放会拿到过期 state
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ======================================================================
# 2. 处理回调
# ======================================================================
@router.get("/auth/sso/callback", summary="单点登录回调",
            description="由 IdP 重定向调用。校验 state（Cookie 双绑）+ nonce，服务端用 code 换 token，"
                        "映射角色与租户后签发本地 JWT，最后 302 回前端页面。")
def sso_callback(
    request: Request,
    code: str = Query("", description="IdP 返回的授权码"),
    state: str = Query("", description="发起时生成的 state"),
    error: str = Query("", description="IdP 返回的错误码，如 access_denied"),
    error_description: str = Query("", description="IdP 返回的错误描述"),
) -> RedirectResponse:
    ip = getattr(request.state, "client_ip", "")
    if not oidc_service.enabled:
        return _login_error("config")

    if error:
        # 用户在 IdP 页面点了"取消"，或 IdP 侧拒绝。这不是异常，是正常流程分支。
        logger.info("SSO 被 IdP 拒绝 error=%s desc=%s ip=%s", error, error_description, ip)
        return _login_error("denied" if error == "access_denied" else "exchange")
    if not code or not state:
        logger.warning("SSO 回调缺少参数 code=%s state=%s ip=%s",
                       bool(code), bool(state), ip)
        return _login_error("state")

    # ---- 防登录 CSRF：state 必须与浏览器 Cookie 完全一致 ----
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        # 两种可能：攻击者构造的回调（Cookie 对不上），或用户换了浏览器。
        # 两者都按失败处理——放行任何一种都等于放弃这道防线。
        logger.warning("SSO state Cookie 不匹配（疑似登录 CSRF）ip=%s", ip)
        return _login_error("state")

    try:
        pending = oidc_service.consume_state(state)
    except UnauthorizedError as exc:
        logger.warning("SSO state 失效：%s ip=%s", exc.message, ip)
        return _login_error("state")

    # ---- 服务端换 token ----
    try:
        token_resp = oidc_service.exchange_code(code, pending, redirect_uri=_redirect_uri(request))
    except UnauthorizedError:
        return _login_error("exchange")
    except AppError:
        return _login_error("unavailable")

    id_token = token_resp.get("id_token") or ""
    access_token = token_resp.get("access_token") or ""
    if not id_token:
        logger.error("SSO token 响应缺少 id_token，keys=%s", list(token_resp.keys()))
        return _login_error("exchange")

    try:
        claims = oidc_service.decode_id_token(id_token, expected_nonce=pending.nonce)
    except UnauthorizedError as exc:
        logger.warning("SSO ID Token 校验失败：%s ip=%s", exc.message, ip)
        return _login_error("exchange")

    # ---- 身份以 userinfo 为准 ----
    # 本地只做了 nonce/aud/iss 的非签名校验。userinfo 是 IdP 直接用 access_token
    # 返回的，不依赖我们的验签实现，因此**优先采信**；只有 IdP 没提供 userinfo 时
    # 才退回 ID Token claims（此时信任基础是 TLS + 服务端换取，可接受但不最优）。
    userinfo = oidc_service.fetch_userinfo(access_token) if access_token else None
    identity = dict(claims)
    if userinfo:
        identity.update(userinfo)
        logger.debug("SSO 身份以 userinfo 为准 sub=%s", userinfo.get("sub"))

    username = pick_claim(identity, settings.oidc_username_claim, "")
    if not username:
        # 退到 email 再退到 sub：有的 IdP 不开 preferred_username scope
        username = pick_claim(identity, settings.oidc_email_claim, "") \
            or pick_claim(identity, "sub", "")
    username = (username or "").strip()
    if not username:
        logger.error("SSO 身份缺少可用标识，claims=%s", sorted(identity.keys()))
        return _login_error("exchange")

    role = resolve_role(identity)
    try:
        tenant_id = resolve_tenant(identity, pending.tenant_hint)
    except AppError as exc:
        logger.warning("SSO 租户标识不合法：%s", exc.message)
        return _login_error("tenant")

    email = pick_claim(identity, settings.oidc_email_claim, "")
    display_name = pick_claim(identity, settings.oidc_name_claim, "") or username

    # ---- 进入租户上下文 ----
    # 审计、日志、后续任何读 current_tenant() 的逻辑都要落在**这次登录判定出的租户**上。
    # 不设的话会落到"中间件预置的默认租户"：表现为 A 公司用户通过 SSO 登录后，
    # 审计记录被记到 default 名下，出事时根本查不到是谁做的。
    _bind_tenant(tenant_id)

    # ---- 本地账号：存在则同步画像，不存在按策略开户 ----
    try:
        user = _sync_or_provision(
            username=username, tenant_id=tenant_id, role=role,
            display_name=display_name, email=email,
        )
    except _SsoAccountError as exc:
        logger.warning("SSO 账号处理失败 username=%s tenant=%s：%s",
                       username, tenant_id, exc.reason)
        return _login_error(exc.code)

    mgr = user_service.jwt_manager()
    if not mgr.enabled:
        logger.error("SSO 回调时 JWT 未启用，无法签发本地会话")
        return _login_error("config")

    tokens = mgr.issue({
        "user_id": user.get("user_id", ""),
        "username": user.get("username", username),
        "role": user.get("role", role),
        # 与密码登录保持完全一致的载荷形状——否则会出现"SSO 进来的看不到文档"这类分裂
        "spaces": ",".join(user_service.user_spaces(user)),
        "perm_groups": user.get("perm_groups", "") or "",
        "tenant_id": user.get("tenant_id") or settings.default_tenant_id,
    })
    try:
        from app_v2.storage.meta_store import meta_store

        meta_store.touch_user_login(user.get("user_id", ""))
    except Exception:
        pass

    audit_service.record(
        action="auth.sso_login",
        actor=user.get("username", username), role=user.get("role", role),
        resource="auth:sso",
        ip=ip, user_agent=request.headers.get("User-Agent", ""),
        detail={"user_id": user.get("user_id"), "tenant_id": tenant_id,
                "idp": settings.oidc_issuer, "via_userinfo": bool(userinfo),
                "provisioned": bool(user.pop("_provisioned", False))},
    )

    ticket = _issue_ticket({**tokens, "user": user_service.public_user(user)})
    target = _safe_next(pending.next_url)
    sep = "&" if "?" in target else "?"
    resp = RedirectResponse(f"{target}{sep}sso_ticket={ticket}", status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.delete_cookie(STATE_COOKIE, path="/")
    logger.info("SSO 登录成功 username=%s tenant=%s role=%s ip=%s",
                user.get("username"), tenant_id, user.get("role"), ip)
    return resp


class _SsoAccountError(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _bind_tenant(tenant_id: str) -> None:
    """把租户写进请求级上下文。失败不影响登录（上下文只是观测维度）。"""
    try:
        from app_v2.services.tenant_service import set_current_tenant

        set_current_tenant(tenant_id)
    except Exception:                                            # pragma: no cover
        pass


def _sync_or_provision(username: str, tenant_id: str, role: str,
                       display_name: str, email: str) -> dict:
    """SSO 用户的本地账号处理。

    **关键设计：SSO 通过后，本地角色以本地记录为准，不以 IdP 每次下发的为准。**
    原因：IdP 的组信息反映的是"这个人在企业里的岗位"，而本系统的角色反映的是
    "他能对本知识库做什么"。二者不是一回事——把 IdP 组直接映射成每天覆盖的本地角色，
    会让管理员在后台做的权限调整被下一次登录静默抹掉，出了问题还查不出是谁改的。
    因此：首次开户时用 `resolve_role` 的结果；后续登录只做画像同步（显示名/邮箱），
    角色变更走管理后台或显式的 `oidc_sync_role` 配置。

    安全上的例外：如果本地账号**已被禁用**，SSO 不绕过禁用——
    否则"停用账号"这个操作在 SSO 部署里就完全失效了。
    """
    from app_v2.services import tenant_service as _ts
    from app_v2.storage.meta_store import meta_store

    try:
        _ts.tenant_service.require_active(tenant_id)
    except AppError as exc:
        # 租户不存在/停用：不给开户，也不给登录
        raise _SsoAccountError("tenant", exc.message)

    existing = None
    try:
        existing = meta_store.get_user_by_name(username, tenant_id)
    except Exception as exc:
        logger.error("SSO 查询本地账号失败：%s", exc)
        raise _SsoAccountError("unavailable", "用户表不可用")

    if existing:
        if not existing.get("enabled", True):
            raise _SsoAccountError("disabled", "账号已被禁用")
        patch: dict = {}
        if display_name and display_name != existing.get("display_name"):
            patch["display_name"] = display_name
        if patch:
            try:
                meta_store.update_user(existing["user_id"], patch)
                existing.update(patch)
            except Exception as exc:
                logger.warning("SSO 同步用户画像失败（不阻断登录）：%s", exc)
        return existing

    if not settings.oidc_auto_provision:
        # 未开自动开户：企业通常要求"先审批开通，再允许 SSO 登录"，
        # 这条提示直接告诉用户去找管理员，而不是让他反复尝试
        raise _SsoAccountError("no_account", "账号未开通且未启用自动开户")

    # 自动开户：随机密码占位。SSO 用户不走密码通道，
    # 但库里必须有 password_hash（create_user 要求），且随机值确保无法被猜中。
    try:
        rec = user_service.create_user(
            username=username,
            password=secrets.token_urlsafe(32),
            role=role,
            spaces=settings.default_space_id,
            display_name=display_name or username,
            perm_groups="",
            created_by="sso",
            tenant_id=tenant_id,
        )
        logger.info("SSO 自动开户 username=%s tenant=%s role=%s", username, tenant_id, role)
        rec["_provisioned"] = True
        return rec
    except AppError as exc:
        # 并发双回调：两个请求同时判定"不存在"，一个成功一个撞 409。
        # 撞 409 的那个应当重新读取并继续登录，而不是给用户报错。
        if exc.code == 409:
            again = meta_store.get_user_by_name(username, tenant_id)
            if again:
                return again
        logger.warning("SSO 自动开户失败 username=%s：%s", username, exc.message)
        raise _SsoAccountError("no_account", exc.message)


# ======================================================================
# 3. 一次性票据换 token
# ======================================================================
# 票据同样必须跨副本可见：回调（发票据）与换票（消费票据）是**两个独立的
# HTTP 请求**，中间隔了一次浏览器 302 + 页面加载，负载均衡完全可能把它们
# 分到不同副本。进程内字典会让换票在另一台机器上永远查不到票，
# 表现为"点登录后回到 /app 又被踢回 /login"。
_TICKET_PREFIX = "rag:sso:ticket:"


def _ticket_redis():
    """与 OIDC state 共用同一个 RedisLayer（cache.l2）。"""
    try:
        from app_v2.services.cache_service import cache

        layer = cache.l2
        if layer.available or layer._ensure():
            return layer
    except Exception:                           # pragma: no cover
        pass
    return None


def _issue_ticket(payload: dict) -> str:
    _gc_tickets()
    ticket = secrets.token_urlsafe(32)
    # 票据里含 access/refresh token，属于高敏数据：只放 Redis，且 TTL 60 秒。
    # 不放 L1，避免 token 在本进程内存里多留一份。
    r = _ticket_redis()
    if r is not None:
        try:
            raw = json.dumps(payload, ensure_ascii=False)
        except Exception:
            raw = ""
        if raw and r.setex_raw(_TICKET_PREFIX + ticket, int(_TICKET_TTL), raw):
            return ticket
    _tickets[ticket] = (payload, {}, time.time())
    return ticket


def _take_ticket(ticket: str) -> Optional[Tuple[dict, dict, float]]:
    """取出并删除票据。Redis 模式用 DEL 返回值判原子性（同 OIDC state）。"""
    r = _ticket_redis()
    if r is not None:
        key = _TICKET_PREFIX + ticket
        raw = r.get(key)
        if raw is not None:
            if r.delete_keys(key) != 1:
                return None
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                return None
            # TTL 已由 Redis 保证，created 只作占位，后续不再用它判过期
            return data, {}, time.time()
    return _tickets.pop(ticket, None)


def _gc_tickets() -> None:
    now = time.time()
    for k in [k for k, v in _tickets.items() if now - v[2] > _TICKET_TTL]:
        _tickets.pop(k, None)


@router.post("/auth/sso/ticket", summary="用一次性票据换取 Token",
            description="SSO 回调后前端拿到 `sso_ticket`，用本接口换成 access/refresh token。"
                        "票据 60 秒内有效、只能用一次。票据本身**不含**任何身份信息，"
                        "因此放在 URL 里是安全的（拿到它也必须立刻来换，且换过即废）。",
            include_in_schema=True)
async def exchange_ticket(request: Request) -> Dict[str, Any]:
    # 用 Request 而不是 pydantic 模型，是为了让字典读取失败时能给出明确文案，
    # 而不是抛 422 的一长串校验细节（这个接口只被前端一个地方调用）。
    ticket = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            ticket = str(body.get("ticket") or "").strip()
    except Exception:
        ticket = ""
    if not ticket:
        raise AppError("缺少 ticket", code=422)

    _gc_tickets()
    item = _take_ticket(ticket)
    if not item:
        # 已被用过 / 已过期 / 从未存在 —— 三种情况对外完全同义，不区分，
        # 否则就成了"票据是否有效"的探测器。
        raise UnauthorizedError("登录票据无效或已过期，请重新登录")
    payload, _meta, created = item
    if time.time() - created > _TICKET_TTL:
        raise UnauthorizedError("登录票据已过期，请重新登录")

    logger.info("SSO 票据换票成功 username=%s",
                (payload.get("user") or {}).get("username"))
    return payload
