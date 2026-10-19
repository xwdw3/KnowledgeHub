"""本地向量索引：用 numpy 落盘实现，替代 P1 阶段的 Qdrant。

为什么 P1 不用 Qdrant：
  原型目标是验证"文档→检索→问答"业务链路是否成立，
  引入数据库会把验证重点转移到部署上。numpy 索引零依赖、可随包分发、
  万级片段毫秒级检索，完全够用。P2 再替换为 Qdrant（接口保持一致）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from app import config
from app.splitter import tokenize

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    id: str
    doc_id: str
    file_name: str
    text: str
    chunk_index: int
    perm: str = "all"
    tokens: list[str] = field(default_factory=list)


@dataclass
class DocMeta:
    doc_id: str
    file_name: str
    chunk_count: int
    char_count: int
    perm: str
    uploaded_at: str


class VectorIndex:
    """向量 + BM25 双索引，单文件落盘（npz 存向量、json 存元数据）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.chunks: list[Chunk] = []
        self.docs: dict[str, DocMeta] = {}
        self.vectors: np.ndarray | None = None
        # BM25 统计量
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0
        self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        meta_path: Path = config.META_FILE
        vec_path: Path = config.INDEX_FILE
        if not meta_path.exists() or not vec_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.chunks = [Chunk(**c) for c in meta.get("chunks", [])]
            self.docs = {d["doc_id"]: DocMeta(**d) for d in meta.get("docs", [])}
            data = np.load(vec_path)
            self.vectors = data["vectors"] if len(self.chunks) else None
            self._rebuild_bm25_stats()
            logger.info("索引已加载：%d 文档 / %d 片段", len(self.docs), len(self.chunks))
        except Exception as exc:
            logger.error("索引加载失败，将从空索引开始：%s", exc)
            self.chunks, self.docs, self.vectors = [], {}, None

    def save(self) -> None:
        with self.lock:
            meta = {
                "chunks": [asdict(c) for c in self.chunks],
                "docs": [asdict(d) for d in self.docs.values()],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            config.META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            vec = self.vectors if self.vectors is not None else np.zeros((0, 1), dtype=np.float32)
            np.savez_compressed(config.INDEX_FILE, vectors=vec)

    # ---------------- 写入 ----------------
    def add_document(self, file_name: str, text_chunks: list[str], vectors: np.ndarray, perm: str = "all", char_count: int = 0) -> DocMeta:
        """同名文档覆盖写入，保证幂等。"""
        self.remove_document(file_name, save=False)
        with self.lock:
            doc_id = "d" + datetime.now().strftime("%y%m%d%H%M%S%f")[:15]
            new_chunks = []
            for i, text in enumerate(text_chunks):
                new_chunks.append(Chunk(
                    id=f"{doc_id}_{i}",
                    doc_id=doc_id,
                    file_name=file_name,
                    text=text,
                    chunk_index=i,
                    perm=perm,
                    tokens=tokenize(text),
                ))
            self.chunks.extend(new_chunks)
            self.vectors = vectors if self.vectors is None or len(self.vectors) == 0 else np.vstack([self.vectors, vectors])

            meta = DocMeta(
                doc_id=doc_id,
                file_name=file_name,
                chunk_count=len(new_chunks),
                char_count=char_count or sum(len(t) for t in text_chunks),
                perm=perm,
                uploaded_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.docs[doc_id] = meta
            self._rebuild_bm25_stats()
            self.save()
            return meta

    def remove_document(self, file_name: str, save: bool = True) -> int:
        """按文件名删除，向量与元数据同步移除。返回删除的片段数。"""
        with self.lock:
            target_ids = {d.doc_id for d in self.docs.values() if d.file_name == file_name}
            if not target_ids:
                return 0
            keep_idx = [i for i, c in enumerate(self.chunks) if c.doc_id not in target_ids]
            removed = len(self.chunks) - len(keep_idx)
            self.chunks = [self.chunks[i] for i in keep_idx]
            if self.vectors is not None and len(self.vectors) > 0:
                self.vectors = self.vectors[keep_idx] if keep_idx else None
            for doc_id in target_ids:
                self.docs.pop(doc_id, None)
            self._rebuild_bm25_stats()
            if save:
                self.save()
            return removed

    # ---------------- BM25 ----------------
    def _rebuild_bm25_stats(self) -> None:
        df: dict[str, int] = {}
        total_len = 0
        for c in self.chunks:
            total_len += len(c.tokens)
            for t in set(c.tokens):
                df[t] = df.get(t, 0) + 1
        self._df = df
        self._avgdl = total_len / len(self.chunks) if self.chunks else 0.0

    def bm25_scores(self, query: str, k1: float = 1.5, b: float = 0.75) -> np.ndarray:
        q_tokens = tokenize(query)
        n = len(self.chunks)
        scores = np.zeros(n, dtype=np.float32)
        if n == 0 or not q_tokens:
            return scores
        for i, c in enumerate(self.chunks):
            tf: dict[str, int] = {}
            for t in c.tokens:
                tf[t] = tf.get(t, 0) + 1
            dl = len(c.tokens) or 1
            s = 0.0
            for q in q_tokens:
                f = tf.get(q, 0)
                if not f:
                    continue
                idf = np.log(1 + (n - self._df.get(q, 0) + 0.5) / (self._df.get(q, 0) + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / self._avgdl))
            scores[i] = s
        return scores

    # ---------------- 向量检索 ----------------
    def vector_scores(self, query_vec: np.ndarray) -> np.ndarray:
        """余弦相似度。维度不一致时返回全 0（由上层降级为纯 BM25）。"""
        if self.vectors is None or len(self.vectors) == 0:
            return np.zeros(0, dtype=np.float32)
        if self.vectors.shape[1] != query_vec.shape[0]:
            logger.warning(
                "向量维度不一致（索引 %d 维 vs 查询 %d 维），本次跳过向量召回；"
                "请用同一模式重建索引",
                self.vectors.shape[1], query_vec.shape[0],
            )
            return np.zeros(len(self.vectors), dtype=np.float32)
        return (self.vectors @ query_vec).astype(np.float32)

    # ---------------- 统计 ----------------
    def stats(self) -> dict:
        return {
            "doc_count": len(self.docs),
            "chunk_count": len(self.chunks),
            "char_count": sum(d.char_count for d in self.docs.values()),
            "avg_chunk_len": round(sum(len(c.text) for c in self.chunks) / len(self.chunks), 1) if self.chunks else 0,
            "vocab_size": len(self._df),
        }

    def list_docs(self) -> list[dict]:
        return [asdict(d) for d in sorted(self.docs.values(), key=lambda x: x.uploaded_at, reverse=True)]


index = VectorIndex()
