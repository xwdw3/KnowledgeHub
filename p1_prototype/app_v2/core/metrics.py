"""监控指标：请求量、延迟分位、错误率、缓存命中、LLM 调用、业务量。

实现选择：**不引入 prometheus_client**，自行输出 Prometheus 文本格式。
理由：私有化交付要尽量减少依赖；文本格式协议极简且稳定，外加一个轻量分位计算即可满足企业监控需求。

暴露方式：
  /metrics   → Prometheus 抓取（text/plain; version=0.0.4）
  /dashboard → 内置 HTML 看板，浏览器直接看

指标设计对齐 USE/RED 方法论：
  - RED：Rate（请求速率）、Errors（错误数）、Duration（延迟分布）
  - 业务：问答量、拒答量、缓存命中率、LLM 调用与失败
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple

from app_v2.core.settings import settings

_LATENCY_WINDOW = 1000          # 保留最近 N 次请求用于分位计算（滚动窗口，内存恒定）
_MAX_PATHS = 60                 # 路径基数上限，防止恶意路径撑爆内存


class _Histogram:
    """滚动窗口分位统计。用固定长度 deque，内存占用恒定。"""

    def __init__(self, window: int = _LATENCY_WINDOW) -> None:
        self._values: Deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._values.append(value)

    def quantile(self, q: float) -> float:
        with self._lock:
            data = sorted(self._values)
        if not data:
            return 0.0
        idx = min(len(data) - 1, max(0, int(round(q * (len(data) - 1)))))
        return data[idx]

    def count(self) -> int:
        with self._lock:
            return len(self._values)

    def mean(self) -> float:
        with self._lock:
            data = list(self._values)
        return sum(data) / len(data) if data else 0.0


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        # HTTP 维度
        self.requests_total: Dict[Tuple[str, str, str], int] = defaultdict(int)   # (method, path, status)
        self.request_duration = _Histogram()
        self.path_duration: Dict[str, _Histogram] = defaultdict(_Histogram)
        self.inflight = 0
        self.slow_requests = 0
        # 业务维度
        self.chat_total = 0
        self.chat_rejected = 0
        self.chat_by_mode: Dict[str, int] = defaultdict(int)
        self.chat_cache_hit = 0
        self.retrieve_total = 0
        self.retrieve_cache_hit = 0
        self.doc_upload_total = 0
        self.doc_delete_total = 0
        self.doc_chunks_total = 0
        self.llm_calls = 0
        self.llm_failures = 0
        self.llm_latency = _Histogram()
        self.embed_calls = 0
        self.embed_latency = _Histogram()
        self.auth_failures = 0
        self.forbidden = 0
        # P3：限流拒绝次数。必须在 __init__ 里显式声明——incr() 走 setattr，
        # 属性不存在时会抛 AttributeError，而中间件里那层 try/except 会把它吞掉，
        # 结果是"限流明明触发了，指标里却永远看不到"。
        self.rate_limited = 0

    # ---------- 记录 ----------
    def observe_request(self, method: str, path: str, status: int, seconds: float) -> None:
        path = _normalize_path(path)
        ms = seconds * 1000
        with self._lock:
            self.requests_total[(method, path, str(status))] += 1
            if ms >= settings.slow_request_ms:
                self.slow_requests += 1
            hist = self.path_duration.get(path)
            if hist is None and len(self.path_duration) < _MAX_PATHS:
                hist = self.path_duration[path]
            if hist is not None:
                hist.observe(ms)
        self.request_duration.observe(ms)

    def incr(self, name: str, delta: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + delta)

    def incr_mode(self, mode: str) -> None:
        with self._lock:
            self.chat_by_mode[mode] += 1

    def observe_llm(self, ok: bool, seconds: float) -> None:
        with self._lock:
            self.llm_calls += 1
            if not ok:
                self.llm_failures += 1
        self.llm_latency.observe(seconds * 1000)

    def observe_embed(self, seconds: float) -> None:
        with self._lock:
            self.embed_calls += 1
        self.embed_latency.observe(seconds * 1000)

    # ---------- 输出 ----------
    def snapshot(self) -> dict:
        with self._lock:
            req = dict(self.requests_total)
            chats, rejected = self.chat_total, self.chat_rejected
            by_mode = dict(self.chat_by_mode)
            llm_calls, llm_fail = self.llm_calls, self.llm_failures
            embed_calls = self.embed_calls
            slow = self.slow_requests
            authf, forb = self.auth_failures, self.forbidden
            ratel = self.rate_limited
            chat_hit, ret_hit = self.chat_cache_hit, self.retrieve_cache_hit
            ret_total, up, dele = self.retrieve_total, self.doc_upload_total, self.doc_delete_total
        total = sum(req.values())
        errors = sum(c for (m, p, s), c in req.items() if s.startswith("4") or s.startswith("5"))
        return {
            "uptime_sec": int(time.time() - self.started_at),
            "requests_total": total,
            "errors_total": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "inflight": self.inflight,
            "slow_requests": slow,
            "latency_ms": {
                "p50": round(self.request_duration.quantile(0.50), 1),
                "p90": round(self.request_duration.quantile(0.90), 1),
                "p95": round(self.request_duration.quantile(0.95), 1),
                "p99": round(self.request_duration.quantile(0.99), 1),
                "avg": round(self.request_duration.mean(), 1),
                "samples": self.request_duration.count(),
            },
            "chat": {"total": chats, "rejected": rejected, "by_mode": by_mode,
                     "cache_hit": chat_hit,
                     "cache_hit_rate": round(chat_hit / chats, 4) if chats else 0.0},
            "retrieve": {"total": ret_total, "cache_hit": ret_hit,
                         "cache_hit_rate": round(ret_hit / ret_total, 4) if ret_total else 0.0},
            "documents": {"upload": up, "delete": dele},
            "llm": {"calls": llm_calls, "failures": llm_fail,
                    "failure_rate": round(llm_fail / llm_calls, 4) if llm_calls else 0.0,
                    "p95_ms": round(self.llm_latency.quantile(0.95), 1)},
            "embed": {"calls": embed_calls, "p95_ms": round(self.embed_latency.quantile(0.95), 1)},
            "security": {"auth_failures": authf, "forbidden": forb,
                         "rate_limited": ratel},
            "by_path": {f"{m} {p}": {"status": s, "count": c} for (m, p, s), c in list(req.items())[:80]},
        }

    def render_prometheus(self) -> str:
        """输出 Prometheus 文本格式。"""
        snap = self.snapshot()
        lines: List[str] = []

        def metric(name: str, mtype: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

        metric("rag_uptime_seconds", "gauge", "服务运行时长（秒）")
        lines.append(f"rag_uptime_seconds {snap['uptime_sec']}")

        metric("rag_requests_total", "gauge", "累计请求数")
        lines.append(f"rag_requests_total {snap['requests_total']}")

        metric("rag_errors_total", "gauge", "累计错误数（4xx+5xx）")
        lines.append(f"rag_errors_total {snap['errors_total']}")

        metric("rag_request_inflight", "gauge", "当前处理中的请求数")
        lines.append(f"rag_request_inflight {snap['inflight']}")

        metric("rag_slow_requests_total", "gauge", f"耗时超过 {settings.slow_request_ms}ms 的请求数")
        lines.append(f"rag_slow_requests_total {snap['slow_requests']}")

        metric("rag_request_duration_ms", "gauge", "请求耗时分位（毫秒）")
        for q in ("p50", "p90", "p95", "p99", "avg"):
            lines.append(f'rag_request_duration_ms{{quantile="{q}"}} {snap["latency_ms"][q]}')

        metric("rag_http_requests_total", "counter", "按方法/路径/状态码的请求数")
        with self._lock:
            req = dict(self.requests_total)
        for (method, path, status), count in req.items():
            lines.append(
                f'rag_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}')

        metric("rag_chat_total", "counter", "问答请求总数")
        lines.append(f"rag_chat_total {snap['chat']['total']}")

        metric("rag_chat_rejected_total", "counter", "因相关度不足被拒答的数量")
        lines.append(f"rag_chat_rejected_total {snap['chat']['rejected']}")

        metric("rag_chat_mode_total", "counter", "按模式统计的问答数")
        for mode, count in snap["chat"]["by_mode"].items():
            lines.append(f'rag_chat_mode_total{{mode="{mode}"}} {count}')

        metric("rag_chat_cache_hit_total", "counter", "问答缓存命中数")
        lines.append(f"rag_chat_cache_hit_total {snap['chat']['cache_hit']}")

        metric("rag_retrieve_total", "counter", "检索请求数")
        lines.append(f"rag_retrieve_total {snap['retrieve']['total']}")

        metric("rag_retrieve_cache_hit_total", "counter", "检索缓存命中数")
        lines.append(f"rag_retrieve_cache_hit_total {snap['retrieve']['cache_hit']}")

        metric("rag_llm_calls_total", "counter", "大模型调用次数")
        lines.append(f"rag_llm_calls_total {snap['llm']['calls']}")

        metric("rag_llm_failures_total", "counter", "大模型调用失败次数")
        lines.append(f"rag_llm_failures_total {snap['llm']['failures']}")

        metric("rag_llm_latency_ms", "gauge", "大模型调用耗时 P95（毫秒）")
        lines.append(f"rag_llm_latency_ms {snap['llm']['p95_ms']}")

        metric("rag_embed_calls_total", "counter", "向量化调用次数")
        lines.append(f"rag_embed_calls_total {snap['embed']['calls']}")

        metric("rag_auth_failures_total", "counter", "认证失败次数")
        lines.append(f"rag_auth_failures_total {snap['security']['auth_failures']}")

        metric("rag_forbidden_total", "counter", "越权被拒次数")
        lines.append(f"rag_forbidden_total {snap['security']['forbidden']}")
        metric("rag_rate_limited_total", "counter", "限流拒绝次数")
        lines.append(f"rag_rate_limited_total {snap['security']['rate_limited']}")

        metric("rag_cache_hit_rate", "gauge", "缓存命中率（L1+L2）")
        try:
            from app_v2.services.cache_service import metrics as cache_metrics
            lines.append(f"rag_cache_hit_rate {cache_metrics.snapshot()['hit_rate']}")
        except Exception:
            pass

        lines.extend(self._render_dependencies())
        lines.extend(self._render_rerank())

        metric("rag_info", "gauge", "服务信息")
        lines.append(f'rag_info{{version="{settings.app_version}",env="{settings.env}"}} 1')

        return "\n".join(lines) + "\n"

    # ---------- P3：依赖组件负载（数据库 / 向量库 / Redis）----------
    def _render_dependencies(self) -> List[str]:
        """暴露依赖组件的健康与负载。

        设计取舍：不额外部署 postgres_exporter / redis_exporter / qdrant 的
        metrics 端口，而是由应用侧实测后暴露成 rag_db_* / rag_qdrant_* / rag_redis_*。
        私有化交付少一个组件就少一个运维面，而这些探测用的都是应用已有的连接。
        代价是粒度不如专用 exporter（拿不到慢查询明细），
        对"QPS/延迟/错误率/负载"这四个监控目标已经足够。
        """
        lines: List[str] = []

        def metric(name: str, mtype: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

        # --- PostgreSQL ---
        db = {"up": 0, "pool_used": 0, "pool_free": 0, "pool_max": settings.db_pool_max,
              "latency_ms": 0.0, "documents": 0, "chunks": 0, "error_logs": 0}
        try:
            from app_v2.storage.meta_store import meta_store as store

            stats = store.pool_stats() if hasattr(store, "pool_stats") else {}
            db.update({k: v for k, v in (stats or {}).items() if k in db})
            counts = store.counts() if hasattr(store, "counts") else {}
            db.update({k: int(v or 0) for k, v in (counts or {}).items() if k in db})
            db["up"] = 1
        except Exception as exc:                       # 依赖挂了也不能让 /metrics 崩
            db["error"] = str(exc)[:120]

        metric("rag_db_up", "gauge", "业务库是否可用（1=可用）")
        lines.append(f"rag_db_up {db['up']}")
        metric("rag_db_pool_used", "gauge", "业务库连接池已用连接数")
        lines.append(f"rag_db_pool_used {db.get('pool_used', 0)}")
        metric("rag_db_pool_max", "gauge", "业务库连接池上限")
        lines.append(f"rag_db_pool_max {db.get('pool_max', settings.db_pool_max)}")
        metric("rag_db_latency_ms", "gauge", "业务库探活耗时（毫秒）")
        lines.append(f"rag_db_latency_ms {db.get('latency_ms', 0.0)}")
        metric("rag_db_documents", "gauge", "文档总数")
        lines.append(f"rag_db_documents {db.get('documents', 0)}")
        metric("rag_db_chunks", "gauge", "切片总数")
        lines.append(f"rag_db_chunks {db.get('chunks', 0)}")

        # --- 向量库 ---
        vec = {"up": 0, "points": 0, "latency_ms": 0.0}
        try:
            from app_v2.storage.vector_store import vector_store as store

            info = store.load() if hasattr(store, "load") else {}
            vec.update({k: v for k, v in (info or {}).items() if k in vec})
        except Exception as exc:
            vec["error"] = str(exc)[:120]

        metric("rag_qdrant_up", "gauge", "向量库是否可用（1=可用）")
        lines.append(f"rag_qdrant_up {vec.get('up', 0)}")
        metric("rag_qdrant_points", "gauge", "向量库中的向量点数")
        lines.append(f"rag_qdrant_points {vec.get('points', 0)}")
        metric("rag_qdrant_latency_ms", "gauge", "向量库探活耗时（毫秒）")
        lines.append(f"rag_qdrant_latency_ms {vec.get('latency_ms', 0.0)}")

        # --- Redis ---
        redis = {"up": 0}
        try:
            from app_v2.services.cache_service import cache as two_level_cache

            redis = two_level_cache.l2.load()
        except Exception as exc:
            redis["error"] = str(exc)[:120]

        metric("rag_redis_up", "gauge", "Redis 是否可用（1=可用）")
        lines.append(f"rag_redis_up {redis.get('up', 0)}")
        if redis.get("up"):
            metric("rag_redis_used_memory_bytes", "gauge", "Redis 已用内存（字节）")
            lines.append(f"rag_redis_used_memory_bytes {redis.get('used_memory_bytes', 0)}")
            metric("rag_redis_maxmemory_bytes", "gauge", "Redis 内存上限（字节）")
            lines.append(f"rag_redis_maxmemory_bytes {redis.get('maxmemory_bytes', 0)}")
            metric("rag_redis_used_ratio", "gauge", "Redis 内存使用率")
            lines.append(f"rag_redis_used_ratio {redis.get('used_ratio', 0.0)}")
            metric("rag_redis_keys", "gauge", "Redis 键总数")
            lines.append(f"rag_redis_keys {redis.get('keys', 0)}")
            metric("rag_redis_evicted_total", "counter", "Redis 因内存上限淘汰的键数")
            lines.append(f"rag_redis_evicted_total {redis.get('evicted', 0)}")
        else:
            metric("rag_redis_used_memory_bytes", "gauge", "Redis 已用内存（字节）")
            lines.append("rag_redis_used_memory_bytes 0")
            metric("rag_redis_used_ratio", "gauge", "Redis 内存使用率")
            lines.append("rag_redis_used_ratio 0")

        return lines

    def _render_rerank(self) -> List[str]:
        """Rerank 效果指标：重排是否生效、降级了多少次。

        没有这几个数字，RERANK_MODE=llm 在线上到底是"在重排"还是"每轮都超时降级"
        完全看不出来——而这两种情况的检索质量差别很大。
        """
        lines: List[str] = []

        def metric(name: str, mtype: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

        try:
            from app_v2.services import retrieval_service
            stats = retrieval_service.rerank_stats()
        except Exception:
            stats = {}

        metric("rag_rerank_total", "counter", "重排执行次数（按模式）")
        for mode, count in (stats.get("by_mode") or {}).items():
            lines.append(f'rag_rerank_total{{mode="{mode}"}} {count}')
        metric("rag_rerank_fallback_total", "counter", "重排降级为启发式的次数")
        lines.append(f"rag_rerank_fallback_total {stats.get('fallback', 0)}")
        metric("rag_rerank_latency_ms", "gauge", "重排耗时 P95（毫秒）")
        lines.append(f"rag_rerank_latency_ms {stats.get('p95_ms', 0.0)}")
        return lines


def _normalize_path(path: str) -> str:
    """把路径参数归一化，避免 /documents/abc、/documents/def 撑爆维度。"""
    parts = path.split("/")
    out = []
    for seg in parts:
        if not seg:
            continue
        # 16~40 位十六进制/混合串视为 ID
        if len(seg) >= 12 and all(c.isalnum() or c in "-_" for c in seg):
            out.append("{id}")
        else:
            out.append(seg)
    return "/" + "/".join(out)


metrics_registry = MetricsRegistry()
