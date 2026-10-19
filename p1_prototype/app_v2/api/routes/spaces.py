"""知识库空间管理接口。

权限模型：
  - 读（列表/详情）：任何能读知识库的人 —— 员工要能看到自己有哪些空间可查
  - 写（增/改/删）：`space:admin`，即 admin 与 kb_admin

列表接口按身份自动裁剪：普通用户只看得到自己被授权的空间，
不返回"存在但无权访问"的空间信息，避免泄露组织架构。

P4：全部操作都先按 principal.tenant_id 收敛到本租户。租户是最外层边界，
空间级权限只在租户内部起作用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app_v2.core.response import AppError
from app_v2.core.security import PERM_KB_READ, PERM_SPACE_ADMIN, space_allowed
from app_v2.api.deps import require_permission
from app_v2.schemas.models import (
    SpaceCreateRequest, SpaceData, SpaceListData, SpaceUpdateRequest,
)
from app_v2.services import audit_service, space_service
from app_v2.services.auth_service import Principal
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["知识库空间"])


@router.get("/spaces", response_model=SpaceListData, summary="空间列表",
            description="返回当前身份可访问的知识库空间。管理员（`*`）可见全部。")
def list_spaces(request: Request,
                include_deleted: bool = False,
                principal: Principal = Depends(require_permission(PERM_KB_READ))) -> SpaceListData:
    items = space_service.list_spaces(
        visible=principal.space_scope,
        status="" if include_deleted else "active",
        tenant_id=principal.tenant_id,
    )
    for it in items:
        try:
            it["doc_count"] = len(
                meta_store.list_documents(principal.tenant_id,
                                          limit=100000, spaces=[it["space_id"]])
            )
        except Exception:
            it["doc_count"] = None
    return SpaceListData(total=len(items), items=[SpaceData(**i) for i in items])


@router.get("/spaces/{space_id}", response_model=SpaceData, summary="空间详情")
def get_space(space_id: str,
              principal: Principal = Depends(require_permission(PERM_KB_READ))) -> SpaceData:
    rec = meta_store.get_space(space_id, principal.tenant_id)
    if not rec or rec.get("status") == "deleted":
        raise AppError("空间不存在", code=404)
    if not space_allowed(space_id, principal.space_scope):
        # 用 404 而非 403：不确认"空间存在但你没权限"，减少信息泄露
        raise AppError("空间不存在", code=404)
    docs = meta_store.list_documents(principal.tenant_id, limit=100000, spaces=[space_id])
    return SpaceData(**space_service.public_space(rec, doc_count=len(docs)))


@router.post("/spaces", response_model=SpaceData, summary="创建空间",
             description="创建知识库空间。需要 `space:admin` 权限。")
def create_space(req: SpaceCreateRequest, request: Request,
                 principal: Principal = Depends(require_permission(PERM_SPACE_ADMIN))) -> SpaceData:
    rec = space_service.create_space(
        space_id=req.space_id, name=req.name, description=req.description,
        owner=principal.actor, by=principal.actor, tenant_id=principal.tenant_id,
    )
    audit_service.record(
        action=audit_service.ACTION_SPACE_CREATE,
        actor=principal.actor, role=principal.role,
        resource=f"space:{req.space_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"name": req.name},
    )
    return SpaceData(**rec)


@router.patch("/spaces/{space_id}", response_model=SpaceData, summary="更新空间")
def update_space(space_id: str, req: SpaceUpdateRequest, request: Request,
                 principal: Principal = Depends(require_permission(PERM_SPACE_ADMIN))) -> SpaceData:
    rec = space_service.update_space(
        space_id, name=req.name, description=req.description, by=principal.actor,
        tenant_id=principal.tenant_id,
    )
    audit_service.record(
        action=audit_service.ACTION_SPACE_UPDATE,
        actor=principal.actor, role=principal.role,
        resource=f"space:{space_id}",
        ip=getattr(request.state, "client_ip", ""),
        detail={"fields": sorted(req.model_dump(exclude_none=True).keys())},
    )
    return SpaceData(**rec)


@router.delete("/spaces/{space_id}", summary="删除空间",
               description="软删除。空间内仍有文档时默认拒绝，需显式 `force=true` 级联删除。"
                           "默认空间不可删除。需要 `space:admin` 权限。")
def delete_space(space_id: str, request: Request, force: bool = False,
                 principal: Principal = Depends(require_permission(PERM_SPACE_ADMIN))) -> Dict[str, Any]:
    result = space_service.delete_space(space_id, force=force, by=principal.actor,
                                       tenant_id=principal.tenant_id)
    audit_service.record(
        action=audit_service.ACTION_SPACE_DELETE,
        actor=principal.actor, role=principal.role,
        resource=f"space:{space_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"force": force, "removed_docs": result.get("removed_docs", 0)},
    )
    return result
