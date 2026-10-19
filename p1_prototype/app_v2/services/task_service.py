"""异步任务调度：统一的提交入口与状态查询。

上层（路由、告警器）只跟这个模块打交道，不直接碰 Celery。
好处是 Celery 不可用时能优雅降级为同步执行，而不是让接口 500。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from app_v2.core.settings import settings
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

TASK_INGEST = "ingest"
TASK_REINDEX = "reindex"
TASK_WEBHOOK = "webhook"

VALID_TYPES = (TASK_INGEST, TASK_REINDEX, TASK_WEBHOOK)

# 大文档专用队列。两个队列都有 worker 监听，但优先级不同：
# worker 启动参数是 `--queues=rag,rag_large`，Celery 会**先取空 rag 再取 rag_large**。
# 于是大文档任务只在没有小任务排队时才被处理，
# 一个 2GB 的入库不会把后面 50 个小文档一起堵住。
QUEUE_DEFAULT = "rag"
QUEUE_LARGE = "rag_large"


def _pick_queue(task_type: str, payload: dict) -> str:
    """按任务体积决定投到哪条队列。

    判据用 payload 里的 `size`（字节）：上传接口在落盘时就记录了它，
    不需要 worker 再 stat 一次文件。超过阈值的入库任务走大文档队列。

    为什么只对 ingest 分流：reindex 天然是"一次处理一批"的重任务，
    webhook 是轻量的 HTTP 调用，两者都不需要区分大小。
    """
    if task_type != TASK_INGEST:
        return QUEUE_DEFAULT
    try:
        size = int(payload.get("size") or payload.get("file_size") or 0)
    except (TypeError, ValueError):
        return QUEUE_DEFAULT
    threshold = settings.stream_ingest_threshold_mb * 1024 * 1024
    return QUEUE_LARGE if size >= threshold else QUEUE_DEFAULT


class TaskService:
    """任务提交与查询。所有写操作都以 tenant_id 为第一参数。"""

    def submit(self, task_type: str, tenant_id: str, payload: dict,
               actor: str = "system", doc_id: str = "") -> dict:
        """提交任务，返回任务记录。

        `celery_eager` 或 Celery 不可用时同步执行：本地开发没有 Redis/worker，
        如果强制走队列，上传接口会一直显示"排队中"而永远不完成。
        """
        if task_type not in VALID_TYPES:
            raise ValueError(f"未知任务类型：{task_type}")

        task_id = uuid.uuid4().hex[:16]
        rec = meta_store.save_task({
            "task_id": task_id,
            "tenant_id": tenant_id,
            "task_type": task_type,
            "status": "pending",
            "progress": 0,
            "payload": payload,
            "actor": actor,
            "doc_id": doc_id,
        })
        logger.info("任务已提交 task=%s type=%s tenant=%s", task_id, task_type, tenant_id)

        from app_v2.worker.tasks import celery_app, run_task

        eager = bool(settings.celery_eager) or celery_app is None
        if eager:
            # 同步执行：状态流转与异步路径完全一致，前端无需区分两种模式
            try:
                run_task(task_id, task_type, tenant_id, payload)
            except Exception as exc:
                logger.warning("同步任务失败 task=%s：%s", task_id, exc)
            return meta_store.get_task(task_id) or rec

        try:
            celery_app.send_task(
                "app_v2.worker.tasks.run",
                args=[task_id, task_type, tenant_id, payload],
                queue=_pick_queue(task_type, payload),
            )
        except Exception as exc:
            logger.error("任务入队失败，退化为同步执行 task=%s：%s", task_id, exc)
            try:
                run_task(task_id, task_type, tenant_id, payload)
            except Exception as exc2:
                logger.warning("同步兜底也失败 task=%s：%s", task_id, exc2)
        return meta_store.get_task(task_id) or rec

    def get(self, task_id: str, tenant_id: str) -> Optional[dict]:
        """按租户取任务。跨租户一律返回 None（表现为 404）。"""
        rec = meta_store.get_task(task_id)
        if not rec:
            return None
        if (rec.get("tenant_id") or settings.default_tenant_id) != tenant_id:
            return None
        return rec

    def list(self, tenant_id: str, status: str = "", task_type: str = "",
             limit: int = 50) -> list[dict]:
        return meta_store.list_tasks(tenant_id=tenant_id, status=status,
                                     task_type=task_type, limit=limit)

    def stats(self, tenant_id: str = "") -> dict:
        return meta_store.task_stats(tenant_id=tenant_id)

    def cancel(self, task_id: str, tenant_id: str) -> dict:
        """取消任务。

        只对 pending 生效：已 running 的任务无法安全中断（可能已经写了一部分向量），
        硬杀会留下"向量有、元数据没有"的脏数据。要停就等它跑完再删文档。
        """
        rec = self.get(task_id, tenant_id)
        if not rec:
            raise KeyError(task_id)
        if rec.get("status") != "pending":
            raise ValueError(f"当前状态 {rec.get('status')} 不支持取消")
        meta_store.update_task(task_id, status="cancelled", finished_at=True)
        return meta_store.get_task(task_id) or rec

    # ---------------- 清理 ----------------
    def purge_finished(self, tenant_id: str = "", older_than_days: int = 7) -> int:
        """清理已完成任务的记录。任务表是纯日志性质，不清会无限膨胀。"""
        try:
            return meta_store.purge_tasks(tenant_id=tenant_id,
                                          older_than_days=older_than_days)
        except Exception as exc:
            logger.warning("任务清理失败：%s", exc)
            return 0


task_service = TaskService()


def wait_for(task_id: str, tenant_id: str, timeout: float = 30.0,
             interval: float = 0.3) -> Optional[dict]:
    """阻塞等待任务结束。仅用于测试与同步降级场景，生产接口禁止调用。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = task_service.get(task_id, tenant_id)
        if rec and rec.get("status") in ("success", "failed", "cancelled"):
            return rec
        time.sleep(interval)
    return task_service.get(task_id, tenant_id)
