"""全链路结构化日志。

两条输出通道，职责不同，不能只要一条：
  - **文本（stdout）**：给人看。本地 `docker logs` 排查时，一眼能看到 trace_id 串起来的时序。
  - **JSON（stdout）**：给机器采集。Filebeat / Loki / ELK 直接按字段建索引，
    不需要用正则去抠日志——日志一改格式就全崩是运维最常见的坑。
  通过 LOG_FORMAT 切换（json | text），默认 json，因为交付形态是容器。

P4 变更（ELK 采集）：
  - 新增**滚动文件通道** `app.jsonl`：Logstash 用 file input 采集它。
    为什么不直接往 Logstash 发 TCP：日志采集通道必须比业务更可靠，
    网络抖动时直接丢日志等于事故现场没有证据；先落文件再由 Filebeat/Logstash
    游标读取，进程崩了也不丢。
  - 每条日志都带 `service / env / tenant_id`，Kibana 里才能按租户切视图。
  - 索引名按日切分（{prefix}-YYYY.MM.DD），配合 ILM 自动滚动删除。

埋点字段（每个请求必带）：
  trace_id / tenant_id / user_id / role / method / path / status / latency_ms / client_ip
  出错时额外带 exc_type / exc_message / stack（截断）

敏感信息处理：
  - 日志里**永远不出现**密码、API Key、JWT 原文、Authorization 头
  - 通用做法：`redact()` 递归处理 dict，按 key 名匹配敏感词并替换为 ***
  - 这是"日志泄露导致密钥外流"这一类事故的唯一有效防线——
    靠人工 review 每一条 log 语句是不现实的
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

# 命中以下子串的 key，其值一律替换为 ***（大小写不敏感）
_SENSITIVE_KEYS = (
    "password", "passwd", "pwd", "secret", "token", "authorization", "api_key",
    "apikey", "access_key", "private_key", "credential", "cookie", "session",
)
_REDACTED = "***"


def redact(obj, depth: int = 0):
    """递归脱敏。depth 上限防止病态嵌套结构把日志打爆。"""
    if depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in _SENSITIVE_KEYS):
                out[k] = _REDACTED
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj[:50]]
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:2000] + "...(truncated)"
    return obj


class TraceFilter(logging.Filter):
    """把上下文变量注入每条记录。

    必须用 filter 而不是在每个调用点手写 extra：漏一处就是一条没有 trace_id
    的日志，而偏偏漏的那条往往就是出错的那条。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        if not hasattr(record, "tenant_id"):
            try:
                from app_v2.services.tenant_service import current_tenant

                record.tenant_id = current_tenant()
            except Exception:
                record.tenant_id = "-"
        return True


_INSTANCE_CACHE: list = []


def _instance_id() -> str:
    """本进程的实例标识（多副本部署用）。

    优先级：settings.instance_id > 容器/主机名。
    容器 hostname 在 K8s/Compose 下就是 pod/容器 ID，天然唯一；
    宿主机直接跑时是机器名，也够用。缓存一次，避免每条日志都取 hostname。
    """
    if _INSTANCE_CACHE:
        return _INSTANCE_CACHE[0]
    try:
        from app_v2.core.settings import settings

        iid = (getattr(settings, "instance_id", "") or "").strip()
    except Exception:
        iid = ""
    if not iid:
        import socket

        try:
            iid = socket.gethostname()
        except Exception:
            iid = "-"
    _INSTANCE_CACHE.append(iid or "-")
    return _INSTANCE_CACHE[0]


def resolve_instance_id() -> str:
    """对外暴露的实例标识。

    日志和 /health 必须用**同一个**值，否则运维拿日志里的 instance 去
    对照 /health 时对不上号——排查"只有某台机器出问题"会直接卡住。
    所以这里只是一个显式的公开别名，不做二次计算。
    """
    return _instance_id()


