"""审计服务：记录「谁、在什么时候、对什么资源、做了什么、结果如何」。

企业合规要求审计日志满足三点：
  1. **完整**：所有写操作与敏感读操作都要记，包括被拒绝的越权尝试
  2. **可关联**：能通过 trace_id 关联到具体请求的完整链路日志
  3. **不可轻易篡改**：只追加、不提供修改/删除接口

写入采用异步队列 + 后台线程落库，避免审计成为请求链路的性能瓶颈；
队列满时降级为同步写并打告警，保证不丢审计（宁可慢，不可丢）。
"""
from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from app_v2.core.logging import trace_id_var
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

# 审计动作常量，避免各处拼字符串不一致
ACTION_CHAT = "chat.query"
ACTION_RETRIEVE = "kb.retrieve"
ACTION_DOC_UPLOAD = "doc.upload"
ACTION_DOC_DELETE = "doc.delete"
ACTION_DOC_REBUILD = "doc.rebuild"
ACTION_DOC_PURGE = "doc.purge"
ACTION_KEY_CREATE = "key.create"
ACTION_KEY_REVOKE = "key.revoke"
ACTION_KEY_LIST = "key.list"
ACTION_AUDIT_READ = "audit.read"
ACTION_AUTH_LOGIN = "auth.verify"
ACTION_METRIC_READ = "metric.read"

# P3：账号与空间管理的审计动作。账号类与空间类是高危操作，
# 必须能回答「谁在什么时候把谁加进了哪个空间/改成了什么角色」。
ACTION_USER_CREATE = "user.create"
ACTION_USER_UPDATE = "user.update"
ACTION_USER_DISABLE = "user.disable"
ACTION_USER_LIST = "user.list"
ACTION_SPACE_CREATE = "space.create"
ACTION_SPACE_UPDATE = "space.update"
ACTION_SPACE_DELETE = "space.delete"
ACTION_SPACE_LIST = "space.list"

RESULT_SUCCESS = "success"
RESULT_DENIED = "denied"
RESULT_FAILED = "failed"

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
_worker: Optional[threading.Thread] = None
_stop = threading.Event()
_stats_lock = threading.Lock()
_stats: Dict[str, int] = {"enqueued": 0, "written": 0, "dropped": 0, "sync_fallback": 0}


def _worker_loop() -> None:
    while not _stop.is_set():
        try:
            record = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            meta_store.log_audit(record)
            with _stats_lock:
                _stats["written"] += 1
        except Exception as exc:
            logger.error("审计落库失败：%s record=%s", exc, record.get("action"))
        finally:
            _queue.task_done()


def start() -> None:
    """启动后台落库线程（应用启动时调用一次）。"""
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, name="audit-writer", daemon=True)
    _worker.start()
    logger.info("审计后台写入线程已启动")


def stop(timeout: float = 5.0) -> None:
    """停止并尽量把队列中剩余记录写完。"""
    _stop.set()
    if _worker:
        _worker.join(timeout=timeout)
    remaining = _queue.qsize()
    if remaining:
        logger.warning("审计队列仍有 %d 条未写入", remaining)


atexit.register(stop)


def record(
    action: str,
    actor: str = "anonymous",
    role: str = "-",
    resource: str = "",
    result: str = RESULT_SUCCESS,
    status_code: int = 200,
    ip: str = "",
    user_agent: str = "",
    detail: Optional[Dict[str, Any]] = None,
    latency_ms: int = 0,
    trace_id: Optional[str] = None,
    tenant_id: str = "",
) -> None:
    """写入一条审计记录（非阻塞）。"""
    # 租户从请求级上下文自动取，而不是让 60 多个调用点各传一遍。
    # 理由：审计是"事后追责"的凭据，漏传一个点就等于那条记录永远无法归属到租户，
    # 而这种遗漏不会有任何报错——只有某天要在审计页查"这家公司谁删了文档"时才发现。
    # 上下文由 deps.bind_tenant 在认证后写入，后台任务（无请求）会落到默认租户。
    if not tenant_id:
        try:
            from app_v2.services.tenant_service import current_tenant

            tenant_id = current_tenant()
        except Exception:
            tenant_id = ""

    rec = {
        "trace_id": trace_id or trace_id_var.get() or "-",
        "actor": actor or "anonymous",
        "role": role or "-",
        "action": action,
        "resource": resource,
        "result": result,
        "status_code": int(status_code),
        "ip": ip,
        "user_agent": user_agent,
        "detail": detail or {},
        "latency_ms": int(latency_ms),
        "tenant_id": tenant_id or "default",
    }
    try:
        _queue.put_nowait(rec)
        with _stats_lock:
            _stats["enqueued"] += 1
    except queue.Full:
        # 队列满：同步写，宁可拖慢也不丢审计
        try:
            meta_store.log_audit(rec)
            with _stats_lock:
                _stats["sync_fallback"] += 1
            logger.warning("审计队列已满，已同步写入 action=%s", action)
        except Exception as exc:
            with _stats_lock:
                _stats["dropped"] += 1
            logger.error("审计写入彻底失败（已丢弃）action=%s err=%s", action, exc)


def flush(timeout: float = 5.0) -> bool:
    """等待队列清空，测试与优雅停机用。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.empty():
            return True
        time.sleep(0.02)
    return _queue.empty()


def writer_stats() -> dict:
    with _stats_lock:
        data = dict(_stats)
    data["queued"] = _queue.qsize()
    return data


def query(actor: str = "", action: str = "", result: str = "", limit: int = 100) -> List[dict]:
    return meta_store.query_audit(actor=actor, action=action, result=result, limit=limit)


def summary() -> dict:
    try:
        stats = meta_store.audit_stats()
    except Exception as exc:
        logger.warning("审计统计读取失败：%s", exc)
        stats = {}
    return {"window": "24h", **stats, "writer": writer_stats()}
