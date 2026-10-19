"""P4 异步任务队列验收。

不依赖 Redis/worker：celery_eager=true 时任务在进程内同步执行，
状态流转与异步路径完全一致，因此可以验证「提交 → 状态流转 → 结果落库」全链路。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")
os.environ["CELERY_EAGER"] = "true"
os.environ["MINIO_ENABLED"] = "false"

from app_v2.core.settings import settings  # noqa: E402
from app_v2.services import task_service  # noqa: E402
from app_v2.services.task_service import TASK_INGEST, TASK_REINDEX, TASK_WEBHOOK  # noqa: E402
from app_v2.storage.meta_store import meta_store  # noqa: E402
from app_v2.storage.vector_store import vector_store  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    if hasattr(vector_store, "ids"):
        import numpy as np
        vector_store.ids, vector_store.payloads = [], []
        vector_store.vectors = np.zeros((0, 0), dtype="float32")

    section("0. 环境")
    check("celery_eager 已生效", settings.celery_eager, str(settings.celery_eager))
    check("broker 与 result 分库",
          settings.effective_broker_url.endswith("/0")
          and settings.effective_result_url.endswith("/1"),
          f"{settings.effective_broker_url} | {settings.effective_result_url}")

    section("1. 入队 → 执行 → 结果落库（以 webhook 任务为例，不依赖文件）")
    svc = task_service.task_service
    rec = svc.submit(TASK_WEBHOOK, "acme",
                     payload={"url": "http://127.0.0.1:1/nonexistent", "body": {"msg": "hi"}},
                     actor="tester")
    check("任务已创建", bool(rec and rec.get("task_id")), str(rec.get("task_id")))
    tid = rec["task_id"]
    final = task_service.wait_for(tid, "acme", timeout=10)
    # 目标端口不通，任务应当落到 failed 而不是卡在 running
    check("不可达的 webhook 任务最终状态为 failed",
          final.get("status") == "failed", str(final.get("status")))
    check("失败原因已记录", bool(final.get("error")), str(final.get("error"))[:60])

    section("2. 任务与租户隔离")
    rec_b = svc.submit(TASK_WEBHOOK, "globex",
                       payload={"url": "http://127.0.0.1:1/x", "body": {}}, actor="tester")
    check("globex 取不到 acme 的任务", svc.get(tid, "globex") is None)
    check("acme 取不到 globex 的任务", svc.get(rec_b["task_id"], "acme") is None)
    lst_acme = {t["task_id"] for t in svc.list("acme")}
    lst_gx = {t["task_id"] for t in svc.list("globex")}
    check("任务列表按租户切分",
          tid in lst_acme and rec_b["task_id"] not in lst_acme
          and rec_b["task_id"] in lst_gx and tid not in lst_gx,
          f"acme={len(lst_acme)} globex={len(lst_gx)}")

    st_a = svc.stats("acme")
    check("统计按租户计算", st_a.get("total") == 1, str(st_a))

    section("3. 未知任务类型被拒绝")
    try:
        svc.submit("no_such_type", "acme", payload={})
        check("未知类型应被拒绝", False)
    except ValueError as exc:
        check("未知类型被拒绝", True, str(exc))

    section("4. 取消语义")
    # 直接构造一个 pending 记录来验证取消；已结束的任务不允许取消
    meta_store.save_task({"task_id": "manual-pending", "tenant_id": "acme",
                          "task_type": TASK_WEBHOOK, "status": "pending",
                          "payload": {}, "actor": "t"})
    try:
        out = svc.cancel("manual-pending", "acme")
        check("pending 任务可取消", out.get("status") == "cancelled", str(out.get("status")))
    except Exception as exc:
        check("pending 任务可取消", False, str(exc))

    try:
        svc.cancel(tid, "acme")
        check("已结束任务不允许取消", False)
    except ValueError as exc:
        check("已结束任务不允许取消", True, str(exc)[:50])

    section("5. 清理已结束任务")
    n = svc.purge_finished("acme", older_than_days=0)
    check("清理接口可执行（按租户）", n >= 1, f"removed={n}")
    check("清理不影响其他租户",
          svc.get(rec_b["task_id"], "globex") is not None)

    section("6. 真实入库任务（用本地文件还原 object_key 缺失路径）")
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp())
    f = tmpdir / "制度说明.md"
    f.write_text("请假制度：员工请假需提前一天在系统提交申请，由直属主管审批。", encoding="utf-8")
    rec_i = svc.submit(TASK_INGEST, "acme",
                       payload={"file_name": "制度说明.md", "local_path": str(f),
                                "perm_group": "all", "space_id": "default"},
                       actor="tester")
    fin = task_service.wait_for(rec_i["task_id"], "acme", timeout=30)
    check("入库任务执行成功", fin.get("status") == "success",
          f"{fin.get('status')} {str(fin.get('error'))[:60]}")
    res = fin.get("result") or {}
    check("任务结果含 chunk_count", int(res.get("chunk_count") or 0) > 0,
          f"chunks={res.get('chunk_count')}")
    doc = meta_store.get_document(res.get("doc_id", ""), "acme")
    check("文档已落库且归属 acme", doc is not None)
    check("文档不会被写到默认租户",
          meta_store.get_document(res.get("doc_id", ""), "default") is None)

    print(f"\n{'=' * 52}")
    print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
    for x in FAIL:
        print(f"  FAILED: {x}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