class JsonFormatter(logging.Formatter):
    """单行 JSON：采集端按行切分，多行堆栈会被拆碎，所以 stack 必须在字段里而不是裸输出。"""
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # @timestamp 用 ISO8601 带时区：ES 的 date 类型能直接解析，
            # 用 ts 这种自定义键名会让 Kibana 把它当字符串，"按时间排序"就失效了。
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                          + f".{int(record.msecs):03d}Z",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "service": "rag-kb",
            # 多副本部署时**必须**有实例标识：否则 3 个 api 副本的日志在 Kibana 里
            # 长得一模一样，出了一个只在某台机器上复现的问题（本地缓存不一致、
            # 该实例连不上某个下游）根本无从定位。
            "instance": _instance_id(),
            "trace_id": getattr(record, "trace_id", "-"),
            "tenant_id": getattr(record, "tenant_id", "-"),
            "message": record.getMessage(),
        }
        # 业务侧通过 logger.info(..., extra={"ctx": {...}}) 追加结构化字段
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(redact(ctx))
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
            payload["stack"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s | %(levelname)-7s | %(trace_id)s | %(tenant_id)s | %(name)s | %(message)s")


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    from app_v2.core.settings import settings

    use_json = (fmt or "").lower() == "json"
    formatter = JsonFormatter() if use_json else TextFormatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(TraceFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stdout_handler)

    # ---------- ELK 采集通道：滚动 JSON 文件 ----------
    if settings.log_ship_enabled:
        try:
            from logging.handlers import RotatingFileHandler
            from pathlib import Path

            path = Path(settings.effective_log_ship_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=settings.log_ship_max_bytes,
                backupCount=settings.log_ship_backup_count,
                encoding="utf-8",
            )
            # 落盘通道**恒为 JSON**：Logstash 依赖 json codec 解析，
            # 若跟随 LOG_FORMAT=text 会让整个索引的字段全部退化成 message。
            file_handler.setFormatter(JsonFormatter())
            file_handler.addFilter(TraceFilter())
            root.addHandler(file_handler)
        except Exception as exc:                  # 日志落盘失败不能拖垮启动
            logging.getLogger(__name__).warning("日志采集通道初始化失败：%s", exc)

    root.setLevel(level.upper())

    # 降低第三方库噪声
    for noisy in ("httpx", "httpcore", "urllib3", "qdrant_client", "celery",
                  "kombu", "amqp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：优先 X-Forwarded-For（经反向代理时），否则 socket 地址。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # X-API-Key 仅为 P2 存量集成保留；严格模式下它不再能进业务接口，
    # 只能拿去 POST /api/v1/auth/token 换 JWT。
    return (request.headers.get("X-API-Key") or "").strip()


def _guard_metrics_token(request: Request, raw_key: str) -> bool:
    """/metrics 的独立令牌校验（Prometheus 只能带静态 header，无法走登录流程）。

    用常量时间比较：这里是高价值目标（能反映系统全貌），
    逐字节比较会把令牌泄露成时序侧信道。
    """
    import hmac as _hmac

    from app_v2.core.settings import settings

    expected = settings.effective_metrics_token
    if not expected:
        return False
    return _hmac.compare_digest(str(raw_key or ""), str(expected))


async def trace_middleware(request: Request, call_next):
    """统一入口：trace_id + 认证（JWT 严格模式 / P2 双通道）+ 限流 + 指标 + 失败审计。"""
    from app_v2.core.metrics import metrics_registry
    from app_v2.core.settings import settings
    from app_v2.services import audit_service, auth_service, cache_service, user_service

    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
    token = trace_id_var.set(trace_id)
    logger = logging.getLogger("access")
    start = time.perf_counter()
    path = request.url.path
    metrics_registry.inflight += 1

    request.state.principal = None
    request.state.client_ip = _client_ip(request)
    is_public = settings.is_public(path)

    try:
        # ---------- 认证 ----------
        raw_key = _extract_bearer(request)
        if not is_public:
            if settings.auth_enabled:
                try:
                    principal = None
                    # 严格模式：只认 JWT。API Key 若被直接当 Bearer 用，会被
                    # auth_service.authenticate 判为"非 JWT"，返回带指引的 401，
                    # 提示改用 POST /api/v1/auth/token 换取 token。
                    if raw_key and user_service.is_jwt(raw_key):
                        principal = user_service.principal_from_jwt(raw_key)
                    else:
                        principal = auth_service.authenticate(
                            raw_key, request.state.client_ip, allow_api_key=not settings.jwt_strict
                        )
                    request.state.principal = principal
                except (auth_service.UnauthorizedError, _jwt_error()) as exc:
                    metrics_registry.incr("auth_failures")
                    _audit_denied(request, audit_service, auth_service, path, exc, 401)
                    cost = (time.perf_counter() - start) * 1000
                    metrics_registry.observe_request(request.method, path, 401, cost / 1000)
                    logger.warning("认证失败 %s %s -> 401 (%.0fms)", request.method, path, cost,
                                   extra={"ctx": {"event": "auth_failed", "path": path,
                                                  "client_ip": request.state.client_ip,
                                                  "auth_mode": settings.auth_mode,
                                                  "reason": getattr(exc, "message", str(exc))}})
                    return _error_response(401, getattr(exc, "message", str(exc)), trace_id)
            else:
                # 鉴权关闭时也要有身份，否则限流键退化成空串，所有人都共享一个桶
                request.state.principal = None
        elif path == "/metrics" and settings.auth_enabled and settings.metrics_protected:
            # /metrics 免的是"JWT"，不是"认证"：抓取器用独立静态令牌。
            # 若不校验，任何能连到 8012 的人都能看到完整接口路径、状态码分布、
            # 错误率与依赖拓扑——这些信息本身就足以支撑一次定向攻击。
            if not _guard_metrics_token(request, raw_key):
                metrics_registry.incr("auth_failures")
                cost = (time.perf_counter() - start) * 1000
                metrics_registry.observe_request(request.method, path, 401, cost / 1000)
                logger.warning("抓取令牌校验失败 %s -> 401", path)
                resp = _error_response(401, "抓取令牌无效（/metrics 需要 METRICS_TOKEN）", trace_id)
                resp.headers["WWW-Authenticate"] = 'Bearer realm="rag-kb-metrics"'
                return resp

        # ---------- 限流 ----------
        # 放在认证之后：优先用已认证身份计数，避免攻击者靠换无效 token 绕过
        if settings.rate_limit_enabled:
            principal = request.state.principal
            identity = principal.actor if principal else f"ip:{request.state.client_ip or 'unknown'}"
            verdict = cache_service.limiter.check(identity, path)
            if not verdict["allowed"]:
                metrics_registry.incr("rate_limited")
                _audit_denied(request, audit_service, auth_service, path,
                              _RateLimitError(f"触发限流（{verdict['scope']} 每 "
                                              f"{settings.rate_limit_window_sec}s ≤ {verdict['limit']} 次）"), 429)
                cost = (time.perf_counter() - start) * 1000
                metrics_registry.observe_request(request.method, path, 429, cost / 1000)
                logger.warning("限流拒绝 %s %s identity=%s scope=%s (%.0fms)",
                               request.method, path, identity, verdict["scope"], cost)
                resp = _error_response(429, "请求过于频繁，请稍后重试", trace_id)
                resp.headers["Retry-After"] = str(verdict["reset"])
                resp.headers["X-RateLimit-Limit"] = str(verdict["limit"])
                resp.headers["X-RateLimit-Remaining"] = "0"
                return resp
            request.state.rate = verdict

        response = await call_next(request)
        response = await _wrap_envelope(request, response)
        cost = (time.perf_counter() - start) * 1000
        response.headers["X-Trace-Id"] = trace_id
        if getattr(request.state, "rate", None):
            response.headers["X-RateLimit-Limit"] = str(request.state.rate["limit"])
            response.headers["X-RateLimit-Remaining"] = str(request.state.rate["remaining"])
        metrics_registry.observe_request(request.method, path, response.status_code, cost / 1000)
        _log_access(logger, request, response.status_code, cost, trace_id, logging.INFO)
        return response

    except Exception as exc:
        cost = (time.perf_counter() - start) * 1000
        metrics_registry.observe_request(request.method, path, 500, cost / 1000)
        _log_access(logger, request, 500, cost, trace_id, logging.ERROR, exc=exc)
        _persist_error(request, exc, 500, trace_id, cost)
        raise
    finally:
        metrics_registry.inflight -= 1
        trace_id_var.reset(token)


def _log_access(logger: logging.Logger, request: Request, status: int, cost_ms: float,
                trace_id: str, level: int, exc: Exception | None = None) -> None:
    """结构化访问日志：埋点 request_id / user_id / 耗时。"""
    principal = getattr(request.state, "principal", None)
    ctx = {
        "event": "access",
        "trace_id": trace_id,
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query or "")[:200],
        "status": status,
        "latency_ms": round(cost_ms, 1),
        "tenant_id": getattr(principal, "tenant_id", "") if principal else "-",
        "user_id": getattr(principal, "actor", "") if principal else "anonymous",
        "role": getattr(principal, "role", "") if principal else "-",
        "client_ip": getattr(request.state, "client_ip", ""),
        "user_agent": (request.headers.get("User-Agent") or "")[:120],
    }
    if exc is not None:
        ctx.update({
            "exc_type": type(exc).__name__,
            "exc_message": str(exc)[:500],
        })
    msg = "%s %s -> %s (%.0fms)" % (request.method, request.url.path, status, cost_ms)
    logger.log(level, msg, extra={"ctx": ctx}, exc_info=exc is not None)


def _persist_error(request: Request, exc: BaseException, status: int,
                   trace_id: str, cost_ms: float) -> None:
    """把 5xx 异常落到 error_logs 表。

    日志是"流"（会被轮转、被采集、7 天后就没了），
    而线上排查往往发生在"用户报障之后"。所以异常必须进库，才能按 trace_id 反查。
    这里必须吞掉异常：写错误日志失败，绝不能再引发一次错误。
    """
    try:
        import traceback

        from app_v2.storage.meta_store import meta_store

        principal = getattr(request.state, "principal", None)
        meta_store.log_error({
            "trace_id": trace_id,
            "path": request.url.path,
            "method": request.method,
            "user_id": getattr(principal, "actor", "") if principal else "",
            "status_code": status,
            "exc_type": type(exc).__name__,
            "message": str(exc)[:2000],
            "stack": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:],
        })
    except Exception:
        pass


