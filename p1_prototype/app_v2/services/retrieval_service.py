"""检索服务：向量召回（Qdrant）+ BM25 关键词召回 → RRF 融合 → Rerank 重排。

P3 演进点：
  1. **空间隔离下推**：向量召回与 BM25 语料都按可访问空间过滤，
     不是召回后再筛——后者会让 top_k 名额被无权片段占掉，召回质量直接掉。
  2. **Rerank 重排**：RRF 只反映"两路名次"，不理解语义匹配度。
     对融合后的前 N 条做一次重排打分，能把"字面命中但答非所问"的片段压下去。
  3. **可降级**：llm 重排超时/报错/离线时自动退回本地启发式，链路不断。

Rerank 三种模式：
  off        不重排（等价 P2 行为，用于 A/B 对照）
  heuristic  本地打分：查询词覆盖 + 向量分 + RRF 名次，零成本零延迟
  llm        调 chat 模型批量打分（一次调用返回 JSON），失败降级为 heuristic
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque

import numpy as np

from app_v2.core.settings import settings
from app_v2.core.security import spaces_wildcard
from app_v2.services.embedder import embed_query
from app_v2.services.splitter import tokenize
from app_v2.services.tenant_service import current_tenant
from app_v2.storage.vector_store import vector_store

logger = logging.getLogger(__name__)

# ---------------- BM25 语料 ----------------
# 说明：每次检索实时 scroll 会导致语料随库增长而变慢，
# 这里加轻量缓存，文档变更时由 ingest 服务调用 invalidate() 主动失效。
_cache: dict[str, object] = {"key": "", "docs": []}
_lock = threading.Lock()
CACHE_TTL = 30.0     # 秒，兜底过期
# 全局语料版本号：文档变更时递增，使所有租户的缓存一起失效
_GLOBAL_CORPUS_VERSION = 0


def invalidate() -> None:
    global _GLOBAL_CORPUS_VERSION
    with _lock:
        _GLOBAL_CORPUS_VERSION += 1
        _cache["key"] = ""


def _corpus_key() -> str:
    """语料缓存键。带上租户维度，否则 A 租户上传文档会让 B 租户也重建语料，
    更糟的是把 A 的语料缓存给 B 用——那等于绕过了向量层的租户过滤。
    """
    return f"{current_tenant()}:{_GLOBAL_CORPUS_VERSION}"


def _load_bm25_candidates(tenant_id: str = "") -> list[dict]:
    """加载 BM25 语料（带缓存）。

    P1 简化：用向量库 scroll 取最多 2000 条片段构建语料。
    规模上来后应改为独立全文索引（PG tsvector / Elasticsearch），
    或直接在入库时把分词结果落到业务库，避免全量拉取。
    """
    key = _corpus_key()
    with _lock:
        if _cache["key"] == key and _cache["docs"]:
            return list(_cache["docs"])        # type: ignore[arg-type]

    docs: list[dict] = []
    if getattr(vector_store, "backend", "") == "qdrant":
        try:
            # scroll 阶段就按租户过滤：BM25 语料只装本租户的片段。
            # 若全量拉再在内存里过滤，等于每次检索都把别家的正文加载进本进程，
            # 既浪费内存也有越界风险（任何一处忘过过滤就是数据泄露）。
            from qdrant_client import models
            points, _ = vector_store.client.scroll(          # type: ignore[attr-defined]
                collection_name=settings.collection_name,
                scroll_filter=models.Filter(must=[
                    models.FieldCondition(key="tenant_id",
                                          match=models.MatchValue(value=tenant_id or "")),
                ]),
                limit=2000,
                with_payload=True,
                with_vectors=False,
            )
            docs = [{"id": str(p.id), **(p.payload or {})} for p in points]
        except Exception as exc:
            logger.warning("BM25 语料拉取失败：%s", exc)
    else:   # 本地降级实现
        ids = getattr(vector_store, "ids", [])
        payloads = getattr(vector_store, "payloads", [])
        docs = [{"id": i, **p} for i, p in zip(ids, payloads)
                if (p or {}).get("tenant_id", "") == (tenant_id or "")]

    with _lock:
        _cache["key"] = key
        _cache["docs"] = docs
    return docs


def _bm25_scores(query_tokens: list[str], docs: list[dict], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    n = len(docs)
    scores = np.zeros(n, dtype=np.float32)
    if n == 0 or not query_tokens:
        return scores
    tokenized = [tokenize(d.get("text", "")) for d in docs]
    avgdl = sum(len(t) for t in tokenized) / n or 1.0
    df: dict[str, int] = {}
    for toks in tokenized:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    for i, toks in enumerate(tokenized):
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks) or 1
        s = 0.0
        for q in query_tokens:
            f = tf.get(q, 0)
            if not f:
                continue
            idf = np.log(1 + (n - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        scores[i] = s
    return scores


def search(query: str, perm_groups: list[str], top_k: int | None = None,
           spaces: list[str] | None = None, rerank: bool | None = None,
           tenant_id: str | None = None) -> dict:
    """执行混合检索 + 重排，返回 {hits, timings, rerank}。

    tenant_id 不传时取请求级上下文（current_tenant），调用方通常在路由里
    已经 set 过；显式传入用于后台任务等无请求上下文的场景。
    """
    tenant = (tenant_id if tenant_id is not None else current_tenant()) or ""
    top_k = top_k or settings.final_top_k
    t0 = time.perf_counter()

    q_vec = embed_query(query)
    t_embed = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    vec_hits = vector_store.search(q_vec, settings.recall_k, perm_groups, spaces,
                                   tenant_id=tenant)
    t_vec = (time.perf_counter() - t1) * 1000

    space_ok = None if spaces_wildcard(spaces) else set(spaces)

    fused: dict[str, dict] = {}
    for rank, h in enumerate(vec_hits, start=1):
        if h["score"] <= 0:
            continue
        fused[h["id"]] = {
            "chunk_id": h["id"],
            "score": 1.0 / (settings.rrf_k + rank),
            "vec_rank": rank,
            "bm25_rank": None,
            "vec_score": round(h["score"], 4),
            "bm25_score": 0.0,
            "payload": h["payload"],
        }

    t2 = time.perf_counter()
    if settings.enable_bm25:
        docs = _load_bm25_candidates(tenant)
        allowed = [
            d for d in docs
            # 三重过滤顺序：租户 -> 空间 -> 权限组。
            # 顺序影响正确性感知：租户不匹配必须先短路，避免同 id 片段串味。
            if d.get("tenant_id", "") == tenant
            and d.get("perm") in perm_groups
            and (space_ok is None or d.get("space_id", "default") in space_ok)
        ]
        if allowed:
            scores = _bm25_scores(tokenize(query), allowed)
            order = np.argsort(-scores)[: settings.recall_k]
            for rank, i in enumerate(order, start=1):
                if scores[i] <= 0:
                    continue
                d = allowed[int(i)]
                item = fused.setdefault(str(d["id"]), {
                    "chunk_id": str(d["id"]),
                    "score": 0.0,
                    "vec_rank": None,
                    "bm25_rank": None,
                    "vec_score": 0.0,
                    "bm25_score": 0.0,
                    "payload": d,
                })
                item["score"] += 1.0 / (settings.rrf_k + rank)
                item["bm25_rank"] = rank
                item["bm25_score"] = round(float(scores[i]), 4)
    t_bm25 = (time.perf_counter() - t2) * 1000

    ranked = sorted(fused.values(), key=lambda x: -x["score"])

    # ---------- Rerank ----------
    t3 = time.perf_counter()
    rerank_info = {"mode": "off", "applied": False, "fallback": None, "candidates": 0}
    want_rerank = settings.rerank_mode != "off" if rerank is None else rerank
    if want_rerank and len(ranked) > 1:
        candidates = ranked[: max(settings.rerank_candidates, top_k)]
        rerank_info["candidates"] = len(candidates)
        ordered, rerank_info = _rerank(query, candidates, rerank_info)
        # 补回未参与重排的尾部候选（保持相对顺序）
        if len(ordered) < len(ranked):
            ordered += [x for x in ranked if x not in ordered]
        ranked = ordered
    t_rerank = (time.perf_counter() - t3) * 1000

    final = ranked[:top_k]
    hits = []
    for pos, item in enumerate(final, start=1):
        p = item["payload"]
        hits.append({
            "chunk_id": item["chunk_id"],
            "file_name": p.get("file_name", ""),
            "doc_id": p.get("doc_id", ""),
            "space_id": p.get("space_id", settings.default_space_id),
            "seq": p.get("seq", 0),
            "perm": p.get("perm", "all"),
            "text": p.get("text", ""),
            "vec_score": item["vec_score"],
            "bm25_score": item["bm25_score"],
            "rrf_score": round(item["score"], 5),
            "vec_rank": item["vec_rank"],
            "bm25_rank": item["bm25_rank"],
            "rerank_score": item.get("rerank_score"),
            "final_rank": pos,
        })

    return {
        "hits": hits,
        "rerank": rerank_info,
        "timings": {
            "embed_ms": round(t_embed),
            "vector_ms": round(t_vec),
            "bm25_ms": round(t_bm25),
            "rerank_ms": round(t_rerank),
            "total_ms": round((time.perf_counter() - t0) * 1000),
        },
    }


# ---------------- Rerank 实现 ----------------
# 重排指标：进程内累计，供 /metrics 暴露。
# 为什么需要：RERANK_MODE=llm 时，线上究竟是"真的在重排"还是"每次都超时降级"，
# 从响应体里看不出来（降级后 mode 会变成 heuristic，但没人会逐条翻日志）。
_rerank_lock = threading.Lock()
_rerank_by_mode: dict[str, int] = {}
_rerank_fallback = 0
_rerank_latency: deque = deque(maxlen=200)


def rerank_stats() -> dict:
    """重排运行状态快照。"""
    with _rerank_lock:
        by_mode = dict(_rerank_by_mode)
        fallback = _rerank_fallback
        lat = sorted(_rerank_latency)
    p95 = 0.0
    if lat:
        p95 = round(lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))], 1)
    return {"by_mode": by_mode, "fallback": fallback, "p95_ms": p95}


def _record_rerank(mode: str, fallback: bool, ms: float) -> None:
    global _rerank_fallback
    with _rerank_lock:
        _rerank_by_mode[mode] = _rerank_by_mode.get(mode, 0) + 1
        if fallback:
            _rerank_fallback += 1
        _rerank_latency.append(ms)


def _rerank(query: str, candidates: list[dict], info: dict) -> tuple[list[dict], dict]:
    mode = settings.rerank_mode
    start = time.perf_counter()
    used_mode, fallback = mode, False
    if mode == "llm":
        try:
            ordered = _llm_rerank(query, candidates)
            if ordered:
                info.update({"mode": "llm", "applied": True})
                _record_rerank("llm", False, (time.perf_counter() - start) * 1000)
                return ordered, info
            info["fallback"] = "llm 返回为空"
        except Exception as exc:
            logger.warning("LLM 重排失败，降级为启发式：%s", exc)
            info["fallback"] = f"llm 失败：{type(exc).__name__}"
        fallback = True
        used_mode = "heuristic"
    ordered = _heuristic_rerank(query, candidates)
    info.update({"mode": "heuristic" if mode != "off" else "off",
                 "applied": mode != "off"})
    _record_rerank(used_mode, fallback, (time.perf_counter() - start) * 1000)
    return ordered, info


def _heuristic_rerank(query: str, candidates: list[dict]) -> list[dict]:
    """本地启发式：查询词覆盖率为主，向量分为辅，RRF 名次做微调。

    设计意图：RRF 分只在 1/(60+rank) 量级，几乎不携带"匹配强度"信息，
    对中文长文档尤其明显——一个只命中一个虚词的片段也可能排在前面。
    覆盖率能把"关键词真正出现在片段里"的片段顶上来。
    """
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return candidates
    for item in candidates:
        tokens = set(tokenize(item["payload"].get("text", "")))
        cover = len(q_tokens & tokens) / len(q_tokens)
        vec = max(0.0, min(item.get("vec_score") or 0.0, 1.0))
        rrf = item.get("score") or 0.0
        item["rerank_score"] = round(0.6 * cover + 0.3 * vec + 0.1 * min(rrf * 60, 1.0), 4)
    return sorted(candidates, key=lambda x: -x.get("rerank_score", 0))


RERANK_PROMPT = """你是检索结果排序器。给定用户问题和若干候选片段，请判断每个片段对回答该问题的有用程度。

