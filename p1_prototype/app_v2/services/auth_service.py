"""鉴权服务：密钥生命周期管理与请求身份认证。

认证流程：
  Authorization: Bearer rag_xxx
    → 解析出前缀与 key_id（SHA256 前 12 位）
    → 按 key_id 索引记录，PBKDF2 校验（常量时间比较）
    → 检查启用/吊销/过期
    → 返回 Principal（身份 + 角色 + 权限集合 + 可访问权限组）

性能说明：PBKDF2 迭代 12 万次，单次约 30~60ms。为避免每次请求都付这个代价，
进程内对「明文 -> Principal」做短 TTL 缓存（默认 60s），密钥被吊销时主动清除。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, List, Optional, Tuple

from app_v2.core.response import AppError, ForbiddenError, UnauthorizedError
from app_v2.core.security import (
    DEFAULT_ROLE, ROLES, has_permission, hash_api_key, generate_api_key,
    normalize_perm_groups, normalize_role, parse_perm_groups, role_permissions,
    verify_api_key, key_id_of, mask_key,
)
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

_VERIFY_CACHE_TTL = 60.0
_MAX_VERIFY_ATTEMPTS = 5
_ATTEMPT_WINDOW = 60.0


@dataclass(frozen=True)
class Principal:
    """通过认证的调用方。

    **必须区分两个"范围"概念，它们正交且语义不同**：

    - `perm_groups`：P2 的「知识权限组」，管的是"**谁能看**"（人的属性）。
      取值形如 `hr` / `finance` / `all`，来自 API Key 的 perm_group 字段。
    - `spaces`：P3 的「知识库空间」，管的是"**内容属于哪个业务域**"。
      取值形如 `default` / `hr_zone`，来自 users.spaces 字段。

    二者恰好常取同名值（都用 `hr`），但**绝不能互相赋值**：
      - 若把 perm_groups 当 spaces 用，P2 存量密钥（perm_group=hr）
        会被判为"无权访问 default 空间"，升级后所有上传/检索 403；
      - 若把 spaces 当 perm_groups 用，跨空间内容就会互相可见，隔离失效。

    API Key 没有 spaces 字段（它面向脚本/服务间调用，本就代表"某套系统"），
    因此沿用 P2 语义：不额外限制空间，只受权限组约束。
    """
    actor: str
    role: str
    key_id: str
    # 读取范围：**含**公共组 all（读取侧会自动补，见 effective_perm_groups）。
    # 用于"能不能看某篇文档"。
    perm_groups: Tuple[str, ...]
    permissions: FrozenSet[str] = field(default_factory=frozenset)
    # 可访问空间；None 表示"未绑定空间限制"（API Key 走权限组语义）
    spaces: Optional[Tuple[str, ...]] = None
    # 写入范围：**不含**自动补的 all，只反映显式授权。
    # 用于"能不能把文档投放到某个密级"。
    #
    # 为什么必须和 perm_groups 分开：
    #   读取时补 all 是安全的——公共文档本来就该人人可读；
    #   但写入时若也把 all 当成"我拥有的组",受限凭据就能把 hr 密件标成 all，
    #   等于自我扩权把密件公开给全员。这两个方向的默认值必须是反的。
    granted_perm_groups: Tuple[str, ...] = ()
    # P4：所属租户。**最外层边界**，与 spaces/perm_groups 不同层：
    # 租户 → 空间 → 密级，前者是"哪家公司"，后者是"这家公司的哪个域/什么密级"。
    # 跨租户访问在本系统里不是"无权限"，而是"该资源不存在"——
    # 让攻击者连"这个租户存在"都探测不到。
    tenant_id: str = ""

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions

    @property
    def is_root_tenant_admin(self) -> bool:
        """是否为跨租户超管。这类账号能切换租户上下文，用于平台运维。"""
        from app_v2.core.settings import settings

        return self.role == settings.root_tenant_admin

    @property
    def writable_perm_groups(self) -> List[str]:
        """可投放的密级集合。未显式记录 granted 时退回 perm_groups。"""
        base = self.granted_perm_groups or self.perm_groups
        return list(base)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def space_scope(self) -> List[str]:
        """空间可见范围。未绑定空间时返回通配，交由权限组把关。"""
        if not self.spaces:
            return ["*"]
        return list(self.spaces)

    def to_audit(self) -> dict:
        return {"actor": self.actor, "role": self.role, "key_id": self.key_id,
                "tenant_id": self.tenant_id}


# ---------------- 认证缓存 ----------------
_verify_cache: Dict[str, Tuple[float, Principal]] = {}
_cache_lock = threading.Lock()

# 失败尝试计数（防暴力破解）
_attempts: Dict[str, List[float]] = {}
_attempt_lock = threading.Lock()


def _cache_get(raw_key: str) -> Optional[Principal]:
    with _cache_lock:
        item = _verify_cache.get(raw_key)
        if not item:
            return None
        ts, principal = item
        if time.time() - ts > _VERIFY_CACHE_TTL:
            _verify_cache.pop(raw_key, None)
            return None
        return principal


def _cache_put(raw_key: str, principal: Principal) -> None:
    with _cache_lock:
        if len(_verify_cache) > 2048:      # 简单上限，防止内存无界增长
            _verify_cache.clear()
        _verify_cache[raw_key] = (time.time(), principal)


def invalidate_key_cache(raw_key: Optional[str] = None) -> None:
    """吊销/变更密钥后清除缓存，保证立即生效。"""
    with _cache_lock:
        if raw_key is None:
            _verify_cache.clear()
        else:
            _verify_cache.pop(raw_key, None)


def _rate_limited(fingerprint: str) -> bool:
    """同一来源在窗口内失败过多则临时拒绝，缓解密钥爆破。"""
    now = time.time()
    with _attempt_lock:
        hist = [t for t in _attempts.get(fingerprint, []) if now - t < _ATTEMPT_WINDOW]
        _attempts[fingerprint] = hist
        return len(hist) >= _MAX_VERIFY_ATTEMPTS


def _record_failure(fingerprint: str) -> None:
    with _attempt_lock:
        _attempts.setdefault(fingerprint, []).append(time.time())


def _clear_failures(fingerprint: str) -> None:
    with _attempt_lock:
        _attempts.pop(fingerprint, None)


# ---------------- 认证 ----------------
def authenticate(raw_key: str, client_ip: str = "", allow_api_key: bool = True) -> Principal:
    """校验明文密钥并返回 Principal；失败抛 UnauthorizedError。

    allow_api_key=False（AUTH_MODE=jwt 严格模式）时，**本函数只做一件事**：
    校验密钥本身是否有效，然后把调用方引导去换 JWT。它不返回可用的 Principal。

    为什么不一刀切删掉 API Key：存量系统集成（CI、定时任务、第三方）用的是密钥，
    直接作废会让升级当天全线中断。正确做法是保留密钥的"入场券"价值，
    但取消它直接访问业务数据的能力——认证强度收紧了，兼容性没丢。
    """
    raw_key = (raw_key or "").strip()
    if not raw_key:
        raise UnauthorizedError("缺少 Authorization: Bearer <JWT>")

    if not allow_api_key:
        # 密钥有效时给出明确指引，而不是笼统的"无效"
        rec = meta_store.get_api_key(key_id_of(raw_key))
        if not rec or rec.get("revoked_at") or not rec.get("enabled", True):
            raise UnauthorizedError("认证失败：请先 POST /api/v1/auth/token 用凭证换取 JWT")
        raise UnauthorizedError(
            "当前为 JWT 强制模式（AUTH_MODE=jwt）：API Key 不能直接访问业务接口，"
            "请先 POST /api/v1/auth/token 用 API Key 换取 JWT，"
            "或用 POST /api/v1/auth/login 账号密码登录"
        )

    cached = _cache_get(raw_key)
    if cached:
        return cached

    fingerprint = f"{client_ip}:{key_id_of(raw_key)}"
    if _rate_limited(fingerprint):
        raise UnauthorizedError("认证失败次数过多，请稍后重试")

    kid = key_id_of(raw_key)
    rec = meta_store.get_api_key(kid)
    if not rec or rec.get("revoked_at"):
        _record_failure(fingerprint)
        logger.warning("认证失败：密钥不存在或已吊销 key_id=%s ip=%s", kid, client_ip)
        raise UnauthorizedError("API Key 无效")

    if not verify_api_key(raw_key, rec["key_hash"], rec["salt"]):
        _record_failure(fingerprint)
        logger.warning("认证失败：密钥校验不通过 key_id=%s ip=%s", kid, client_ip)
        raise UnauthorizedError("API Key 无效")

    if not rec.get("enabled", True):
        _record_failure(fingerprint)
        raise UnauthorizedError("API Key 已被禁用")

    principal = Principal(
        actor=rec.get("owner", "unknown"),
        role=rec.get("role", DEFAULT_ROLE),
        key_id=kid,
        # 这里必须用 parse（不扩大）：密钥上写的是 hr，这个密钥就是 hr 权限，
        # 不能自动补 all —— 否则「受限密钥」形同全公开密钥，权限组隔离直接失效。
        # 补 all 的动作只发生在读取侧比对（effective_perm_groups）。
        perm_groups=tuple(sorted(parse_perm_groups(rec.get("perm_group", "all")))),
        # 写入侧用同一份显式授权：密钥写了 hr，就只能往 hr 投放。
        granted_perm_groups=tuple(sorted(parse_perm_groups(rec.get("perm_group", "all")))),
        permissions=role_permissions(rec.get("role", DEFAULT_ROLE)),
        tenant_id=rec.get("tenant_id") or "default",
    )
    if not _is_expired(rec.get("expires_at")):
        _clear_failures(fingerprint)
        _cache_put(raw_key, principal)
        try:
            meta_store.touch_api_key(kid)
        except Exception:                    # 使用时间戳更新失败不影响认证
            pass
        return principal

    _record_failure(fingerprint)
    raise UnauthorizedError("API Key 已过期")


def _is_expired(expires_at) -> bool:
    if not expires_at:
        return False
    try:
        exp = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp
    except (ValueError, TypeError):
        return False


# ---------------- 授权 ----------------
def require(principal: Optional[Principal], permission: str) -> Principal:
    """确认已认证且具备权限点，否则抛 401/403。"""
    if principal is None:
        raise UnauthorizedError()
    if not principal.can(permission):
        logger.warning("越权访问 actor=%s role=%s 缺少 %s", principal.actor, principal.role, permission)
        raise ForbiddenError(f"角色 {principal.role} 缺少权限 {permission}")
    return principal


def effective_perm_groups(principal: Principal) -> List[str]:
    """调用方实际可访问的知识权限组。"""
    return sorted(set(principal.perm_groups) | {"all"})


# ---------------- 密钥管理 ----------------
def create_key(owner: str, role: str = DEFAULT_ROLE, perm_group: str = "all",
               description: str = "", expires_in_days: Optional[int] = None,
               created_by: str = "system") -> dict:
    """创建密钥。明文只在此处返回一次，请调用方立即保存。"""
    # 旧角色名必须能继续用：P2 的 editor/viewer 在 P3 合并为 kb_admin/user，
    # 但所有已发出的密钥与外部脚本仍在传旧名。直接拒绝会让升级变成破坏性变更——
    # 这里先归一化，落库统一存新名。
    role = normalize_role(role)
    if role not in ROLES:
        raise AppError(f"未知角色：{role}，可选 {sorted(ROLES)}", code=422)
    if not owner or not owner.strip():
        raise AppError("owner 不能为空", code=422)

    raw = generate_api_key()
    key_id = key_id_of(raw)
    key_hash, salt = hash_api_key(raw)
    expires_at = None
    if expires_in_days:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=int(expires_in_days))).isoformat()

    meta_store.save_api_key({
        "key_id": key_id, "owner": owner.strip(), "role": role,
        "key_hash": key_hash, "salt": salt, "perm_group": perm_group,
        "enabled": True, "description": description, "expires_at": expires_at,
        "created_by": created_by,
    })
    logger.info("创建 API Key owner=%s role=%s key_id=%s by=%s", owner, role, key_id, created_by)
    return {
        "key_id": key_id, "api_key": raw, "owner": owner, "role": role,
        "perm_group": perm_group, "expires_at": expires_at,
        "description": description,
        "warning": "明文密钥仅显示一次，请立即保存到密钥管理系统",
    }


def revoke_key(key_id: str, by: str = "system") -> bool:
    ok_flag = meta_store.revoke_api_key(key_id)
    invalidate_key_cache()
    if ok_flag:
        logger.info("吊销 API Key key_id=%s by=%s", key_id, by)
    return ok_flag


def list_keys(owner: str = "", include_revoked: bool = False) -> List[dict]:
    """列出密钥（绝不含明文与哈希）。"""
    rows = meta_store.list_api_keys(owner=owner, include_revoked=include_revoked)
    out = []
    for r in rows:
        out.append({
            "key_id": r.get("key_id"),
            "owner": r.get("owner"),
            "role": r.get("role"),
            "perm_group": r.get("perm_group"),
            "enabled": r.get("enabled"),
            "description": r.get("description"),
            "expires_at": _iso(r.get("expires_at")),
            "last_used_at": _iso(r.get("last_used_at")),
            "revoked_at": _iso(r.get("revoked_at")),
            "created_at": _iso(r.get("created_at")),
            "status": "revoked" if r.get("revoked_at") else
                      ("expired" if _is_expired(r.get("expires_at")) else
                       ("active" if r.get("enabled", True) else "disabled")),
        })
    return out


def _iso(v) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v else None)


def bootstrap_admin_key() -> Optional[str]:
    """首次启动且系统无任何密钥时自动创建一个管理员密钥并打印到日志。

    这是企业部署的必要兜底：否则全新环境无法调用任何管理接口。
    生产环境应通过环境变量 BOOTSTRAP_ADMIN_KEY 指定，或用后立即吊销。
    """
    try:
        existing = meta_store.list_api_keys(include_revoked=True)
    except Exception as exc:
        logger.warning("密钥表不可用，跳过管理员密钥初始化：%s", exc)
        return None

    if existing:
        return None

    info = create_key(owner="system", role="admin", perm_group="all",
                      description="初始管理员密钥（请尽快轮换）", created_by="bootstrap")
    logger.warning("=" * 72)
    logger.warning("已生成初始管理员 API Key（仅本次输出，请立即保存）：")
    logger.warning("  %s", info["api_key"])
    logger.warning("  用法：curl -H 'Authorization: Bearer %s' ...", mask_key(info["api_key"]))
    logger.warning("=" * 72)
    return info["api_key"]