def _jwt_error():
    """延迟导入 JWTError，避免 core 模块间循环依赖。"""
    from app_v2.core.jwt_utils import JWTError

    return JWTError


class _RateLimitError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _error_response(status: int, message: str, trace_id: str,
                    extra_headers: dict | None = None) -> JSONResponse:
    headers = {"X-Trace-Id": trace_id}
    if status == 401:
        # 标准语义：告诉客户端该用哪种认证方式，Swagger UI 的 Authorize 也依赖它
        headers["WWW-Authenticate"] = 'Bearer realm="rag-kb"'
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        status_code=status,
        content={"code": status, "message": message, "data": None, "trace_id": trace_id},
        headers=headers,
    )


async def _wrap_envelope(request: Request, response):
    """把成功响应统一包装为 {code, message, data, trace_id}。

    必须放在"业务处理之后、返回之前"，而不是用 response_model 声明：
      - response_model 会让 FastAPI 按模型重新序列化，Dict[str,Any] 类型的 data
        会被过滤成 null，业务数据整体丢失（这是本项目踩过的真实坑）。
      - 包装必须跳过 StreamingResponse（否则要整体缓冲，破坏流式）与
        非 JSON 响应（HTML/纯文本/文件下载）。
    """
    from app_v2.core.response import should_envelope

    # 注意：Starlette 的 JSONResponse 本身继承自 _StreamingResponse，
    # 直接 isinstance(response, StreamingResponse) 会把所有普通 JSON 响应误判为流式，
    # 导致包装被静默跳过。必须先排除 JSONResponse，再判断真正的流式响应。
    if isinstance(response, JSONResponse):
        pass
    elif isinstance(response, StreamingResponse):
        return response
    if not should_envelope(request.url.path, response.status_code,
                           response.headers.get("content-type", "")):
        return response
    if response.status_code == 204:
        return response

    # 取响应体：JSONResponse 有 .body；BaseHTTPMiddleware 下 FastAPI 的
    # 普通响应可能是 _StreamingResponse，其 .body 为空、内容在 body_iterator 里。
    body = getattr(response, "body", None)
    if not body:
        body = b"".join([chunk async for chunk in response.body_iterator])
    if not body:
        return response

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return response

    # 已经被包装过（例如兜底 path 返回的 code/message/data）则不重复包装
    if isinstance(payload, dict) and {"code", "message", "data"} <= set(payload):
        payload.setdefault("trace_id", trace_id_var.get())
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return JSONResponse(status_code=response.status_code, content=payload, headers=headers)

    wrapped = {"code": 200, "message": "ok", "data": payload,
               "trace_id": trace_id_var.get()}
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return JSONResponse(status_code=response.status_code, content=wrapped, headers=headers)


def _audit_denied(request: Request, audit_service, auth_service, path: str,
                  exc: Exception, status: int) -> None:
    """认证失败必须留痕：安全事件排查的第一手证据。"""
    try:
        raw_key = _extract_bearer(request)
        kid = auth_service.key_id_of(raw_key) if raw_key else ""
        audit_service.record(
            action="auth.verify",
            actor="anonymous",
            role="-",
            resource=path,
            result=audit_service.RESULT_DENIED,
            status_code=status,
            ip=request.state.client_ip,
            user_agent=request.headers.get("User-Agent", ""),
            detail={"reason": str(exc), "key_id": kid, "method": request.method},
            trace_id=trace_id_var.get(),
        )
    except Exception:      # 审计失败不能影响响应
        pass