评分标准（0-10 整数）：
- 10：直接包含问题的答案
- 7-9：包含回答所需的关键信息
- 4-6：主题相关但缺少关键信息
- 1-3：仅字面重合，实际无关
- 0：完全无关

只输出 JSON 数组，格式：[{"id": 1, "score": 8}, ...]，不要输出任何解释或多余文字。
必须为每一个候选都给出分数，id 用给定的序号。"""


def _llm_rerank(query: str, candidates: list[dict]) -> list[dict]:
    """调 chat 模型批量打分。一次调用评完所有候选，避免 N 次往返。"""
    from app_v2.services import llm

    if not llm.is_available():
        raise RuntimeError("模型不可用")

    lines = [f"用户问题：{query}", "", "候选片段："]
    for i, item in enumerate(candidates, start=1):
        text = re.sub(r"\s+", " ", item["payload"].get("text", ""))[:400]
        lines.append(f"[{i}] {text}")
    messages = [
        {"role": "system", "content": RERANK_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
    out = llm.chat(messages, temperature=0.0)
    scores = _parse_score_json(out.get("content", ""), len(candidates))

    for i, item in enumerate(candidates, start=1):
        s = scores.get(i)
        # 用 0-10 归一到 0-1
        item["rerank_score"] = round((s if s is not None else 5.0) / 10.0, 4)

    # 并列破除：LLM 打分是 0-10 的整数，候选稍多就必然出现并列（实测出现过
    # 两篇都打 10 分），此时若只按 rerank_score 排，并列项的相对顺序完全取决于
    # sorted 的稳定性 + 输入顺序，等价于"没重排"。
    # 解法是二级排序键：先比 rerank_score，再比 RRF 名次（融合了向量与 BM25 两路证据）。
    # 这样"分数相同"时仍然有依据可循，而不是退化成随机。
    for idx, item in enumerate(candidates):
        item["_orig_rank"] = idx
    return sorted(
        candidates,
        key=lambda x: (-(x.get("rerank_score") or 0), x.get("_orig_rank", 0)),
    )


def _parse_score_json(text: str, n: int) -> dict[int, float]:
    """从模型输出里抠出 JSON 数组。模型偶尔会包 ```json 或加说明文字。"""
    raw = (text or "").strip()
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        # 兜底：逐行匹配 {"id": 1, "score": 8}
        pairs = re.findall(r'"?id"?\s*[:=]\s*(\d+)\s*,\s*"?(?:score|分)"?\s*[:=]\s*([\d.]+)', raw)
        if not pairs:
            raise ValueError("模型输出中未找到分数 JSON")
        return {int(i): float(s) for i, s in pairs if 1 <= int(i) <= n}

    data = json.loads(match.group(0))
    result: dict[int, float] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("id"))
            score = float(entry.get("score"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= n:
            result[idx] = max(0.0, min(score, 10.0))
    if not result:
        raise ValueError("模型输出的分数为空")
    return result


def relevance(hits: list[dict], query: str) -> float:
    """相关度 0~1：向量余弦 + BM25 绝对分 + 查询词覆盖，低覆盖压分。

    不使用 RRF 分判断：RRF 只反映名次，库外问题也能拿到较高名次分。
    """
    if not hits:
        return 0.0
    top = hits[0]
    q_tokens = set(tokenize(query))
    cover = len(q_tokens & set(tokenize(top["text"]))) / len(q_tokens) if q_tokens else 0.0
    vec_norm = max(0.0, min(top["vec_score"], 1.0))
    bm25_norm = min(top["bm25_score"] / 8.0, 1.0)
    score = 0.45 * vec_norm + 0.35 * bm25_norm + 0.20 * cover
    if cover < 0.30:
        score *= 0.6
    return round(score, 4)
