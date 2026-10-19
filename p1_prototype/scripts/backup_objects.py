"""对象存储原件备份（CronJob / 运维手工运行共用）。

用法：
    python -m scripts.backup_objects --dest /backup/20260101
    python -m scripts.backup_objects --dest /backup/20260101 --tenant acme

设计要点（每一条都对应一个真实会踩的坑）：

1. **流式复制，不进内存**。GB 级原件走 `get()` 会把整个对象读进内存，
   备份 20 个 1GB 文件就会 OOM。这里用 `backup_prefix`，内部按 1MB 分块。

2. **校验而不只是复制**。复制完立刻比对"本地文件数 = 对象数"且大小一致。
   备份脚本最常见的失败是"看起来成功了"——循环里某个对象读取失败被
   `except` 吞掉，最后打印 "done"。所以任何不一致都必须非零退出。

3. **不覆盖已有备份**。`--dest` 由调用方带日期；同一天重跑会覆盖当天目录，
   这是刻意的（重跑通常是为了修复当天那次失败），但不会影响历史目录。

4. **租户边界**。不传 `--tenant` 时按"列出所有租户前缀"逐个备份，
   每个租户单独校验；传了则只备份该租户。
   任何情况下都不会出现"把 A 租户的对象写进 B 租户目录"。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许以 `python scripts/backup_objects.py` 方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_v2.core.logging import setup_logging          # noqa: E402
from app_v2.core.settings import settings              # noqa: E402
from app_v2.storage.meta_store import meta_store       # noqa: E402
from app_v2.storage.object_store import object_storage  # noqa: E402


def _tenant_prefixes() -> list[str]:
    """列出所有租户前缀。

    从元数据里取而不是扫桶：扫桶得到的是"对象存储里现在有啥"，
    元数据得到的是"按理该有啥"。备份要以元数据为准，
    才能发现"对象丢了"这种问题——若按桶里现有对象来备份，
    一个已经丢失的文件永远不会被察觉，直到某天要恢复时才发现备份里也没有。
    """
    tenants: list[str] = []
    try:
        rows = meta_store.list_tenants()
        # list_tenants 返回的是 dict 列表（SELECT * FROM tenants），
        # 不是字符串列表。直接当字符串用会把 dict 拼进前缀，得到 "{'tenant_id':...}/"。
        for row in rows or []:
            tid = (row.get("tenant_id") if isinstance(row, dict) else str(row)) or ""
            if tid:
                tenants.append(tid)
    except Exception as exc:
        print(f"[backup] 读取租户列表失败，退回默认租户：{exc}", flush=True)
    if not tenants:
        tenants = [settings.default_tenant_id]
    return tenants


def main() -> int:
    ap = argparse.ArgumentParser(description="备份对象存储中的文档原件")
    ap.add_argument("--dest", required=True, help="本地备份目录")
    ap.add_argument("--tenant", default="", help="只备份指定租户；留空则全部租户")
    args = ap.parse_args()

    setup_logging(settings.log_level, "text")

    if not settings.minio_enabled:
        print("[backup] 对象存储未启用（MINIO_ENABLED=false），无可备份内容", flush=True)
        # 这不是错误：本地开发环境本来就没有对象存储。
        # 返回 0 让 CronJob 不至于天天红，但日志里说清楚。
        return 0

    tenants = [args.tenant] if args.tenant else _tenant_prefixes()
    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    report: list[dict] = []
    failed = 0
    for tenant in tenants:
        prefix = f"{tenant}/"
        target = dest_root / tenant
        print(f"[backup] tenant={tenant} -> {target}", flush=True)
        try:
            res = object_storage.backup_prefix(prefix, str(target), tenant_id=tenant)
        except Exception as exc:
            print(f"[backup] 失败 tenant={tenant}: {exc}", flush=True)
            failed += 1
            continue

        # ---- 校验：本地文件数与对象数必须一致 ----
        local_files = [p for p in target.rglob("*") if p.is_file()] if target.exists() else []
        local_bytes = sum(p.stat().st_size for p in local_files)
        entry = {
            "tenant": tenant,
            "objects": res["objects"],
            "copied": res["files"],
            "bytes": res["bytes"],
            "local_files": len(local_files),
            "local_bytes": local_bytes,
            "skipped": res["skipped"],
        }
        report.append(entry)

        if res["skipped"]:
            print(f"[backup] ⚠ tenant={tenant} 有 {len(res['skipped'])} 个对象未备份成功", flush=True)
            failed += 1
        if len(local_files) != res["files"] or local_bytes != res["bytes"]:
            print(f"[backup] ✗ 校验失败 tenant={tenant}: "
                  f"本地 {len(local_files)} 个/{local_bytes}B，"
                  f"应为 {res['files']} 个/{res['bytes']}B", flush=True)
            failed += 1
        else:
            print(f"[backup] ✓ tenant={tenant} {res['files']} 个文件 "
                  f"{res['bytes'] / 1024 / 1024:.1f}MB 校验通过", flush=True)

    manifest = dest_root / "manifest.json"
    manifest.write_text(json.dumps(
        {"dest": str(dest_root), "tenants": report, "failed": failed},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backup] 清单已写入 {manifest}", flush=True)

    if failed:
        print(f"[backup] 完成但有 {failed} 项失败，退出码 1（让 CronJob 标红）", flush=True)
        return 1
    print("[backup] 全部租户备份完成并通过校验", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
