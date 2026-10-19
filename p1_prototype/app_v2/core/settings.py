"""集中配置：所有可变项来自环境变量，支持容器注入与本地 .env 两种方式。

设计原则：
  - 不在业务代码里出现任何硬编码的连接串、模型名、阈值
  - 每个配置项都有安全默认值，缺配置时给出明确报错而不是静默失败
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]             # p1_prototype/（core → app_v2 → p1_prototype）
PROJECT_DIR = BASE_DIR.parent                              # 项目根 E:/knowledge
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data_v2")))


def _project_root() -> Path:
    """向上查找包含 docker-compose.yml 或 .env 的目录，避免层级写死。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "docker-compose.yml").exists() or (parent / ".env").exists():
            return parent
    return PROJECT_DIR

# 本地开发：复用根目录密钥，再允许 app_v2 自己的 .env 覆盖
load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(BASE_DIR / ".env", override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # ---------- 服务 ----------
    app_name: str = "企业智能知识库"
    app_version: str = "1.3.0-P4"
    # 多实例部署时区分"哪个副本"。留空则取容器/主机名。
    # 用途：日志的 instance 字段、/health 的实例信息、排查"只有某台机器出问题"。
    instance_id: str = ""
    env: str = Field(default="dev")          # dev | prod
    host: str = "0.0.0.0"
    port: int = 8012
    log_level: str = "INFO"
    log_format: str = "json"                 # P3：json（生产采集用）| text（本地排查用）
    cors_origins: str = "*"

    # ---------- 向量库 ----------
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    collection_name: str = "kb_chunks"
    embed_dim: int = 1024
    # auto：连不上 Qdrant 时降级为本地索引（便于无 Docker 环境下演示）
    vector_backend: str = "auto"             # auto | qdrant | local
    local_index_path: str = ""               # 留空则用 DATA_DIR/local_index.npy（裸 npy，分块写）

    # ---------- 业务库 ----------
    database_url: str = "postgresql://raguser:ragpass123@127.0.0.1:5432/rag_db"
    db_pool_min: int = 1
    db_pool_max: int = 8
    # auto：连不上 PG 时降级为内存存储
    meta_backend: str = "auto"                # auto | postgres | memory

    # ---------- 模型 ----------
    embed_model: str = "BAAI/bge-m3"
    embed_api_key: str = ""
    embed_base_url: str = "https://api.siliconflow.cn/v1"
    chat_model: str = "deepseek-flash"
    chat_api_key: str = ""
    chat_base_url: str = "https://api.deepseek.com"
    llm_temperature: float = 0.2
    # offline 模式下用确定性假模型，保证演示链路完整（不编造内容，仅做抽取）
    model_mode: str = "auto"                  # auto | real | offline

    # ---------- 切片与检索 ----------
    chunk_size: int = 450
    chunk_overlap: int = 80
    recall_k: int = 10
    rrf_k: int = 60
    final_top_k: int = 3
    min_score: float = 0.25
    enable_bm25: bool = True

    # ---------- P3：Rerank 重排 ----------
    # off：不重排，直接用 RRF 结果（回归 P2 行为）
    # llm：用 chat 模型对候选片段打分重排，失败或超时自动降级为 heuristic
    # heuristic：本地启发式（词覆盖 + 向量分 + 名次），零成本零延迟
    rerank_mode: str = "llm"                 # off | llm | heuristic
    rerank_candidates: int = 10              # 参与重排的候选数（RRF 召回后截断）
    rerank_timeout_sec: int = 12
    rerank_min_gain: float = 0.0             # 重排后相关度低于原值的多少倍时回退原排序

    # ---------- Agent ----------
    agent_max_rounds: int = 3
    agent_max_repeat: int = 2
    agent_timeout_sec: int = 60
    agent_max_tool_calls: int = 6

    # ---------- 上传 ----------
    max_upload_mb: int = 20                  # 同步接口（小文件）上限
    allow_ext: str = ".txt,.md,.pdf,.docx"

    # ---------- P4：GB 级大文档 ----------
    # 异步任务通道的大小上限。与 max_upload_mb 分开是刻意的：
    #   同步接口把文件读进内存、在一个请求里跑完，上限必须小（否则 OOM + 网关超时）；
    #   异步任务走流式解析，内存与文件大小无关，可以放到 GB 级。
    # 两个数字混用一个配置，会导致"为了支持大文件而把同步接口也放开"，
    # 那是最典型的自伤配置。
    max_async_upload_mb: int = 4096
    # 超过这个阈值就走流式入库（iter_parse_file + StreamingSplitter + 分批向量化）。
    # 低于它走全量路径：全量更快、代码更直白，没必要为小文件付生成器开销。
    stream_ingest_threshold_mb: int = 8

    # ---------- P2：鉴权 ----------
    auth_enabled: bool = True                # 关掉仅用于本地调试，生产必须为 True
    # "jwt"：P3 收敛模式。除固定的"入场券/机器抓取"端点外，一律要求 JWT。
    #        API Key 只剩一个合法用途——签发 JWT（见 POST /api/v1/auth/token）。
    # "any"：P2 兼容模式。API Key 可直接当 Bearer 调业务接口（保留给存量集成）。
    # 默认取严格模式：验收标准要求"所有接口必须携带 JWT Token"，
    # 而"双通道默认开启"在语义上等于"不携带 JWT 也能访问"，两者不能同时成立。
    auth_mode: str = "jwt"                   # jwt | any
    auth_public_paths: str = (
        "/,/docs,/redoc,/openapi.json,/api/v1/health,/api/v1/ready,"
        "/api/v1/auth/login,/api/v1/auth/refresh,/api/v1/auth/methods,"
        "/api/v1/auth/sso/login,/api/v1/auth/sso/callback,/api/v1/auth/sso/ticket,"
        "/api/v1/auth/token,/metrics,/dashboard,/healthz"
    )
    # 严格模式下**永远**免认证的端点：它们本身就是"获取凭证"或"机器探活/抓取"，
    # 让它们要求凭证会形成逻辑闭环。单独列出而不是混在 auth_public_paths 里，
    # 是为了让"必须带 JWT 的业务接口"与"入场券接口"在代码里一目了然。
    auth_bootstrap_paths: str = (
        "/,/docs,/redoc,/openapi.json,/api/v1/health,/api/v1/ready,"
        "/api/v1/auth/login,/api/v1/auth/refresh,/api/v1/auth/methods,"
        # SSO 三个端点必须在白名单里，否则整条链路是死的：
        #   /sso/login    —— 此时用户还没有任何凭证，要求凭证等于"登录前必须先登录"
        #   /sso/callback —— 由 IdP 的浏览器跳转过来，请求里只有 code/state 和 state Cookie
        #   /sso/ticket   —— 用一次性票据换 token，票据本身就是凭证（60 秒、一次性）
        # 三者的安全性都不依赖 JWT，而分别依赖：IdP 侧的登录、state+nonce 校验、
        # 以及"票据不可预测且只能换一次"。
        "/api/v1/auth/sso/login,/api/v1/auth/sso/callback,/api/v1/auth/sso/ticket,"
        "/api/v1/auth/token"
    )
    # /metrics 与 /dashboard 不走 JWT，而是走一条独立的"抓取令牌"（Bearer）：
    # Prometheus 只支持静态 header，没法先登录再刷新 token。
    # /metrics 默认要求该令牌（否则整个路径/状态码/延迟分布对任何能连 8012 的人开放）；
    # /dashboard 返回的是不含数据的空壳，页面自己带令牌去拉数据，故默认放行。
    metrics_token: str = ""                  # 留空则自动生成并打印到日志
    metrics_protected: bool = True           # False：/metrics 完全免认证（仅限纯内网调试）
    dashboard_public: bool = True
    auth_cache_ttl: int = 60                 # 密钥校验结果缓存秒数（PBKDF2 较慢）
    bootstrap_admin_key: str = ""            # 指定初始管理员密钥，留空则自动生成并打印

    # ---------- P3：JWT ----------
    jwt_enabled: bool = True
    jwt_secret: str = ""                     # 留空则读取/生成 DATA_DIR/jwt_secret
    jwt_secret_file: str = ""                # 指定密钥文件路径，留空用 DATA_DIR/jwt_secret
    jwt_issuer: str = "rag-kb"
    jwt_access_ttl: int = 1800               # access token 30 分钟
    jwt_refresh_ttl: int = 604800            # refresh token 7 天
    jwt_blacklist_enabled: bool = True
    jwt_blacklist_prefix: str = "rag:jwt:blacklist:"
    bootstrap_admin_user: str = ""           # 初始管理员账号，留空则用 admin
    bootstrap_admin_password: str = ""       # 初始管理员密码，留空则自动生成并打印

    # ---------- P3：知识库空间 ----------
    default_space_id: str = "default"
    default_space_name: str = "默认空间"

    # ---------- P3：限流 ----------
    rate_limit_enabled: bool = True
    rate_limit_default: int = 120            # 每窗口允许请求数
    rate_limit_window_sec: int = 60
    # 细分规则：路径前缀=次数，命中更长前缀优先；未命中走 default
    rate_limit_rules: str = "/api/v1/chat=30,/api/v1/documents=20,/api/v1/auth/login=10,/metrics=600"
    rate_limit_prefix: str = "rag:rl:"

    # ---------- P3：缓存（新增两类） ----------
    cache_ttl_context: int = 1800            # 对话上下文缓存 30 分钟
    context_max_turns: int = 8               # 上下文最多保留轮数

    # ---------- P2：缓存 ----------
    cache_enabled: bool = True
    redis_url: str = ""                      # 留空则只用进程内缓存
    redis_timeout_ms: int = 800
    cache_l1_size: int = 2048
    cache_ttl_retrieval: int = 300           # 检索结果 5 分钟
    cache_ttl_answer: int = 600              # 问答结果 10 分钟
    cache_ttl_stats: int = 10

    # ---------- P2：审计 ----------
    audit_enabled: bool = True
    audit_body_limit: int = 512              # 审计 detail 中记录请求体/参数的截断长度

    # ---------- P2：监控 ----------
    metrics_enabled: bool = True
    slow_request_ms: int = 3000              # 超过该耗时的请求记为慢请求

    # ---------- P3：监控栈（Prometheus / Grafana）----------
    # 只影响容器编排与文档提示，应用侧不需要知道 Grafana 地址
    grafana_port: int = 3000
    prometheus_retention_days: int = 7

    # ==================== P4 ====================

    # ---------- P4：多租户 ----------
    # 租户是**最外层**的隔离边界：先按租户切分，再在租户内按空间/密级切分。
    # 层级关系固定为 租户 → 空间 → 密级，三层缺一不可。
    tenant_enabled: bool = True
    default_tenant_id: str = "default"       # 存量数据归入该租户，保证升级不丢数据
    default_tenant_name: str = "默认租户"
    # 超管可跨租户视角；业务角色永远只在本租户内活动。
    # 这个开关只影响"能否切换租户上下文"，不影响数据隔离本身。
    root_tenant_admin: str = "root"          # 拥有该角色的账号可跨租户

    # ---------- P4：MinIO 对象存储 ----------
    # 原件不再只存在内存/临时目录：MinIO 提供可回溯的原件与预签名直链。
    minio_enabled: bool = True
    minio_endpoint: str = "minio:9000"       # 容器内地址；宿主机访问用 localhost:9000
    minio_public_endpoint: str = ""          # 预签名 URL 对外的地址，留空则用 minio_endpoint
    minio_access_key: str = "ragadmin"
    minio_secret_key: str = "ragminio2026"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False
    minio_presign_expire: int = 3600         # 预签名链接有效期（秒）
    # 关闭时降级为"不存原件"（P3 行为），不影响入库与检索
    #
    # 超时必须显式给：minio-py 默认不设超时，依赖 urllib3 的重试策略，
    # 一次 DNS 解析失败（比如 compose 里 minio 还没起来）要重试十几秒才返回。
    # 症状是**对象存储挂掉时所有入库任务一起变慢**，而不是干脆地失败——
    # 这类"降级但慢"的故障比直接报错更难定位。给短超时 + 少重试，
    # 让它快速失败，走已有的"无原件模式"降级。
    minio_timeout: int = 5                   # 连接/读超时（秒）
    minio_retries: int = 1                   # 重试次数：1 次足够覆盖瞬时抖动
    minio_failure_cooldown: int = 30         # 失败冷却：期间直接走降级，不再重试

    # ---------- P4：异步任务队列（Celery）----------
    celery_enabled: bool = True
    # 留空则用 redis_url 的 0 号库；broker 与 result backend 分开库，避免键冲突
    celery_broker_url: str = ""
    celery_result_url: str = ""
    celery_task_max_retries: int = 3
    celery_task_time_soft_limit: int = 300   # 单任务软超时（秒），超时抛异常可清理
    celery_task_time_hard_limit: int = 360
    # 同步模式：置 true 时任务在进程内直接执行（无 worker 也能跑通验收）
    celery_eager: bool = False

    # ---------- P4：ELK 日志集群 ----------
    log_ship_enabled: bool = True            # 结构化日志额外落文件，供 Logstash 采集
    log_ship_path: str = ""                  # 留空则用 DATA_DIR/logs/app.jsonl
    log_ship_max_bytes: int = 64 * 1024 * 1024
    log_ship_backup_count: int = 3
    kibana_port: int = 5601
    elasticsearch_url: str = "http://elasticsearch:9200"
    log_index_prefix: str = "rag-kb-logs"

    # ---------- P4：告警通知 ----------
    alert_enabled: bool = True
    alert_eval_interval_sec: int = 60        # 规则评估周期
    alert_silence_sec: int = 300             # 同一规则重复触发的最小间隔（去重/静默）
    # 应用内评估开关。多实例部署时，若每个实例都跑评估，同一条故障会被
    # 每个实例各报一次（N 份重复告警）。生产建议置 false，由独立的
    # `alert` 进程或某个指定实例承担（见部署文档"告警归属"一节）。
    alert_eval_in_process: bool = True
    # Webhook 渠道：企业微信/钉钉/Slack 都是"POST 一段 JSON"
    alert_webhook_url: str = ""
    alert_webhook_timeout: int = 5
    # 邮件渠道（SMTP）。未配置时该渠道静默跳过，不报错。
    alert_smtp_host: str = ""
    alert_smtp_port: int = 465
    alert_smtp_user: str = ""
    alert_smtp_password: str = ""
    alert_smtp_from: str = ""
    alert_mail_to: str = ""

    # ---------- P4：OIDC 单点登录 ----------
    oidc_enabled: bool = False               # 未配置 IdP 时保持关闭
    oidc_issuer: str = ""                    # 例：http://keycloak:8080/realms/rag
    oidc_client_id: str = "rag-kb"
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""              # 留空则按请求 Host 推导
    oidc_scopes: str = "openid profile email"
    # 从 IdP 的 userinfo/claims 里取哪些字段映射到本地账号
    oidc_username_claim: str = "preferred_username"
    oidc_email_claim: str = "email"
    oidc_name_claim: str = "name"
    # 默认角色与租户：SSO 首次登录自动建号时使用；
    # 若 IdP 侧按组下发角色，可用 oidc_role_claim + oidc_role_map 覆盖。
    oidc_default_role: str = "user"
    oidc_default_tenant: str = "default"
    oidc_role_claim: str = ""
    oidc_role_map: str = ""                  # 例："rag-admins=admin,rag-users=user"
    # 租户 claim：IdP 侧把租户/组织标识放在哪个字段。
    # 留空则退回前端传的 hint 或默认租户。支持点号路径（如 `org.tenant`）。
    oidc_tenant_claim: str = ""
    # 自动建号开关：关闭时只允许已存在的本地账号通过 SSO 登录
    oidc_auto_provision: bool = True

    # ---------- P3：结构化日志 ----------
    log_json_indent: int = 0                 # >0 则美化输出，仅本地调试用

    # ---------- 派生属性 ----------
    @property
    def allow_ext_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allow_ext.split(",") if e.strip()}

    @property
    def rate_limit_rule_map(self) -> list[tuple[str, int]]:
        """解析为 [(路径前缀, 限次)]，按前缀长度倒序，保证更长前缀优先匹配。

        注意这里必须做一次路径规范化：环境变量里的 `/api/v1/chat=30` 在
        Git Bash / MSYS2 / Cygwin 下会被 shell 自动改写为
        `E:/Git/api/v1/chat=30`（POSIX 路径到 Windows 路径的转换），
        若不做归一，这条规则会静默失效、全部请求落回默认阈值——
        排查时表现是"限流配了却不生效"，极难定位。
        """
        rules: list[tuple[str, int]] = []
        for item in (self.rate_limit_rules or "").split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            prefix, _, count = item.rpartition("=")
            prefix = self._normalize_rule_prefix(prefix)
            if not prefix.startswith("/"):
                continue
            try:
                rules.append((prefix, int(count)))
            except ValueError:
                continue
        return sorted(rules, key=lambda x: -len(x[0]))

    @staticmethod
    def _normalize_rule_prefix(prefix: str) -> str:
        """把被 shell 改写过的路径还原成以 `/` 开头的 URL 路径。

        MSYS 的转换规则并非只加盘符，取决于安装位置，可能得到：
          `E:/api/v1/chat`               → `/api/v1/chat`
          `/c/Git/api/v1/chat`           → `/api/v1/chat`
          `D:/work/tools/api/v1/chat`    → `/api/v1/chat`
        因此不能只剥盘符，而要**以第一个 `/api/`（或 `/metrics`）为锚点反推**，
        这才是真正稳定的判据：URL 路径的前缀只可能是这两种起点之一。
        """
        p = (prefix or "").strip().replace("\\", "/")
        for anchor in ("/api/", "/metrics"):
            idx = p.find(anchor)
            if idx >= 0:
                return p[idx:]
        if len(p) > 2 and p[1] == ":" and p[2] == "/":
            return p[2:]
        return p

    @property
    def jwt_key_file(self) -> Path:
        return Path(self.jwt_secret_file) if self.jwt_secret_file else DATA_DIR / "jwt_secret"

    def resolved_jwt_secret(self) -> str:
        """密钥优先级：环境变量 > 密钥文件 > 自动生成并落盘。

        必须持久化：容器重建后若换了密钥，所有已签发 token 立即失效，
        用户会被强制重新登录（演示效果差，也不符合"可滚动重启"的生产要求）。
        """
        if self.jwt_secret:
            return self.jwt_secret
        from app_v2.core.jwt_utils import load_or_create_secret

        return load_or_create_secret(str(self.jwt_key_file))

    @property
    def cors_list(self) -> list[str]:
        return [c.strip() for c in self.cors_origins.split(",") if c.strip()]

    @property
    def docs_dir(self) -> Path:
        return BASE_DIR / "data" / "docs"

    @property
    def public_path_set(self) -> set[str]:
        return {p.strip() for p in self.auth_public_paths.split(",") if p.strip()}

    @property
    def bootstrap_path_set(self) -> set[str]:
        """严格模式下依然免认证的端点（入场券 + 探活 + 文档）。"""
        return {p.strip() for p in self.auth_bootstrap_paths.split(",") if p.strip()}

    @property
    def jwt_strict(self) -> bool:
        """是否开启"只认 JWT"的严格模式。

        metrics/dashboard 被单独摘出来：它们不服务人类用户，而是服务
        Prometheus 抓取器与看板空壳页，且各有独立的令牌机制。
        """
        return self.auth_enabled and self.auth_mode.lower() != "any"

    def is_public(self, path: str) -> bool:
        """该路径是否完全跳过认证。

        前端页面与静态资源单独处理（见下方的 P4 分支），不进白名单字符串：
        它们的数量会随前端迭代变化，写死在环境变量里必然出现
        "新加一个页面结果 401"的情况，而且这类 401 发生在浏览器拿 HTML 时，
        用户看到的是空白页 + JSON 错误，极难联想到鉴权配置。
        """

        # ---------- P4：前端页面与静态资源 ----------
        # 页面本身**不含任何数据**，数据全部来自带鉴权的 /api/v1 接口，
        # 所以放行页面不构成信息泄露；真正拦住未登录用户的是接口层。
        # 反过来，若页面要求认证：浏览器打开 /login 会拿到 401 JSON，
        # 用户连登录框都看不到——登录页自己需要登录，是死循环。
        if path in ("/", "/login", "/app", "/admin", "/index.html"):
            return True
        if path.startswith("/static/"):
            return True

        if path in self.public_path_set:
            return True
        if self.jwt_strict:
            if path in self.bootstrap_path_set:
                return True
            if path == "/metrics" and not self.metrics_protected:
                return True
            if path == "/dashboard" and self.dashboard_public:
                return True
        return False

    @property
    def effective_metrics_token(self) -> str:
        """抓取令牌。未配置时自动生成并持久化，重启后不变（否则 Prometheus 会一直 401）。"""
        if self.metrics_token:
            return self.metrics_token
        from app_v2.core.jwt_utils import load_or_create_secret

        return load_or_create_secret(str(DATA_DIR / "metrics_token"))

    # ---------- P4 派生配置 ----------

    @property
    def effective_minio_endpoint(self) -> str:
        """S3 客户端连接用的地址：**内网** endpoint。

        客户端（API 进程）与浏览器看到的地址不是一回事：
        进程认 `minio:9000`（compose 服务名），浏览器只认对外域名。
        因此这里返回内网地址，对外地址只在生成预签名 URL 时替换 host。
        """
        return self.minio_endpoint

    @property
    def minio_browser_endpoint(self) -> str:
        """浏览器可见地址。未配置时退回内网地址（容器内自测可用）。"""
        return self.minio_public_endpoint or self.minio_endpoint

    @property
    def effective_broker_url(self) -> str:
        """Celery broker。默认复用 Redis 的 0 号库。"""
        if self.celery_broker_url:
            return self.celery_broker_url
        return self.redis_url or "redis://127.0.0.1:6379/0"

    @property
    def effective_result_url(self) -> str:
        """任务结果后端。用 1 号库，避免与 broker 的键混在一起——
        broker 里的键由 Celery 自己管理，混库排查时极难区分。"""
        if self.celery_result_url:
            return self.celery_result_url
        base = self.effective_broker_url
        if "/" in base.rsplit("@", 1)[-1]:
            head, _, tail = base.rpartition("/")
            if tail.isdigit():
                return f"{head}/1"
        return base.rstrip("/") + "/1"

    @property
    def effective_log_ship_path(self) -> str:
        if self.log_ship_path:
            return self.log_ship_path
        return str(DATA_DIR / "logs" / "app.jsonl")

    @property
    def effective_oidc_redirect_uri(self) -> str:
        return self.oidc_redirect_uri

    @property
    def oidc_role_map_parsed(self) -> dict:
        """`rag-admins=admin,rag-users=user` -> {"rag-admins": "admin", ...}

        IdP 侧的角色名通常是英文组名，本地角色是 admin/kb_admin/user，
        两者必须显式映射，不能靠字符串猜。
        """
        out = {}
        for item in (self.oidc_role_map or "").split(","):
            if "=" not in item:
                continue
            k, _, v = item.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
        return out

    def resolved_model_mode(self) -> str:
        if self.model_mode != "auto":
            return self.model_mode
        return "real" if (self.embed_api_key and self.chat_api_key) else "offline"


