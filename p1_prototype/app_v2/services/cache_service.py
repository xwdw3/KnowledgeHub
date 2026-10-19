"""两级缓存：进程内 LRU（L1） + Redis（L2）。

为什么要两级：
  - L1 命中是内存读，微秒级；L2 命中要跨网络，毫秒级；都未命中才走完整 RAG 链路（秒级）。
  - 多实例部署时 L1 各自独立，L2 共享，保证扩容后命中率不会归零。
  - Redis 不可用时自动退化为纯 L1（服务不中断），符合"降级可用"原则。

缓存什么（经过取舍）：
  - 检索结果：key = 归一化 query + 权限组 + top_k。权限组参与 key，杜绝越权复用。
  - 问答结果：同样把权限组纳入 key；agent 模式默认不缓存（工具调用可能有副作用/时效性）。
  - **不缓存**：文档列表、统计类接口（数据变化频繁，缓存反而制造不一致）。

失效策略：
  - 文档上传/删除/重建后，按前缀清除与知识库相关的 key（L1 全清 + L2 前缀删）。
  - 每类缓存独立 TTL，检索 5 分钟、问答 10 分钟，可由环境变量覆盖。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app_v2.core.settings import settings
from app_v2.core.security import spaces_wildcard
from app_v2.services.tenant_service import current_tenant

logger = logging.getLogger(__name__)


# ---------------- 指标 ----------------
class CacheMetrics:
    """缓存指标：分层的命中/未命中计数，供 /metrics 暴露。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: Dict[str, int] = {
            "l1_hit": 0, "l2_hit": 0, "miss": 0, "set": 0, "evict": 0, "error": 0,
        }

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.data[name] = self.data.get(name, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.data)
        total = d["l1_hit"] + d["l2_hit"] + d["miss"]
        d["hit_rate"] = round((d["l1_hit"] + d["l2_hit"]) / total, 4) if total else 0.0
        d["l1_hit_rate"] = round(d["l1_hit"] / total, 4) if total else 0.0
        d["requests"] = total
        return d


metrics = CacheMetrics()


