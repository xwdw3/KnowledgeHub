"""安全接口：登录、Token 生命周期、用户管理、密钥管理、当前身份查询。

所有管理接口都要求相应权限点，且每次操作都写审计——账号与密钥是最高危的资产，
创建与吊销必须留痕（谁、什么时候、给谁开的什么角色）。

认证双通道的分工：
  - `/auth/login` + JWT：人类用户（浏览器、IM、客户端）
  - `/auth/keys` + API Key：系统间调用（脚本、CI、第三方集成）
两条路签出的身份都会归一成 Principal，下游无需感知差异。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request

from app_v2.api.deps import get_principal, require_permission
from app_v2.core.response import NotFoundError, UnauthorizedError, ValidationError
from app_v2.core.security import (
    PERM_KB_READ, PERM_USER_ADMIN, ROLES, role_permissions,
)
from app_v2.schemas.models import (
    KeyCreateRequest, KeyCreatedData, KeyListData, LoginRequest, LogoutRequest,
    PrincipalData, RefreshRequest, TokenFromKeyRequest, UserCreateRequest, UserData,
    UserListData, UserUpdateRequest,
)
from app_v2.services import audit_service, auth_service, user_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["安全"])


# ==================== P3：登录与 Token ====================
@router.post("/auth/login", summary="账号密码登录",
             description="校验用户名密码，签发 access + refresh token。"
                         "access 默认 30 分钟，refresh 默认 7 天且**一次性使用**（刷新即轮换）。")
def login(req: LoginRequest, request: Request) -> Dict[str, Any]:
    result = user_service.login(req.username, req.password,
                                client_ip=getattr(request.state, "client_ip", ""),
                                tenant_id=req.tenant_id)
    audit_service.record(
        action="auth.login",
        actor=result["user"]["username"], role=result["user"]["role"],
        resource="auth:login",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"user_id": result["user"]["user_id"],
                "tenant_id": result["user"].get("tenant_id", "")},
    )
    return result


@router.post("/auth/refresh", summary="刷新 Token",
             description="用 refresh token 换一对新 token。旧 refresh token 立即失效（轮换策略），"
                         "防止泄露后被无限续期。")
def refresh(req: RefreshRequest, request: Request) -> Dict[str, Any]:
    result = user_service.refresh(req.refresh_token)
    audit_service.record(
        action="auth.refresh",
        actor=result["user"]["username"], role=result["user"]["role"],
        resource="auth:refresh",
        ip=getattr(request.state, "client_ip", ""),
        detail={"user_id": result["user"]["user_id"]},
    )
    return result


@router.post("/auth/logout", summary="注销 Token",
             description="把 token 的 jti 写入黑名单，在自然过期前立即失效。"
                         "传 refresh_token 可一并注销；重复注销幂等。")
def logout(req: LogoutRequest, request: Request) -> Dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    access = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    access = req.access_token or access
    if not access and not req.refresh_token:
        raise ValidationError("请提供 access_token 或 refresh_token")
    result = user_service.logout(access, req.refresh_token or "")
    principal = getattr(request.state, "principal", None)
    audit_service.record(
        action="auth.logout",
        actor=principal.actor if principal else "anonymous",
        role=principal.role if principal else "-",
        resource="auth:logout",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"revoked": result["revoked"], "backend": result["backend"]},
    )
    return result


@router.post("/auth/token", summary="用 API Key 换取 JWT",
             description="**JWT 强制模式下的唯一密钥入口**。用有效 API Key 换一对 access/refresh token，"
                         "之后所有业务接口一律携带该 JWT。\n\n"
                         "为什么要多这一步：JWT 有有效期、可注销（jti 黑名单）、权限变化即时生效；"
                         "而长期密钥一旦泄露就是永久后门。让密钥只能换「短期票据」、不能直接开门，"
                         "是把「一把万能钥匙」降级成「一台发票机」。\n\n"
                         "本接口免 JWT 认证（否则形成逻辑闭环），但请求体里的 API Key 会被完整校验。")
def token_from_key(req: TokenFromKeyRequest, request: Request) -> Dict[str, Any]:
    from app_v2.core.settings import settings

    principal = auth_service.authenticate(req.api_key, getattr(request.state, "client_ip", ""))
    # 密钥身份转成"用户记录"形状后走同一套签发逻辑，保证两条入口签出的
    # token 载荷完全一致——否则下游会出现"登录来的 token 有 spaces、换来的没有"这类分裂。
    # ⚠ 空间与密级是两个正交维度，绝不互相赋值（见 auth_service.Principal 的说明）：
    #   spaces    = 能进哪些业务空间（内容维度）
    #   perm_groups = 能看哪些密级片段（人的维度）
    # API Key 只有 perm_group 这一个维度，没有空间概念，因此：
    #   - spaces 落默认空间（与 P2 行为一致：密钥不被空间限制挡在门外）
    #   - perm_group 原样搬到 pg（密级），不能当成 spaces 塞进去——
    #     否则 perm_group=hr 的密钥换出的票会变成"只能进 hr 空间"，
    #     连自己刚上传的 default 空间文档都投不进去（403）。
    user = {
        "user_id": f"key:{principal.key_id}",
        "username": principal.actor,
        "role": principal.role,
        "spaces": settings.default_space_id,
        "perm_groups": ",".join(principal.perm_groups),
        # 把来源密钥写进 token：吊销密钥时才能连带作废用它换出的所有票据。
        # 不写的话，"吊销密钥"只挡住换票入口，已经发出去的 token 还能用满 30 分钟——
        # 对离职交接、密钥泄露这类场景，30 分钟足够造成实质损失。
        "src_key": principal.key_id,
    }
    result = user_service.jwt_manager().issue(user)
    audit_service.record(
        action="auth.token_from_key",
        actor=principal.actor, role=principal.role,
        resource=f"key:{principal.key_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"key_id": principal.key_id},
    )
    return {
        **result,
        "principal": {
            "actor": principal.actor, "role": principal.role,
            "key_id": principal.key_id, "perm_groups": list(principal.perm_groups),
        },
    }


@router.get("/auth/methods", summary="可用认证方式",
            description="公开接口，供登录页决定展示哪些登录方式（密码登录是否开启）。")
def methods() -> Dict[str, Any]:
    from app_v2.core.settings import settings

    # SSO 是否可用不能只看 `oidc_enabled`，还要看 issuer / client_id 是否填齐。
    # 只配了开关没配 issuer 的话，前端会画出一个点进去必失败的按钮——
    # 这比不显示按钮更糟：用户会以为是系统坏了，而不是"没配好"。
    oidc_ready = bool(settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id)

    return {
        "password": bool(settings.jwt_enabled and user_service.jwt_manager().enabled),
        "api_key": bool(settings.auth_enabled),
        # 让前端/脚本一眼看出当前是"只认 JWT"还是"双通道"，
        # 避免按 P2 习惯直接拿密钥调业务接口，收到 401 却不明原因
        "auth_mode": settings.auth_mode,
        "jwt_required": settings.jwt_strict,
        "token_from_key": "/api/v1/auth/token",
        "token_types": ["Bearer"],
        "access_ttl": settings.jwt_access_ttl,
        "refresh_ttl": settings.jwt_refresh_ttl,
        # ---------- P4：单点登录 ----------
        # 字段名与 login.js 的读取保持一致（oidc_enabled / oidc_label）
        "oidc_enabled": oidc_ready,
        "oidc_label": _sso_label(settings),
        "oidc_issuer": settings.oidc_issuer if oidc_ready else "",
        "oidc_login_url": "/api/v1/auth/sso/login" if oidc_ready else "",
        "env": settings.env,
        "version": settings.app_version,
    }


def _sso_label(settings) -> str:
    """SSO 按钮文案。按 issuer 猜一个可读的 IdP 名称。

    猜不出就退化成通用文案——写死"企业微信"或"钉钉"而在别的 IdP 上显示，
    会让用户点开一个明显不对的页面。
    """
    iss = (settings.oidc_issuer or "").lower()
    if "keycloak" in iss:
        return "使用 Keycloak 账号登录"
    if "login.microsoftonline" in iss or "azure" in iss:
        return "使用 Microsoft 账号登录"
    if "accounts.google" in iss:
        return "使用 Google 账号登录"
    if "authing" in iss:
        return "使用 Authing 账号登录"
    return "使用企业账号登录（SSO）"


# ==================== 当前身份 ====================
@router.get("/auth/me", response_model=PrincipalData, summary="查询当前身份",
            description="返回当前凭据对应的身份、角色与权限集合，便于调用方自检权限。")
def whoami(principal: Principal = Depends(get_principal)) -> PrincipalData:
    if principal is None:
        raise UnauthorizedError()
    role = ROLES.get(principal.role)
    return PrincipalData(
        actor=principal.actor,
        role=principal.role,
        key_id=principal.key_id,
        permissions=sorted(principal.permissions),
        perm_groups=list(principal.perm_groups),
        role_label=role.label if role else principal.role,
        is_admin=principal.is_admin,
    )


# ==================== P3：用户管理 ====================
@router.post("/auth/users", response_model=UserData, summary="创建用户",
             description="创建账号。密码至少 8 位，仅存 PBKDF2 哈希。需要 `user:admin` 权限。")
def create_user(req: UserCreateRequest, request: Request,
                principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> UserData:
    rec = user_service.create_user(
        username=req.username, password=req.password, role=req.role,
        spaces=req.spaces, display_name=req.display_name or "",
        perm_groups=req.perm_groups or "",
        created_by=principal.actor,
    )
    audit_service.record(
        action=audit_service.ACTION_USER_CREATE,
        actor=principal.actor, role=principal.role,
        resource=f"user:{rec['user_id']}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"username": req.username, "role": req.role, "spaces": req.spaces,
                "perm_groups": req.perm_groups},
    )
    return UserData(**rec)


@router.get("/auth/users", response_model=UserListData, summary="用户列表",
            description="列出用户（**不含密码哈希与盐**）。需要 `user:admin` 权限。")
def list_users(request: Request,
               principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> UserListData:
    items = user_service.list_users()
    audit_service.record(
        action=audit_service.ACTION_USER_LIST,
        actor=principal.actor, role=principal.role, resource="user:list",
        ip=getattr(request.state, "client_ip", ""),
        detail={"count": len(items)},
    )
    return UserListData(total=len(items), items=[UserData(**u) for u in items])


@router.patch("/auth/users/{user_id}", response_model=UserData, summary="更新用户",
              description="改角色、可访问空间、启用状态或密码。需要 `user:admin` 权限。")
def update_user(user_id: str, req: UserUpdateRequest, request: Request,
                principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> UserData:
    rec = user_service.update_user(user_id, req.model_dump(exclude_none=True),
                                   by=principal.actor)
    audit_service.record(
        action=audit_service.ACTION_USER_UPDATE,
        actor=principal.actor, role=principal.role,
        resource=f"user:{user_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        # 只记字段名，绝不记密码内容
        detail={"fields": sorted(req.model_dump(exclude_none=True).keys())},
    )
    return UserData(**rec)


@router.delete("/auth/users/{user_id}", summary="禁用用户",
               description="软禁用（保留审计链）。禁用后已签发的 token 在下次校验时被拒。"
                           "不能禁用自己。需要 `user:admin` 权限。")
def disable_user(user_id: str, request: Request,
                 principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> Dict[str, Any]:
    if principal.actor == user_id or principal.key_id.endswith(user_id[:12]):
        raise ValidationError("不能禁用自己的账号")
    rec = user_service.update_user(user_id, {"enabled": False}, by=principal.actor)
    audit_service.record(
        action=audit_service.ACTION_USER_DISABLE,
        actor=principal.actor, role=principal.role,
        resource=f"user:{user_id}",
        ip=getattr(request.state, "client_ip", ""),
        detail={"username": rec.get("username")},
    )
    return {"user_id": user_id, "enabled": False}


# ==================== 密钥管理 ====================
@router.post("/auth/keys", response_model=KeyCreatedData, summary="创建 API Key",
             description="创建新密钥并返回明文（**仅此一次**）。需要 `user:admin` 权限。")
def create_key(req: KeyCreateRequest, request: Request,
               principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> KeyCreatedData:
    info = auth_service.create_key(
        owner=req.owner, role=req.role, perm_group=req.perm_group,
        description=req.description, expires_in_days=req.expires_in_days,
        created_by=principal.actor,
    )
    audit_service.record(
        action=audit_service.ACTION_KEY_CREATE,
        actor=principal.actor, role=principal.role,
        resource=f"key:{info['key_id']}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"owner": req.owner, "role": req.role, "perm_group": req.perm_group,
                "expires_in_days": req.expires_in_days},
    )
    return KeyCreatedData(**info)


@router.get("/auth/keys", response_model=KeyListData, summary="密钥列表",
            description="列出密钥元信息（**不含明文与哈希**）。需要 `user:admin` 权限。")
def list_keys(owner: str = "", include_revoked: bool = False, request: Request = None,
              principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> KeyListData:
    items = auth_service.list_keys(owner=owner, include_revoked=include_revoked)
    audit_service.record(
        action=audit_service.ACTION_KEY_LIST,
        actor=principal.actor, role=principal.role,
        resource="key:list",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"owner_filter": owner, "count": len(items)},
    )
    return KeyListData(total=len(items), items=items)


@router.delete("/auth/keys/{key_id}",
               summary="吊销 API Key",
               description="吊销后该密钥立即失效（缓存同步清除）。需要 `user:admin` 权限。"
                           "禁止吊销自己正在使用的密钥，避免把自己锁在门外。")
def revoke_key(key_id: str, request: Request,
               principal: Principal = Depends(require_permission(PERM_USER_ADMIN))) -> Dict[str, Any]:
    if key_id == principal.key_id:
        raise ValidationError("不能吊销当前正在使用的密钥，请先用另一个管理员密钥操作")

    done = auth_service.revoke_key(key_id, by=principal.actor)
    audit_service.record(
        action=audit_service.ACTION_KEY_REVOKE,
        actor=principal.actor, role=principal.role,
        resource=f"key:{key_id}",
        result=audit_service.RESULT_SUCCESS if done else audit_service.RESULT_FAILED,
        status_code=200 if done else 404,
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"revoked": done},
    )
    if not done:
        raise NotFoundError(f"密钥不存在或已吊销：{key_id}")
    return {"key_id": key_id, "revoked": True}


@router.get("/auth/roles",
            summary="角色与权限定义",
            description="返回系统内置角色及各角色权限点，供前端做按钮级控制。")
def roles(principal: Principal = Depends(require_permission(PERM_KB_READ))) -> List[Dict[str, Any]]:
    return [
        {
            "name": r.name, "label": r.label, "description": r.description,
            "permissions": sorted(role_permissions(r.name)),
        }
        for r in ROLES.values()
    ]
