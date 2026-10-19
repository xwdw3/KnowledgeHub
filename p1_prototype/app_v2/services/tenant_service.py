"""多租户：租户注册、配额校验、上下文绑定。

**三层隔离的次序是有意义的**（顺序反了就会串户）：

```
租户 tenant_id   ← 最外层，跨公司边界，绝对不能穿透
  └─ 空间 space_id   ← 业务域（人事/研发/财务）
       └─ 密级 perm_group  ← 同一域内的敏感级别
```

为什么不合并成一层：
  - 空间和密级都在**租户内部**才有意义。A 公司的 `hr` 空间与 B 公司的 `hr` 空间
    是两份完全无关的数据，若不用 tenant_id 先切一刀，`space_id='hr'` 这个条件
    会把两家公司的数据同时捞出来。
  - 密级则更细：同一租户同一空间内，还要区分"全员可见"与"仅 HR 可见"。

隔离手段（三层，逐层兜底）：
  1. **应用层强制下推**：所有查询函数都要求显式传 tenant_id，参数不设默认值，
     漏传会直接报错而不是静默返回全量。这是主力手段。
  2. **缓存 key 维度**：租户 + 权限组都进 key，避免"管理员问过一次"被别的租户复用。
  3. **向量库 payload 过滤**：检索时 tenant_id 作为 must 条件下推到 Qdrant，
     而不是取回来再在内存里筛（后者在数据量大时会漏）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app_v2.core.response import AppError, ForbiddenError, NotFoundError
from app_v2.core.settings import settings

logger = logging.getLogger(__name__)

# 租户 ID 会被拼进对象存储的 key 前缀（`{tenant}/original/...`），
# 因此只允许安全字符。允许 `/`、`..`、空格会让 S3 key 出现路径穿越或歧义。
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

# 请求级上下文：中间件写入、业务代码读取。
# 用 ContextVar 而不是全局变量，避免多线程下 A 请求读到 B 请求的租户。
import contextvars

_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tenant", default=settings.default_tenant_id
)


def current_tenant() -> str:
    """当前请求的租户。无上下文时返回默认租户（后台任务/CLI 场景）。"""
    return _current_tenant.get()


def set_current_tenant(tenant_id: str) -> contextvars.Token:
    return _current_tenant.set(tenant_id or settings.default_tenant_id)


def reset_current_tenant(token: contextvars.Token) -> None:
    try:
        _current_tenant.reset(token)
    except ValueError:
        # 跨上下文 reset（如异步任务里）会抛错，忽略即可：新上下文本就有自己的值
        pass


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    description: str = ""
    max_users: int = 100
    max_docs: int = 10000
    max_qps: int = 0
    status: str = "active"

    @property
    def active(self) -> bool:
        return self.status == "active"


def normalize_tenant_id(raw: str) -> str:
    """校验并规范化租户 ID。不合法直接报 422，不做"猜测修复"。"""
    tid = (raw or "").strip().lower()
    if not tid:
        raise AppError("tenant_id 不能为空", code=422)
    if not _TENANT_ID_RE.match(tid):
        raise AppError(
            "tenant_id 只能包含小写字母、数字、下划线和连字符，长度 3-64，"
            "且不能以连字符/下划线开头结尾", code=422)
    return tid


class TenantService:
    """租户管理。数据落 meta_store，业务侧通过 require_active 校验可用性。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 进程内缓存：租户状态变化极少，但每次请求都查库代价不小
        self._cache: Dict[str, tuple[float, Tenant]] = {}
        self._cache_ttl = 30.0

    # ---------------- 读写 ----------------
    def get(self, tenant_id: str) -> Optional[Tenant]:
        tid = (tenant_id or "").strip()
        if not tid:
            return None
        now = time.time()
        hit = self._cache.get(tid)
        if hit and hit[0] > now:
            return hit[1]
        try:
            from app_v2.storage.meta_store import meta_store

            rec = meta_store.get_tenant(tid)
        except Exception as exc:                       # 库不可用时不阻断鉴权
            logger.warning("租户查询失败 tenant=%s：%s", tid, exc)
            return None
        if not rec:
            return None
        t = Tenant(
            tenant_id=rec["tenant_id"], name=rec.get("name", ""),
            description=rec.get("description", ""),
            max_users=int(rec.get("max_users") or 0),
            max_docs=int(rec.get("max_docs") or 0),
            max_qps=int(rec.get("max_qps") or 0),
            status=rec.get("status", "active"),
        )
        self._cache[tid] = (now + self._cache_ttl, t)
        return t

    def invalidate(self, tenant_id: str = "") -> None:
        with self._lock:
            if tenant_id:
                self._cache.pop(tenant_id, None)
            else:
                self._cache.clear()

    def list_all(self) -> List[dict]:
        from app_v2.storage.meta_store import meta_store

        return meta_store.list_tenants()

    def create(self, tenant_id: str, name: str, description: str = "",
               max_users: int = 100, max_docs: int = 10000, max_qps: int = 0) -> dict:
        from app_v2.storage.meta_store import meta_store

        tid = normalize_tenant_id(tenant_id)
        if not (name or "").strip():
            raise AppError("租户名称不能为空", code=422)
        if meta_store.get_tenant(tid):
            raise AppError(f"租户已存在：{tid}", code=409)
        rec = meta_store.save_tenant({
            "tenant_id": tid, "name": name.strip(), "description": description or "",
            "max_users": max_users, "max_docs": max_docs, "max_qps": max_qps,
            "status": "active",
        })
        self.invalidate(tid)
        logger.info("创建租户 tenant=%s name=%s", tid, name)
        return rec

    def update(self, tenant_id: str, fields: dict) -> dict:
        from app_v2.storage.meta_store import meta_store

        if not meta_store.get_tenant(tenant_id):
            raise NotFoundError(f"租户不存在：{tenant_id}")
        rec = meta_store.update_tenant(tenant_id, fields)
        self.invalidate(tenant_id)
        return rec

    # ---------------- 校验 ----------------
    def require_active(self, tenant_id: str) -> Tenant:
        """租户必须存在且处于 active。不存在按 404 处理而非 403——
        403 会暴露"这个租户存在但你无权"，等于给攻击者一个租户名探测器。"""
        t = self.get(tenant_id)
        if not t:
            raise NotFoundError(f"租户不存在：{tenant_id}")
        if not t.active:
            raise ForbiddenError(f"租户已停用：{tenant_id}")
        return t

    def check_user_quota(self, tenant_id: str) -> None:
        """建用户前校验配额。配额是共享实例上最现实的互相影响来源。"""
        t = self.require_active(tenant_id)
        if t.max_users <= 0:
            return
        from app_v2.storage.meta_store import meta_store

        used = meta_store.count_users(tenant_id)
        if used >= t.max_users:
            raise AppError(
                f"租户 {tenant_id} 用户数已达上限（{used}/{t.max_users}）", code=429)

    def check_doc_quota(self, tenant_id: str) -> None:
        t = self.require_active(tenant_id)
        if t.max_docs <= 0:
            return
        from app_v2.storage.meta_store import meta_store

        used = meta_store.count_documents(tenant_id)
        if used >= t.max_docs:
            raise AppError(
                f"租户 {tenant_id} 文档数已达上限（{used}/{t.max_docs}）", code=429)

    def stats(self, tenant_id: str) -> dict:
        """租户用量概览，供管理后台展示。"""
        from app_v2.storage.meta_store import meta_store

        t = self.get(tenant_id)
        return {
            "tenant_id": tenant_id,
            "name": t.name if t else "",
            "status": t.status if t else "missing",
            "users": meta_store.count_users(tenant_id),
            "max_users": t.max_users if t else 0,
            "documents": meta_store.count_documents(tenant_id),
            "max_docs": t.max_docs if t else 0,
            "spaces": meta_store.count_spaces(tenant_id),
        }

    def bootstrap_default(self) -> None:
        """确保默认租户存在。升级后第一次启动必走这里。"""
        from app_v2.storage.meta_store import meta_store

        tid = settings.default_tenant_id
        try:
            if not meta_store.get_tenant(tid):
                meta_store.save_tenant({
                    "tenant_id": tid,
                    "name": settings.default_tenant_name,
                    "description": "升级自 P3 的存量数据默认归属该租户",
                    "max_users": 0, "max_docs": 0, "max_qps": 0,
                    "status": "active",
                })
                logger.info("已创建默认租户 tenant=%s", tid)
        except Exception as exc:
            logger.error("默认租户初始化失败：%s", exc)


tenant_service = TenantService()