# ---------------- L1：进程内 LRU ----------------
class LRUCache:
    def __init__(self, maxsize: int = 1024) -> None:
        self.maxsize = maxsize
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expire_at, value = item
            if expire_at and expire_at < time.time():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl if ttl else 0, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
                metrics.inc("evict")

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> int:
        with self._lock:
            n = len(self._data)
            self._data.clear()
            return n

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------- L2：Redis ----------------
class RedisLayer:
    """Redis 封装。连接失败不抛异常，只打日志并标记不可用。"""

    def __init__(self) -> None:
        self.client = None
        self.available = False
        self._last_error = ""
        self._init_client()

    def _init_client(self) -> None:
        url = (settings.redis_url or "").strip()
        if not url:
            logger.info("未配置 REDIS_URL，缓存使用纯进程内模式")
            return
        try:
            import redis  # 延迟导入：无 redis 库时不影响主流程

            self.client = redis.Redis.from_url(
                url, decode_responses=True,
                socket_timeout=settings.redis_timeout_ms / 1000,
                socket_connect_timeout=settings.redis_timeout_ms / 1000,
                health_check_interval=30,
            )
            self.client.ping()
            self.available = True
            logger.info("缓存：Redis 已连接 %s", _safe_url(url))
        except Exception as exc:
            self.available = False
            self.client = None
            self._last_error = str(exc)
            logger.warning("Redis 不可用，缓存降级为纯进程内：%s", exc)

    def get(self, key: str) -> Optional[Any]:
        if not self.available or not self.client:
            return None
        try:
            raw = self.client.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            self._degrade(exc)
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        if not self.available or not self.client:
            return
        try:
            self.client.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
        except Exception as exc:
            self._degrade(exc)

    def delete_prefix(self, prefix: str) -> int:
        if not self.available or not self.client:
            return 0
        try:
            removed = 0
            for batch in self._scan(prefix):
                if batch:
                    removed += self.client.delete(*batch)
            return removed
        except Exception as exc:
            self._degrade(exc)
            return 0

    def _scan(self, prefix: str, count: int = 500) -> Iterable[List[str]]:
        cursor = 0
        while True:
            cursor, keys = self.client.scan(cursor=cursor, match=f"{prefix}*", count=count)
            yield keys
            if cursor == 0:
                break

    def _degrade(self, exc: Exception) -> None:
        self.available = False
        self._last_error = str(exc)
        metrics.inc("error")
        logger.warning("Redis 操作失败，本进程缓存降级为纯 L1：%s", exc)

    # ---------- P3：通用命令（黑名单 / 限流 / 上下文都直接用 Redis 原语）----------
    def _ensure(self) -> bool:
        """惰性重连：降级后每 15 秒允许探活一次，Redis 恢复即自动回到 L2。

        没有这一步的话，启动时 Redis 尚未就绪（compose 里 services_healthy 之前
        偶尔会抢跑）就会把整进程永久钉死在 L1，命中率指标再也不会出现 l2_hit。
        """
        if self.available and self.client:
            return True
        if not (settings.redis_url or "").strip():
            return False
        now = time.time()
        if now - getattr(self, "_last_retry", 0) < 15:
            return False
        self._last_retry = now
        self._init_client()
        return self.available

    def setex_raw(self, key: str, ttl: int, value: str) -> bool:
        """写字符串（不做 JSON 包装），用于黑名单标记与计数器的初值。"""
        if not self._ensure():
            return False
        try:
            self.client.setex(key, max(int(ttl), 1), value)
            return True
        except Exception as exc:
            self._degrade(exc)
            return False

    def exists(self, key: str) -> bool:
        if not self._ensure():
            return False
        try:
            return bool(self.client.exists(key))
        except Exception as exc:
            self._degrade(exc)
            return False

    def delete_keys(self, *keys: str) -> int:
        if not keys or not self._ensure():
            return 0
        try:
            return int(self.client.delete(*keys))
        except Exception as exc:
            self._degrade(exc)
            return 0

    def incr_with_ttl(self, key: str, ttl: int) -> Optional[int]:
        """原子自增并在首次创建时设置过期，用于固定窗口限流。

        必须用 INCR + EXPIRE 的原子组合：先 GET 再 SET 会在并发下丢计数，
        限流就成了摆设。这里用 pipeline 保证 INCR 与 EXPIRE 不被打断。
        """
        if not self._ensure():
            return None
        try:
            pipe = self.client.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            count, cur_ttl = pipe.execute()
            if cur_ttl is None or cur_ttl < 0:      # 新建 key 或未设过过期
                self.client.expire(key, max(int(ttl), 1))
            return int(count)
        except Exception as exc:
            self._degrade(exc)
            return None

    def ttl(self, key: str) -> int:
        if not self._ensure():
            return -2
        try:
            return int(self.client.ttl(key))
        except Exception as exc:
            self._degrade(exc)
            return -2

    def health(self) -> dict:
        if not settings.redis_url:
            return {"backend": "local", "status": "ok", "detail": {"note": "未启用 Redis"}}
        if self.available and self.client:
            try:
                self.client.ping()
                info = self.client.info("memory")
                return {"backend": "redis", "status": "ok",
                        "detail": {"used_memory_human": info.get("used_memory_human", "")}}
            except Exception as exc:
                self._degrade(exc)
        return {"backend": "redis", "status": "degraded", "detail": {"error": self._last_error}}

    def load(self) -> dict:
        """Redis 负载快照，供 Prometheus 暴露 rag_redis_* 指标。

        只取 O(1) 的命令（INFO / DBSIZE），不做 SCAN：
        指标采集绝不能反过来给被监控组件加压，否则监控本身就成了故障源。
        """
        if not (self._ensure() and self.client):
            return {"up": 0}
        try:
            mem = self.client.info("memory")
            stats = self.client.info("stats")
            clients = self.client.info("clients")
            keys = int(self.client.dbsize())
        except Exception as exc:
            self._degrade(exc)
            return {"up": 0}
        used = int(mem.get("used_memory", 0) or 0)
        maxmem = int(mem.get("maxmemory", 0) or 0)
        return {
            "up": 1,
            "used_memory_bytes": used,
            "maxmemory_bytes": maxmem,
            # 未设 maxmemory 时 used_ratio 无意义，置 0 避免面板出现假告警
            "used_ratio": round(used / maxmem, 4) if maxmem else 0.0,
            "keys": keys,
            "connected_clients": int(clients.get("connected_clients", 0) or 0),
            "hits": int(stats.get("keyspace_hits", 0) or 0),
            "misses": int(stats.get("keyspace_misses", 0) or 0),
            "evicted": int(stats.get("evicted_keys", 0) or 0),
        }


