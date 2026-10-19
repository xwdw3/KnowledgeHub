"""Celery 应用与任务定义。

为什么要异步队列：
  上传一份 PDF 的完整链路（解析 → 切片 → 向量化 → 写库）在 20MB 文档上
  可能跑几十秒。同步接口会让请求超时、前端卡死、worker 被长请求占满。
  异步化之后，上传接口只负责"把原件存好 + 建任务"，剩下的交给 worker。

三条硬性约束（不遵守就会出现"任务成功但数据错"）：
  1. **租户必须显式随任务传**。worker 是独立进程，没有 HTTP 请求上下文，
     ContextVar 里什么都没有。若任务里用 current_tenant() 取租户，会拿到默认租户，
     等于把 A 公司的文档写进 default 租户。
  2. **acks_late + 幂等**。worker 可能在任务执行中途被杀，acks_late 会让任务重投；
     因此每个任务都必须能安全重跑（覆盖同 doc_id，不产生重复）。
  3. **broker 与 result 分库**。混在一个 Redis db 里，FLUSHDB 或者误清理
     会把"待执行的任务"和"已完成的结果"一起干掉。
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from app_v2.core.settings import settings

logger = logging.getLogger(__name__)

try:
    from celery import Celery
    _CELERY_AVAILABLE = True
except Exception:                                    # pragma: no cover
    Celery = None                                    # type: ignore
    _CELERY_AVAILABLE = False
    logger.warning("celery 未安装，异步任务将退化为同步执行")


def _build_app():
    if not _CELERY_AVAILABLE:
        return None
    app = Celery(
        "rag_kb",
        broker=settings.effective_broker_url,
        backend=settings.effective_result_url,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=True,
        # 迟到确认：worker 崩溃时任务会被重投，而不是随着进程一起消失。
        # 代价是任务可能被执行两次，所以每个任务都必须幂等。
        task_acks_late=True,
        # 一次只预取一个任务：文档解析是重任务，预取多了会让某个 worker
        # 囤积一堆活而其他 worker 空转。
        worker_prefetch_multiplier=1,
        # 硬超时：卡死的任务必须被杀掉，否则会永久占住一个 worker 进程
        task_time_limit=settings.celery_task_time_soft_limit * 2,
        task_soft_time_limit=settings.celery_task_time_soft_limit,
        result_expires=24 * 3600,
        # 默认队列。**刻意不配 task_routes**：
        # task_routes 是"按任务名静态指定队列"，一旦写成
        # {"app_v2.worker.tasks.*": {"queue": "rag"}}，就会**覆盖**
        # send_task(..., queue=...) 显式传入的队列名——
        # 结果是大文档分流完全失效，所有任务仍然挤在 rag 里，
        # 而且没有任何报错，只是"分流看起来没生效"。
        # 队列选择交给 task_service._pick_queue 按任务体积动态决定。
        task_default_queue="rag",
    )
    return app


celery_app = _build_app()


# ======================================================================
# 任务实现
# ======================================================================
def _mark(task_id: str, **fields) -> None:
    """更新任务状态。失败不影响主流程——状态只是可观测性，不是业务数据。"""
    try:
        from app_v2.storage.meta_store import meta_store

        meta_store.update_task(task_id, **fields)
    except Exception as exc:
        logger.warning("任务状态更新失败 task=%s：%s", task_id, exc)


def _run_ingest(task_id: str, tenant_id: str, payload: dict) -> dict:
    """文档入库任务的实际逻辑。

    幂等性来自 ingest_service 本身：同名文件重复上传会先清旧再写新，
    所以这个函数被重投多次的结果与执行一次相同。

    **大文件走流式路径**：判据是"原件在磁盘/对象存储上且超过阈值"。
    小于阈值仍走全量路径——全量路径更简单、更快（少了一层生成器开销），
    没必要为了统一而牺牲小文件的吞吐。
    """
    from app_v2.services import ingest_service, retrieval_service
    from app_v2.services.tenant_service import reset_current_tenant, set_current_tenant

    # 在 worker 里也要绑租户：ingest_service 内部会读上下文做兜底，
    # 不绑就可能落到默认租户。token 用完即复位，避免影响同一 worker 的下一个任务。
    token = set_current_tenant(tenant_id)
    started = time.perf_counter()
    try:
        _mark(task_id, status="running", progress=10, started_at=True,
              tenant_id=tenant_id)

        object_key = payload.get("object_key") or ""
        file_name = payload["file_name"]
        perm_group = payload.get("perm_group") or "all"
        space_id = payload.get("space_id") or settings.default_space_id
        uploader = payload.get("uploader") or "system"
        # ⚠ 这里曾经只读 "file_size"，而上传接口写入的键叫 "size"——
        # 结果 file_size 恒为 0，`use_stream` 判断恒为 False，
        # **GB 级文件也走全量 read_bytes()**，流式链路形同未接。
        # 症状极具迷惑性：任务成功、日志正常，只是 worker 内存随文件大小线性上涨。
        # 现在两个键都认，并且在拿到真实大小时以落盘文件为准（见下方 stat）。
        file_size = int(payload.get("file_size") or payload.get("size") or 0)

        # ---------- 取出原件到本地临时文件 ----------
        # 为什么非要落临时文件：解析器（pypdf / python-docx）都要求文件路径，
        # 而流式读取需要可重复打开的 seekable 源。
        # 关键点是**流式下载**：object_storage.get() 会把整个对象读进内存，
        # 1GB 对象直接吃掉 1GB+ 内存，流式解析就白做了。
        src_path: Path
        tmp_holder = None
        if object_key:
            from app_v2.storage.object_store import object_storage

            tmp_holder = Path(tempfile.gettempdir()) / f"kb_task_{task_id}{Path(file_name).suffix}"
            if not object_storage.download_to_file(object_key, str(tmp_holder)):
                raise RuntimeError(f"原件读取失败：{object_key}")
            src_path = tmp_holder
            file_size = file_size or tmp_holder.stat().st_size
        else:
            src_path = Path(payload["local_path"])
            file_size = file_size or src_path.stat().st_size

        _mark(task_id, progress=20)

        # ---------- 选择入库路径 ----------
        stream_threshold = settings.stream_ingest_threshold_mb * 1024 * 1024
        use_stream = file_size >= stream_threshold

        def _on_progress(read_bytes: int, total_bytes: int, chunks: int) -> None:
            # 进度映射到 20→90 区间：剩下 10% 留给元数据写入与缓存失效，
            # 让进度条不会"到 100% 却还没好"。
            ratio = (read_bytes / total_bytes) if total_bytes else 0
            progress = int(20 + max(0.0, min(ratio, 1.0)) * 70)
            _mark(task_id, progress=progress, chunk_count=chunks)

        if use_stream:
            logger.info("任务 %s 走流式入库 file=%s size=%.1fMB",
                        task_id, file_name, file_size / 1024 / 1024)
            result = ingest_service.ingest_stream(
                file_name=file_name,
                source_path=src_path,
                perm_group=perm_group,
                uploader=uploader,
                space_id=space_id,
                tenant_id=tenant_id,
                on_progress=_on_progress,
            )
        else:
            content = src_path.read_bytes()
            _mark(task_id, progress=30)
            result = ingest_service.ingest_bytes(
                file_name=file_name,
                content=content,
                perm_group=perm_group,
                uploader=uploader,
                space_id=space_id,
                tenant_id=tenant_id,
            )
        _mark(task_id, progress=92)

        try:
            from app_v2.services import cache_service

            cache_service.invalidate_knowledge()
        except Exception:
            pass
        retrieval_service.invalidate()

        elapsed = int((time.perf_counter() - started) * 1000)
        result["elapsed_ms"] = elapsed
        result["streamed"] = bool(result.get("streamed"))
        _mark(task_id, status="success", progress=100, result=result,
              finished_at=True, elapsed_ms=elapsed)
        logger.info("入库任务完成 task=%s tenant=%s doc=%s %dms stream=%s",
                    task_id, tenant_id, result.get("doc_id"), elapsed,
                    result.get("streamed"))
        return result
    except Exception as exc:
        logger.exception("入库任务失败 task=%s", task_id)
        _mark(task_id, status="failed", error=str(exc), finished_at=True)
        # 重抛让 Celery 记录失败并按策略重试；不重抛则任务显示成功但实际没入库
        raise
    finally:
        reset_current_tenant(token)
        # 临时文件必须删：GB 级原件留在 /tmp 里会把磁盘吃满，
        # 而且容器重启后残留会累积。放在 finally 保证异常路径也清理。
        if tmp_holder is not None:
            try:
                tmp_holder.unlink(missing_ok=True)
            except Exception as exc:            # pragma: no cover
                logger.warning("临时文件清理失败 %s：%s", tmp_holder, exc)


def _run_reindex(task_id: str, tenant_id: str, payload: dict) -> dict:
    """重建索引：把目录下所有文档重新走一遍入库。"""
    from app_v2.services import ingest_service
    from app_v2.services.tenant_service import reset_current_tenant, set_current_tenant

    token = set_current_tenant(tenant_id)
    try:
        _mark(task_id, status="running", progress=5, started_at=True, tenant_id=tenant_id)
        result = ingest_service.rebuild_from_dir(
            payload["dir_path"],
            perm_group=payload.get("perm_group") or "all",
            tenant_id=tenant_id,
        )
        ok = len(result.get("success") or [])
        bad = len(result.get("failed") or [])
        _mark(task_id, status="success", progress=100, result=result,
              finished_at=True)
        logger.info("重建索引完成 task=%s 成功=%d 失败=%d", task_id, ok, bad)
        return result
    except Exception as exc:
        logger.exception("重建索引失败 task=%s", task_id)
        _mark(task_id, status="failed", error=str(exc), finished_at=True)
        raise
    finally:
        reset_current_tenant(token)


def _run_webhook(task_id: str, tenant_id: str, payload: dict) -> dict:
    """告警投递（HTTP webhook）。

    放在队列里做而不是告警触发时同步发：对方接口慢或挂掉时，
    同步调用会把告警评估线程一起拖死，导致后续告警全部漏报。
    """
    import json
    import urllib.request

    url = payload.get("url") or ""
    body = json.dumps(payload.get("body") or {}, ensure_ascii=False).encode()
    if not url:
        _mark(task_id, status="failed", error="webhook url 为空", finished_at=True)
        return {"sent": False}

    started = time.perf_counter()
    try:
        _mark(task_id, status="running", progress=50, started_at=True, tenant_id=tenant_id)
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
        elapsed = int((time.perf_counter() - started) * 1000)
        result = {"sent": True, "status_code": code, "elapsed_ms": elapsed}
        _mark(task_id, status="success", progress=100, result=result, finished_at=True)
        return result
    except Exception as exc:
        logger.warning("webhook 投递失败 task=%s url=%s：%s", task_id, url, exc)
        _mark(task_id, status="failed", error=str(exc), finished_at=True)
        raise


_TASK_IMPLS = {
    "ingest": _run_ingest,
    "reindex": _run_reindex,
    "webhook": _run_webhook,
}


def run_task(task_id: str, task_type: str, tenant_id: str, payload: dict) -> dict:
    """任务入口。Celery 已注册的任务和同步降级路径都走这里，保证行为一致。"""
    impl = _TASK_IMPLS.get(task_type)
    if impl is None:
        _mark(task_id, status="failed", error=f"未知任务类型：{task_type}", finished_at=True)
        raise ValueError(f"未知任务类型：{task_type}")
    return impl(task_id, tenant_id, payload)


if celery_app is not None:

    @celery_app.task(bind=True, name="app_v2.worker.tasks.run", max_retries=3,
                     default_retry_delay=10)
    def celery_run(self, task_id: str, task_type: str, tenant_id: str, payload: dict):
        try:
            return run_task(task_id, task_type, tenant_id, payload)
        except Exception as exc:
            # 重试次数用尽后不再重抛，避免 Celery 把任务塞进死信
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            _mark(task_id, status="failed",
                  error=f"{exc}（已重试 {self.max_retries} 次）", finished_at=True)
            raise
