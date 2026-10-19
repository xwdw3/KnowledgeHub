"""FastAPI 应用装配：路由、中间件、异常处理、自动接口文档。

接口文档：
  Swagger UI  → /docs
  ReDoc       → /redoc
  OpenAPI     → /openapi.json
运行看板：
  /dashboard  → 内置 HTML 看板（无外部依赖）
指标：
  /metrics    → Prometheus 文本格式

P2 已在 P1 基础上补齐企业上线必备的四项能力：鉴权（API Key + RBAC）、
两级缓存（L1 进程内 + L2 Redis）、全链路审计、Prometheus 监控。
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app_v2.api.routes import (
    alerts, audit, auth, chat, documents, metrics, spaces, sso, system, tasks, tenants,
)
from app_v2.core.logging import setup_logging, trace_id_var, trace_middleware
from app_v2.core.response import AppError
from app_v2.core.settings import settings
from app_v2.services import (
    audit_service, auth_service, cache_service, tenant_service, user_service,
)
from app_v2.storage.meta_store import meta_store
from app_v2.storage.vector_store import vector_store

logger = logging.getLogger(__name__)

TAGS_METADATA = [
    {"name": "问答", "description": "智能问答与检索。`rag` 固定管道适合常规提问；`agent` 支持多步工具调用（检索/列文档/计算），内置三重熔断。"},
    {"name": "文档管理", "description": "文档上传入库、列表、删除。同名覆盖，幂等安全。写操作需要 `kb:write`。"},
    {"name": "安全", "description": "账号登录（JWT）、Token 刷新/注销、用户管理、API Key 管理、角色权限、当前身份。\n\n两种认证并存：人类用户走 `login` 拿 JWT，系统间调用走 API Key。密钥/密码明文均只在创建时返回一次。"},
    {"name": "单点登录", "description": "企业 SSO（OIDC Authorization Code Flow + PKCE）。`/sso/login` 302 跳 IdP，`/sso/callback` 由 IdP 回调后签发本地 JWT 并以一次性票据交回前端，`/sso/ticket` 换票。\n\n三层校验：state 与 HttpOnly Cookie 双绑（防登录 CSRF）、nonce（防 ID Token 重放）、code 服务端换取且原子消费（防并发双换）。"},
    {"name": "知识库空间", "description": "多知识库隔离。空间是**内容**的属性，权限组是**人**的属性，两者正交。文档与检索均按空间下推过滤。"},
    {"name": "租户", "description": "多租户开通与配额。租户是最外层隔离边界，空间与密级都在租户内部才有意义。\n\n租户 ID 会进对象存储 key 前缀与所有查询条件，因此严格限制字符集。列表接口只返回元信息与用量计数，**不含任何业务数据**。"},
    {"name": "审计", "description": "操作留痕查询。只读、只追加，记录包括被拒绝的越权尝试。"},
    {"name": "监控", "description": "Prometheus 指标、JSON 快照、缓存管理与内置看板。"},
    {"name": "告警", "description": "规则评估与告警事件。规则判据既有 Prometheus 能看到的（错误率、延迟），也有只有应用知道的（LLM 失败率、任务积压、对象存储可用性）。重复触发按规则静默，不会刷屏。"},
    {"name": "系统", "description": "健康检查、就绪探针、运行参数与统计。"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level, settings.log_format)
    logger.info("服务启动 version=%s model_mode=%s env=%s",
                settings.app_version, settings.resolved_model_mode(), settings.env)
    logger.info("向量库：%s | 业务库：%s", vector_store.backend, meta_store.backend)
    logger.info("安全：auth=%s | 缓存：redis=%s | 审计：%s | 指标：%s",
                settings.auth_enabled,
                cache_service.cache.l2.health().get("backend"),
                settings.audit_enabled, settings.metrics_enabled)

    if vector_store.backend == "local" or meta_store.backend == "memory":
        logger.warning("当前为降级存储（local/memory），数据仅用于验证，生产请启动 Qdrant 与 PostgreSQL")

    if settings.audit_enabled:
        audit_service.start()

    if settings.jwt_enabled and user_service.jwt_manager().enabled:
        logger.info("JWT 已启用 issuer=%s access_ttl=%ds refresh_ttl=%ds",
                    settings.jwt_issuer, settings.jwt_access_ttl, settings.jwt_refresh_ttl)

    # 默认空间必须存在：所有未指定空间的文档都归入这里，缺了会导致列表页空
    user_service.ensure_default_space()

    # 全新环境没有任何凭据时自动引导：先建账号（人来登录），再建密钥（系统来调用）
    if settings.auth_enabled:
        if settings.bootstrap_admin_key:
            _install_bootstrap_key(settings.bootstrap_admin_key)
        else:
            try:
                auth_service.bootstrap_admin_key()
            except Exception as exc:
                logger.error("初始化管理员密钥失败：%s", exc)
    if settings.jwt_enabled:
        try:
            user_service.bootstrap_admin_user()
        except Exception as exc:
            logger.error("初始化管理员账号失败：%s", exc)

    # 告警评估线程最后启动：它一上线就会立刻采样，
    # 若在依赖初始化之前启动，第一轮必然读到"全部不可用"并白白消耗静默窗口。
    try:
        from app_v2.services.alert_service import alert_service

        alert_service.start()
    except Exception as exc:
        logger.error("告警评估线程启动失败：%s", exc)

    yield

    try:
        from app_v2.services.alert_service import alert_service

        alert_service.stop()
    except Exception:
        pass
    if settings.audit_enabled:
        audit_service.stop()
    logger.info("服务关闭")


def _install_bootstrap_key(raw_key: str) -> None:
    """按环境变量指定的固定密钥写入（生产推荐，避免每次重建都换密钥）。"""
    try:
        existing = meta_store.list_api_keys(include_revoked=True)
        if any(k.get("owner") == "bootstrap" for k in existing):
            return
        from app_v2.core.security import hash_api_key, key_id_of

        kid = key_id_of(raw_key)
        key_hash, salt = hash_api_key(raw_key)
        meta_store.save_api_key({
            "key_id": kid, "owner": "bootstrap", "role": "admin", "key_hash": key_hash,
            "salt": salt, "perm_group": "all", "enabled": True,
            "description": "环境变量指定的初始管理员密钥",
        })
        logger.info("已安装环境变量指定的管理员密钥 key_id=%s", kid)
    except Exception as exc:
        logger.error("安装 BOOTSTRAP_ADMIN_KEY 失败：%s", exc)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "企业私有化 RAG 知识库后端（P3 多知识库协同版）。\n\n"
        "**核心链路**：文档解析 → 切片 → 向量化 → Qdrant 入库 → 混合检索（向量 + BM25 + RRF）→ Rerank 重排 → LLM 生成 → 引用溯源。\n\n"
        "**两种问答模式**：\n"
        "- `rag`：固定管道，一次检索一次生成，延迟低、行为可预测\n"
        "- `agent`：Function Calling 自主编排，可多次检索、调用计算器，带轮次/重复/超时三重熔断\n\n"
        "**两种认证通道**（都归一到同一个身份模型）：\n"
        "1. `POST /api/v1/auth/login` 账号密码 → JWT（access 30 分钟 / refresh 7 天，refresh 一次性轮换）\n"
        "2. `Authorization: Bearer <API Key>` 长期密钥，适合脚本与系统集成\n"
        "另有企业 SSO（OIDC）：`GET /api/v1/auth/sso/login` 发起，回调后同样签发本地 JWT。\n"
        "角色：`admin`（全部）/ `kb_admin`（读写知识库与空间）/ `user`（只读，兼容旧名 editor/viewer）。\n"
        "**双层隔离**：`perm_group` 管「谁能看」（人的属性），`space_id` 管「属于哪个业务域」（内容的属性），两者同时下推到元数据层与向量层。\n\n"
        "**统一响应**：`{code, message, data, trace_id}`，HTTP 状态码与 code 一致。"
    ),
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True,
                           "filter": True, "tryItOutEnabled": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id", "X-Process-Time"],
)
app.middleware("http")(trace_middleware)


# ---------------- 全局异常处理 ----------------
@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("业务异常 %s %s: %s", exc.code, request.url.path, exc.message)
    return JSONResponse(status_code=exc.code, content={
        "code": exc.code, "message": exc.message, "data": exc.data, "trace_id": trace_id_var.get(),
    })


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "message": e.get("msg", "")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={
        "code": 422, "message": "参数校验失败", "data": errors, "trace_id": trace_id_var.get(),
    })


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s", request.url.path)
    # 兜底异常也要进 error_logs：中间件那层只在"异常穿透到中间件"时才会落库，
    # 而这个 handler 会先把异常吞掉变成 500 响应，所以两条路径都要写，
    # 否则会出现"有 trace_id 但错误表里查不到"的空档。
    try:
        import traceback

        from app_v2.storage.meta_store import meta_store

        principal = getattr(request.state, "principal", None)
        meta_store.log_error({
            "trace_id": trace_id_var.get(),
            "path": request.url.path,
            "method": request.method,
            "user_id": getattr(principal, "actor", "") if principal else "",
            "status_code": 500,
            "exc_type": type(exc).__name__,
            "message": str(exc)[:2000],
            "stack": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:],
        })
    except Exception:
        pass
    return JSONResponse(status_code=500, content={
        "code": 500, "message": "服务内部错误，请联系管理员并提供 trace_id",
        "data": None, "trace_id": trace_id_var.get(),
    })


# ---------------- 路由 ----------------
API_PREFIX = "/api/v1"
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(auth.router, prefix=API_PREFIX)
# SSO 单独成模块：它的返回形态是 302 跳转而不是 JSON 信封，
# 与 auth 模块里"接口即数据"的风格不同，混在一起容易误改。
app.include_router(sso.router, prefix=API_PREFIX)
app.include_router(spaces.router, prefix=API_PREFIX)
app.include_router(tenants.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
app.include_router(system.router, prefix=API_PREFIX)
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(alerts.router, prefix=API_PREFIX)

# 监控路由挂在 /api/v1 下，同时把 /metrics 单独暴露给 Prometheus（路径更短更常规）
app.include_router(metrics.router, prefix=API_PREFIX)
app.add_api_route("/metrics", metrics.prometheus_metrics, methods=["GET"],
                  include_in_schema=False, tags=["监控"])
app.add_api_route("/dashboard", metrics.dashboard, methods=["GET"],
                  include_in_schema=False, tags=["监控"])


@app.get("/info", include_in_schema=False)
def root() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "stage": "P4",
        "auth": {"password_login": settings.jwt_enabled, "api_key": settings.auth_enabled},
        "docs": "/docs",
        "dashboard": "/dashboard",
        "metrics": "/metrics",
        "openapi": "/openapi.json",
        "api_prefix": API_PREFIX,
    }


@app.middleware("http")
async def add_process_time(request: Request, call_next):
    """把处理耗时写进响应头，便于前端与排障定位慢请求。"""
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{(time.perf_counter() - start) * 1000:.0f}ms"
    return response


@app.middleware("http")
async def tenant_context_guard(request: Request, call_next):
    """请求出口复位租户上下文。

    为什么要单独一层：认证依赖里 set 了 ContextVar，但 ContextVar 在
    ASGI 任务完成后不会自动回滚；一旦线程/任务被复用，下一个请求
    在认证之前就有机会读到上一个请求的租户。这里的 reset 是"兜底复位"，
    不负责 set（set 必须发生在认证之后，见 deps.bind_tenant）。
    """
    token = tenant_service.set_current_tenant(settings.default_tenant_id)
    try:
        return await call_next(request)
    finally:
        tenant_service.reset_current_tenant(token)


# ======================================================================
# P4 前端：静态资源与页面路由
# ======================================================================
# 为什么用 StaticFiles 直接托管而不是引入模板引擎：
#   前端是零构建的原生 HTML/CSS/JS。Jinja 会带来"到底谁负责渲染"的歧义
#   （同一段 HTML 有两处来源），而且模板里再拼一遍 API 契约等于双份维护。
#   静态托管 = 前端只通过 /api/v1 拿数据，契约只有一处。
_WEB_DIR = Path(__file__).resolve().parent / "web"

if _WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")

    _PAGES = {
        "/": ("login.html", False),
        "/login": ("login.html", False),
        "/app": ("app.html", True),
        "/admin": ("admin.html", True),
    }

    def _serve_page(filename: str, require_login: bool = False):
        """返回页面。

        `require_login` 只做**前端跳转**，不是安全边界：
        真正的鉴权在 API 层（每个接口都要 JWT）。这里加跳转是为了体验——
        未登录用户直接打开 /admin 会先看到一堆空面板再被各接口 401 打回来。
        **绝不能**因为这段代码就以为页面本身需要保护——
        页面里没有任何数据，数据全部来自带鉴权的接口。
        """
        path = _WEB_DIR / filename
        if not path.exists():
            return JSONResponse(status_code=404, content={
                "code": 404, "message": f"页面未找到：{filename}", "data": None})
        # 页面不缓存：发版后用户刷新要能立刻拿到新版，
        # 否则改了前端但用户看到的是缓存的旧 HTML，会怀疑"根本没修好"。
        return FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})

    for _route, (_file, _need_login) in _PAGES.items():

        def _make(fname: str = _file, need: bool = _need_login):
            def _handler() -> FileResponse:
                return _serve_page(fname, need)
            return _handler

        app.add_api_route(_route, _make(), methods=["GET"],
                          include_in_schema=False, name=f"page{_route}")
else:                                                            # pragma: no cover
    logger.warning("前端资源目录不存在，页面路由未注册：%s", _WEB_DIR)