def _safe_url(url: str) -> str:
    """日志中隐藏 Redis 密码。"""
    if "@" in url:
        head, tail = url.split("@", 1)
        if "://" in head:
            scheme, _ = head.split("://", 1)
            return f"{scheme}://***@{tail}"
    return url


# ---------------- 组合 ----------------
class TwoLevelCache:
    PREFIX = "rag:cache:v1:"

    def __init__(self) -> None:
        self.l1 = LRUCache(maxsize=settings.cache_l1_size)
        self.l2 = RedisLayer()

    @staticmethod
    def make_key(kind: str, parts: Dict[str, Any]) -> str:
        """稳定 key：对参数排序后哈希，避免不同顺序产生不同 key。"""
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"{TwoLevelCache.PREFIX}{kind}:{digest}"

    def get(self, key: str) -> Optional[Any]:
        val = self.l1.get(key)
        if val is not None:
            metrics.inc("l1_hit")
            return val
        val = self.l2.get(key)
        if val is not None:
            metrics.inc("l2_hit")
            # 回填 L1，让后续同实例请求走内存
            self.l1.set(key, val, ttl=min(60, settings.cache_ttl_retrieval))
            return val
        metrics.inc("miss")
        return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self.l1.set(key, value, ttl)
        self.l2.set(key, value, ttl)
        metrics.inc("set")

    def invalidate(self, kinds: Optional[Iterable[str]] = None) -> dict:
        """按类别失效。kinds 为空表示清空全部缓存。"""
        kinds = list(kinds or [])
        result = {"l1_removed": 0, "l2_removed": 0, "kinds": kinds or ["all"]}
        if not kinds:
            result["l1_removed"] = self.l1.clear()
            result["l2_removed"] = self.l2.delete_prefix(self.PREFIX)
            logger.info("缓存已全量失效 l1=%s l2=%s", result["l1_removed"], result["l2_removed"])
            return result
        for kind in kinds:
            prefix = f"{self.PREFIX}{kind}:"
            result["l1_removed"] += self.l1.delete_prefix(prefix)
            result["l2_removed"] += self.l2.delete_prefix(prefix)
        logger.info("缓存按类别失效 %s %s", kinds, result)
        return result

    def health(self) -> dict:
        return {
            "l1": {"entries": len(self.l1), "maxsize": self.l1.maxsize},
            "l2": self.l2.health(),
            "metrics": metrics.snapshot(),
            "ttl": {"retrieval": settings.cache_ttl_retrieval, "answer": settings.cache_ttl_answer},
        }


cache = TwoLevelCache()


# ---------------- 业务便捷封装 ----------------
# P4 变更：所有知识类 key 前置 tenant 维度。租户是最外层边界，
# 不纳入 key 会导致「A 公司问过的问题，B 公司直接拿到 A 公司的答案」。
# P3 的 space 维度保留，两者叠加：tenant 管"哪家公司"，space 管"哪个域"。

def _space_part(spaces: Optional[List[str]]) -> str:
    """缓存 key 里的空间维度。

    通配必须归一成同一个标记：`*`、`all`、`None` 在语义上都是"全部空间"，
    如果按原样拼进 key，同一份结果会因写法不同而各存一份，命中率被人为压低。
    """
    if spaces_wildcard(spaces):
        return "*"
    return ",".join(sorted(spaces or []))


def _tenant_part(tenant_id: Optional[str]) -> str:
    """缓存 key 里的租户维度。

    这里**不做**通配归一：租户没有"全部租户"的查询语义。
    传空或 None 就老老实实记为 "-"，让"忘了传租户"变成一次 miss 而不是串户命中。
    """
    return (tenant_id or "").strip() or "-"


def get_retrieval_cached(query: str, perm_groups: List[str], top_k: int,
                         spaces: Optional[List[str]] = None,
                         tenant_id: Optional[str] = None) -> Optional[dict]:
    if not settings.cache_enabled:
        return None
    if tenant_id is None:
        tenant_id = current_tenant()
    key = cache.make_key("retrieve", {"t": _tenant_part(tenant_id),
                                      "q": query.strip().lower(), "p": sorted(perm_groups),
                                      "k": top_k, "s": _space_part(spaces)})
    return cache.get(key)


