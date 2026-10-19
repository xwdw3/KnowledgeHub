"""检索：向量召回 + BM25 召回 → RRF 真正双路融合 → 输出可溯源的片段。

关键点（相比旧实现修复的核心缺陷）：
  RRF 必须分别对"向量排名"和"BM25 排名"计算 1/(k+rank) 再相加，
  只对单路结果计分等于没做融合。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from app import config
from app.embedder import embed_query
from app.store import index

logger = logging.getLogger(__name__)


@dataclass
class Hit:
    chunk_id: str
    file_name: str
    text: str
    chunk_index: int
    vec_score: float
    bm25_score: float
    rrf_score: float
    vec_rank: int | None = None
    bm25_rank: int | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "file_name": self.file_name,
            "text": self.text,
            "chunk_index": self.chunk_index,
            "vec_score": round(self.vec_score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "rrf_score": round(self.rrf_score, 4),
            "vec_rank": self.vec_rank,
            "bm25_rank": self.bm25_rank,
        }


def _top_ranks(scores: np.ndarray, k: int, perm: str) -> dict[int, int]:
    """返回 {行索引: 排名(从1开始)}，仅保留该权限组内 top-k。"""
    if len(scores) == 0:
        return {}
    candidate_idx = [i for i, c in enumerate(index.chunks) if c.perm in (perm, "all")]
    if not candidate_idx:
        return {}
    sub = scores[candidate_idx]
    order = np.argsort(-sub)[:k]
    return {candidate_idx[pos]: rank + 1 for rank, pos in enumerate(order) if sub[pos] > 0}


def search(query: str, top_k: int | None = None, perm: str = "all") -> list[Hit]:
    """混合检索。返回按融合分降序的片段列表。"""
    top_k = top_k or config.FINAL_TOP_K
    if not index.chunks:
        return []

    vec_scores = np.zeros(len(index.chunks), dtype=np.float32)
    vec_ranks: dict[int, int] = {}
    try:
        q_vec = embed_query(query)
        vec_scores = index.vector_scores(q_vec)
        vec_ranks = _top_ranks(vec_scores, config.RECALL_K, perm)
    except Exception as exc:
        logger.warning("向量检索失败，仅用 BM25：%s", exc)

    bm25_scores = index.bm25_scores(query)
    bm25_ranks = _top_ranks(bm25_scores, config.RECALL_K, perm)

    # ---------- RRF 双路融合 ----------
    fused: dict[int, float] = {}
    for ranks in (vec_ranks, bm25_ranks):
        for idx, rank in ranks.items():
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (config.RRF_K + rank)

    if not fused:
        return []

    hits: list[Hit] = []
    for idx, score in sorted(fused.items(), key=lambda x: -x[1])[:top_k]:
        c = index.chunks[idx]
        hits.append(Hit(
            chunk_id=c.id,
            file_name=c.file_name,
            text=c.text,
            chunk_index=c.chunk_index,
            vec_score=float(vec_scores[idx]),
            bm25_score=float(bm25_scores[idx]),
            rrf_score=score,
            vec_rank=vec_ranks.get(idx),
            bm25_rank=bm25_ranks.get(idx),
        ))
    return hits


def relevance(hits: list[Hit], query: str) -> float:
    """估算相关度 0~1，用于"是否应该拒答"。

    为什么不能只看 RRF 分：RRF 是排名倒数之和，只要库里有文档，
    哪怕完全不相关，排名第一的片段也必然拿到较高的 RRF 分（名次分虚高）。
    因此这里用"绝对信号"主导：
      - BM25 绝对分：是否命中真实关键词（库外问题通常为 0）
      - 向量余弦：语义层面是否接近
      - 查询词覆盖率：问题中的实词有多少能在片段中找到
    """
    if not hits:
        return 0.0
    from app.splitter import tokenize

    top = hits[0]
    q_tokens = set(tokenize(query))
    cover = len(q_tokens & set(tokenize(top.text))) / len(q_tokens) if q_tokens else 0.0

    vec_norm = max(0.0, min(top.vec_score, 1.0))                 # 余弦已在 [-1,1]
    bm25_norm = min(top.bm25_score / 8.0, 1.0)                    # 8 分视为强关键词命中

    score = 0.45 * vec_norm + 0.35 * bm25_norm + 0.20 * cover

    # 覆盖率极低说明问的实体在文档里根本没有，直接压分
    if cover < 0.30:
        score *= 0.6
    return round(score, 4)
