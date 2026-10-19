"""异步任务接口：提交、查询、取消、清理。

设计取舍：任务查询**只按 tenant_id 收敛**，不再叠加空间/权限组过滤。
理由：任务属于操作者的工作产物，不是知识内容。一个用户提交的任务，
同租户的其他管理员应该能看到（便于排障），但绝不能跨租户看到。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app_v2.api.deps import require_permission
from app_v2.core.response import ForbiddenError, NotFoundError, ValidationError
from app_v2.core.security import PERM_KB_ADMIN, PERM_KB_READ, PERM_KB_WRITE
from app_v2.services import audit_service, task_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["异步任务"])


def _public(rec: dict) -> dict:
    """对外输出。payload 里可能含本地路径等内部信息，只给关键字段。"""
    payload = rec.get("payload") or {}
    return {
        "task_id": rec.get("task_id"),
        "task_type": rec.get("task_type"),
        "status": rec.get("status"),
        "progress": int(rec.get("progress") or 0),
        "doc_id": rec.get("doc_id") or "",
        "actor": rec.get("actor") or "",
        "error": rec.get("error") or "",
        "retries": int(rec.get("retries") or 0),
        "elapsed_ms": int(rec.get("elapsed_ms") or 0),
        "file_name": payload.get("file_name") or "",
        "space_id": payload.get("space_id") or "",
        "result": rec.get("result") or {},
        "created_at": str(rec.get("created_at") or ""),
        "started_at": str(rec.get("started_at") or ""),
        "finished_at": str(rec.get("finished_at") or ""),
    }


@router.get("/tasks", summary="任务列表",
            description="按租户返回异步任务。可用于前端轮询上传进度。")
def list_tasks(status: str = "", task_type: str = "", limit: int = 50,
               principal: Principal = Depends(require_permission(PERM_KB_READ))) -> Dict[str, Any]:
    limit = max(1, min(limit, 200))
    items = task_service.task_service.list(principal.tenant_id, status=status,
                                           task_type=task_type, limit=limit)
    return {
        "total": len(items),
        "items": [_public(i) for i in items],
        "stats": task_service.task_service.stats(principal.tenant_id),
    }


@router.post("/tasks/purge", summary="清理已结束的任务记录",
             description="任务表是日志性质，长期运行会膨胀。清理 N 天前已结束的记录。"
                         "需要 `kb:admin` 权限。")
def purge_tasks(older_than_days: int = 7, request: Request = None,
                principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    if older_than_days < 0:
        raise ValidationError("older_than_days 不能为负数")
    removed = task_service.task_service.purge_finished(principal.tenant_id, older_than_days)
    audit_service.record(
        action="task.purge",
        actor=principal.actor, role=principal.role,
        resource="task:purge",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"removed": removed, "older_than_days": older_than_days,
                "tenant_id": principal.tenant_id},
    )
    return {"removed": removed, "older_than_days": older_than_days}


@router.get("/tasks/{task_id}", summary="任务详情")
def get_task(task_id: str,
             principal: Principal = Depends(require_permission(PERM_KB_READ))) -> Dict[str, Any]:
    rec = task_service.task_service.get(task_id, principal.tenant_id)
    if not rec:
        raise NotFoundError(f"任务不存在：{task_id}")
    return _public(rec)


@router.post("/tasks/{task_id}/cancel", summary="取消任务",
             description="只支持取消**排队中**的任务。执行中的任务无法安全中断"
                         "（可能已写入部分向量），强行中止会留下脏数据。")
def cancel_task(task_id: str, request: Request,
                principal: Principal = Depends(require_permission(PERM_KB_WRITE))) -> Dict[str, Any]:
    try:
        rec = task_service.task_service.cancel(task_id, principal.tenant_id)
    except KeyError:
        raise NotFoundError(f"任务不存在：{task_id}")
    except ValueError as exc:
        raise ValidationError(str(exc))
    audit_service.record(
        action="task.cancel",
        actor=principal.actor, role=principal.role,
        resource=f"task:{task_id}",
        ip=getattr(request.state, "client_ip", ""),
        detail={"tenant_id": principal.tenant_id},
    )
    return _public(rec)