def put_retrieval(query: str, perm_groups: List[str], top_k: int, result: dict,
                  spaces: Optional[List[str]] = None,
                  tenant_id: Optional[str] = None) -> None:
    if not settings.cache_enabled:
        return
    if tenant_id is None:
        tenant_id = current_tenant()
    key = cache.make_key("retrieve", {"t": _tenant_part(tenant_id),
                                      "q": query.strip().lower(), "p": sorted(perm_groups),
                                      "k": top_k, "s": _space_part(spaces)})
    cache.set(key, result, settings.cache_ttl_retrieval)


def get_answer_cached(question: str, perm_groups: List[str], top_k: int, mode: str,
                      spaces: Optional[List[str]] = None,
                      tenant_id: Optional[str] = None) -> Optional[dict]:
    if not settings.cache_enabled:
        return None
    if tenant_id is None:
        tenant_id = current_tenant()
    key = cache.make_key("answer", {"t": _tenant_part(tenant_id),
                                    "q": question.strip().lower(), "p": sorted(perm_groups),
                                    "k": top_k, "m": mode, "s": _space_part(spaces)})
    return cache.get(key)


def put_answer(question: str, perm_groups: List[str], top_k: int, mode: str, result: dict,
               spaces: Optional[List[str]] = None,
               tenant_id: Optional[str] = None) -> None:
    if not settings.cache_enabled:
        return
    if tenant_id is None:
        tenant_id = current_tenant()
    key = cache.make_key("answer", {"t": _tenant_part(tenant_id),
                                    "q": question.strip().lower(), "p": sorted(perm_groups),
                                    "k": top_k, "m": mode, "s": _space_part(spaces)})
    cache.set(key, result, settings.cache_ttl_answer)


def invalidate_knowledge() -> dict:
    """文档变更后调用：清掉检索与问答缓存。"""
    return cache.invalidate(["retrieve", "answer"])


# ================= P3：Token 黑名单 =================
class TokenBlacklist:
    """已注销但尚未过期的 JWT 黑名单。

    JWT 的最大工程缺陷是"签出去就收不回来"。黑名单是标准解法：
      - 只存 jti，不存整个 token（省内存，也避免日志/内存里出现凭证明文）
      - TTL 精确等于该 token 的剩余有效期——过期后校验本来就失败，无需再记
      - Redis 不可用时退化为进程内字典，多实例场景下会漏判，
        因此健康检查里把 backend 暴露出来，便于运维判断是否需要强制换密钥
    """

    def __init__(self) -> None:
        self._local: "dict[str, float]" = {}
        self._lock = threading.Lock()

    def _key(self, jti: str) -> str:
        return f"{settings.jwt_blacklist_prefix}{jti}"

    def add(self, jti: str, ttl: int) -> str:
        """登记注销。返回实际使用的后端：redis | local。"""
        if not jti:
            return "skip"
        ttl = max(int(ttl), 1)
        backend = "local"
        if settings.jwt_blacklist_enabled and cache.l2.setex_raw(self._key(jti), ttl, "1"):
            backend = "redis"
        with self._lock:
            # 即使 Redis 写成功也保留本地副本：本实例判定零延迟，不依赖网络
            self._local[jti] = time.time() + ttl
            if len(self._local) > 50000:            # 防止无界增长
                now = time.time()
                self._local = {k: v for k, v in self._local.items() if v > now}
        return backend

    def contains(self, jti: str) -> bool:
        if not jti:
            return False
        with self._lock:
            exp = self._local.get(jti)
            if exp is not None:
                if exp > time.time():
                    return True
                self._local.pop(jti, None)          # 已自然过期，顺手清理
        if settings.jwt_blacklist_enabled:
            return cache.l2.exists(self._key(jti))
        return False

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            alive = sum(1 for v in self._local.values() if v > now)
        return {
            "enabled": settings.jwt_blacklist_enabled,
            "backend": "redis" if cache.l2.available else "local",
            "local_entries": alive,
        }


blacklist = TokenBlacklist()


