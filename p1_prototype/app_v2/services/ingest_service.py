"""入库服务：解析 → 清洗 → 切片 → 向量化 → 双写（Qdrant 向量 + PG 元数据）。

一致性策略：
  1. 以 (tenant_id, space_id, file_name) 为逻辑主键，同名上传视为覆盖
  2. 先写向量再写元数据，元数据失败则回滚已写入的向量，避免"检索得到但管理页看不到"
  3. 删除时同时清理向量、片段元数据、文档状态

P4 新增：原件落 MinIO。
  写入顺序 = 对象存储 -> 向量 -> 元数据。
  对象存储放最前是刻意的：它是最可能失败也最容易留下孤儿的一步。
  若放在中间，"元数据已写但原件没传成功"会让下载链接永久 404；
  放在最前则最坏情况只是一份没被引用的孤儿对象，可由定时任务按前缀回收。
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from app_v2.core.response import AppError
from app_v2.core.settings import settings
from app_v2.services.embedder import embed_texts
from app_v2.services.loader import ParseError, parse_file
from app_v2.services.splitter import split_text
from app_v2.storage.meta_store import meta_store
from app_v2.storage.object_store import build_key, object_storage
from app_v2.storage.vector_store import vector_store

logger = logging.getLogger(__name__)


_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _guess_content_type(file_name: str) -> str:
    return _CONTENT_TYPES.get(Path(file_name).suffix.lower(), "application/octet-stream")


def _chunk_id(doc_id: str, seq: int) -> str:
    # Qdrant 只接受无符号整数或 UUID，这里用确定性 UUID5 便于幂等覆盖
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{seq}"))


def _new_doc_id(file_name: str, space_id: str = "", tenant_id: str = "") -> str:
    """doc_id = hash(租户 + 空间 + 文件名)。

    两层都必须进哈希：否则「A公司/hr/考勤制度.md」和「B公司/hr/考勤制度.md」
    会算出同一个 doc_id，后上传的把先上传的整份覆盖掉——这是最难排查的一类 bug，
    因为两份文档都"上传成功"了，只是其中一个悄悄消失。
    """
    return uuid.uuid5(uuid.NAMESPACE_URL,
                      f"doc:{tenant_id}:{space_id}:{file_name}").hex[:16]


def ingest_bytes(file_name: str, content: bytes, perm_group: str = "all", uploader: str = "system",
                 space_id: str = "", tenant_id: str = "") -> dict:
    """从内存字节入库（上传接口直接调用，避免落临时文件）。"""
    suffix = Path(file_name).suffix.lower()
    if suffix not in settings.allow_ext_set:
        raise AppError(f"不支持的文件类型 {suffix}，仅支持 {settings.allow_ext}", code=415)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise AppError(f"文件超过大小限制 {settings.max_upload_mb}MB", code=413)

    tmp = Path(tempfile.gettempdir()) / f"kb_upload_{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(content)
    try:
        return _ingest_path(tmp, file_name, len(content), perm_group, uploader,
                            space_id, tenant_id)
    finally:
        tmp.unlink(missing_ok=True)


def ingest_path(path: str | Path, perm_group: str = "all", uploader: str = "system",
                space_id: str = "", tenant_id: str = "") -> dict:
    p = Path(path)
    return _ingest_path(p, p.name, p.stat().st_size, perm_group, uploader, space_id, tenant_id)


def _ingest_path(tmp_path: Path, file_name: str, file_size: int, perm_group: str, uploader: str,
                 space_id: str = "", tenant_id: str = "") -> dict:
    """解析阶段沿用临时文件名，但入库一律使用原始文件名。"""
    parsed_name, text = parse_file(tmp_path)      # 抛 ParseError
    if parsed_name != file_name:
        logger.debug("临时文件 %s 对应原始文件 %s", parsed_name, file_name)

    chunks = split_text(text)
    if not chunks:
        raise ParseError("切片结果为空")

    tenant_id = tenant_id or settings.default_tenant_id
    space_id = space_id or settings.default_space_id
    doc_id = _new_doc_id(file_name, space_id, tenant_id)
    # 覆盖：先清除旧数据，保证幂等。作用域限定在「同租户 + 同空间」内，
    # 跨租户/跨空间的同名文件是各自独立的文档，绝不能互相清除。
    old = meta_store.get_document_by_name(file_name, tenant_id, space_id)
    if old:
        _purge(old["doc_id"], old.get("object_key") or "", tenant_id)

    # ---------- 原件落对象存储 ----------
    object_key = ""
    if settings.minio_enabled:
        object_key = build_key(tenant_id, space_id, doc_id, file_name)
        if object_storage.put(object_key, tmp_path.read_bytes(),
                              content_type=_guess_content_type(file_name)):
            logger.debug("原件已上传 key=%s", object_key)
        else:
            # 不阻断入库：正文已经能解析、能检索，只是"下载原件"这个附加能力缺失
            object_key = ""
            logger.warning("原件上传失败，文档将以无原件模式入库 file=%s", file_name)

    vectors = embed_texts(chunks)

    points = []
    chunk_rows = []
    for seq, (chunk, vec) in enumerate(zip(chunks, vectors)):
        cid = _chunk_id(doc_id, seq)
        points.append({
            "id": cid,
            "vector": vec.tolist(),
            "payload": {
                "text": chunk,
                "doc_id": doc_id,
                "file_name": file_name,
                "seq": seq,
                "perm": perm_group,
                "space_id": space_id,
                # P4：向量库 payload 是向量层的唯一过滤依据，租户必须写进去
                "tenant_id": tenant_id,
            },
        })
        chunk_rows.append({"chunk_id": cid, "seq": seq, "char_len": len(chunk)})

    written = vector_store.upsert(points)
    try:
        meta_store.replace_chunks(doc_id, chunk_rows)
        meta_store.save_document({
            "doc_id": doc_id,
            "file_name": file_name,
            "file_ext": Path(file_name).suffix.lower(),
            "file_size": file_size,
            "char_count": len(text),
            "chunk_count": len(chunks),
            "perm_group": perm_group,
            "space_id": space_id,
            "tenant_id": tenant_id,
            "object_key": object_key,
            "status": "active",
            "uploader": uploader,
        })
    except Exception as exc:
        logger.error("元数据写入失败，回滚向量：%s", exc)
        vector_store.delete_by_doc(doc_id)
        raise

    logger.info("入库完成 tenant=%s file=%s space=%s chunks=%d chars=%d",
                tenant_id, file_name, space_id, len(chunks), len(text))
    from app_v2.services import retrieval_service
    retrieval_service.invalidate()          # 语料变化，BM25 缓存立即失效
    return {
        "doc_id": doc_id,
        "file_name": file_name,
        "chunk_count": len(chunks),
        "char_count": len(text),
        "vector_points": written,
        "perm_group": perm_group,
        "space_id": space_id,
        "tenant_id": tenant_id,
        "object_key": object_key,
        "overwritten": bool(old),
    }


def _purge(doc_id: str, object_key: str = "", tenant_id: str = "") -> None:
    """清理一个文档的全部落点。

    object_key 由调用方传入（来自文档记录），而不是靠 doc_id 反推：
    key 的前两段是 tenant/space，只凭 doc_id 无法拼出正确前缀，
    盲猜前缀可能删到别的租户的对象。
    """
    vector_store.delete_by_doc(doc_id)
    meta_store.delete_document(doc_id)
    meta_store.replace_chunks(doc_id, [])
    if settings.minio_enabled and object_key:
        # 按 "{tenant}/{space}/{doc_id}/" 前缀删，把该文档的衍生对象一起带走
        prefix = object_key.rsplit("/", 1)[0] + "/"
        if object_storage.delete_prefix(prefix) == 0:
            object_storage.delete(object_key)   # 兜底：前缀删不可用时按精确 key 删
    from app_v2.services import retrieval_service
    retrieval_service.invalidate()


def delete_by_name(file_name: str, space_id: str = "", tenant_id: str = "") -> dict:
    doc = meta_store.get_document_by_name(file_name, tenant_id or settings.default_tenant_id,
                                         space_id)
    if not doc:
        raise AppError(f"文档不存在：{file_name}", code=404)
    _purge(doc["doc_id"], doc.get("object_key") or "", doc.get("tenant_id") or "")
    return {"doc_id": doc["doc_id"], "file_name": file_name}


def rebuild_from_dir(dir_path: str | Path, perm_group: str = "all", space_id: str = "",
                     tenant_id: str = "") -> dict:
    ok, failed = [], []
    for p in sorted(Path(dir_path).iterdir()):
        if p.is_file() and p.suffix.lower() in settings.allow_ext_set and not p.name.startswith("."):
            try:
                ok.append(ingest_path(p, perm_group=perm_group, space_id=space_id,
                                      tenant_id=tenant_id))
            except Exception as exc:
                failed.append({"file_name": p.name, "reason": str(exc)})
    return {"success": ok, "failed": failed, "total": len(ok) + len(failed)}


# ======================================================================
# P4：GB 级文档的流式入库
# ======================================================================
# 单批向量化的切片数。16 与 embed_texts 内部 batch_size 对齐：
# 每批既是一次 embedding 请求，也是一次向量库写入，两边的批大小一致
# 才能让"内存峰值"和"单次请求时长"都被一个数字控制住。
STREAM_EMBED_BATCH = 16

# 每处理多少个切片更新一次任务进度。太密会刷爆任务表，
# 太疏则用户在进度条上看到"卡住"。200 片一跳在 GB 级文档上是合适的粒度。
PROGRESS_STEP_CHUNKS = 200


def ingest_stream(file_name: str, source_path: str | Path, perm_group: str = "all",
                  uploader: str = "system", space_id: str = "", tenant_id: str = "",
                  on_progress=None) -> dict:
    """流式入库：内存占用与文件大小**无关**。

    与 `ingest_bytes` / `ingest_path` 的区别：

      | 环节 | 全量版 | 流式版 |
      |------|--------|--------|
      | 读取 | read_bytes() 全文件进内存 | 1MB 块增量解码 |
      | 解析 | 返回完整字符串 | 增量产出文本块 |
      | 切片 | 对全文切片，返回 list | StreamingSplitter 增量产出 |
      | 向量化 | 所有切片一次性 embed | 每 16 片批处理 |
      | 写库 | 所有点一次性 upsert | 每批写完即释放 |

    峰值内存从 O(文件大小) 降到 O(批大小 + 切片窗口)，1GB 与 1MB 文档
    的内存占用基本相同。

    **代价：不是原子的。** 中途失败会留下已写入的部分向量。所以失败路径
    会调用 `_purge` 清理（见 except 分支）——否则检索会命中半份文档，
    用户看到"答案引用了不存在的第 7 章"。

    `on_progress(read_bytes, total_bytes, chunk_count)` 由调用方传入，
    用于把进度写回任务表。不传则完全静默。
    """
    from app_v2.services.loader import iter_parse_file
    from app_v2.services.splitter import StreamingSplitter

    src = Path(source_path)
    suffix = src.suffix.lower()
    if suffix not in settings.allow_ext_set:
        raise AppError(f"不支持的文件类型 {suffix}，仅支持 {settings.allow_ext}", code=415)

    tenant_id = tenant_id or settings.default_tenant_id
    space_id = space_id or settings.default_space_id
    doc_id = _new_doc_id(file_name, space_id, tenant_id)
    total_bytes = src.stat().st_size

    # 覆盖语义：先清旧，保证同名重传是幂等的（与全量版一致）
    old = meta_store.get_document_by_name(file_name, tenant_id, space_id)
    if old:
        _purge(old["doc_id"], old.get("object_key") or "", tenant_id)

    # 原件先落对象存储（流式写，不读进内存）
    object_key = ""
    if settings.minio_enabled:
        object_key = build_key(tenant_id, space_id, doc_id, file_name)
        if object_storage.put_file(object_key, str(src),
                                   content_type=_guess_content_type(file_name)):
            logger.debug("原件已流式上传 key=%s", object_key)
        else:
            object_key = ""
            logger.warning("原件上传失败，以无原件模式入库 file=%s", file_name)

    splitter = StreamingSplitter()
    pending_chunks: list[str] = []
    total_chunks = 0
    total_chars = 0
    written = 0

    def _flush_batch() -> None:
        """把 pending_chunks 向量化并写入。调用后 pending_chunks 被清空。"""
        nonlocal pending_chunks, written, total_chunks
        if not pending_chunks:
            return
        batch = pending_chunks
        pending_chunks = []
        base = total_chunks
        vecs = embed_texts(batch)
        points = []
        for offset, (chunk, vec) in enumerate(zip(batch, vecs)):
            points.append({
                "id": _chunk_id(doc_id, base + offset),
                "vector": vec.tolist(),
                "payload": {
                    "text": chunk,
                    "doc_id": doc_id,
                    "file_name": file_name,
                    "seq": base + offset,
                    "perm": perm_group,
                    "space_id": space_id,
                    "tenant_id": tenant_id,
                },
            })
        written += vector_store.upsert(points)
        # upsert 成功后才推进计数：失败时抛异常，整份文档会被 except 清理，
        # 所以这里的中间状态不会被观察到。顺序写成"先写后计数"是为了
        # 让 total_chunks 始终等于"已确认写入的片段数"。
        total_chunks = base + len(batch)

    try:
        read_bytes = 0
        for block in iter_parse_file(src):
            read_bytes += len(block.encode("utf-8", "ignore"))
            total_chars += len(block)
            for chunk in splitter.feed(block):
                pending_chunks.append(chunk)
                if len(pending_chunks) >= STREAM_EMBED_BATCH:
                    _flush_batch()
            if on_progress and total_chunks and total_chunks % PROGRESS_STEP_CHUNKS < STREAM_EMBED_BATCH:
                on_progress(min(read_bytes, total_bytes), total_bytes, total_chunks)

        for chunk in splitter.flush():
            pending_chunks.append(chunk)
            if len(pending_chunks) >= STREAM_EMBED_BATCH:
                _flush_batch()
        _flush_batch()

        if total_chunks == 0:
            raise ParseError("文档解析成功，但未提取到任何文本（可能是扫描件或加密文档）")

        # 向量必须先真正落盘，再写元数据。
        # 本地后端对 upsert 做了落盘节流（见 vector_store._LOCAL_SAVE_EVERY），
        # 不显式 flush 的话，最后不足一个窗口的批次只在内存里；
        # 此时元数据已经写入并让文档"可见"，进程一重启就会出现
        # "列表里有这份文档、检索却只命中前 90%"的诡异现象。
        # Qdrant 后端的 flush 是空操作，不影响正常部署。
        _flush_backend = getattr(vector_store, "flush", None)
        if callable(_flush_backend):
            _flush_backend()

        # 元数据最后写：它一旦写入，这份文档对检索与列表就"可见"了。
        # 放在最后保证"能看到 = 数据完整"。
        chunk_rows = [{"chunk_id": _chunk_id(doc_id, i), "seq": i, "char_len": 0}
                      for i in range(total_chunks)]
        try:
            meta_store.replace_chunks(doc_id, chunk_rows)
            meta_store.save_document({
                "doc_id": doc_id,
                "file_name": file_name,
                "file_ext": suffix,
                "file_size": total_bytes,
                "char_count": total_chars,
                "chunk_count": total_chunks,
                "perm_group": perm_group,
                "space_id": space_id,
                "tenant_id": tenant_id,
                "object_key": object_key,
                "status": "active",
                "uploader": uploader,
            })
        except Exception as exc:
            logger.error("元数据写入失败，回滚向量：%s", exc)
            vector_store.delete_by_doc(doc_id)
            raise

        if on_progress:
            on_progress(total_bytes, total_bytes, total_chunks)

        logger.info("流式入库完成 tenant=%s file=%s size=%dB chunks=%d",
                    tenant_id, file_name, total_bytes, total_chunks)
        from app_v2.services import retrieval_service
        retrieval_service.invalidate()
        return {
            "doc_id": doc_id,
            "file_name": file_name,
            "chunk_count": total_chunks,
            "char_count": total_chars,
            "vector_points": written,
            "perm_group": perm_group,
            "space_id": space_id,
            "tenant_id": tenant_id,
            "object_key": object_key,
            "overwritten": bool(old),
            "streamed": True,
        }
    except Exception:
        # 非原子操作必须自己收拾现场：已写入的向量如果不删，
        # 检索会命中半份文档，而元数据里根本没这条记录——
        # 用户会看到"引用了一份不存在的文档"。
        logger.warning("流式入库失败，清理已写入的向量 doc=%s", doc_id)
        try:
            vector_store.delete_by_doc(doc_id)
            meta_store.replace_chunks(doc_id, [])
        except Exception as clean_exc:
            logger.error("清理失败，可能残留向量 doc=%s：%s", doc_id, clean_exc)
        raise