def _alias_legacy_keys() -> None:
    """兼容项目原有 .env 的变量名（API_KEY_SILICON / DEEPSEEK_API_KEY 等）。

    原型复用根目录已有配置，不要求用户为同一套模型再配一遍。
    """
    aliases = {
        "EMBED_API_KEY": ("API_KEY_SILICON", "SILICONFLOW_API_KEY"),
        "EMBED_BASE_URL": ("BASE_URL_SILICON",),
        "EMBED_MODEL": ("EMBED_MODEL_NAME",),
        "CHAT_API_KEY": ("DEEPSEEK_API_KEY",),
        "CHAT_BASE_URL": ("DEEPSEEK_BASE_URL",),
        "COLLECTION_NAME": ("COLLECTION_NAME",),
        "EMBED_DIM": ("EMBED_DIM",),
        "CHUNK_SIZE": ("CHUNK_SIZE",),
        "CHUNK_OVERLAP": ("CHUNK_OVERLAP",),
        "TOP_K": ("TOP_K",),
    }
    for target, sources in aliases.items():
        if os.getenv(target):
            continue
        for src in sources:
            val = os.getenv(src)
            if val:
                os.environ[target] = val
                break


@lru_cache
def get_settings() -> Settings:
    _alias_legacy_keys()
    s = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()