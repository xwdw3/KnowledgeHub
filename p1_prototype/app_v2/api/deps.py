"""依赖注入：配置、存储、当前用户与权限校验。

权限校验用法（在路由上声明，权限点集中在 security.py，便于审计）：
    principal: Principal = Depends(require_permission(PERM_KB_WRITE))
缺少权限时统一抛 403，未认证抛 401，均由全局异常处理器输出标准错误体。

P4：认证成功后把 principal.tenant_id 写入请求级 ContextVar（`bind_tenant`），
让检索/缓存/入库这些拿不到 Request 的深层函数也能感知租户。
顺序很关键——**先认证再绑租户**，否则未认证请求会带着默认租户往下跑。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import Depends, Request

from app_v2.core.security import PERM_KB_READ
from app_v2.core.settings import Settings, settings
from app_v2.services import auth_service, audit_service, tenant_service
from app_v2.services.auth_service import Principal
from app_v2.storage.meta_store import MetaStore, meta_store
from app_v2.storage.vector_store import VectorStore, vector_store


def get_settings() -> Settings:
    return settings


def get_vector_store() -> VectorStore:
    return vector_store


def get_meta_store() -> MetaStore:
    return meta_store


def bind_tenant(principal: Principal) -> str:
    """把身份的租户写进请求级上下文，返回实际生效的租户。

    关闭多租户时强制归入默认租户：否则老数据（tenant_id 为空）会与
    新写入的数据（tenant_id=default）分裂成两个互不可见的集合。
    """
    tid = (principal.tenant_id or "").strip()
    if not settings.tenant_enabled:
        tid = settings.default_tenant_id
    tid = tid or settings.default_tenant_id
    tenant_service.set_current_tenant(tid)
    return tid


def get_principal(request: Request) -> Optional[Principal]:
    """已认证身份；未开启鉴权时返回一个本地超级用户。"""
    principal = getattr(request.state, "principal", None)
    if principal:
        if not principal.tenant_id:
            principal.tenant_id = settings.default_tenant_id
        return principal
    if not settings.auth_enabled:
        return Principal(
            actor="local-dev", role="admin", key_id="-",
            tenant_id=settings.default_tenant_id,
            perm_groups=("all",), permissions=frozenset({"*"}),
            spaces=("*",),
            # 本地超级用户可以投放任意密级（含 all），这是关闭鉴权时的调试便利，
            # 与生产路径不同：生产路径的写入范围绝不自动补 all。
            granted_perm_groups=("*",),
        )
    return None


def require_permission(permission: str):
    """生成一个校验指定权限点的依赖。"""

    def _dep(request: Request,
             principal: Optional[Principal] = Depends(get_principal)) -> Principal:
        try:
            checked = auth_service.require(principal, permission)
            # 认证通过才绑定租户上下文：放在 require 之后，
            # 保证 401 请求不会带着任何租户信息进入业务层。
            bind_tenant(checked)
            return checked
        except auth_service.ForbiddenError as exc:
            # 越权尝试写审计：合规要求必须记录"谁试图做了什么被拒绝"
            audit_service.record(
                action=f"auth.forbidden:{permission}",
                actor=principal.actor if principal else "anonymous",
                role=principal.role if principal else "-",
                resource=request.url.path,
                result=audit_service.RESULT_DENIED,
                status_code=403,
                ip=getattr(request.state, "client_ip", ""),
                user_agent=request.headers.get("User-Agent", ""),
                detail={"permission": permission, "method": request.method},
            )
            raise

    return _dep


def get_tenant(request: Request,
               principal: Optional[Principal] = Depends(get_principal)) -> str:
    """当前请求的租户。仅供不需要特定权限点的只读路由使用。"""
    if principal is None:
        return settings.default_tenant_id
    return bind_tenant(principal)


# 常用依赖，避免每个路由重复写 require_permission(...)
require_read = require_permission(PERM_KB_READ)
