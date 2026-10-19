"""系统接口：健康检查、就绪探针、运行配置、Agent 配置。"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter

from app_v2.api.deps import get_meta_store, get_settings, get_vector_store
from app_v2.core.logging import resolve_instance_id
from app_v2.schemas.models import AgentConfigData, HealthData
from app_v2.services import cache_service, llm
from app_v2.services.agent_tools import TOOLS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["系统"])


@router.get("/health", response_model=HealthData, summary="健康检查",
            description="返回整体状态、模型模式与各存储后端健康情况。任一后端异常时 status=degraded。")
def health() -> HealthData:
    settings = get_settings()
    vec = get_vector_store().health()
    meta = get_meta_store().health()

    storage = {
        "vector": {"backend": vec.get("backend"), "status": vec.get("status"),
                   "detail": {k: v for k, v in vec.items() if k not in ("backend", "status")}},
        "meta": {"backend": meta.get("backend"), "status": meta.get("status"),
                 "detail": {k: v for k, v in meta.items() if k not in ("backend", "status")}},
        "cache": cache_service.cache.health(),
    }
    degraded = any(storage[k]["status"] != "ok" for k in ("vector", "meta"))
    if not llm.is_available():
        degraded = True

    return HealthData(
        status="degraded" if degraded else "ok",
        app_version=settings.app_version,
        model_mode=settings.resolved_model_mode(),
        # 多实例部署时，光知道"服务整体是 ok 的"没有意义：N 个副本里只有一个是坏的
        # 时候，负载均衡器把请求轮询到坏的那台上就是随机 1/N 的失败率。
        # 暴露实例标识，运维才能把"某次失败"对应到"某台机器"。
        instance_id=resolve_instance_id(),
        storage=storage,
        stats=_safe_stats(),
    )


@router.get("/ready", summary="就绪探针", include_in_schema=False)
def ready() -> Any:
    """容器编排用：依赖可用才返回 200，否则 503。"""
    from fastapi.responses import JSONResponse

    vec = get_vector_store().health()
    meta = get_meta_store().health()
    iid = resolve_instance_id()
    if vec["status"] == "ok" and meta["status"] == "ok":
        # 该路径在 ENVELOPE_EXCLUDE 白名单内，不会被中间件包装，故手工对齐统一响应结构
        return {"code": 200, "message": "ok", "data": {"ready": True, "instance": iid}}
    return JSONResponse(status_code=503, content={"code": 503, "message": "依赖未就绪", "data": {
        "vector": vec["status"], "meta": meta["status"], "instance": iid}})


@router.get("/config", response_model=AgentConfigData, summary="运行参数",
            description="查看 Agent 熔断参数与可用工具，便于答辩时说明约束设计。")
def config() -> AgentConfigData:
    s = get_settings()
    return AgentConfigData(
        max_rounds=s.agent_max_rounds,
        max_repeat=s.agent_max_repeat,
        timeout_sec=s.agent_timeout_sec,
        max_tool_calls=s.agent_max_tool_calls,
        tools=[t["function"]["name"] for t in TOOLS],
        auth_enabled=s.auth_enabled,
        cache_enabled=s.cache_enabled,
        audit_enabled=s.audit_enabled,
        metrics_enabled=s.metrics_enabled,
        roles=sorted(__import__("app_v2.core.security", fromlist=["ROLES"]).ROLES.keys()),
    )


@router.get("/stats", summary="知识库与问答统计")
def stats() -> Dict[str, Any]:
    return _safe_stats()


def _safe_stats() -> dict:
    try:
        return get_meta_store().stats()
    except Exception as exc:
        logger.warning("统计读取失败：%s", exc)
        return {}