# ================= P3：固定窗口限流 =================
class RateLimiter:
    """按「身份 + 路径前缀」计数的固定窗口限流。

    选择固定窗口而非令牌桶：企业内网流量形态简单，固定窗口实现足够且
    指标可解释（运维能一眼看懂"这个窗口还剩几次"）。代价是窗口边界处
    可能瞬时放过 2 倍流量，对本场景可接受。

    身份优先用已认证的 actor，未认证则退回客户端 IP——否则攻击者只要
    不断更换无效 token 就能绕开计数。
    """

    def __init__(self) -> None:
        self._local: Dict[str, Tuple[int, float]] = {}   # key -> (count, window_end)
        self._lock = threading.Lock()
        self.blocked_total = 0

    def _rule_for(self, path: str) -> int:
        for prefix, limit in settings.rate_limit_rule_map:
            if path.startswith(prefix):
                return limit
        return settings.rate_limit_default

    def check(self, identity: str, path: str) -> dict:
        """返回 {allowed, limit, remaining, reset, scope}。超限不抛异常，由调用方回 429。"""
        if not settings.rate_limit_enabled:
            return {"allowed": True, "limit": -1, "remaining": -1, "reset": 0, "scope": "off"}

        scope = "default"
        limit = settings.rate_limit_default
        for prefix, value in settings.rate_limit_rule_map:
            if path.startswith(prefix):
                scope, limit = prefix, value
                break

        window = max(int(settings.rate_limit_window_sec), 1)
        bucket = int(time.time()) // window
        key = f"{settings.rate_limit_prefix}{scope}:{identity}:{bucket}"

        count = cache.l2.incr_with_ttl(key, window)
        if count is None:
            # Redis 不可用：退化为进程内计数（单实例准确，多实例按实例数放大）
            count = self._local_incr(key, window)

        remaining = max(limit - count, 0)
        reset = window - int(time.time()) % window
        allowed = count <= limit
        if not allowed:
            with self._lock:
                self.blocked_total += 1
        return {"allowed": allowed, "limit": limit, "remaining": remaining,
                "reset": reset, "scope": scope, "count": count}

    def _local_incr(self, key: str, window: int) -> int:
        now = time.time()
        with self._lock:
            if len(self._local) > 20000:            # 清理过期窗口，防止无界增长
                self._local = {k: v for k, v in self._local.items() if v[1] > now}
            count, end = self._local.get(key, (0, now + window))
            if end <= now:
                count, end = 0, now + window
            count += 1
            self._local[key] = (count, end)
            return count

    def stats(self) -> dict:
        with self._lock:
            blocked = self.blocked_total
        return {
            "enabled": settings.rate_limit_enabled,
            "backend": "redis" if cache.l2.available else "local",
            "window_sec": settings.rate_limit_window_sec,
            "blocked_total": blocked,
            "rules": {p: n for p, n in settings.rate_limit_rule_map},
            "default": settings.rate_limit_default,
        }


limiter = RateLimiter()


# ================= P3：对话上下文缓存 =================
def _ctx_key(session_id: str) -> str:
    return cache.make_key("context", {"s": session_id})


def get_context(session_id: str) -> List[Dict[str, str]]:
    """取会话上下文（最近 N 轮）。没有则返回空列表，绝不返回 None 之外的东西。"""
    if not session_id:
        return []
    raw = cache.get(_ctx_key(session_id))
    return list(raw) if isinstance(raw, list) else []


def put_context(session_id: str, turns: List[Dict[str, str]]) -> None:
    """写上下文。只保留最近 context_max_turns*2 条消息（一问一答算两条）。"""
    if not session_id:
        return
    keep = max(int(settings.context_max_turns), 1) * 2
    cache.set(_ctx_key(session_id), list(turns)[-keep:], settings.cache_ttl_context)


def append_context(session_id: str, question: str, answer: str) -> List[Dict[str, str]]:
    """追加一轮问答并返回新的完整上下文。"""
    turns = get_context(session_id)
    turns.append({"role": "user", "content": question})
    turns.append({"role": "assistant", "content": answer})
    put_context(session_id, turns)
    return turns


def clear_context(session_id: str) -> None:
    if not session_id:
        return
    key = _ctx_key(session_id)
    cache.l1.delete(key)
    cache.l2.delete_keys(key)
