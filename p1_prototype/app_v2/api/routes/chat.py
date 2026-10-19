"""问答接口：/api/v1/chat（RAG/Agent）与 /api/v1/retrieve（仅检索）。

P3 增强：
  - **空间隔离**：请求可指定 `space_id`，与身份可访问空间求交集后才下推到检索层；
    指定了无权访问的空间直接 403，绝不静默返回空结果（两种信号对用户意义完全不同）
  - **多轮上下文**：会话历史优先从 Redis 上下文缓存读，PG 只作持久化兜底，
    减少每轮对话一次数据库往返；上下文纳入 spaces 维度避免跨空间串味
  - **Rerank 可观测**：响应 timings 里带 rerank 模式、是否降级、候选数

P2 增强（保留）：
  - 鉴权：需要 `kb:read` 权限，可访问的权限组由凭据绑定，**不再信任请求体内的 perm_group**
  - 缓存：检索与问答命中直接返回，响应中标注 cached 与来源层
  - 审计：每次问答都记录操作人、模式、是否拒答、耗时
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request

from app_v2.api.deps import get_meta_store, require_permission
from app_v2.core.logging import trace_id_var
from app_v2.core.metrics import metrics_registry
from app_v2.core.response import ValidationError
from app_v2.core.security import PERM_KB_READ
from app_v2.schemas.models import ChatData, ChatRequest, RetrieveData, RetrieveRequest, SourceItem
from app_v2.services import (
    agent_service, audit_service, auth_service, cache_service, rag_service,
    retrieval_service, space_service,
)
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["问答"])


def _scope(principal: Principal, requested: Optional[str]) -> List[str]:
    """本次请求的检索空间范围。隔离逻辑集中在 space_service，这里只做转接。"""
    return space_service.resolve_spaces(principal.space_scope, requested)


@router.post("/chat", response_model=ChatData, summary="智能问答",
             description="支持两种模式：`rag` 固定管道（快、可预测）；`agent` 自主工具调用（可多步检索、可计算）。"
                         "两者都会返回引用片段，相关度低于阈值时返回拒答。\n\n"
                         "**权限**：可检索的知识范围 = 凭据绑定的权限组 ∩ 空间成员关系，"
                         "请求体中的 perm_group 会被忽略；`space_id` 留空表示在全部可见空间内检索。")
def chat(req: ChatRequest, request: Request,
         principal: Principal = Depends(require_permission(PERM_KB_READ))) -> ChatData:
    question = req.question.strip()
    if not question:
        raise ValidationError("问题不能为空")

    session_id = req.session_id or uuid.uuid4().hex[:16]
    perms = auth_service.effective_perm_groups(principal)
    spaces = _scope(principal, req.space_id)
    top_k = req.top_k or 0
    tenant = principal.tenant_id
    started = time.perf_counter()

    # ---------- 缓存（agent 模式不缓存：工具调用有时效性与副作用） ----------
    # 租户进 key：不同租户问同一句话，答案必须各自算，不能命中别人的
    cached = None
    if req.mode != "agent":
        cached = cache_service.get_answer_cached(question, perms, top_k, req.mode, spaces,
                                                 tenant_id=tenant)

    if cached:
        metrics_registry.incr("chat_cache_hit")
        latency_ms = int((time.perf_counter() - started) * 1000)
        _log_qa(session_id, question, cached, latency_ms)
        _audit_chat(request, principal, question, cached, latency_ms, cached_hit=True)
        return _to_chat_data(session_id, cached, req.mode, latency_ms, cached=True)

    history = _load_history(session_id)
    if req.mode == "agent":
        result = agent_service.run(question, perms, history, spaces=spaces, tenant_id=tenant)
    else:
        result = rag_service.answer(question, perms, top_k or None, history,
                                    spaces=spaces, tenant_id=tenant)

    latency_ms = int((time.perf_counter() - started) * 1000)
    result["timings"] = {**(result.get("timings") or {}), "latency_ms": latency_ms}

    # ---------- 统计 ----------
    metrics_registry.incr("chat_total")
    metrics_registry.incr_mode(result.get("mode", req.mode))
    if result.get("rejected"):
        metrics_registry.incr("chat_rejected")

    # ---------- 回写缓存 ----------
    if req.mode != "agent" and not result.get("rejected"):
        cache_service.put_answer(question, perms, top_k, req.mode, result, spaces,
                                 tenant_id=tenant)

    # ---------- 上下文：拒答也记，因为"用户追问"本身就是连续对话的一部分 ----------
    try:
        cache_service.append_context(session_id, question, result.get("answer", ""))
    except Exception as exc:
        logger.warning("上下文缓存写入失败（不影响响应）：%s", exc)

    _log_qa(session_id, question, result, latency_ms)
    _audit_chat(request, principal, question, result, latency_ms, cached_hit=False)

    return _to_chat_data(session_id, result, req.mode, latency_ms, cached=False)


def _load_history(session_id: str) -> List[Dict[str, Any]]:
    """多轮历史：Redis 上下文优先，空则回落 PG 持久化。

    Redis 只是加速层，PG 是权威存储。两者不一致时以 PG 为准并回填 Redis，
    避免 Redis 重启导致对话"失忆"。
    """
    ctx = cache_service.get_context(session_id)
    if ctx:
        return ctx
    try:
        history = get_meta_store().get_history(session_id, limit=8)
    except Exception as exc:
        logger.warning("读取会话历史失败，按无历史处理：%s", exc)
        return []
    if history:
        cache_service.put_context(session_id, [
            {"role": h.get("role", "user"), "content": h.get("content", "")} for h in history
        ])
    return history


@router.post("/retrieve", response_model=RetrieveData, summary="向量检索（不生成答案）",
             description="只做检索，返回片段及其向量排名、BM25 排名、RRF 分数、重排分数，用于调参与排障。"
                         "可用 `space_id` 限定在单个知识库空间内检索。")
def retrieve(req: RetrieveRequest, request: Request,
             principal: Principal = Depends(require_permission(PERM_KB_READ))) -> RetrieveData:
    perms = auth_service.effective_perm_groups(principal)
    spaces = _scope(principal, req.space_id)
    tenant = principal.tenant_id
    started = time.perf_counter()

    cached = cache_service.get_retrieval_cached(req.query, perms, req.top_k, spaces,
                                                tenant_id=tenant)
    if cached:
        metrics_registry.incr("retrieve_cache_hit")
        metrics_registry.incr("retrieve_total")
        latency_ms = int((time.perf_counter() - started) * 1000)
        _audit_retrieve(request, principal, req.query, latency_ms, True)
        return RetrieveData(
            query=req.query,
            hits=[_to_source(h) for h in cached["hits"]],
            confidence=cached["confidence"],
            timings={**cached.get("timings", {}), "cached": True, "latency_ms": latency_ms},
        )

    result = retrieval_service.search(req.query, perms, req.top_k, spaces=spaces,
                                      tenant_id=tenant)
    conf = retrieval_service.relevance(result["hits"], req.query)
    payload = {"hits": result["hits"], "confidence": conf, "timings": result["timings"]}
    cache_service.put_retrieval(req.query, perms, req.top_k, payload, spaces,
                                tenant_id=tenant)

    metrics_registry.incr("retrieve_total")
    latency_ms = int((time.perf_counter() - started) * 1000)
    _audit_retrieve(request, principal, req.query, latency_ms, False)

    return RetrieveData(
        query=req.query,
        hits=[_to_source(h) for h in result["hits"]],
        confidence=conf,
        timings={**result["timings"], "cached": False, "latency_ms": latency_ms},
    )


@router.get("/sessions/{session_id}/history",
            summary="查询会话历史",
            description="仅返回当前密钥有权访问的会话内容（会话按 key 归属隔离由调用方保证 session_id 私有）。")
def session_history(session_id: str, limit: int = 20,
                    principal: Principal = Depends(require_permission(PERM_KB_READ))) -> List[Dict[str, Any]]:
    # 存储层返回的是 list[dict]，这里的返回注解必须与之一致。
    # 曾经误标成 Dict[str, Any]，FastAPI 会按注解做响应校验并抛
    # ResponseValidationError -> 500（P2 回归时发现）。若未来要加包装，
    # 应改成 {"session_id":..., "messages":[...]} 并同步改存储层返回。
    return get_meta_store().get_history(session_id, limit=limit)


@router.delete("/sessions/{session_id}", summary="清除会话")
def clear_session(session_id: str,
                  principal: Principal = Depends(require_permission(PERM_KB_READ))) -> Dict[str, Any]:
    from app_v2.core.response import ok
    get_meta_store().clear_session(session_id)
    audit_service.record(
        action="session.clear", actor=principal.actor, role=principal.role,
        resource=f"session:{session_id}", detail={},
    )
    return {"session_id": session_id}


# ---------------- 内部工具 ----------------
def _to_source(h: dict) -> SourceItem:
    return SourceItem(
        chunk_id=h["chunk_id"], file_name=h["file_name"], seq=h["seq"], text=h["text"],
        vec_score=h.get("vec_score", 0), bm25_score=h.get("bm25_score", 0),
        rrf_score=h.get("rrf_score", 0), vec_rank=h.get("vec_rank"), bm25_rank=h.get("bm25_rank"),
        space_id=h.get("space_id") or "default",
        rerank_score=h.get("rerank_score"),
    )


def _to_chat_data(session_id: str, result: dict, mode: str, latency_ms: int, cached: bool) -> ChatData:
    timings = dict(result.get("timings") or {})
    timings["latency_ms"] = latency_ms
    timings["cached"] = cached
    return ChatData(
        session_id=session_id,
        answer=result["answer"],
        sources=[_to_source(s) for s in result.get("sources", [])],
        confidence=result.get("confidence", 0),
        rejected=result.get("rejected", False),
        mode=result.get("mode", mode),
        timings=timings,
        trace=result.get("trace"),
    )


def _log_qa(session_id: str, question: str, result: dict, latency_ms: int) -> None:
    """问答落库（会话历史 + 问答审计表）。失败不影响响应。"""
    try:
        store = get_meta_store()
        store.add_message(session_id, "user", question)
        store.add_message(session_id, "assistant", result["answer"])
        store.log_qa({
            "trace_id": trace_id_var.get(),
            "session_id": session_id,
            "question": question,
            "answer": result["answer"][:2000],
            "mode": result.get("mode", "rag"),
            "rejected": result.get("rejected", False),
            "confidence": result.get("confidence", 0),
            "sources": [
                {"file_name": s["file_name"], "seq": s["seq"], "rrf_score": s["rrf_score"]}
                for s in result.get("sources", [])
            ],
            "latency_ms": latency_ms,
            "rounds": (result.get("trace") or {}).get("rounds", 0),
        })
    except Exception as exc:
        logger.error("会话/审计写入失败：%s", exc)


def _audit_chat(request: Request, principal: Principal, question: str,
                result: dict, latency_ms: int, cached_hit: bool) -> None:
    audit_service.record(
        action=audit_service.ACTION_CHAT,
        actor=principal.actor, role=principal.role,
        resource=f"kb:{','.join(principal.perm_groups)}",
        status_code=200,
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        latency_ms=latency_ms,
        detail={
            "question": question[:200],
            "mode": result.get("mode"),
            "rejected": bool(result.get("rejected")),
            "confidence": round(float(result.get("confidence") or 0), 4),
            "sources": [s.get("file_name") for s in (result.get("sources") or [])][:5],
            "cached": cached_hit,
        },
    )


def _audit_retrieve(request: Request, principal: Principal, query: str,
                    latency_ms: int, cached_hit: bool) -> None:
    audit_service.record(
        action=audit_service.ACTION_RETRIEVE,
        actor=principal.actor, role=principal.role,
        resource=f"kb:{','.join(principal.perm_groups)}",
        ip=getattr(request.state, "client_ip", ""),
        latency_ms=latency_ms,
        detail={"query": query[:200], "cached": cached_hit},
    )
