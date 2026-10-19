"""文档接口：上传入库、列表、详情、删除、重建、清理。

P4 变更：
  - **租户归属**：所有查询/写入都以 `principal.tenant_id` 为第一过滤条件。
    跨租户访问与"不存在"完全同义（404），不给租户名探测留信道
  - doc_id 把租户纳入哈希，不同租户的同名文件是两份独立文档
  - 写侧配额校验：上传前检查租户文档数上限，超限直接拒绝而不是先写后错

P3 变更：
  - **空间归属**：文档多了 space_id 维度。上传可指定目标空间，列表/详情/删除
    都按空间过滤；无权访问的空间一律按"不存在"处理（404 而非 403），避免探测
  - doc_id 把空间纳入哈希，不同空间的同名文件是两份独立文档，互不覆盖

P2 变更（保留）：
  - 写操作需要 `kb:write`，重建/清理需要 `kb:admin`
  - **权限归属**：上传时写入的 perm_group 不能超出调用方凭据自身的权限范围，
    否则受限凭据可以把公司密件标成 all，造成越权扩散
  - 文档变更后自动失效检索与问答缓存
  - 全量审计：谁上传/删除了什么文件、影响多少片段
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app_v2.api.deps import get_meta_store, require_permission
from app_v2.core.metrics import metrics_registry
from app_v2.core.response import (
    AppError, ForbiddenError, NotFoundError, ValidationError,
)
from app_v2.core.security import (
    PERM_KB_ADMIN, PERM_KB_READ, PERM_KB_WRITE, parse_perm_groups, space_allowed,
)
from app_v2.core.settings import BASE_DIR, settings
from app_v2.schemas.models import DocumentItem, DocumentListData, IngestData
from app_v2.services import (
    audit_service, auth_service, cache_service, ingest_service, space_service,
)
from app_v2.services.auth_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["文档管理"])


def _check_perm_group(requested: str, principal: Principal) -> str:
    """限制调用方只能把文档投放到自己可访问的权限组内。

    典型越权：只有 hr 权限的编辑把文档标成 all，等于把密件公开给全员。
    因此目标组必须是 principal 自身权限组的子集——
    注意**不把 all 无条件加进允许集**：all 意味着全员可见，只有本身拥有 all 的
    凭据（管理员/公共凭据）才能往 all 投放，否则受限凭据就能自我扩权。
    """
    wanted = parse_perm_groups(requested)
    if len(wanted) > 1:
        raise ValidationError("perm_group 只能指定一个权限组")
    target = sorted(wanted)[0]
    # ⚠ 必须用 `writable_perm_groups`（写入范围），不能用 `perm_groups`。
    # 后者是**读取**范围，里面含系统自动补的 all（公共文档人人可读）。
    # 拿它来判断写入，等于每个受限凭据都"拥有 all"，editor(hr) 就能把
    # hr 密件标成 all 公开给全员——这是最不希望发生的那种越权。
    allowed = set(principal.writable_perm_groups)   # 严格：不补 all
    # `*` 表示"全部权限组"（管理员账号），必须视为通配。
    # 否则管理员会被自己的通配身份挡住——判据是"是否包含目标"，
    # 而不是"能否在字面集合里找到 all"。
    if "*" in allowed or target in allowed:
        return target
    raise ForbiddenError(
        f"无权将文档归入权限组 {target}，当前凭据仅限 {sorted(allowed)}")


def _check_space(space_id: str, principal: Principal) -> str:
    """校验目标空间：留空归入默认空间，否则必须是自身可见空间之一。

    判据用 `principal.space_scope` 而不是 `perm_groups`：
    两套范围概念正交（权限组=谁能看，空间=内容属于哪个域），混用会导致
    P2 存量密钥（perm_group=hr）被判为"无权访问 default 空间"而全部 403。
    """
    target = (space_id or "").strip() or settings.default_space_id
    scope = principal.space_scope
    try:
        allowed = space_service.resolve_spaces(scope, target)
    except ForbiddenError:
        raise ForbiddenError(
            f"无权在该空间投放文档：{target}，当前可访问 {sorted(scope)}")
    return allowed[0]


def _assert_space_visible(doc: dict, principal: Principal) -> None:
    """文档所属空间必须在调用方可见范围内。不可见时按"不存在"处理。"""
    space = doc.get("space_id") or settings.default_space_id
    if space_allowed(space, principal.space_scope):
        return
    raise NotFoundError(f"文档不存在：{doc.get('doc_id')}")


@router.post("/documents", response_model=IngestData, summary="上传文档并入库",
             description="支持 pdf/docx/txt/md。同名文件重复上传视为**覆盖更新**（先删旧片段再写新片段），"
                         "因此该接口是幂等的，可安全重复调用。需要 `kb:write` 权限。\n\n"
                         "**大小上限按模式区分**：同步模式受 `MAX_UPLOAD_MB`（默认 20MB）限制，"
                         "因为同步路径要把全文读进内存并在一个请求内跑完；"
                         "异步模式（`async_mode=true`）受 `MAX_ASYNC_UPLOAD_MB`（默认 4096MB）限制，"
                         "原件按 1MB 分块落盘后转对象存储，内存占用与文件大小无关。")
def upload_document(
    request: Request,
    file: UploadFile = File(..., description="文档文件；同步模式最大 20MB，异步模式最大 4096MB"),
    perm_group: str = Form("all", description="权限组（不能超出当前凭据的可访问范围）"),
    uploader: str = Form("", description="上传人；留空则取当前凭据的 actor"),
    space_id: str = Form("", description="目标知识库空间，留空则归入默认空间"),
    async_mode: bool = Form(False, description="true 则只落原件并建任务，立即返回 task_id；"
                                               "GB 级大文件必须开启，否则同步请求会超时且 OOM"),
    principal: Principal = Depends(require_permission(PERM_KB_WRITE)),
) -> IngestData:
    safe_group = _check_perm_group(perm_group, principal)
    actor = (uploader or principal.actor).strip() or principal.actor
    # 写入侧同样做空间校验：能读某个空间不等于能往里写，
    # 但这里复用同一套可见范围判断——企业内网场景下"空间成员"即"可读写成员"，
    # 若将来需要读写分离，应在此处换成空间级的 owner/editor 列表。
    safe_space = _check_space(space_id, principal)

    file_name = file.filename or "unnamed"

    # ---------- 异步路径：Streaming 落盘 → 对象存储 → 建任务，立即返回 ----------
    # ⚠ 异步分支必须在 `read()` 之前分流。
    # 之前的写法是"一进来就读全量 bytes，再判断走哪条路"——
    # 那样 1GB 文件光是在上传接口里就已经吃掉 1GB+ 常驻内存，
    # 后面 worker 再快也救不回来（网关先被拖死或直接被 OOMKill）。
    if async_mode:
        return _submit_ingest_task_stream(
            request=request, principal=principal, upload=file,
            file_name=file_name, perm_group=safe_group,
            uploader=actor, space_id=safe_space,
        )

    # ---------- 同步路径：保持原有的全量读取，但上限更严 ----------
    # UploadFile.file 是 SpooledTemporaryFile，直接同步读取；
    # 若改用 await file.read() 需把整个路由改成 async，会牵动同步的存储/模型调用。
    max_sync = settings.max_upload_mb * 1024 * 1024
    try:
        content = file.file.read(max_sync + 1)
    except Exception as exc:
        raise AppError(f"读取上传文件失败：{exc}", code=400)
    if not content:
        raise AppError("上传文件为空", code=400)
    if len(content) > max_sync:
        # 多读 1 字节即可判定超限，无需把整个大文件读完再报错。
        raise AppError(
            f"同步模式单文件上限 {settings.max_upload_mb}MB，"
            f"大文件请勾选「异步处理」（上限 {settings.max_async_upload_mb}MB）",
            code=413,
        )

    result = ingest_service.ingest_bytes(
        file_name=file_name,
        content=content,
        perm_group=safe_group,
        uploader=actor,
        space_id=safe_space,
        tenant_id=principal.tenant_id,
    )

    metrics_registry.incr("doc_upload_total")
    metrics_registry.incr("doc_chunks_total", int(result.get("chunk_count") or 0))
    invalidated = cache_service.invalidate_knowledge()

    audit_service.record(
        action=audit_service.ACTION_DOC_UPLOAD,
        actor=principal.actor, role=principal.role,
        resource=f"doc:{result['doc_id']}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={
            "file_name": result["file_name"],
            "tenant_id": principal.tenant_id,
            "perm_group": safe_group,
            "space_id": safe_space,
            "chunk_count": result["chunk_count"],
            "char_count": result["char_count"],
            "overwritten": result.get("overwritten", False),
            "uploader": actor,
            "cache_invalidated": invalidated,
        },
    )
    return IngestData(**result)


@router.get("/documents", response_model=DocumentListData, summary="文档列表",
            description="只返回当前密钥权限范围内的文档（按 perm_group 过滤）。需要 `kb:read` 权限。")
def list_documents(keyword: str = "", limit: int = 100, space_id: str = "",
                   principal: Principal = Depends(require_permission(PERM_KB_READ))) -> DocumentListData:
    allowed = sorted(set(principal.perm_groups) | {"all"})
    # 过滤必须下推到存储层：先在 DB 里按权限组/空间筛，再截断 limit。
    # 若先取 limit 再在内存里过滤，受限凭据会因为"先截断后鉴权"而看不到本该可见的
    # 高编号文档（分页语义错乱，且 total 与实际不符）。
    spaces = space_service.resolve_spaces(principal.space_scope, space_id or None)
    items = get_meta_store().list_documents(
        principal.tenant_id, keyword=keyword, limit=limit,
        perm_groups=allowed, spaces=spaces,
    )
    return DocumentListData(total=len(items), items=[_to_item(d) for d in items])


@router.get("/documents/{doc_id}", response_model=DocumentItem, summary="文档详情")
def get_document(doc_id: str,
                 principal: Principal = Depends(require_permission(PERM_KB_READ))) -> DocumentItem:
    doc = get_meta_store().get_document(doc_id, principal.tenant_id)
    if not doc:
        raise NotFoundError(f"文档不存在：{doc_id}")
    allowed = set(principal.perm_groups) | {"all"}
    if (doc.get("perm_group") or "all") not in allowed:
        # 不泄露"存在但无权"的信息，统一按不存在处理，避免探测
        raise NotFoundError(f"文档不存在：{doc_id}")
    _assert_space_visible(doc, principal)
    return _to_item(doc)


@router.delete("/documents", summary="按文件名删除文档",
               description="同时清理向量片段、片段元数据与文档状态，保证两侧一致。需要 `kb:write` 权限。")
def delete_document(file_name: str, request: Request, space_id: str = "",
                    principal: Principal = Depends(require_permission(PERM_KB_WRITE))) -> Dict[str, Any]:
    doc = get_meta_store().get_document_by_name(file_name, principal.tenant_id, space_id)
    if doc:
        allowed = set(principal.perm_groups) | {"all"}
        if (doc.get("perm_group") or "all") not in allowed:
            audit_service.record(
                action=audit_service.ACTION_DOC_DELETE,
                actor=principal.actor, role=principal.role,
                resource=f"doc:{doc.get('doc_id')}",
                result=audit_service.RESULT_DENIED, status_code=403,
                ip=getattr(request.state, "client_ip", ""),
                detail={"file_name": file_name, "reason": "权限组不匹配"},
            )
            raise ForbiddenError(f"无权删除文档：{file_name}")
        _assert_space_visible(doc, principal)

    result = ingest_service.delete_by_name(file_name, space_id, principal.tenant_id)
    metrics_registry.incr("doc_delete_total")
    invalidated = cache_service.invalidate_knowledge()

    audit_service.record(
        action=audit_service.ACTION_DOC_DELETE,
        actor=principal.actor, role=principal.role,
        resource=f"doc:{result.get('doc_id')}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"file_name": file_name, "cache_invalidated": invalidated},
    )
    return result


@router.post("/documents/rebuild", summary="从示例目录批量重建索引",
             description="读取服务器 data/docs 下的全部文档重新入库，用于演示与初始化。需要 `kb:admin` 权限。")
def rebuild(request: Request, principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    docs_dir = BASE_DIR / "data" / "docs"
    if not docs_dir.exists():
        raise NotFoundError(f"示例目录不存在：{docs_dir}")
    result = ingest_service.rebuild_from_dir(docs_dir, tenant_id=principal.tenant_id)
    invalidated = cache_service.invalidate_knowledge()
    success = result.get("success") or []
    metrics_registry.incr("doc_upload_total", len(success))

    audit_service.record(
        action=audit_service.ACTION_DOC_REBUILD,
        actor=principal.actor, role=principal.role,
        resource="doc:rebuild",
        ip=getattr(request.state, "client_ip", ""),
        detail={"rebuilt": len(success), "cache_invalidated": invalidated},
    )
    return result


@router.post("/documents/purge", summary="清理已删除文档的残留记录",
             description="删除采用**软删除**（保留 status=deleted 供审计），长期运行会持续累积。"
                         "本接口按天数物理回收这些行，默认清理 7 天前已删除的记录。需要 `kb:admin` 权限。")
def purge_deleted(older_than_days: int = 7, request: Request = None,
                  principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    if older_than_days < 0:
        raise AppError("older_than_days 不能为负数", code=422)
    removed = get_meta_store().purge_deleted(older_than_days=older_than_days)
    logger.info("清理软删除文档 %d 条（older_than_days=%d）", removed, older_than_days)

    audit_service.record(
        action=audit_service.ACTION_DOC_PURGE,
        actor=principal.actor, role=principal.role,
        resource="doc:purge",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"removed": removed, "older_than_days": older_than_days},
    )
    return {"removed": removed, "older_than_days": older_than_days}


def _to_item(d: dict) -> DocumentItem:
    """DB 行 → 响应模型，缺省字段用默认值兜底（兼容内存实现与 PG 返回格式）。"""
    return DocumentItem(
        doc_id=d["doc_id"],
        file_name=d["file_name"],
        file_ext=d.get("file_ext") or "",
        file_size=int(d.get("file_size") or 0),
        char_count=int(d.get("char_count") or 0),
        chunk_count=int(d.get("chunk_count") or 0),
        perm_group=d.get("perm_group") or "all",
        space_id=d.get("space_id") or settings.default_space_id,
        status=d.get("status") or "active",
        uploader=d.get("uploader") or "system",
        created_at=str(d["created_at"]) if d.get("created_at") else None,
        updated_at=str(d["updated_at"]) if d.get("updated_at") else None,
    )


@router.post("/documents/backup", summary="备份当前租户的原件到本地目录",
             description="把本租户前缀下的全部对象流式复制到指定目录，返回清单。"
                         "需要 `kb:admin` 权限。\n\n"
                         "**为什么前缀由服务端决定、而不是让调用方传**：备份能力天然能读到全量数据，"
                         "如果允许传 `prefix=''`，一个租户的管理员就能把整个桶（包含其他企业原件）"
                         "打包带走。这里强制用 `principal.tenant_id` 作为前缀，"
                         "越界在同租户内也不可能发生。")
def backup_documents(dest_dir: str, request: Request,
                     principal: Principal = Depends(require_permission(PERM_KB_ADMIN))) -> Dict[str, Any]:
    from app_v2.storage.object_store import object_storage

    if not settings.minio_enabled:
        raise AppError("对象存储未启用，无法备份", code=503)
    if not (dest_dir or "").strip():
        raise AppError("必须指定备份目录", code=400)

    # 前缀只由服务端拼，不接受外部输入（见接口说明）
    prefix = f"{_safe_prefix(principal.tenant_id)}/"
    result = object_storage.backup_prefix(
        prefix, dest_dir, tenant_id=_safe_prefix(principal.tenant_id))

    audit_service.record(
        action="doc.backup",
        actor=principal.actor, role=principal.role,
        resource=f"tenant:{principal.tenant_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={"prefix": prefix, "dest_dir": dest_dir,
                "files": result["files"], "bytes": result["bytes"],
                "skipped": len(result["skipped"])},
    )
    return {
        "tenant_id": principal.tenant_id,
        "prefix": prefix,
        "dest_dir": dest_dir,
        "files": result["files"],
        "bytes": result["bytes"],
        # skipped 非空说明有对象读取失败（或越界被拒），
        # 必须让调用方看到——静默跳过会让"备份完成"变成一句谎话。
        "skipped": result["skipped"],
    }


def _safe_prefix(tenant_id: str) -> str:
    """租户 ID 进对象前缀前的清洗。

    与 object_store 里 build_key 用的是同一套规则：
    前缀拼接前不清洗，`tenant_id="../other"` 就能让备份读到邻居的目录。
    这里是备份功能的唯一入口，必须自己兜底，不能指望调用方守规矩。
    """
    clean = (tenant_id or "").strip().replace("/", "_").replace("\\", "_")
    clean = clean.replace("..", "_")
    return clean or "default"


@router.get("/documents/{doc_id}/download-url", summary="获取原件下载链接",
            description="返回对象存储的**预签名**下载地址（默认 1 小时有效）。"
                        "链接由 MinIO 直接服务，不经过本服务，因此适合大文件下载。\n\n"
                        "**权限**：与文档详情一致，需同时通过租户、空间、权限组三层校验。"
                        "链接一旦签发，在其有效期内可被任何人使用——这是预签名 URL 的固有特性，"
                        "所以有效期不宜设置过长。")
def get_download_url(doc_id: str, expire: int = 0, request: Request = None,
                     principal: Principal = Depends(require_permission(PERM_KB_READ))) -> Dict[str, Any]:
    from app_v2.core.response import AppError as _AppError
    from app_v2.storage.object_store import object_storage

    doc = get_meta_store().get_document(doc_id, principal.tenant_id)
    if not doc:
        raise NotFoundError(f"文档不存在：{doc_id}")
    allowed = set(principal.perm_groups) | {"all"}
    if (doc.get("perm_group") or "all") not in allowed:
        raise NotFoundError(f"文档不存在：{doc_id}")
    _assert_space_visible(doc, principal)

    key = doc.get("object_key") or ""
    if not key:
        raise _AppError("该文档以无原件模式入库（上传时对象存储不可用），无法下载原件", code=404)

    url = object_storage.presigned_get_url(key, expire=expire,
                                           download_name=doc.get("file_name") or "")
    audit_service.record(
        action="doc.download_url",
        actor=principal.actor, role=principal.role,
        resource=f"doc:{doc_id}",
        ip=getattr(request.state, "client_ip", "") if request else "",
        detail={"file_name": doc.get("file_name"), "tenant_id": principal.tenant_id,
                "expire": expire or settings.minio_presign_expire},
    )
    return {
        "doc_id": doc_id,
        "file_name": doc.get("file_name"),
        "url": url,
        "expire": expire or settings.minio_presign_expire,
        "backend": "minio",
    }


# 上传落盘的分块大小。1MB 是内存占用与 IO 次数之间的折中：
# 再小会让 GB 级文件产生上千次 read 调用，再大则峰值内存跟着涨。
_UPLOAD_CHUNK = 1024 * 1024


def _submit_ingest_task_stream(request: Request, principal: Principal, upload: UploadFile,
                               file_name: str, perm_group: str, uploader: str,
                               space_id: str) -> IngestData:
    """异步上传：**按块**落临时文件 → 对象存储 → 建任务。

    为什么原件必须在上传接口里就存好：worker 不该再接收一次文件流
    （那必须把整份二进制塞进 broker 消息，Redis 会被撑爆）。
    先把原件落地，任务里只传一个 object_key，消息体恒定很小。

    与旧实现的区别只有一点，但很关键：**不再 `upload.file.read()` 全量读入**。
    改为 1MB 一次循环读、边读边写临时文件、边累计大小做超限判定，
    再 `put_file()`（MinIO 的 fput_object，同样是流式分块）。
    整条路径的峰值内存 = 2 × 1MB（一读一写），与文件是 20MB 还是 4GB 无关。
    """
    from app_v2.services import task_service as ts
    from app_v2.storage.object_store import build_key, guess_content_type, object_storage

    if not settings.minio_enabled:
        raise AppError("异步入库依赖对象存储，当前 MINIO_ENABLED=false，请改用同步模式", code=503)

    max_async = settings.max_async_upload_mb * 1024 * 1024
    task_doc_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"pending:{principal.tenant_id}:{space_id}:{file_name}"
    ).hex[:16]
    object_key = build_key(principal.tenant_id, space_id, task_doc_id, file_name)

    tmp_path: Path | None = None
    size = 0
    try:
        # delete=False：文件要在 with 结束后继续被 MinIO 读取，不能提前删。
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file_name).suffix) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                buf = upload.file.read(_UPLOAD_CHUNK)
                if not buf:
                    break
                size += len(buf)
                if size > max_async:
                    # 一超限就停手：不把剩下的几百兆白白读完再报错，
                    # 否则用户等了十分钟才被告知"文件太大"。
                    raise AppError(
                        f"异步模式单文件上限 {settings.max_async_upload_mb}MB，"
                        f"当前已超过该上限", code=413)
                tmp.write(buf)

        if size == 0:
            raise AppError("上传文件为空", code=400)

        if not object_storage.put_file(object_key, str(tmp_path),
                                       guess_content_type(file_name)):
            raise AppError("原件上传对象存储失败，请稍后重试", code=503)
    finally:
        # 无论成功失败都要清掉临时文件：GB 级原件留在 /tmp 里，
        # 几次上传就能把磁盘吃满，而且报错后更难排查。
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    rec = ts.task_service.submit(
        ts.TASK_INGEST, principal.tenant_id,
        payload={
            "file_name": file_name,
            "object_key": object_key,
            "perm_group": perm_group,
            "uploader": uploader,
            "space_id": space_id,
            # 两个键都写：worker 历史版本读 file_size，新版本读 size。
            # 只写一个会让"流式路径是否启用"取决于两边版本是否对齐——
            # 这个 bug 已经真实发生过一次（file_size 恒为 0 → 永远走全量路径）。
            "size": size,
            "file_size": size,
        },
        actor=principal.actor,
        doc_id=task_doc_id,
    )

    audit_service.record(
        action=audit_service.ACTION_DOC_UPLOAD,
        actor=principal.actor, role=principal.role,
        resource=f"doc:{task_doc_id}",
        ip=getattr(request.state, "client_ip", ""),
        user_agent=request.headers.get("User-Agent", ""),
        detail={
            "file_name": file_name, "tenant_id": principal.tenant_id,
            "perm_group": perm_group, "space_id": space_id,
            "async": True, "task_id": rec.get("task_id"),
            "status": rec.get("status"), "size": size,
            "object_key": object_key,
        },
    )
    return IngestData(
        doc_id=task_doc_id,
        file_name=file_name,
        chunk_count=0,
        char_count=0,
        vector_points=0,
        perm_group=perm_group,
        space_id=space_id,
        tenant_id=principal.tenant_id,
        object_key=object_key,
        overwritten=False,
        task_id=rec.get("task_id", ""),
        async_mode=True,
    )
