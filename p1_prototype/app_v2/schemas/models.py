"""请求/响应模型：既做运行时校验，也作为自动接口文档的 Schema 来源。

注意：项目需兼容 Python 3.9，因此不使用 `X | None` 新语法，统一用 Optional。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------- 问答 ----------------
class SourceItem(BaseModel):
    chunk_id: str = Field(description="片段唯一 ID")
    file_name: str = Field(description="来源文件名")
    seq: int = Field(description="片段在文档中的序号（从 0 开始）")
    text: str = Field(description="片段原文，用于溯源核对")
    vec_score: float = Field(0, description="向量余弦相似度")
    bm25_score: float = Field(0, description="BM25 关键词得分")
    rrf_score: float = Field(0, description="RRF 融合分")
    vec_rank: Optional[int] = Field(None, description="向量召回排名")
    bm25_rank: Optional[int] = Field(None, description="关键词召回排名")
    space_id: str = Field("default", description="片段所属知识库空间（答案溯源需精确到空间）")
    rerank_score: Optional[float] = Field(None, description="重排后的相关度，未重排时为 null")


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000, description="用户问题，最长 1000 字",
                          examples=["去上海出差住宿能报多少钱？"])
    session_id: Optional[str] = Field(None, max_length=64, description="会话 ID，留空则自动创建")
    top_k: Optional[int] = Field(None, ge=1, le=10, description="检索片段数量，默认取服务端配置")
    mode: Literal["rag", "agent"] = Field("rag", description="rag=固定管道；agent=自主工具调用")
    perm_group: str = Field("all", max_length=64, description="已废弃：权限组以凭据为准，此字段被忽略")
    space_id: Optional[str] = Field(None, max_length=64,
                                    description="限定在某个知识库空间内检索；留空表示全部可见空间")


class ChatData(BaseModel):
    session_id: str
    answer: str
    sources: List[SourceItem] = []
    confidence: float = Field(0, description="相关度 0~1，越低越可能是知识库外问题")
    rejected: bool = Field(False, description="是否因相关度不足而拒答")
    mode: str = Field("rag", description="rag / rag-offline / agent / agent-offline")
    timings: Dict[str, Any] = {}
    trace: Optional[Dict[str, Any]] = Field(None, description="Agent 执行轨迹（仅 agent 模式）")


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="检索语句")
    top_k: int = Field(5, ge=1, le=20, description="返回片段数")
    perm_group: str = Field("all", description="已废弃：权限组以凭据为准，此字段被忽略")
    space_id: Optional[str] = Field(None, max_length=64,
                                    description="限定在某个知识库空间内检索；留空表示全部可见空间")


class RetrieveData(BaseModel):
    query: str
    hits: List[SourceItem]
    confidence: float
    timings: Dict[str, Any] = {}


# ---------------- 文档 ----------------
class DocumentItem(BaseModel):
    doc_id: str
    file_name: str
    file_ext: str = ""
    file_size: int = 0
    char_count: int = 0
    chunk_count: int = 0
    perm_group: str = "all"
    space_id: str = Field("default", description="所属知识库空间")
    status: str = "active"
    uploader: str = "system"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class IngestData(BaseModel):
    doc_id: str
    file_name: str
    chunk_count: int
    char_count: int
    vector_points: int
    perm_group: str
    space_id: str = Field("default", description="入库到的知识库空间")
    overwritten: bool = False
    # P4
    tenant_id: str = Field("default", description="文档归属租户")
    object_key: str = Field("", description="原件在对象存储中的 key；空表示以无原件模式入库")
    task_id: str = Field("", description="异步入库时返回的任务 ID，用于轮询进度")
    async_mode: bool = Field(False, description="是否为异步入库")


class DocumentListData(BaseModel):
    total: int
    items: List[DocumentItem]


class DeleteRequest(BaseModel):
    file_name: str = Field(..., min_length=1, description="要删除的文件名（精确匹配）")


# ---------------- 系统 ----------------
class HealthData(BaseModel):
    status: str = Field(description="整体状态：ok / degraded")
    app_version: str
    model_mode: str = Field(description="real=真实模型，offline=离线兜底")
    instance_id: str = Field(default="", description="当前副本标识，多实例部署时用于区分机器")
    storage: Dict[str, Any] = Field(description="各存储后端健康状态")
    stats: Dict[str, Any] = Field(default_factory=dict)


class AgentConfigData(BaseModel):
    max_rounds: int
    max_repeat: int
    timeout_sec: int
    max_tool_calls: int
    tools: List[str]
    auth_enabled: bool = True
    cache_enabled: bool = True
    audit_enabled: bool = True
    metrics_enabled: bool = True
    roles: List[str] = []


# ---------------- P2：鉴权 ----------------
class KeyCreateRequest(BaseModel):
    owner: str = Field(..., min_length=1, max_length=128, description="密钥归属人/系统标识")
    role: Literal["admin", "editor", "viewer"] = Field("viewer", description="角色：admin/editor/viewer")
    perm_group: str = Field("all", description="可访问的知识权限组，逗号分隔，如 hr,finance")
    description: str = Field("", max_length=512, description="用途说明，便于审计追溯")
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650, description="有效期天数，留空为长期")


class KeyCreatedData(BaseModel):
    key_id: str
    api_key: str = Field(description="明文密钥，仅本次返回，请立即保存")
    owner: str
    role: str
    perm_group: str
    expires_at: Optional[str] = None
    description: str = ""
    warning: str


class KeyItem(BaseModel):
    key_id: str
    owner: str
    role: str
    perm_group: str
    enabled: bool
    description: str = ""
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    revoked_at: Optional[str] = None
    created_at: Optional[str] = None
    status: str = Field(description="active / disabled / expired / revoked")


class KeyListData(BaseModel):
    total: int
    items: List[KeyItem]


class PrincipalData(BaseModel):
    actor: str
    role: str
    key_id: str
    permissions: List[str]
    perm_groups: List[str]
    role_label: str = ""
    is_admin: bool = False


# ---------------- P2：审计 ----------------
class AuditItem(BaseModel):
    id: Optional[int] = None
    trace_id: str = "-"
    actor: str = "anonymous"
    role: str = "-"
    action: str
    resource: str = ""
    result: str = Field(description="success / denied / failed")
    status_code: int = 200
    ip: str = ""
    user_agent: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    created_at: Optional[str] = None


class AuditListData(BaseModel):
    total: int
    items: List[AuditItem]


# ---------------- P2：监控 ----------------
class CacheInvalidateRequest(BaseModel):
    kinds: List[str] = Field(default_factory=list,
                             description="要失效的缓存类别：retrieve / answer，为空表示全部")


class MetricSnapshotData(BaseModel):
    uptime_sec: int
    requests_total: int
    errors_total: int
    error_rate: float
    inflight: int
    slow_requests: int
    latency_ms: Dict[str, Any] = Field(default_factory=dict)
    chat: Dict[str, Any] = Field(default_factory=dict)
    retrieve: Dict[str, Any] = Field(default_factory=dict)
    documents: Dict[str, Any] = Field(default_factory=dict)
    llm: Dict[str, Any] = Field(default_factory=dict)
    embed: Dict[str, Any] = Field(default_factory=dict)
    security: Dict[str, Any] = Field(default_factory=dict)
    by_path: Dict[str, Any] = Field(default_factory=dict)


# ==================== P3：账号与 Token ====================
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128, examples=["admin"])
    password: str = Field(..., min_length=1, max_length=128, examples=["Admin@12345"])
    # P4：用户名只在租户内唯一，登录必须能指明是哪个租户。
    # 留空时按"该用户名最早创建的那条记录"归属（兼容单租户部署），
    # 多租户部署应由前端把租户填进登录表单，或走 SSO 由 IdP 决定。
    tenant_id: str = Field("", max_length=64, description="租户标识；留空则自动推断",
                           examples=["acme"])


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, description="登录时返回的 refresh_token")


class TokenFromKeyRequest(BaseModel):
    """用 API Key 换取 JWT。密钥放在请求体而不是 header：

    协议本身是「密钥 → 票据」，把密钥当 header 发容易让人误以为它可以直连业务接口。
    """
    api_key: str = Field(..., min_length=8, max_length=256,
                         description="P2 创建的 API Key（rag_ 开头）",
                         examples=["rag_xxxxxxxxxxxxxxxxxxxx"])



class LogoutRequest(BaseModel):
    access_token: Optional[str] = Field(None, description="留空则取 Authorization 头里的 token")
    refresh_token: Optional[str] = Field(None, description="可选，一并注销 refresh token")


class UserData(BaseModel):
    user_id: str
    username: str
    display_name: str = ""
    role: str = "user"
    spaces: List[str] = []
    perm_groups: str = Field("", description="显式密级；空串表示按空间名派生")
    enabled: bool = True
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=128, description="至少 8 位")
    role: str = Field("user", description="admin / kb_admin / user，兼容旧名 editor / viewer")
    spaces: str = Field("default", description="逗号分隔的空间 ID，`*` 表示全部空间")
    perm_groups: str = Field(
        "", max_length=512,
        description="密级（知识权限组），逗号分隔，`*` 表示全部。"
                    "留空表示按空间名自动派生（兼容存量行为）。"
                    "例：`hr,finance` 表示可读 hr 与 finance 密级的文档。")
    display_name: Optional[str] = Field(None, max_length=64)


class UserUpdateRequest(BaseModel):
    role: Optional[str] = None
    spaces: Optional[str] = None
    perm_groups: Optional[str] = Field(None, max_length=512, description="密级，逗号分隔；空串表示改为按空间名派生")
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8, max_length=128)


class UserListData(BaseModel):
    total: int
    items: List[UserData] = []


# ==================== P3：知识库空间 ====================
class SpaceData(BaseModel):
    space_id: str
    name: str
    description: str = ""
    owner: str = ""
    status: str = "active"
    doc_count: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SpaceCreateRequest(BaseModel):
    space_id: str = Field(..., min_length=2, max_length=64,
                          description="2~64 位字母/数字/下划线/短横线")
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=512)


class SpaceUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)


class SpaceListData(BaseModel):
    total: int
    items: List[SpaceData] = []
