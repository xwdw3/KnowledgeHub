"""Embedding：真实模式调 BGE-M3；离线模式用本地哈希向量（零依赖、可演示）。"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable

import numpy as np

from app import config

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=config.EMBED_API_KEY, base_url=config.EMBED_BASE_URL)
    return _client


def _hash_embedding(text: str, dim: int = 1024) -> np.ndarray:
    """离线向量：字符 n-gram 哈希到固定维度，L2 归一化。

    效果远不及语义模型，但能保证"相同表达→相似向量"，
    用于无网络/无额度时演示完整链路（入库、检索、排序、引用）。
    维度默认与 BGE-M3 对齐，避免与真实模型建立的索引不兼容。
    """
    from app.splitter import tokenize

    vec = np.zeros(dim, dtype=np.float32)
    tokens = tokenize(text)
    if not tokens:
        tokens = list(text)
    for tok in tokens:
        for n in (1, 2):
            if len(tok) >= n:
                for i in range(len(tok) - n + 1):
                    gram = tok[i:i + n]
                    h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
                    vec[h % dim] += 1.0 / n
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def embed_texts(texts: Iterable[str]) -> np.ndarray:
    """批量向量化，返回 (n, dim) 的 L2 归一化矩阵。"""
    texts = list(texts)
    if not texts:
        return np.zeros((0, 1), dtype=np.float32)

    if config.MODE == "real":
        try:
            # 硅基流动批量上限 32，分批提交
            vectors: list[list[float]] = []
            batch = 16
            for i in range(0, len(texts), batch):
                resp = _get_client().embeddings.create(
                    model=config.EMBED_MODEL_NAME,
                    input=texts[i:i + batch],
                )
                vectors.extend([d.embedding for d in resp.data])
            mat = np.array(vectors, dtype=np.float32)
            logger.info("Embedding 真实调用完成：%d 条，维度 %d", mat.shape[0], mat.shape[1])
        except Exception as exc:      # 网络/额度异常自动降级，演示不中断
            logger.warning("Embedding 调用失败，降级为离线向量：%s", exc)
            mat = np.vstack([_hash_embedding(t) for t in texts])
    else:
        mat = np.vstack([_hash_embedding(t) for t in texts])

    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]
