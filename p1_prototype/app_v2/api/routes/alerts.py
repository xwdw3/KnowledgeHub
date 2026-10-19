"""告警接口：事件列表、规则状态、手动评估、静默。

权限设计：
  - 看告警 → `metric:read`（与监控指标同级，运维视角）
  - 改规则开关 / 手动评估 / 静默 → `kb:admin`。静默是一条"让告警闭嘴"的操作，
    必须有人在审计里留下痕迹，所以这些动作全部记审计。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request

from app_v2.api.deps import require_permission
from app_v2.core.response import ForbiddenError, NotFoundError, ValidationError
from app_v2.core.security import PERM_KB_ADMIN, PERM_METRIC_READ
from app_v2.services import audit_service
from app_v2.services.alert_service import alert_service
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["告警"])


@router.get("/alerts", summary="告警事件列表",
            description="返回本租户可见的告警。平台级告警（tenant_id='*'）对所有租户可见。")
def list_alerts(limit: int = 50, severity: str = "",
                principal: Principal = Depends(require_permission(PERM_METRIC_READ))) -> Dict[str, Any]:
    limit = max(1, min(limit, 200))
    if severity and severity not in ("info", "warning", "critical"):
        raise ValidationError("severity 只能是 info / warning / critical")
    items = alert_service.list(principal.tenant_id, limit=limit, severity=severity)
    return {
        "total": len(items),
        "items": items,
        "stats": alert_service.stats(principal.tenant_id),
    }


@router.get("/alerts/rules", summary="告警规则与最近一次评估结果",
            description="展示每条规则的阈值、连续轮数、当前值，以及是否处于静默。"
                        "便于排查「为什么没告警」——通常是 value 没越过 threshold，或还在 for_rounds 计数中。")
def list_rules(principal: Principal = Depends(require_permission(PERM_METRIC_READ))) -> Dict[str, Any]:
    return {
        "total": len(alert_service.rules()),
        "items": alert_service.rule_states(),
        "stats": alert_service.stats(principal.tenant_id),
    }


@router.post("/alerts/evaluate", summary="手动触发一轮评估",
             description="同步跑一次规则评估。`dry_run=true` 时只返回会触发哪些规则，"
                         "不落库、不投递——用于调阈值时观察当前真实值。")
def evaluate(dry_run: bool = False, request: Request = None,
             principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    fired = alert_service.evaluate(dry_run=dry_run)
    if not dry_run:
        audit_service.record(
            action="alert.evaluate",
            actor=principal.actor, role=principal.role,
            resource="alert:evaluate",
            ip=getattr(request.state, "client_ip", "") if request else "",
            detail={"fired": [f["rule_id"] for f in fired],
                    "tenant_id": principal.tenant_id},
        )
    return {"dry_run": dry_run, "fired": len(fired), "items": fired}


@router.post("/alerts/rules/{rule_id}/enable", summary="启用/停用告警规则",
             description="停用后该规则不再评估。停用是为了应对「已知会持续报」的场景，"
                         "比静默更彻底——静默到期会自动恢复，停用不会。")
def toggle_rule(rule_id: str, enabled: bool = True, request: Request = None,
                principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    try:
        rule = alert_service.set_enabled(rule_id, enabled)
    except KeyError:
        raise NotFoundError(f"规则不存在：{rule_id}")
    audit_service.record(
        action="alert.rule.toggle",
        actor=principal.actor, role=principal.role,
        resource=f"alert:rule:{rule_id}",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"enabled": enabled, "tenant_id": principal.tenant_id},
    )
    return {"rule_id": rule.rule_id, "enabled": rule.enabled}


@router.post("/alerts/rules/{rule_id}/silence", summary="静默告警规则",
             description="在 `seconds` 秒内不再通知该规则。用于维护窗口。"
                         "静默只影响**通知**，告警仍会照常评估，到期自动恢复。")
def silence_rule(rule_id: str, seconds: int = 3600, reason: str = "",
                 request: Request = None,
                 principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    if alert_service.get_rule(rule_id) is None:
        raise NotFoundError(f"规则不存在：{rule_id}")
    if seconds <= 0:
        raise ValidationError("seconds 必须为正数")
    if seconds > 7 * 24 * 3600:
        raise ValidationError("单次静默不能超过 7 天，长期告警请显式停用规则并记录原因")
    until = alert_service.silence(rule_id, seconds=seconds, reason=reason)
    audit_service.record(
        action="alert.silence",
        actor=principal.actor, role=principal.role,
        resource=f"alert:rule:{rule_id}",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"seconds": seconds, "reason": reason, "tenant_id": principal.tenant_id},
    )
    return {"rule_id": rule_id, "silenced_until": until, "seconds": seconds}
