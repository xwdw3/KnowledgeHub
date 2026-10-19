"""审计接口：查询操作留痕。

审计日志是合规刚需，但本身也是敏感数据（含操作人、IP、越权尝试记录），
因此查询权限收敛到 admin，且不允许修改或删除——只读、只追加。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request

from app_v2.api.deps import get_meta_store, require_permission
from app_v2.core.security import PERM_AUDIT_READ
from app_v2.schemas.models import AuditItem, AuditListData
from app_v2.services import audit_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["审计"])


def _to_item(r: dict) -> AuditItem:
    detail = r.get("detail")
    if isinstance(detail, str):
        import json
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {"raw": detail}
    created = r.get("created_at")
    return AuditItem(
        id=r.get("id"),
        trace_id=r.get("trace_id") or "-",
        actor=r.get("actor") or "anonymous",
        role=r.get("role") or "-",
        action=r.get("action") or "-",
        resource=r.get("resource") or "",
        result=r.get("result") or "success",
        status_code=int(r.get("status_code") or 200),
        ip=r.get("ip") or "",
        user_agent=r.get("user_agent") or "",
        detail=detail or {},
        latency_ms=int(r.get("latency_ms") or 0),
        created_at=str(created) if created else None,
    )


@router.get("/audit/logs", response_model=AuditListData, summary="查询审计日志",
            description="按操作人、动作、结果过滤，倒序返回。需要 `audit:read` 权限。"
                        "审计日志只读，系统不提供修改或删除接口。")
def list_audit(action: Optional[str] = None, actor: Optional[str] = None,
               result: Optional[str] = None, limit: int = 100,
               principal: Principal = Depends(require_permission(PERM_AUDIT_READ))) -> AuditListData:
    rows = audit_service.query(actor=actor or "", action=action or "",
                               result=result or "", limit=limit)
    items = [_to_item(r) for r in rows]
    return AuditListData(total=len(items), items=items)


@router.get("/audit/summary", summary="审计概览",
            description="最近 24 小时的审计统计与写入队列健康度。需要 `audit:read` 权限。")
def audit_summary(principal: Principal = Depends(require_permission(PERM_AUDIT_READ))) -> Dict[str, Any]:
    return audit_service.summary()


@router.get("/audit/errors", summary="查询错误日志",
            description="按 trace_id 反查 5xx 异常的完整堆栈。需要 `audit:read` 权限。\n\n"
                        "**为什么需要这个接口**：日志会被轮转、被采集、几天后就没了，而线上排查"
                        "往往发生在用户报障之后。把异常落库，才能凭响应体里返回的 trace_id 反查到根因。")
def list_errors(trace_id: str = "", limit: int = 50,
                principal: Principal = Depends(require_permission(PERM_AUDIT_READ))) -> Dict[str, Any]:
    rows = get_meta_store().query_errors(trace_id=trace_id or "", limit=limit)
    return {"total": len(rows), "items": rows}


@router.post("/audit/flush", summary="刷新审计队列",
             description="等待后台队列落库完成，用于停机前或验证时确保数据已写入。需要 `audit:read` 权限。")
def flush(principal: Principal = Depends(require_permission(PERM_AUDIT_READ))) -> Dict[str, Any]:
    done = audit_service.flush(timeout=5)
    return {"flushed": done, **audit_service.writer_stats()}
