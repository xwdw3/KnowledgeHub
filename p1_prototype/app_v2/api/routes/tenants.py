"""租户管理接口（P4）。

**为什么必须有这组接口**：多租户隔离做完之后，如果只在库里手工插租户，
那"隔离"就成了一个只有开发者能用的能力——运维要开一家新公司，
得连数据库执行 SQL。缺失的这层运营入口，会让整套隔离能力事实上不可用。

同时要有一个**租户视角的配额与用量查询**：`max_users/max_docs/max_qps`
是共享实例上唯一能防止"一家公司把资源吃光"的手段，
但没有任何接口能看到"用了多少、还剩多少"，配额就形同虚设。

权限：全部要求 `kb:admin`，跨租户查看要求 `root`。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app_v2.core.response import NotFoundError, ValidationError
from app_v2.core.security import PERM_KB_ADMIN
from app_v2.api.deps import require_permission
from app_v2.services import audit_service, tenant_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["租户"])


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=3, max_length=64,
                           description="租户标识，仅小写字母/数字/下划线/连字符，会进对象存储 key 前缀")
    name: str = Field(..., min_length=1, max_length=128, description="公司/组织名称")
    description: str = Field("", max_length=512)
    max_users: int = Field(100, ge=0, description="用户数上限，0 表示不限制")
    max_docs: int = Field(10000, ge=0, description="文档数上限，0 表示不限制")
    max_qps: int = Field(0, ge=0, description="QPS 上限，0 表示不限制")


class TenantUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = None
    max_users: Optional[int] = Field(None, ge=0)
    max_docs: Optional[int] = Field(None, ge=0)
    max_qps: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, description="active / suspended")


class TenantData(BaseModel):
    tenant_id: str
    name: str
    description: str = ""
    max_users: int = 0
    max_docs: int = 0
    max_qps: int = 0
    status: str = "active"


class TenantListData(BaseModel):
    total: int
    items: list[TenantData]


def _with_usage(rec: dict) -> Dict[str, Any]:
    """拼上实时用量。

    配额不显示用量等于没有配额：管理员看到 max_docs=10000 无法判断
    "这家公司快满了还是刚开户"。用量本身也算一次查询，租户数量不大时可接受。
    """
    tid = rec.get("tenant_id", "")
    data = dict(rec)
    try:
        data["usage"] = tenant_service.tenant_service.stats(tid)
    except Exception:
        data["usage"] = {}
    return data


@router.post("/tenants", response_model=TenantData, summary="创建租户",
             description="新开一家公司/组织。`tenant_id` 会进入对象存储的 key 前缀与所有查询条件，"
                         "因此严格限制字符集（不允许 `/`、`..`、空格）。需要 `kb:admin` 权限。")
def create_tenant(req: TenantCreateRequest, request: Request,
                 principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> TenantData:
    rec = tenant_service.tenant_service.create(
        tenant_id=req.tenant_id, name=req.name, description=req.description,
        max_users=req.max_users, max_docs=req.max_docs, max_qps=req.max_qps,
    )
    audit_service.record(
        action="tenant.create", actor=principal.actor, role=principal.role,
        resource=f"tenant:{rec['tenant_id']}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"name": req.name, "max_users": req.max_users, "max_docs": req.max_docs},
    )
    return TenantData(**_with_usage(rec))


@router.get("/tenants", response_model=TenantListData, summary="租户列表",
            description="列出所有租户及其用量。需要 `kb:admin` 权限。"
                        "**注意**：这是唯一一个跨租户读取的接口，因此结果里不含任何业务数据，"
                        "只有租户元信息与计数。")
def list_tenants(request: Request,
                 principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> TenantListData:
    items = [_with_usage(t) for t in tenant_service.tenant_service.list_all()]
    audit_service.record(
        action="tenant.list", actor=principal.actor, role=principal.role,
        resource="tenant:list",
        ip=getattr(request.state, "client_ip", ""),
        detail={"count": len(items)},
    )
    return TenantListData(total=len(items), items=[TenantData(**i) for i in items])


@router.get("/tenants/{tenant_id}", response_model=TenantData, summary="租户详情",
            description="查看单个租户及其用量。需要 `kb:admin` 权限。")
def get_tenant(tenant_id: str, request: Request,
               principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> TenantData:
    t = tenant_service.tenant_service.get(tenant_id)
    if not t:
        # 与"无权查看"用同一个 404 —— 403 会变成租户名探测器
        raise NotFoundError(f"租户不存在：{tenant_id}")
    from app_v2.storage.meta_store import meta_store

    rec = meta_store.get_tenant(t.tenant_id) or {
        "tenant_id": t.tenant_id, "name": t.name, "description": t.description,
        "max_users": t.max_users, "max_docs": t.max_docs, "max_qps": t.max_qps,
        "status": t.status,
    }
    return TenantData(**_with_usage(rec))


@router.patch("/tenants/{tenant_id}", response_model=TenantData, summary="更新租户",
              description="改名称、配额或状态。`status=suspended` 停用后：该租户所有用户无法登录、"
                          "已签发的 token 在下次请求时被拒（`require_active` 在登录与建号路径上）。"
                          "需要 `kb:admin` 权限。")
def update_tenant(tenant_id: str, req: TenantUpdateRequest, request: Request,
                  principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> TenantData:
    fields = req.model_dump(exclude_none=True)
    if not fields:
        raise ValidationError("没有需要更新的字段")
    if "status" in fields and fields["status"] not in ("active", "suspended", "disabled"):
        raise ValidationError("status 只能是 active / suspended / disabled")
    rec = tenant_service.tenant_service.update(tenant_id, fields)
    audit_service.record(
        action="tenant.update", actor=principal.actor, role=principal.role,
        resource=f"tenant:{tenant_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"fields": sorted(fields.keys())},
    )
    return TenantData(**_with_usage(rec))
