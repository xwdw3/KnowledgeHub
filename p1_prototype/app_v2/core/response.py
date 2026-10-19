"""统一错误响应与业务异常。

约定：成功响应直接返回业务数据模型（由 OpenAPI 自动生成 Schema），
失败响应统一为 {"code", "message", "data", "trace_id"}，且 HTTP 状态码与 code 一致
（避免出现"HTTP 200 里包一个 code=500"的反模式）。
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常基类：由全局异常处理器统一转换为响应。"""

    code = 400
    message = "请求处理失败"

    def __init__(self, message: str | None = None, code: int | None = None, data: Any = None):
        self.message = message or self.message
        self.code = code or self.code
        self.data = data
        super().__init__(self.message)


class NotFoundError(AppError):
    code = 404
    message = "资源不存在"


class UnauthorizedError(AppError):
    code = 401
    message = "未认证或凭证无效"


class ForbiddenError(AppError):
    code = 403
    message = "权限不足"


class ValidationError(AppError):
    code = 422
    message = "参数校验失败"


class UpstreamError(AppError):
    code = 502
    message = "上游服务异常"


class StorageError(AppError):
    code = 503
    message = "存储服务不可用"


def ok(data: Any = None, message: str = "ok") -> dict:
    """手工包装响应。

    注意：本项目的成功响应由 `envelope_middleware` **统一自动包装**，
    路由函数一律直接 `return 业务数据`，不要再用本函数包一层，
    否则会出现 data 里再嵌 data 的双重包装。
    保留本函数仅用于中间件内部和极少数需要自定义 message 的场景。
    """
    return {"code": 200, "message": message, "data": data}


# ---------------------------------------------------------------------------
# 统一响应约定
#
# 设计取舍（踩过坑，记录在此避免回退）：
#   最初用 `response_model=Envelope[T]` 让包装层进入 OpenAPI Schema，
#   但 FastAPI 会把 handler 的返回值按该模型**重新序列化**，
#   而 data 声明为 Dict[str,Any]/Any 时会被过滤成 null——业务数据整体丢失。
#
#   正确做法：包装由中间件在"序列化之后"统一完成，
#   路由只负责返回业务数据并把它的强类型模型声明给 OpenAPI。
#   这样既有准确的 Schema，又不可能丢字段。
# ---------------------------------------------------------------------------
ENVELOPE_EXCLUDE_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/metrics", "/dashboard",
                             # P4 前端静态资源与页面。HTML/CSS/JS 不能被套成 JSON 信封，
                             # 否则浏览器拿到的是一段 JSON 而不是可执行的脚本。
                             "/static", "/login", "/app", "/admin", "/index.html")


def should_envelope(path: str, status_code: int, media_type: str) -> bool:
    """判断该响应是否需要套统一包装。

    排除：文档页、Prometheus 文本、看板 HTML、非 2xx（错误已由异常处理器统一格式化）、
    非 JSON 响应（文件下载、HTML、纯文本）。
    """
    if status_code < 200 or status_code >= 300:
        return False
    if path.startswith(ENVELOPE_EXCLUDE_PREFIXES):
        return False
    return "application/json" in (media_type or "")

