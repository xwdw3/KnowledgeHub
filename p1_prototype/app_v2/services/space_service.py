"""知识库空间：租户内的业务域隔离。

为什么需要"空间"而不是只有"权限组"：
  - **权限组**回答"谁能看"，是**人**的属性（hr 组的人能看 hr 文档）
  - **空间**回答"这份知识属于哪个业务域"，是**内容**的属性
  两者正交。一个员工既需要看「人事空间」的制度，也需要看「研发空间」的规范，
  但绝不该看到「财务空间」的报表——只用权限组表达会退化成笛卡尔积。

P4 起空间不再是"最外层"：租户 -> 空间 -> 权限组，空间退到第二层。
同一个 space_id（如 default）在每个租户下各存在一份，互不相通。

隔离落点（四层都要做，缺一层就等于没隔离）：
  1. **元数据层**：documents.tenant_id + documents.space_id 双下推
  2. **向量层**：Qdrant payload 带 tenant_id + space_id 并同时过滤
  3. **缓存层**：检索/问答 key 纳入 tenant + spaces，避免跨租户命中别人的缓存
  4. **对象存储**：key 前缀 `{tenant_id}/{space_id}/...`，桶级隔离之外再加路径隔离
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from app_v2.core.response import AppError, ForbiddenError
from app_v2.core.settings import settings
from app_v2.core.security import spaces_wildcard
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")


def _validate_space_id(space_id: str) -> str:
    """空间 ID 会进 SQL 参数、Qdrant payload、Redis key，必须限制字符集。

    直接放行任意字符串的风险：Redis key 里出现 `*` 会被 SCAN 匹配误伤，
    Qdrant 里出现特殊字符会让过滤条件解析异常。
    """
    space_id = (space_id or "").strip()
    if not _SPACE_ID_RE.match(space_id):
        raise AppError("space_id 需为 2~64 位字母/数字/下划线/短横线", code=422)
    return space_id


def public_space(rec: dict, doc_count: Optional[int] = None) -> dict:
    out = {
        "space_id": rec.get("space_id"),
        "name": rec.get("name"),
        "description": rec.get("description", ""),
        "owner": rec.get("owner", ""),
        "status": rec.get("status", "active"),
        "created_at": str(rec.get("created_at") or ""),
        "updated_at": str(rec.get("updated_at") or ""),
    }
    if doc_count is not None:
        out["doc_count"] = doc_count
    return out


def create_space(space_id: str, name: str, description: str = "",
                 owner: str = "system", by: str = "system",
                 tenant_id: str = "") -> dict:
    """创建空间。tenant_id 必传——空间名只在租户内唯一。"""
    if not tenant_id:
        raise AppError("缺少租户上下文，无法创建空间", code=500)
    space_id = _validate_space_id(space_id)
    name = (name or "").strip() or space_id
    # 唯一性检查必须带租户：否则 A 公司建了 hr，B 公司就建不了同名的 hr
    if meta_store.get_space(space_id, tenant_id):
        raise AppError(f"空间已存在：{space_id}", code=409)
    rec = {
        "space_id": space_id, "name": name, "description": description or "",
        "owner": owner or by, "tenant_id": tenant_id, "status": "active",
    }
    meta_store.save_space(rec)
    logger.info("创建空间 tenant=%s space_id=%s name=%s by=%s", tenant_id, space_id, name, by)
    return public_space(rec)


def update_space(space_id: str, name: Optional[str] = None,
                 description: Optional[str] = None, by: str = "system",
                 tenant_id: str = "") -> dict:
    if not tenant_id:
        raise AppError("缺少租户上下文，无法更新空间", code=500)
    rec = meta_store.get_space(space_id, tenant_id)
    if not rec or rec.get("status") == "deleted":
        raise AppError("空间不存在", code=404)
    merged = dict(rec)
    if name:
        merged["name"] = name.strip()
    if description is not None:
        merged["description"] = description
    meta_store.save_space(merged)
    logger.info("更新空间 tenant=%s space_id=%s by=%s", tenant_id, space_id, by)
    return public_space(merged)


def delete_space(space_id: str, force: bool = False, by: str = "system",
                 tenant_id: str = "") -> dict:
    """删除空间（软删）。

    默认拒绝删除「还有文档」的空间——级联删文档是不可逆操作，
    必须由调用方显式声明 force=true，且审计里留痕。
    默认空间永不可删：它是所有未指定空间文档的落点。
    """
    if not tenant_id:
        raise AppError("缺少租户上下文，无法删除空间", code=500)
    if space_id == settings.default_space_id:
        raise ForbiddenError("默认空间不允许删除")
    rec = meta_store.get_space(space_id, tenant_id)
    if not rec or rec.get("status") == "deleted":
        raise AppError("空间不存在", code=404)

    docs = meta_store.list_documents(tenant_id, limit=1, spaces=[space_id])
    if docs and not force:
        raise AppError(
            f"空间内仍有 {len(docs)}+ 份文档，请先迁移或使用 force=true 级联删除",
            code=409,
        )

    removed_docs = 0
    if force:
        for d in meta_store.list_documents(tenant_id, limit=10000, spaces=[space_id]):
            meta_store.delete_document(d["doc_id"])
            removed_docs += 1
        logger.warning("级联删除空间内文档 space_id=%s count=%s by=%s", space_id, removed_docs, by)

    meta_store.delete_space(space_id, tenant_id)
    logger.info("删除空间 tenant=%s space_id=%s force=%s removed_docs=%s by=%s",
                tenant_id, space_id, force, removed_docs, by)
    return {"space_id": space_id, "deleted": True, "removed_docs": removed_docs}


def list_spaces(visible: Optional[List[str]] = None, status: str = "active",
                tenant_id: str = "") -> List[dict]:
    """列出空间。visible 为 None 或含 `*` 时返回全部，否则只返回可访问的。

    租户过滤先于权限裁剪：tenant_id 决定"能看见哪家公司"，visible 只决定
    "在这家公司里能看见哪几个域"。
    """
    rows = meta_store.list_spaces(status=status, tenant_id=tenant_id)
    if visible is not None and not spaces_wildcard(visible):
        allow = set(visible)
        rows = [r for r in rows if r.get("space_id") in allow]
    return [public_space(r) for r in rows]


def resolve_spaces(principal_spaces: List[str], requested: Optional[str] = None) -> List[str]:
    """把「请求指定的空间」与「身份可访问的空间」求交集，得出本次检索范围。

    这是隔离的唯一入口，务必只在这里做交运算：
      - 未指定 → 用身份的可见空间（`*` 表示全部）
      - 指定了但无权访问 → 直接 403，**不能**静默降级为空结果，
        因为"查到了但没内容"和"你没权限"对用户是完全不同的信号
    """
    visible = list(principal_spaces or [settings.default_space_id])
    if not requested:
        return ["*"] if spaces_wildcard(visible) else visible

    requested = requested.strip()
    # 通配判据必须与 space_allowed 保持一致：P2 存量密钥的 perm_group 可能是 "all"，
    # 若这里只认 "*"，管理员密钥指定空间检索时会被误判为越权。
    if spaces_wildcard(visible):
        return [requested]
    if requested not in visible:
        raise ForbiddenError(f"无权访问知识库空间 {requested}，当前可访问 {sorted(visible)}")
    return [requested]


def default_scope(principal_spaces: List[str]) -> List[str]:
    """检索默认范围：`*` 原样传（不过滤），否则传可见列表。"""
    visible = list(principal_spaces or [settings.default_space_id])
    return ["*"] if spaces_wildcard(visible) else visible
