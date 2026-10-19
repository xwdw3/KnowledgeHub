"""Embedding 服务：真实调用 BGE-M3；无密钥时降级为本地哈希向量。

维度始终与 settings.embed_dim 一致，避免"索引 1024 维、查询 256 维"导致向量召回被跳过。
"""
from __future__ import annotations

import logging
import time
import zlib

import numpy as np

from app_v2.core.response import UpstreamError
from app_v2.core.settings import settings
from app_v2.services.splitter import tokenize

logger = logging.getLogger(__name__)

_client = None


def _client_or_none():
    global _client
    if settings.resolved_model_mode() != "real":
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=settings.embed_api_key, base_url=settings.embed_base_url)
    return _client


# 离线向量算法的版本标识，写进本地索引元数据。
# 换算法时必须改这个值——它唯一的作用就是让"旧索引 + 新查询"这种
# 静默错配变得可发现。
#   历史值：md5-1/2gram-search（md5 + 全模式分词）
#   当前值：crc32-1/2gram-cut  （crc32 + 精确模式分词）
HASH_SCHEME = "crc32-1/2gram-cut"

# 离线向量用的停用词。比 BM25 的停用词表短：
# 哈希向量靠"方向相似"工作，虚词带来的噪声本来就被大量实词稀释，
# 过滤得太狠反而让短文本向量变得过于稀疏。
_HASH_STOPWORDS = frozenset({
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "它", "这", "那",
    "吗", "呢", "吧", "与", "及", "或", "把", "被", "对", "为", "以",
})


def _hash_tokens(text: str) -> list[str]:
    """离线向量专用的轻量分词。

    与 BM25 用的 `tokenize()` 分开是刻意取舍：
      - BM25 要召回率，用 `lcut_for_search`（全模式，长词切出所有子词），
        代价是 token 数翻几倍。它只对**查询**跑一次，可以尽情追求质量。
      - 离线向量要对**每篇文档的每一片**跑，同一份 16MB 文档就是 1.3 万次。
        全模式在这里实测要 56 秒，而它带来的召回提升
        对"哈希向量"这种本来就粗糙的兜底方案毫无意义。
    精确模式（cut）的 token 数约为全模式的 1/3，语义单元完整，
    足够让相同主题的文本落在相近的方向上。
    """
    import jieba

    out: list[str] = []
    for t in jieba.cut(text.lower()):
        t = t.strip()
        if t and t not in _HASH_STOPWORDS and not t.isspace():
            out.append(t)
    return out


def _hash_embedding(text: str, dim: int) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    tokens = _hash_tokens(text) or list(text)
    for tok in tokens:
        for n in (1, 2):
            for i in range(max(len(tok) - n + 1, 0)):
                gram = tok[i:i + n]
                # 用 crc32 而不是 md5：
                #   - md5 要构造 hashlib 对象再 hexdigest() 转字符串再转 int，
                #     每 gram 三次转换。实测 16MB 文档（13701 片）光这一项就 67 秒。
                #   - crc32 在 C 层一次算完，快一个数量级。
                #
                # 为什么**不能**用内置 hash()（更快）：Python 3 的 str hash
                # 带 PYTHONHASHSEED 随机化，同一份文本在不同进程里算出不同值。
                # 后果是进程重启后旧向量全部错位，检索 silently 变差——
                # 没有报错、没有异常，只是"答案越来越不准"，极难定位。
                # crc32 跨进程、跨平台结果稳定，这里必须选它。
                vec[zlib.crc32(gram.encode("utf-8")) % dim] += 1.0 / n
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def embed_texts(texts: list[str], batch_size: int = 16) -> np.ndarray:
    """批量向量化，返回 (n, dim) 的 L2 归一化矩阵。"""
    if not texts:
        return np.zeros((0, settings.embed_dim), dtype=np.float32)

    client = _client_or_none()
    if client is not None:
        _t0 = time.perf_counter()
        try:
            vectors: list[list[float]] = []
            for i in range(0, len(texts), batch_size):
                resp = client.embeddings.create(model=settings.embed_model, input=texts[i:i + batch_size])
                vectors.extend(d.embedding for d in resp.data)
            mat = np.asarray(vectors, dtype=np.float32)
            if mat.shape[1] != settings.embed_dim:
                raise UpstreamError(f"向量维度 {mat.shape[1]} 与配置 {settings.embed_dim} 不一致")
            return _normalize(mat)
        except UpstreamError:
            raise
        except Exception as exc:
            logger.warning("Embedding 调用失败，降级本地向量：%s", exc)
        finally:
            try:
                from app_v2.core.metrics import metrics_registry
                metrics_registry.observe_embed(time.perf_counter() - _t0)
            except Exception:
                pass

    return _normalize(np.vstack([_hash_embedding(t, settings.embed_dim) for t in texts]))


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0].tolist()


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms
