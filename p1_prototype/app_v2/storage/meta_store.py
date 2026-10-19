"""业务库（PostgreSQL）：文档元数据、会话消息、问答审计。

为什么需要业务库（而不只是向量库）：
  向量库存的是"片段语义"，回答不了"谁在什么时候传了什么、问过什么"。
  文档生命周期、会话历史、审计追踪必须靠关系库，这也是企业场景的刚需。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app_v2.core.settings import settings
from app_v2.core.security import spaces_wildcard

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id            BIGSERIAL PRIMARY KEY,
    doc_id        VARCHAR(64) UNIQUE NOT NULL,
    file_name     VARCHAR(512) NOT NULL,
    file_ext      VARCHAR(16)  NOT NULL DEFAULT '',
    file_size     BIGINT       NOT NULL DEFAULT 0,
    char_count    INTEGER      NOT NULL DEFAULT 0,
    chunk_count   INTEGER      NOT NULL DEFAULT 0,
    perm_group    VARCHAR(64)  NOT NULL DEFAULT 'all',
    space_id      VARCHAR(64)  NOT NULL DEFAULT 'default',
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    uploader      VARCHAR(64)  NOT NULL DEFAULT 'system',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_documents_file_name ON documents(file_name);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_space ON documents(space_id);
-- 同名覆盖的语义在 P3 收窄为「同一空间内同名覆盖」：不同空间可以有同名文件，
-- 否则两个部门各放一份《报销制度》会互相覆盖。
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_space_name
    ON documents(space_id, file_name) WHERE status='active';

CREATE TABLE IF NOT EXISTS chunks_meta (
    id          BIGSERIAL PRIMARY KEY,
    chunk_id    VARCHAR(128) UNIQUE NOT NULL,
    doc_id      VARCHAR(64)  NOT NULL,
    seq         INTEGER      NOT NULL,
    char_len    INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks_meta(doc_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  VARCHAR(64)  NOT NULL,
    role        VARCHAR(16)  NOT NULL,
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS qa_logs (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    VARCHAR(32)  NOT NULL DEFAULT '-',
    session_id  VARCHAR(64)  NOT NULL DEFAULT '',
    question    TEXT         NOT NULL,
    answer      TEXT         NOT NULL DEFAULT '',
    mode        VARCHAR(16)  NOT NULL DEFAULT 'rag',
    rejected    BOOLEAN      NOT NULL DEFAULT FALSE,
    confidence  REAL         NOT NULL DEFAULT 0,
    sources     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    latency_ms  INTEGER      NOT NULL DEFAULT 0,
    rounds      INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qa_created ON qa_logs(created_at DESC);

-- ===== P2：鉴权与审计 =====
CREATE TABLE IF NOT EXISTS api_keys (
    id            BIGSERIAL PRIMARY KEY,
    key_id        VARCHAR(32)  UNIQUE NOT NULL,
    owner         VARCHAR(128) NOT NULL,
    role          VARCHAR(32)  NOT NULL DEFAULT 'viewer',
    key_hash      VARCHAR(128) NOT NULL,
    salt          VARCHAR(64)  NOT NULL,
    perm_group    VARCHAR(128) NOT NULL DEFAULT 'all',
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    description   VARCHAR(512) NOT NULL DEFAULT '',
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    VARCHAR(32)  NOT NULL DEFAULT '-',
    actor       VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    role        VARCHAR(32)  NOT NULL DEFAULT '-',
    action      VARCHAR(64)  NOT NULL,
    resource    VARCHAR(256) NOT NULL DEFAULT '',
    result      VARCHAR(16)  NOT NULL DEFAULT 'success',
    status_code INTEGER      NOT NULL DEFAULT 200,
    ip          VARCHAR(64)  NOT NULL DEFAULT '',
    user_agent  VARCHAR(256) NOT NULL DEFAULT '',
    detail      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    latency_ms  INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_logs(result);

-- ===== P3：知识库空间 =====
CREATE TABLE IF NOT EXISTS kb_spaces (
    id          BIGSERIAL PRIMARY KEY,
    space_id    VARCHAR(64)  UNIQUE NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(512) NOT NULL DEFAULT '',
    owner       VARCHAR(128) NOT NULL DEFAULT 'system',
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_spaces_status ON kb_spaces(status);
-- ===== P3：用户（账号密码登录，签发 JWT）=====
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    user_id       VARCHAR(64)  UNIQUE NOT NULL,
    username      VARCHAR(128) UNIQUE NOT NULL,
    display_name  VARCHAR(128) NOT NULL DEFAULT '',
    password_hash VARCHAR(128) NOT NULL,
    salt          VARCHAR(64)  NOT NULL,
    role          VARCHAR(32)  NOT NULL DEFAULT 'user',
    spaces        VARCHAR(512) NOT NULL DEFAULT 'default',   -- 逗号分隔，* 表示全部
    -- P3：密级（知识权限组）。与 spaces 正交：
    --   spaces  = 能进哪些业务空间（内容归属）
    --   perm_groups = 能看哪些密级片段（人的属性，如 hr / finance）
    -- 空串表示"跟随空间名派生"（见 user_service._derive_perm_groups），
    -- 保持存量行为不变；显式填写则完全以填写的为准。
    perm_groups   VARCHAR(512) NOT NULL DEFAULT '',
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
-- 存量库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段，
-- 必须显式 ALTER。ADD COLUMN IF NOT EXISTS 是幂等的，重启多少次都安全。
ALTER TABLE users ADD COLUMN IF NOT EXISTS perm_groups VARCHAR(512) NOT NULL DEFAULT '';

-- ===== P3：异常堆栈留痕（结构化日志的落库版本，便于事后排障）=====
CREATE TABLE IF NOT EXISTS error_logs (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    VARCHAR(32)  NOT NULL DEFAULT '-',
    path        VARCHAR(256) NOT NULL DEFAULT '',
    method      VARCHAR(16)  NOT NULL DEFAULT '',
    user_id     VARCHAR(128) NOT NULL DEFAULT '',
    status_code INTEGER      NOT NULL DEFAULT 500,
    exc_type    VARCHAR(128) NOT NULL DEFAULT '',
    message     TEXT         NOT NULL DEFAULT '',
    stack       TEXT         NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_error_created ON error_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_trace ON error_logs(trace_id);

-- ==================================================================
-- ===== P4：多租户 =====
-- ==================================================================
-- 租户是所有业务数据的**最外层**边界：租户 → 空间 → 密级，三层依次收敛。
-- 表设计上采用"宽表 + tenant_id 列"而不是"每租户一个 schema"：
--   - 每租户一 schema 在租户数上千时会让 catalog 膨胀、迁移变慢；
--   - 宽表配合 tenant_id 索引，单库可支撑到数千租户，且迁移只有一份；
--   - 真正的隔离靠**应用层强制下推** + 可选的 RLS 兜底，不依赖 schema 分裂。
CREATE TABLE IF NOT EXISTS tenants (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    VARCHAR(64)  UNIQUE NOT NULL,
    name         VARCHAR(128) NOT NULL,
    description  VARCHAR(512) NOT NULL DEFAULT '',
    -- 租户级配额：防止单租户把共享实例吃满（多租户最现实的故障源）
    max_users    INTEGER      NOT NULL DEFAULT 100,
    max_docs     INTEGER      NOT NULL DEFAULT 10000,
    max_qps      INTEGER      NOT NULL DEFAULT 0,      -- 0 表示不限
    status       VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);

-- 存量表补 tenant_id。DEFAULT 'default' 让升级瞬间所有旧数据归入默认租户，
-- 因此老数据不会因为"没有租户"而从任何查询里消失。
ALTER TABLE users        ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE kb_spaces    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE documents    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE audit_logs   ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE qa_logs      ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';
ALTER TABLE api_keys     ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';

-- 时间序列表的查询几乎都带 "WHERE tenant_id=? ORDER BY created_at DESC"，
-- 所以索引必须 (tenant_id, created_at) 复合，单列索引在租户多时会被优化器放弃。
CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents(tenant_id, created_at DESC);

-- 原件在对象存储里的 key。空串表示"以无原件模式入库"（MinIO 当时不可用）
ALTER TABLE documents ADD COLUMN IF NOT EXISTS object_key VARCHAR(512) NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_spaces_tenant ON kb_spaces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qa_tenant ON qa_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_tenant ON chat_messages(tenant_id, session_id, id);

-- 唯一性约束必须带上租户：两个租户都有 "admin" 账号、都有《报销制度》是正常的。
-- P3 的 username 全局唯一与 (space_id,file_name) 唯一在这里都要放宽。
DO $$
BEGIN
    -- users.username 的全局唯一约束
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_username_key') THEN
        ALTER TABLE users DROP CONSTRAINT users_username_key;
    END IF;
EXCEPTION WHEN others THEN
    RAISE NOTICE 'drop users_username_key skipped: %', SQLERRM;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tenant_name ON users(tenant_id, username);
CREATE UNIQUE INDEX IF NOT EXISTS uq_spaces_tenant_id  ON kb_spaces(tenant_id, space_id);

-- 文档同名覆盖的语义再收窄一层：同一租户 + 同一空间内同名才覆盖。
-- 原来只看 (space_id, file_name) 的话，A 租户的 default 空间和 B 租户的 default
-- 空间会互相覆盖——这是多租户改造里最隐蔽的数据串户风险。
DROP INDEX IF EXISTS uq_documents_space_name;
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_tenant_space_name
    ON documents(tenant_id, space_id, file_name) WHERE status='active';

-- ==================================================================
-- ===== P4：异步任务 =====
-- ==================================================================
-- 任务表是"业务侧的任务记录"，Celery 的 result backend 只存结果值。
-- 为什么要自己存一份：运维要能回答"这个租户今天有多少任务失败""谁传的文档卡住了"，
-- 而这些维度（tenant_id/actor/doc_id）Celery 都不管。
CREATE TABLE IF NOT EXISTS async_tasks (
    id           BIGSERIAL PRIMARY KEY,
    task_id      VARCHAR(64)  UNIQUE NOT NULL,
    tenant_id    VARCHAR(64)  NOT NULL DEFAULT 'default',
    task_type    VARCHAR(32)  NOT NULL,                -- ingest | reindex | delete
    status       VARCHAR(16)  NOT NULL DEFAULT 'pending', -- pending|running|success|failed
    progress     INTEGER      NOT NULL DEFAULT 0,       -- 0-100
    payload      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    result       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    error        TEXT         NOT NULL DEFAULT '',
    retries      INTEGER      NOT NULL DEFAULT 0,
    actor        VARCHAR(128) NOT NULL DEFAULT 'system',
    doc_id       VARCHAR(64)  NOT NULL DEFAULT '',
    elapsed_ms   INTEGER      NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE async_tasks ADD COLUMN IF NOT EXISTS elapsed_ms INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON async_tasks(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON async_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_doc ON async_tasks(doc_id);

-- ==================================================================
-- ===== P4：告警 =====
-- ==================================================================
CREATE TABLE IF NOT EXISTS alert_events (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    VARCHAR(64)  NOT NULL DEFAULT '*',     -- * 表示平台级告警
    rule_id      VARCHAR(64)  NOT NULL,
    severity     VARCHAR(16)  NOT NULL DEFAULT 'warning', -- info|warning|critical
    title        VARCHAR(256) NOT NULL,
    message      TEXT         NOT NULL DEFAULT '',
    value        DOUBLE PRECISION NOT NULL DEFAULT 0,
    threshold    DOUBLE PRECISION NOT NULL DEFAULT 0,
    labels       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    channels     VARCHAR(128) NOT NULL DEFAULT '',       -- 实际投递成功的渠道，逗号分隔
    status       VARCHAR(16)  NOT NULL DEFAULT 'firing', -- firing|resolved|silenced
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_created ON alert_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_rule ON alert_events(rule_id, created_at DESC);

-- 告警静默：同一规则在窗口内只通知一次，否则 60s 采样会让手机被刷爆。
CREATE TABLE IF NOT EXISTS alert_silences (
    rule_id      VARCHAR(64) PRIMARY KEY,
    until_ts     TIMESTAMPTZ NOT NULL,
    reason       VARCHAR(256) NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> float | None:
    """把 ISO 时间串或 datetime 转成时间戳，解析失败返回 None。"""
    if isinstance(value, datetime):
        return value.timestamp()
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _now_fields() -> dict:
    """内存实现推进的时间字段。与 Postgres 的 updated_at=NOW() 对应。"""
    ts = _now()
    return {"updated_at": ts, "created_at": ts}


class MetaStore:
    """业务库抽象接口（便于降级实现与测试替换）。"""

    backend = "base"

    def init(self) -> None: ...
    def health(self) -> dict: ...
    def save_document(self, doc: dict) -> None: ...
    def get_document(self, doc_id: str, tenant_id: str) -> dict | None: ...
    def get_document_by_name(self, file_name: str, tenant_id: str, space_id: str = "") -> dict | None: ...
    def list_documents(self, tenant_id: str, keyword: str = "", limit: int = 100,
                       perm_groups: list[str] | None = None) -> list[dict]: ...
    def delete_document(self, doc_id: str) -> None: ...
    def replace_chunks(self, doc_id: str, chunks: list[dict]) -> None: ...
    def count_chunks(self, doc_id: str) -> int: ...
    def add_message(self, session_id: str, role: str, content: str) -> None: ...
    def get_history(self, session_id: str, limit: int = 10) -> list[dict]: ...
    def clear_session(self, session_id: str) -> None: ...
    def log_qa(self, record: dict) -> None: ...
    def stats(self) -> dict: ...
    # ---- P2：密钥与审计 ----
    def save_api_key(self, rec: dict) -> None: ...
    def get_api_key(self, key_id: str) -> dict | None: ...
    def find_api_key_by_hash(self, key_hash: str) -> dict | None: ...
    def list_api_keys(self, owner: str = "", include_revoked: bool = False) -> list[dict]: ...
    def revoke_api_key(self, key_id: str) -> bool: ...
    def touch_api_key(self, key_id: str) -> None: ...
    def log_audit(self, record: dict) -> None: ...
    def query_audit(self, actor: str = "", action: str = "", result: str = "",
                    limit: int = 100, tenant_id: str = "") -> list[dict]: ...
    def audit_stats(self) -> dict: ...
    # ---- P3：空间 / 用户 / 错误日志 ----
    def save_space(self, rec: dict) -> None: ...
    def get_space(self, space_id: str, tenant_id: str = "") -> dict | None: ...
    def list_spaces(self, status: str = "active", tenant_id: str = "") -> list[dict]: ...
    def delete_space(self, space_id: str, tenant_id: str = "") -> bool: ...
    def save_user(self, rec: dict) -> None: ...
    def get_user(self, user_id: str) -> dict | None: ...
    def get_user_by_name(self, username: str, tenant_id: str = "") -> dict | None: ...
    def list_users(self, tenant_id: str = "") -> list[dict]: ...
    def update_user(self, user_id: str, fields: dict) -> bool: ...
    def touch_user_login(self, user_id: str) -> None: ...
    def log_error(self, record: dict) -> None: ...
    def query_errors(self, trace_id: str = "", limit: int = 50) -> list[dict]: ...
    def space_stats(self) -> dict: ...
    # ---- P4：租户 ----
    def save_tenant(self, rec: dict) -> dict: ...
    def get_tenant(self, tenant_id: str) -> dict | None: ...
    def list_tenants(self) -> list[dict]: ...
    def update_tenant(self, tenant_id: str, fields: dict) -> dict: ...
    def count_users(self, tenant_id: str) -> int: ...
    def count_documents(self, tenant_id: str) -> int: ...
    def count_spaces(self, tenant_id: str) -> int: ...
    # ---- P4：异步任务 ----
    def save_task(self, rec: dict) -> dict: ...
    def update_task(self, task_id: str, fields: dict | None = None, **kwargs) -> None: ...
    def purge_tasks(self, tenant_id: str = "", older_than_days: int = 7) -> int: ...
    def get_task(self, task_id: str, tenant_id: str = "") -> dict | None: ...
    def list_tasks(self, tenant_id: str = "", status: str = "", task_type: str = "",
                   limit: int = 50) -> list[dict]: ...
    def task_stats(self, tenant_id: str = "") -> dict: ...
    # ---- P4：告警 ----
    def save_alert(self, rec: dict) -> None: ...
    def list_alerts(self, tenant_id: str = "", limit: int = 50,
                    severity: str = "") -> list[dict]: ...
    def alert_stats(self) -> dict: ...
    def get_silence(self, rule_id: str) -> float | None: ...
    def set_silence(self, rule_id: str, until_ts: float, reason: str = "") -> None: ...


class PostgresMetaStore(MetaStore):
    backend = "postgres"

    def __init__(self) -> None:
        from psycopg2.pool import ThreadedConnectionPool

        self.pool = ThreadedConnectionPool(
            settings.db_pool_min, settings.db_pool_max, dsn=settings.database_url
        )
        self._lock = threading.Lock()

    # ---------- 基础 ----------
    def init(self) -> None:
        conn = self.pool.getconn()             # type: ignore[union-attr]
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
        finally:
            self.pool.putconn(conn)            # type: ignore[union-attr]
        logger.info("业务库表结构就绪")

    def _query(self, sql: str, params: tuple = (), fetch: str = "all") -> Any:
        conn = self.pool.getconn()             # type: ignore[union-attr]
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    cols = [c[0] for c in cur.description]
                    rows = cur.fetchall()
                    # 带 RETURNING 的写语句同样有 description，必须先提交再返回，
                    # 否则连接归还时事务被回滚，写入静默失效。
                    if not sql.lstrip().upper().startswith("SELECT"):
                        conn.commit()
                    if fetch == "one":
                        return dict(zip(cols, rows[0])) if rows else None
                    return [dict(zip(cols, row)) for row in rows]
                conn.commit()
                return cur.rowcount
        finally:
            self.pool.putconn(conn)            # type: ignore[union-attr]

    def health(self) -> dict:
        try:
            self._query("SELECT 1", fetch="one")
            return {"backend": self.backend, "status": "ok"}
        except Exception as exc:
            return {"backend": self.backend, "status": "error", "error": str(exc)}

    # ---------- 文档 ----------
    def save_document(self, doc: dict) -> None:
        self._query(
            """INSERT INTO documents (doc_id, file_name, file_ext, file_size, char_count,
                                      chunk_count, perm_group, space_id, status, uploader, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (doc_id) DO UPDATE SET
                 file_name=EXCLUDED.file_name, chunk_count=EXCLUDED.chunk_count,
                 char_count=EXCLUDED.char_count, perm_group=EXCLUDED.perm_group,
                 space_id=EXCLUDED.space_id,
                 status=EXCLUDED.status, updated_at=NOW()""",
            (doc["doc_id"], doc["file_name"], doc.get("file_ext", ""), doc.get("file_size", 0),
             doc.get("char_count", 0), doc.get("chunk_count", 0), doc.get("perm_group", "all"),
             doc.get("space_id", settings.default_space_id),
             doc.get("status", "active"), doc.get("uploader", "system"),
             doc.get("tenant_id", settings.default_tenant_id)),
        )

    def get_document(self, doc_id: str, tenant_id: str) -> dict | None:
        """按 doc_id 取文档。**必须带 tenant_id**：doc_id 虽是雪花串，
        但仍可能被跨租户猜到/复用，只按 doc_id 取等于把隔离交给随机性。"""
        return self._query(
            "SELECT * FROM documents WHERE doc_id=%s AND tenant_id=%s AND status='active'",
            (doc_id, tenant_id), "one")

    def get_document_by_name(self, file_name: str, tenant_id: str, space_id: str = "") -> dict | None:
        """同名覆盖的语义：同一**租户**、同一空间内才算同名。"""
        if space_id:
            return self._query(
                """SELECT * FROM documents WHERE file_name=%s AND space_id=%s AND tenant_id=%s
                   AND status='active' ORDER BY id DESC LIMIT 1""",
                (file_name, space_id, tenant_id), "one")
        return self._query(
            """SELECT * FROM documents WHERE file_name=%s AND tenant_id=%s
               AND status='active' ORDER BY id DESC LIMIT 1""",
            (file_name, tenant_id), "one")

    def list_documents(self, tenant_id: str, keyword: str = "", limit: int = 100,
                       perm_groups: list[str] | None = None,
                       spaces: list[str] | None = None,
                       space_id: str = "") -> list[dict]:
        """perm_groups / spaces 非 None 时在 SQL 层过滤，避免"先截断后鉴权"。

        perm_group 列存的是单组字符串（如 hr），历史数据可能有多组或空值，
        空值统一按 all（公共知识）处理。

        tenant_id 放在 WHERE 的第一个条件：即便调用方在其他参数上写错，
        租户边界这一刀也一定先落下。复合索引 (tenant_id, created_at) 也依赖它最左。
        """
        sql = "SELECT * FROM documents WHERE tenant_id=%s AND status='active'"
        params: list = [tenant_id]
        if keyword:
            sql += " AND file_name ILIKE %s"
            params.append(f"%{keyword}%")
        if perm_groups is not None:
            groups = [g for g in perm_groups if g] or ["all"]
            sql += " AND COALESCE(NULLIF(perm_group, ''), 'all') = ANY(%s)"
            params.append(groups)
        if space_id:
            # 显式指定单个空间时优先于白名单
            sql += " AND space_id = %s"
            params.append(space_id)
        elif not spaces_wildcard(spaces):
            sql += " AND space_id = ANY(%s)"
            params.append([s for s in spaces if s] or [settings.default_space_id])
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        return self._query(sql, tuple(params))

    def delete_document(self, doc_id: str) -> None:
        self._query("UPDATE documents SET status='deleted', updated_at=NOW() WHERE doc_id=%s", (doc_id,))

    def purge_deleted(self, older_than_days: int = 7) -> int:
        """物理清理软删除记录。

        删除采用软删除（保留 status='deleted' 以便审计），但长期运行会持续膨胀，
        因此提供按天数的物理回收。片段元数据已随删除同步清理，这里只回收 documents 行。
        """
        days = max(int(older_than_days), 0)
        rows = self._query(
            """DELETE FROM documents
               WHERE status='deleted'
                 AND updated_at < NOW() - (%s || ' days')::interval
               RETURNING doc_id""", (days,))
        return len(rows or [])

    def replace_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self._query("DELETE FROM chunks_meta WHERE doc_id=%s", (doc_id,))
        conn = self.pool.getconn()             # type: ignore[union-attr]
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks_meta (chunk_id, doc_id, seq, char_len) VALUES (%s,%s,%s,%s)",
                    [(c["chunk_id"], doc_id, c["seq"], c["char_len"]) for c in chunks],
                )
            conn.commit()
        finally:
            self.pool.putconn(conn)            # type: ignore[union-attr]

    def count_chunks(self, doc_id: str) -> int:
        row = self._query("SELECT COUNT(*) AS n FROM chunks_meta WHERE doc_id=%s", (doc_id,), "one")
        return int(row["n"]) if row else 0

    # ---------- 会话 ----------
    def add_message(self, session_id: str, role: str, content: str) -> None:
        self._query("INSERT INTO chat_messages (session_id, role, content) VALUES (%s,%s,%s)",
                    (session_id, role, content))

    def get_history(self, session_id: str, limit: int = 10) -> list[dict]:
        rows = self._query(
            """SELECT role, content FROM (
                 SELECT role, content, id FROM chat_messages WHERE session_id=%s ORDER BY id DESC LIMIT %s
               ) t ORDER BY id ASC""", (session_id, limit))
        return rows

    def clear_session(self, session_id: str) -> None:
        self._query("DELETE FROM chat_messages WHERE session_id=%s", (session_id,))

    # ---------- 审计 ----------
    def log_qa(self, record: dict) -> None:
        self._query(
            """INSERT INTO qa_logs (trace_id, session_id, question, answer, mode, rejected,
                                    confidence, sources, latency_ms, rounds)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (record.get("trace_id", "-"), record.get("session_id", ""), record.get("question", ""),
             record.get("answer", ""), record.get("mode", "rag"), record.get("rejected", False),
             record.get("confidence", 0), json.dumps(record.get("sources", []), ensure_ascii=False),
             record.get("latency_ms", 0), record.get("rounds", 0)),
        )

    def stats(self) -> dict:
        return self._query(
            """SELECT
                 (SELECT COUNT(*) FROM documents WHERE status='active') AS doc_count,
                 (SELECT COUNT(*) FROM chunks_meta) AS chunk_count,
                 (SELECT COUNT(*) FROM qa_logs) AS qa_count,
                 (SELECT COUNT(*) FROM qa_logs WHERE rejected) AS rejected_count,
                 (SELECT COALESCE(ROUND(AVG(latency_ms)),0) FROM qa_logs) AS avg_latency_ms""",
            fetch="one") or {}

    # ---------- P2：密钥管理 ----------
    def save_api_key(self, rec: dict) -> None:
        self._query(
            """INSERT INTO api_keys (key_id, owner, role, key_hash, salt, perm_group,
                                     enabled, description, expires_at, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()))""",
            (rec["key_id"], rec["owner"], rec.get("role", "viewer"), rec["key_hash"], rec["salt"],
             rec.get("perm_group", "all"), rec.get("enabled", True), rec.get("description", ""),
             rec.get("expires_at"), rec.get("created_at")),
        )

    def get_api_key(self, key_id: str) -> dict | None:
        return self._query("SELECT * FROM api_keys WHERE key_id=%s", (key_id,), "one")

    def find_api_key_by_hash(self, key_hash: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM api_keys WHERE key_hash=%s AND revoked_at IS NULL LIMIT 1",
            (key_hash,))
        return rows[0] if rows else None

    def list_api_keys(self, owner: str = "", include_revoked: bool = False) -> list[dict]:
        sql = "SELECT * FROM api_keys WHERE 1=1"
        params: list[Any] = []
        if owner:
            sql += " AND owner=%s"
            params.append(owner)
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY id DESC"
        return self._query(sql, tuple(params))

    def revoke_api_key(self, key_id: str) -> bool:
        n = self._query(
            "UPDATE api_keys SET revoked_at=NOW(), enabled=FALSE WHERE key_id=%s AND revoked_at IS NULL",
            (key_id,))
        return bool(n)

    def touch_api_key(self, key_id: str) -> None:
        self._query("UPDATE api_keys SET last_used_at=NOW() WHERE key_id=%s", (key_id,))

    # ---------- P2：操作审计 ----------
    def log_audit(self, record: dict) -> None:
        self._query(
            """INSERT INTO audit_logs (trace_id, actor, role, action, resource, result,
                                       status_code, ip, user_agent, detail, latency_ms, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (record.get("trace_id", "-"), record.get("actor", "anonymous"),
             record.get("role", "-"), record.get("action", "-"),
             (record.get("resource") or "")[:256], record.get("result", "success"),
             int(record.get("status_code", 200)), (record.get("ip") or "")[:64],
             (record.get("user_agent") or "")[:256],
             json.dumps(record.get("detail", {}), ensure_ascii=False, default=str),
             int(record.get("latency_ms", 0)),
             # 租户必须落库：审计页按租户过滤（idx_audit_tenant），
             # 不写的话所有记录都堆在 DEFAULT 'default' 上，
             # 表现为"B 租户的管理员在审计页能看见 A 租户的全部操作"。
             (record.get("tenant_id") or "default")),
        )

    def query_audit(self, actor: str = "", action: str = "", result: str = "",
                    limit: int = 100, tenant_id: str = "") -> list[dict]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if tenant_id:
            sql += " AND tenant_id=%s"
            params.append(tenant_id)
        if actor:
            sql += " AND actor=%s"
            params.append(actor)
        if action:
            sql += " AND action LIKE %s"
            params.append(f"{action}%")
        if result:
            sql += " AND result=%s"
            params.append(result)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(max(1, min(int(limit), 500)))
        return self._query(sql, tuple(params))

    def audit_stats(self) -> dict:
        return self._query(
            """SELECT
                 COUNT(*) AS total,
                 COUNT(*) FILTER (WHERE result='success') AS success,
                 COUNT(*) FILTER (WHERE result='denied')  AS denied,
                 COUNT(*) FILTER (WHERE result='failed')  AS failed,
                 COUNT(DISTINCT actor) AS actors
               FROM audit_logs WHERE created_at > NOW() - INTERVAL '24 hours'""",
            fetch="one") or {}

    # ---------- P3：知识库空间 ----------
    def save_space(self, rec: dict) -> None:
        self._query(
            """INSERT INTO kb_spaces (space_id, name, description, owner, status, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, space_id) DO UPDATE SET
                 name=EXCLUDED.name, description=EXCLUDED.description,
                 status=EXCLUDED.status, updated_at=NOW()""",
            (rec["space_id"], rec["name"], rec.get("description", ""),
             rec.get("owner", "system"), rec.get("status", "active"),
             rec.get("tenant_id", settings.default_tenant_id)),
        )

    def get_space(self, space_id: str, tenant_id: str = "") -> dict | None:
        """取空间。带租户时按 (tenant_id, space_id) 定位——
        两个租户都有自己的 `default`/`hr` 空间，只按 space_id 取必然串户。"""
        if tenant_id:
            return self._query(
                "SELECT * FROM kb_spaces WHERE space_id=%s AND tenant_id=%s",
                (space_id, tenant_id), "one")
        return self._query(
            "SELECT * FROM kb_spaces WHERE space_id=%s ORDER BY id ASC LIMIT 1",
            (space_id,), "one")

    def list_spaces(self, status: str = "active", tenant_id: str = "") -> list[dict]:
        sql = "SELECT * FROM kb_spaces WHERE 1=1"
        params: list[Any] = []
        if tenant_id:
            sql += " AND tenant_id=%s"
            params.append(tenant_id)
        if status:
            sql += " AND status=%s"
            params.append(status)
        sql += " ORDER BY (space_id=%s) DESC, id ASC"
        params.append(settings.default_space_id)
        rows = self._query(sql, tuple(params))
        # 顺带带上各空间的文档数，前端列表页直接用，不必逐个再查。
        # 统计同样要按租户收口：否则 A 租户的 default 空间会显示 B 租户的文档数。
        counts: dict = {}
        if rows:
            cnt_sql = ("SELECT space_id, COUNT(*) AS n FROM documents "
                       "WHERE status='active'")
            cnt_params: list[Any] = []
            if tenant_id:
                cnt_sql += " AND tenant_id=%s"
                cnt_params.append(tenant_id)
            cnt_sql += " GROUP BY space_id"
            counts = {r["space_id"]: r["n"] for r in self._query(cnt_sql, tuple(cnt_params))}
        for r in rows:
            r["doc_count"] = int(counts.get(r["space_id"], 0))
        return rows

    def delete_space(self, space_id: str, tenant_id: str = "") -> bool:
        """软删除空间。空间内还有活跃文档时拒绝删除，避免文档变成孤儿。

        tenant_id 必带：space_id 只在租户内唯一，不带租户会一次删掉
        所有租户的同名空间（default 空间会被一次性打掉）。
        """
        if not tenant_id:
            raise ValueError("delete_space 必须指定 tenant_id")
        n = self._query(
            "UPDATE kb_spaces SET status='deleted', updated_at=NOW() "
            "WHERE tenant_id=%s AND space_id=%s AND status='active'",
            (tenant_id, space_id))
        return bool(n)

    def space_stats(self) -> dict:
        return self._query(
            """SELECT
                 (SELECT COUNT(*) FROM kb_spaces WHERE status='active') AS space_count,
                 (SELECT COUNT(*) FROM documents WHERE status='active') AS doc_count""",
            fetch="one") or {}

    # ---------- P4：租户 ----------
    def save_tenant(self, rec: dict) -> dict:
        row = self._query(
            """INSERT INTO tenants (tenant_id, name, description, max_users, max_docs,
                                    max_qps, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id) DO UPDATE SET
                 name=EXCLUDED.name, description=EXCLUDED.description,
                 max_users=EXCLUDED.max_users, max_docs=EXCLUDED.max_docs,
                 max_qps=EXCLUDED.max_qps, status=EXCLUDED.status, updated_at=NOW()
               RETURNING *""",
            (rec["tenant_id"], rec["name"], rec.get("description", ""),
             int(rec.get("max_users") or 0), int(rec.get("max_docs") or 0),
             int(rec.get("max_qps") or 0), rec.get("status", "active")),
            fetch="one")
        return row or rec

    def get_tenant(self, tenant_id: str) -> dict | None:
        return self._query("SELECT * FROM tenants WHERE tenant_id=%s", (tenant_id,), "one")

    def list_tenants(self) -> list[dict]:
        return self._query("SELECT * FROM tenants ORDER BY tenant_id")

    def update_tenant(self, tenant_id: str, fields: dict) -> dict:
        allowed = {"name", "description", "max_users", "max_docs", "max_qps", "status"}
        sets, params = [], []
        for k, v in (fields or {}).items():
            if k not in allowed:
                continue
            sets.append(f"{k}=%s")
            params.append(v)
        if sets:
            sets.append("updated_at=NOW()")
            params.append(tenant_id)
            self._query(f"UPDATE tenants SET {', '.join(sets)} WHERE tenant_id=%s", tuple(params))
        return self.get_tenant(tenant_id) or {}

    def count_users(self, tenant_id: str) -> int:
        row = self._query("SELECT COUNT(*) AS c FROM users WHERE tenant_id=%s",
                          (tenant_id,), "one")
        return int((row or {}).get("c") or 0)

    def count_documents(self, tenant_id: str) -> int:
        row = self._query(
            "SELECT COUNT(*) AS c FROM documents WHERE tenant_id=%s AND status='active'",
            (tenant_id,), "one")
        return int((row or {}).get("c") or 0)

    def count_spaces(self, tenant_id: str) -> int:
        row = self._query(
            "SELECT COUNT(*) AS c FROM kb_spaces WHERE tenant_id=%s AND status='active'",
            (tenant_id,), "one")
        return int((row or {}).get("c") or 0)

    # ---------- P4：异步任务 ----------
    def save_task(self, rec: dict) -> dict:
        self._query(
            """INSERT INTO async_tasks (task_id, tenant_id, task_type, status, progress,
                                        payload, actor, doc_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (task_id) DO NOTHING""",
            (rec["task_id"], rec.get("tenant_id", settings.default_tenant_id),
             rec["task_type"], rec.get("status", "pending"), int(rec.get("progress") or 0),
             json.dumps(rec.get("payload") or {}, ensure_ascii=False),
             rec.get("actor", "system"), rec.get("doc_id", "")),
        )
        return self.get_task(rec["task_id"]) or rec

    def update_task(self, task_id: str, fields: dict | None = None, **kwargs) -> None:
        """更新任务状态。

        同时接受位置字典与关键字两种写法：worker 里习惯写
        `update_task(tid, status="running", progress=10)`，
        而通用更新逻辑又需要一个 dict。两种都支持，省掉一层适配。
        """
        merged = {**(fields or {}), **kwargs}
        allowed = {"status", "progress", "result", "error", "retries",
                   "doc_id", "started_at", "finished_at", "elapsed_ms"}
        sets, params = [], []
        for k, v in merged.items():
            if k not in allowed:
                continue
            if k == "result":
                v = json.dumps(v, ensure_ascii=False, default=str)
            elif k == "started_at" and v is True:
                v = datetime.now()
            elif k == "finished_at" and v is True:
                v = datetime.now()
            sets.append(f"{k}=%s")
            params.append(v)
        if not sets:
            return
        params.append(task_id)
        self._query(f"UPDATE async_tasks SET {', '.join(sets)} WHERE task_id=%s", tuple(params))

    def purge_tasks(self, tenant_id: str = "", older_than_days: int = 7) -> int:
        """清理已结束的任务记录。任务表是日志性质，不清理会无限膨胀。"""
        sql = ("DELETE FROM async_tasks WHERE status IN ('success','failed','cancelled') "
               "AND COALESCE(finished_at, created_at) < NOW() - (%s || ' days')::interval")
        params: list = [str(max(int(older_than_days), 0))]
        if tenant_id:
            sql += " AND tenant_id=%s"
            params.append(tenant_id)
        return int(self._query(sql, tuple(params)) or 0)

    def get_task(self, task_id: str, tenant_id: str = "") -> dict | None:
        if tenant_id:
            return self._query(
                "SELECT * FROM async_tasks WHERE task_id=%s AND tenant_id=%s",
                (task_id, tenant_id), "one")
        return self._query("SELECT * FROM async_tasks WHERE task_id=%s", (task_id,), "one")

    def list_tasks(self, tenant_id: str = "", status: str = "", task_type: str = "",
                   limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM async_tasks WHERE 1=1"
        params: list = []
        if tenant_id:
            sql += " AND tenant_id=%s"
            params.append(tenant_id)
        if status:
            sql += " AND status=%s"
            params.append(status)
        if task_type:
            sql += " AND task_type=%s"
            params.append(task_type)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        return self._query(sql, tuple(params))

    def task_stats(self, tenant_id: str = "") -> dict:
        where, params = "", ()
        if tenant_id:
            where, params = " WHERE tenant_id=%s", (tenant_id,)
        row = self._query(
            f"""SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='pending') AS pending,
                       COUNT(*) FILTER (WHERE status='running') AS running,
                       COUNT(*) FILTER (WHERE status='success') AS success,
                       COUNT(*) FILTER (WHERE status='failed')  AS failed
                FROM async_tasks{where}""",
            params, "one")
        return row or {"total": 0, "pending": 0, "running": 0, "success": 0, "failed": 0}

    # ---------- P4：告警 ----------
    def save_alert(self, rec: dict) -> None:
        self._query(
            """INSERT INTO alert_events (tenant_id, rule_id, severity, title, message,
                                         value, threshold, labels, channels, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (rec.get("tenant_id", "*"), rec["rule_id"], rec.get("severity", "warning"),
             rec["title"], rec.get("message", ""), float(rec.get("value") or 0),
             float(rec.get("threshold") or 0),
             json.dumps(rec.get("labels") or {}, ensure_ascii=False),
             rec.get("channels", ""), rec.get("status", "firing")),
        )

    def list_alerts(self, tenant_id: str = "", limit: int = 50,
                    severity: str = "") -> list[dict]:
        sql = "SELECT * FROM alert_events WHERE 1=1"
        params: list = []
        if tenant_id:
            # 平台级告警（tenant_id='*'）对所有租户可见，租户自己的告警只对自己可见
            sql += " AND (tenant_id=%s OR tenant_id='*')"
            params.append(tenant_id)
        if severity:
            sql += " AND severity=%s"
            params.append(severity)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        return self._query(sql, tuple(params))

    def alert_stats(self) -> dict:
        return self._query(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE severity='critical') AS critical,
                      COUNT(*) FILTER (WHERE severity='warning')  AS warning,
                      COUNT(*) FILTER (WHERE status='firing')     AS firing
               FROM alert_events WHERE created_at > NOW() - INTERVAL '24 hours'""",
            fetch="one") or {}

    def get_silence(self, rule_id: str) -> float | None:
        row = self._query(
            "SELECT EXTRACT(EPOCH FROM until_ts) AS ts FROM alert_silences WHERE rule_id=%s",
            (rule_id,), "one")
        return float(row["ts"]) if row and row.get("ts") is not None else None

    def set_silence(self, rule_id: str, until_ts: float, reason: str = "") -> None:
        self._query(
            """INSERT INTO alert_silences (rule_id, until_ts, reason)
               VALUES (%s, to_timestamp(%s), %s)
               ON CONFLICT (rule_id) DO UPDATE SET
                 until_ts=EXCLUDED.until_ts, reason=EXCLUDED.reason""",
            (rule_id, until_ts, reason),
        )

    # ---------- P3：用户 ----------
    def save_user(self, rec: dict) -> None:
        self._query(
            """INSERT INTO users (user_id, username, display_name, password_hash, salt,
                                  role, spaces, perm_groups, enabled, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (rec["user_id"], rec["username"], rec.get("display_name", ""),
             rec["password_hash"], rec["salt"], rec.get("role", "user"),
             rec.get("spaces", settings.default_space_id),
             rec.get("perm_groups", ""), rec.get("enabled", True),
             rec.get("tenant_id", settings.default_tenant_id)),
        )

    def get_user(self, user_id: str) -> dict | None:
        return self._query("SELECT * FROM users WHERE user_id=%s", (user_id,), "one")

    def get_user_by_name(self, username: str, tenant_id: str = "") -> dict | None:
        """按用户名取用户。

        tenant_id 为空时取**最早**的一条：升级前的调用方（登录、SSO 回调）
        可能在拿到租户之前就要解析账号。此时取最早记录等价于"存量账号"，
        而新租户的账号一定排在后面，不会被误取。

        用户名现在只在租户内唯一（uq_users_tenant_name），所以带 tenant_id
        时必须用 (tenant_id, username) 定位，否则同名账号会串户。
        """
        if tenant_id:
            return self._query(
                "SELECT * FROM users WHERE username=%s AND tenant_id=%s",
                (username, tenant_id), "one")
        return self._query(
            "SELECT * FROM users WHERE username=%s ORDER BY id ASC LIMIT 1",
            (username,), "one")

    def list_users(self, tenant_id: str = "") -> list[dict]:
        if tenant_id:
            return self._query(
                "SELECT * FROM users WHERE tenant_id=%s ORDER BY id ASC", (tenant_id,))
        return self._query("SELECT * FROM users ORDER BY id ASC")

    def update_user(self, user_id: str, fields: dict) -> bool:
        allowed = {"display_name", "role", "spaces", "perm_groups", "enabled",
                   "password_hash", "salt"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=%s")
            params.append(value)
        if not sets:
            return False
        params.append(user_id)
        n = self._query(f"UPDATE users SET {', '.join(sets)} WHERE user_id=%s", tuple(params))
        return bool(n)

    def touch_user_login(self, user_id: str) -> None:
        self._query("UPDATE users SET last_login_at=NOW() WHERE user_id=%s", (user_id,))

    # ---------- P3：错误日志 ----------
    def log_error(self, record: dict) -> None:
        self._query(
            """INSERT INTO error_logs (trace_id, path, method, user_id, status_code,
                                       exc_type, message, stack)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (record.get("trace_id", "-"), (record.get("path") or "")[:256],
             (record.get("method") or "")[:16], (record.get("user_id") or "")[:128],
             int(record.get("status_code", 500)), (record.get("exc_type") or "")[:128],
             (record.get("message") or "")[:2000], (record.get("stack") or "")[:8000]),
        )

    def query_errors(self, trace_id: str = "", limit: int = 50) -> list[dict]:
        if trace_id:
            return self._query("SELECT * FROM error_logs WHERE trace_id=%s ORDER BY id DESC LIMIT %s",
                               (trace_id, max(1, min(int(limit), 200))))
        return self._query("SELECT * FROM error_logs ORDER BY id DESC LIMIT %s",
                           (max(1, min(int(limit), 200)),))

    # ---------- P3：监控探针 ----------
    def pool_stats(self) -> dict:
        """连接池占用 + 一次 SELECT 1 探活。

        psycopg2 的 ThreadedConnectionPool 把空闲连接放在内部 list 里（_pool），
        没有公开 API。这里读私有属性是刻意的：监控指标不值得为它引入
        SQLAlchemy 或自建连接池，而 psycopg2 的 pool 结构十年未变。
        """
        start = time.perf_counter()
        latency = 0.0
        try:
            self._query("SELECT 1", fetch="one")
            latency = round((time.perf_counter() - start) * 1000, 2)
        except Exception:
            latency = -1.0
        try:
            idle = len(getattr(self.pool, "_pool", []) or [])
            used = len(getattr(self.pool, "_used", {}) or {})
        except Exception:
            idle, used = 0, 0
        return {"pool_used": used, "pool_free": idle,
                "pool_max": settings.db_pool_max, "latency_ms": latency}

    def counts(self) -> dict:
        """业务量统计：文档数、切片数、错误日志数。"""
        out = {"documents": 0, "chunks": 0, "error_logs": 0}
        try:
            rows = self._query(
                "SELECT (SELECT COUNT(*) FROM documents) AS documents,"
                "       (SELECT COALESCE(SUM(chunk_count),0) FROM documents) AS chunks,"
                "       (SELECT COUNT(*) FROM error_logs) AS error_logs",
                fetch="one",
            )
            if isinstance(rows, dict):
                out.update({k: int(v or 0) for k, v in rows.items() if k in out})
        except Exception:
            pass
        return out


class MemoryMetaStore(MetaStore):
    """内存降级实现：无 PostgreSQL 时保证接口可用（不持久化）。"""

    backend = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.docs: dict[str, dict] = {}
        self.chunks: dict[str, list[dict]] = {}
        self.messages: dict[str, list[dict]] = {}
        self.logs: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.audits: list[dict] = []
        self.spaces: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.errors: list[dict] = []
        # P4
        self.tenants: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.silences: dict[str, tuple[float, str]] = {}

    def init(self) -> None:
        logger.warning("业务库使用内存实现，重启后数据丢失（仅用于无数据库环境验证）")

    def health(self) -> dict:
        return {"backend": self.backend, "status": "ok", "documents": len(self.docs)}

    def save_document(self, doc: dict) -> None:
        with self._lock:
            doc = {
                **doc,
                # 与 Postgres 的 DEFAULT 'active' 对齐：内存实现若不给默认值，
                # list_documents 的 status=='active' 过滤会把所有文档都筛掉。
                "status": doc.get("status", "active"),
                "perm_group": doc.get("perm_group") or "all",
                "space_id": doc.get("space_id", settings.default_space_id),
                "tenant_id": doc.get("tenant_id", settings.default_tenant_id),
                "updated_at": _now(), "created_at": doc.get("created_at") or _now(),
            }
            self.docs[doc["doc_id"]] = doc

    def get_document(self, doc_id: str, tenant_id: str) -> dict | None:
        d = self.docs.get(doc_id)
        if not d or d.get("status") != "active":
            return None
        return d if d.get("tenant_id", settings.default_tenant_id) == tenant_id else None

    def get_document_by_name(self, file_name: str, tenant_id: str, space_id: str = "") -> dict | None:
        for d in sorted(self.docs.values(), key=lambda x: x.get("created_at", ""), reverse=True):
            if d["file_name"] != file_name or d.get("status") != "active":
                continue
            if d.get("tenant_id", settings.default_tenant_id) != tenant_id:
                continue
            if space_id and d.get("space_id", settings.default_space_id) != space_id:
                continue
            return d
        return None

    def list_documents(self, tenant_id: str, keyword: str = "", limit: int = 100,
                       perm_groups: list[str] | None = None,
                       spaces: list[str] | None = None,
                       space_id: str = "") -> list[dict]:
        allowed = set(perm_groups) if perm_groups is not None else None
        space_set = set(spaces) if spaces is not None else None
        items = []
        for d in self.docs.values():
            if d.get("status") != "active":
                continue
            if d.get("tenant_id", settings.default_tenant_id) != tenant_id:
                continue
            if keyword and keyword.lower() not in d["file_name"].lower():
                continue
            if allowed is not None and (d.get("perm_group") or "all") not in allowed:
                continue
            if space_id and d.get("space_id", settings.default_space_id) != space_id:
                continue
            if not space_id and not spaces_wildcard(space_set):
                if d.get("space_id", settings.default_space_id) not in space_set:
                    continue
            items.append(d)
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]

    def delete_document(self, doc_id: str) -> None:
        if doc_id in self.docs:
            self.docs[doc_id]["status"] = "deleted"
            self.docs[doc_id]["updated_at"] = _now()

    def purge_deleted(self, older_than_days: int = 7) -> int:
        """物理清理软删除记录（内存后端：按 updated_at 判断，缺失则视为已过期）。"""
        cutoff = time.time() - max(int(older_than_days), 0) * 86400
        stale = []
        for doc_id, d in self.docs.items():
            if d.get("status") != "deleted":
                continue
            ts = _parse_ts(d.get("updated_at"))
            if ts is None or ts < cutoff:
                stale.append(doc_id)
        for doc_id in stale:
            self.docs.pop(doc_id, None)
            self.chunks.pop(doc_id, None)
        return len(stale)

    def replace_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self.chunks[doc_id] = [dict(c) for c in chunks]

    def count_chunks(self, doc_id: str) -> int:
        return len(self.chunks.get(doc_id, []))

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.messages.setdefault(session_id, []).append({"role": role, "content": content})

    def get_history(self, session_id: str, limit: int = 10) -> list[dict]:
        return self.messages.get(session_id, [])[-limit:]

    def clear_session(self, session_id: str) -> None:
        self.messages.pop(session_id, None)

    def log_qa(self, record: dict) -> None:
        self.logs.append({**record, "created_at": _now()})

    def stats(self) -> dict:
        active = [d for d in self.docs.values() if d.get("status") == "active"]
        lat = [x.get("latency_ms", 0) for x in self.logs]
        return {
            "doc_count": len(active),
            "chunk_count": sum(len(v) for v in self.chunks.values()),
            "qa_count": len(self.logs),
            "rejected_count": sum(1 for x in self.logs if x.get("rejected")),
            "avg_latency_ms": round(sum(lat) / len(lat)) if lat else 0,
        }

    # ---------- P2：密钥管理 ----------
    def save_api_key(self, rec: dict) -> None:
        with self._lock:
            self.keys[rec["key_id"]] = {**rec, "created_at": rec.get("created_at") or _now()}

    def get_api_key(self, key_id: str) -> dict | None:
        return self.keys.get(key_id)

    def find_api_key_by_hash(self, key_hash: str) -> dict | None:
        for rec in self.keys.values():
            if rec.get("key_hash") == key_hash and not rec.get("revoked_at"):
                return rec
        return None

    def list_api_keys(self, owner: str = "", include_revoked: bool = False) -> list[dict]:
        items = [r for r in self.keys.values()
                 if (not owner or r.get("owner") == owner)
                 and (include_revoked or not r.get("revoked_at"))]
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)

    def revoke_api_key(self, key_id: str) -> bool:
        with self._lock:
            rec = self.keys.get(key_id)
            if not rec or rec.get("revoked_at"):
                return False
            rec["revoked_at"] = _now()
            rec["enabled"] = False
            return True

    def touch_api_key(self, key_id: str) -> None:
        if key_id in self.keys:
            self.keys[key_id]["last_used_at"] = _now()

    # ---------- P3：知识库空间 ----------
    # P4：空间主键从 space_id 改为 (tenant_id, space_id)。
    # 只用 space_id 作键会让两个租户的 default 空间互相覆盖——
    # 内存实现看似"能用"，实则是最后一个租户赢了，前面所有租户的空间凭空消失。
    @staticmethod
    def _space_key(space_id: str, tenant_id: str = "") -> tuple[str, str]:
        return (tenant_id or settings.default_tenant_id, space_id)

    def save_space(self, rec: dict) -> None:
        with self._lock:
            tid = rec.get("tenant_id") or settings.default_tenant_id
            self.spaces[self._space_key(rec["space_id"], tid)] = {
                **rec, "updated_at": _now(), "tenant_id": tid,
                "created_at": rec.get("created_at") or _now(),
            }

    def get_space(self, space_id: str, tenant_id: str = "") -> dict | None:
        # tenant_id 留空表示"不限定租户"，沿用 P3 的宽松语义以兼容内部调用；
        # 业务层一律显式传入，避免不传就拿到别人的空间。
        if tenant_id:
            return self.spaces.get(self._space_key(space_id, tenant_id))
        for (_, sid), rec in self.spaces.items():
            if sid == space_id and rec.get("status") != "deleted":
                return rec
        return None

    def list_spaces(self, status: str = "active", tenant_id: str = "") -> list[dict]:
        items = [s for s in self.spaces.values()
                 if (not status or s.get("status") == status)
                 and (not tenant_id
                      or s.get("tenant_id", settings.default_tenant_id) == tenant_id)]
        counts: dict[str, int] = {}
        for d in self.docs.values():
            if d.get("status") == "active" and (
                    not tenant_id
                    or d.get("tenant_id", settings.default_tenant_id) == tenant_id):
                sid = d.get("space_id", settings.default_space_id)
                counts[sid] = counts.get(sid, 0) + 1
        for s in items:
            s["doc_count"] = counts.get(s["space_id"], 0)
        return sorted(items, key=lambda x: (x["space_id"] != settings.default_space_id, x.get("created_at", "")))

    def delete_space(self, space_id: str, tenant_id: str = "") -> bool:
        with self._lock:
            key = (self._space_key(space_id, tenant_id) if tenant_id
                   else next((k for k, v in self.spaces.items()
                              if k[1] == space_id and v.get("status") == "active"), None))
            if not key:
                return False
            rec = self.spaces.get(key)
            if not rec or rec.get("status") != "active":
                return False
            rec["status"] = "deleted"
            rec["updated_at"] = _now()
            return True

    def space_stats(self) -> dict:
        return {
            "space_count": sum(1 for s in self.spaces.values() if s.get("status") == "active"),
            "doc_count": sum(1 for d in self.docs.values() if d.get("status") == "active"),
        }

    # ---------- P3：监控探针（内存实现同样要提供，否则指标代码要处处判类型）----------
    def pool_stats(self) -> dict:
        return {"pool_used": 0, "pool_free": 0, "pool_max": 0, "latency_ms": 0.0}

    def counts(self) -> dict:
        docs = [d for d in self.docs.values() if d.get("status") == "active"]
        return {
            "documents": len(docs),
            "chunks": sum(int(d.get("chunk_count") or 0) for d in docs),
            "error_logs": len(getattr(self, "errors", []) or []),
        }

    # ---------- P3：用户 ----------
    def save_user(self, rec: dict) -> None:
        with self._lock:
            self.users[rec["user_id"]] = {
                **rec,
                "tenant_id": rec.get("tenant_id", settings.default_tenant_id),
                "created_at": rec.get("created_at") or _now(),
            }

    def get_user(self, user_id: str) -> dict | None:
        return self.users.get(user_id)

    def get_user_by_name(self, username: str, tenant_id: str = "") -> dict | None:
        for u in sorted(self.users.values(), key=lambda x: x.get("created_at", "")):
            if u.get("username") != username:
                continue
            if tenant_id and u.get("tenant_id", settings.default_tenant_id) != tenant_id:
                continue
            return u
        return None

    def list_users(self, tenant_id: str = "") -> list[dict]:
        items = [u for u in self.users.values()
                 if not tenant_id
                 or u.get("tenant_id", settings.default_tenant_id) == tenant_id]
        return sorted(items, key=lambda x: x.get("created_at", ""))

    def update_user(self, user_id: str, fields: dict) -> bool:
        with self._lock:
            rec = self.users.get(user_id)
            if not rec:
                return False
            allowed = {"display_name", "role", "spaces", "perm_groups", "enabled",
                   "password_hash", "salt"}
            rec.update({k: v for k, v in fields.items() if k in allowed})
            return True

    def touch_user_login(self, user_id: str) -> None:
        if user_id in self.users:
            self.users[user_id]["last_login_at"] = _now()

    # ---------- P3：错误日志 ----------
    def log_error(self, record: dict) -> None:
        with self._lock:
            self.errors.append({**record, "created_at": _now()})
            if len(self.errors) > 2000:
                del self.errors[:500]

    def query_errors(self, trace_id: str = "", limit: int = 50) -> list[dict]:
        items = [e for e in self.errors if not trace_id or e.get("trace_id") == trace_id]
        return list(reversed(items))[:max(1, min(int(limit), 200))]

    # ---------- P2：操作审计 ----------
    def log_audit(self, record: dict) -> None:
        with self._lock:
            self.audits.append({**record, "created_at": _now()})
            if len(self.audits) > 5000:          # 内存模式下防止无限增长
                del self.audits[:1000]

    def query_audit(self, actor: str = "", action: str = "", result: str = "",
                    limit: int = 100, tenant_id: str = "") -> list[dict]:
        items = [a for a in self.audits
                 if (not tenant_id or (a.get("tenant_id") or "default") == tenant_id)
                 and (not actor or a.get("actor") == actor)
                 and (not action or str(a.get("action", "")).startswith(action))
                 and (not result or a.get("result") == result)]
        return list(reversed(items))[:max(1, min(int(limit), 500))]

    def audit_stats(self) -> dict:
        return {
            "total": len(self.audits),
            "success": sum(1 for a in self.audits if a.get("result") == "success"),
            "denied": sum(1 for a in self.audits if a.get("result") == "denied"),
            "failed": sum(1 for a in self.audits if a.get("result") == "failed"),
            "actors": len({a.get("actor") for a in self.audits}),
        }

    # ---------- P4：租户 / 任务 / 告警（内存实现，仅为本地调试对齐接口）----------
    def save_tenant(self, rec: dict) -> dict:
        with self._lock:
            self.tenants[rec["tenant_id"]] = {**rec, **_now_fields()}
        return self.tenants[rec["tenant_id"]]

    def get_tenant(self, tenant_id: str) -> dict | None:
        return self.tenants.get(tenant_id)

    def list_tenants(self) -> list[dict]:
        return sorted(self.tenants.values(), key=lambda x: x["tenant_id"])

    def update_tenant(self, tenant_id: str, fields: dict) -> dict:
        with self._lock:
            rec = self.tenants.get(tenant_id)
            if not rec:
                return {}
            allowed = {"name", "description", "max_users", "max_docs", "max_qps", "status"}
            rec.update({k: v for k, v in (fields or {}).items() if k in allowed})
            rec.update(_now_fields())
            return rec

    def count_users(self, tenant_id: str) -> int:
        return sum(1 for u in self.users.values()
                   if u.get("tenant_id", settings.default_tenant_id) == tenant_id)

    def count_documents(self, tenant_id: str) -> int:
        return sum(1 for d in self.docs.values()
                   if d.get("status") == "active"
                   and d.get("tenant_id", settings.default_tenant_id) == tenant_id)

    def count_spaces(self, tenant_id: str) -> int:
        return sum(1 for s in self.spaces.values()
                   if s.get("status") == "active"
                   and s.get("tenant_id", settings.default_tenant_id) == tenant_id)

    def save_task(self, rec: dict) -> dict:
        with self._lock:
            self.tasks.setdefault(rec["task_id"], {
                **rec, "progress": int(rec.get("progress") or 0),
                "status": rec.get("status", "pending"),
                "tenant_id": rec.get("tenant_id", settings.default_tenant_id),
                "result": {}, "error": "", "retries": 0, "elapsed_ms": 0,
                "created_at": _now(),
            })
            return dict(self.tasks[rec["task_id"]])

    def update_task(self, task_id: str, fields: dict | None = None, **kwargs) -> None:
        merged = {**(fields or {}), **kwargs}
        with self._lock:
            rec = self.tasks.get(task_id)
            if not rec:
                return
            for k, v in merged.items():
                # True 作为"打点"信号，转成真实时间戳（与 PG 实现保持一致）
                if k in ("started_at", "finished_at") and v is True:
                    v = _now()
                rec[k] = v

    def purge_tasks(self, tenant_id: str = "", older_than_days: int = 7) -> int:
        with self._lock:
            keep, removed = {}, 0
            for k, v in self.tasks.items():
                if (v.get("status") in ("success", "failed", "cancelled")
                        and (not tenant_id or v.get("tenant_id") == tenant_id)):
                    removed += 1
                    continue
                keep[k] = v
            self.tasks = keep
            return removed

    def get_task(self, task_id: str, tenant_id: str = "") -> dict | None:
        rec = self.tasks.get(task_id)
        if not rec:
            return None
        if tenant_id and rec.get("tenant_id") != tenant_id:
            return None
        return rec

    def list_tasks(self, tenant_id: str = "", status: str = "", task_type: str = "",
                   limit: int = 50) -> list[dict]:
        items = [t for t in self.tasks.values()
                 if (not tenant_id or t.get("tenant_id") == tenant_id)
                 and (not status or t.get("status") == status)
                 and (not task_type or t.get("task_type") == task_type)]
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]

    def task_stats(self, tenant_id: str = "") -> dict:
        items = [t for t in self.tasks.values() if not tenant_id or t.get("tenant_id") == tenant_id]
        out = {"total": len(items)}
        for st in ("pending", "running", "success", "failed"):
            out[st] = sum(1 for t in items if t.get("status") == st)
        return out

    def save_alert(self, rec: dict) -> None:
        with self._lock:
            self.alerts.append({**rec, "created_at": _now()})

    def list_alerts(self, tenant_id: str = "", limit: int = 50, severity: str = "") -> list[dict]:
        items = [a for a in self.alerts
                 if (not tenant_id or a.get("tenant_id") in (tenant_id, "*"))
                 and (not severity or a.get("severity") == severity)]
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]

    def alert_stats(self) -> dict:
        return {
            "total": len(self.alerts),
            "critical": sum(1 for a in self.alerts if a.get("severity") == "critical"),
            "warning": sum(1 for a in self.alerts if a.get("severity") == "warning"),
            "firing": sum(1 for a in self.alerts if a.get("status") == "firing"),
        }

    def get_silence(self, rule_id: str) -> float | None:
        if rule_id in self.silences:
            until, _ = self.silences[rule_id]
            return until
        return None

    def set_silence(self, rule_id: str, until_ts: float, reason: str = "") -> None:
        with self._lock:
            self.silences[rule_id] = (until_ts, reason)


def build_meta_store() -> MetaStore:
    mode = settings.meta_backend.lower()
    if mode == "memory":
        store: MetaStore = MemoryMetaStore()
    elif mode == "postgres":
        store = PostgresMetaStore()
    else:
        try:
            store = PostgresMetaStore()
            store.init()
            logger.info("业务库：PostgreSQL")
        except Exception as exc:
            logger.warning("PostgreSQL 不可用，降级为内存存储：%s", exc)
            store = MemoryMetaStore()
    store.init()
    return store


meta_store: MetaStore = build_meta_store()


def new_doc_id() -> str:
    return uuid.uuid4().hex[:16]
