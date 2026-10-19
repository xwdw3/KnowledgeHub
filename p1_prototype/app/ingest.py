"""入库服务：解析 → 切片 → 向量化 → 写入索引。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app import config
from app.embedder import embed_texts
from app.loader import ParseError, parse_file
from app.splitter import split_text
from app.store import index

logger = logging.getLogger(__name__)


def ingest_path(file_path: str | Path, perm: str = "all", keep_copy: bool = True) -> dict:
    """入库单个文件，同名自动覆盖。"""
    path = Path(file_path)
    file_name, text = parse_file(path)          # 抛 ParseError 由上层转成友好提示

    chunks = split_text(text)
    if not chunks:
        raise ParseError("文档切片结果为空")

    vectors = embed_texts(chunks)
    meta = index.add_document(file_name, chunks, vectors, perm=perm, char_count=len(text))

    if keep_copy and path.parent.resolve() != config.DOCS_DIR.resolve():
        shutil.copy2(path, config.DOCS_DIR / file_name)

    logger.info("入库完成：%s → %d 片段", file_name, meta.chunk_count)
    return {
        "file_name": meta.file_name,
        "doc_id": meta.doc_id,
        "chunk_count": meta.chunk_count,
        "char_count": meta.char_count,
        "perm": meta.perm,
    }


def ingest_dir(dir_path: str | Path = None) -> dict:
    """批量入库目录下的全部支持格式文档，单文件失败不影响其他文件。"""
    dir_path = Path(dir_path or config.DOCS_DIR)
    ok, failed = [], []
    for p in sorted(dir_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            ok.append(ingest_path(p, keep_copy=False))
        except ParseError as exc:
            failed.append({"file_name": p.name, "reason": str(exc)})
        except Exception as exc:      # 不因单个文件异常中断批量入库
            logger.exception("入库失败：%s", p.name)
            failed.append({"file_name": p.name, "reason": f"入库异常：{exc}"})
    return {"success": ok, "failed": failed, "total": len(ok) + len(failed)}


def delete_doc(file_name: str) -> int:
    removed = index.remove_document(file_name)
    copy = config.DOCS_DIR / file_name
    if copy.exists():
        copy.unlink()
    return removed
