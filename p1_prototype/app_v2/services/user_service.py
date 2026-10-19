"""用户账号服务：密码登录、刷新、注销，与 API Key 认证并存。

为什么保留双通道：
  - **API Key**：给系统间调用、脚本、CI 用，无状态、易轮换、可精确限权。
  - **JWT**：给人类用户用，浏览器/IM 里存 token 比存长期密钥安全，
    且有自然过期时间，丢了也不会永久泄露。
两者最终都归一成同一个 `Principal`，业务代码只认 Principal，不关心来源。

密码存储：
  - PBKDF2-HMAC-SHA256，迭代 26 万次（OWASP 2023 建议值），每用户独立随机盐。
  - 校验用 `hmac.compare_digest` 常量时间比较，防时序侧信道。
  - 明文密码永不落日志、永不入审计（审计只记 username）。

登录防护：
  - 按 username 计数，5 次失败锁 60 秒（与密钥爆破防护同一套阈值语义）。
  - 用户不存在与密码错误返回**同一条**提示，避免账号枚举。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app_v2.core import jwt_utils
from app_v2.core.response import AppError, UnauthorizedError
from app_v2.core.security import (
    DEFAULT_ROLE, ROLES, normalize_role, role_permissions, spaces_wildcard,
)
from app_v2.core.settings import settings
from app_v2.services import cache_service
from app_v2.services.auth_service import Principal, invalidate_key_cache
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

PBKDF2_ROUNDS = 260000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_SEC = 60

# 账号枚举防护：用户不存在也走一遍哈希，让响应耗时与"密码错误"一致
_FAKE_SALT = secrets.token_hex(16)

_login_attempts: Dict[str, List[float]] = {}
_login_lock = threading.Lock()


# ---------------- 密码 ----------------
def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """返回 (hash_hex, salt_hex)。"""
    if not password:
        raise AppError("密码不能为空", code=422)
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), PBKDF2_ROUNDS)
    return digest.hex(), salt


def verify_password(password: str, hash_hex: str, salt: str) -> bool:
    try:
        calc, _ = hash_password(password, salt)
    except Exception:
        return False
    return hmac.compare_digest(calc, hash_hex or "")


def _burn_cycles(password: str) -> None:
    """用户不存在时也消耗一次哈希时间，防止通过响应耗时枚举账号。"""
    hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                        bytes.fromhex(_FAKE_SALT), PBKDF2_ROUNDS)


# ---------------- 登录限流 ----------------
def _login_locked(username: str) -> bool:
    now = time.time()
    with _login_lock:
        hist = [t for t in _login_attempts.get(username, []) if now - t < LOGIN_LOCK_SEC]
        _login_attempts[username] = hist
        return len(hist) >= LOGIN_MAX_ATTEMPTS


def _login_fail(username: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(username, []).append(time.time())


def _login_ok(username: str) -> None:
    with _login_lock:
        _login_attempts.pop(username, None)


# ---------------- JWT 管理器 ----------------
_jwt: Optional[jwt_utils.JWTManager] = None


def jwt_manager() -> jwt_utils.JWTManager:
    """惰性构造，避免导入期就读密钥文件（容器只读挂载时也能启动）。"""
    global _jwt
    if _jwt is None:
        secret = settings.resolved_jwt_secret() if settings.jwt_enabled else ""
        _jwt = jwt_utils.JWTManager(
            secret=secret,
            access_ttl=settings.jwt_access_ttl,
            refresh_ttl=settings.jwt_refresh_ttl,
            issuer=settings.jwt_issuer,
        )
        if not secret:
            logger.warning("JWT 未启用（JWT_ENABLED=false 或密钥为空），仅接受 API Key 认证")
    return _jwt


def reset_jwt_manager() -> None:
    """配置变更或测试时重建（密钥换了必须重建，否则旧密钥继续签）。"""
    global _jwt
    _jwt = None


# ---------------- 用户记录 -> Principal ----------------
def _user_to_principal(user: dict) -> Principal:
    spaces = tuple(_parse_spaces(user.get("spaces", settings.default_space_id)))
    return Principal(
        actor=user.get("username", "unknown"),
        role=user.get("role", DEFAULT_ROLE),
        key_id=f"jwt:{user.get('user_id', '')[:12]}",
        # 权限组必须独立取值：用户是"人"，同时受两套正交范围约束——
        #   spaces   决定能进哪些业务域；
        #   perm_groups 决定在同一域内能看到哪些密级的文档。
        # 早期版本把 spaces 直接塞进 perm_groups，结果同空间内的
        # hr 密级文档对所有能进该空间的人都可见，权限组隔离形同失效。
        perm_groups=_derive_perm_groups(spaces, user.get("perm_groups", "")),
        # 写入范围同样不能复用上一行（含自动补的 all），见 Principal 的说明。
        granted_perm_groups=_explicit_granted(spaces, user.get("perm_groups", "")),
        permissions=role_permissions(user.get("role", DEFAULT_ROLE)),
        spaces=spaces,
        tenant_id=user.get("tenant_id") or settings.default_tenant_id,
    )


def _derive_perm_groups(spaces: Tuple[str, ...], explicit: str = "") -> Tuple[str, ...]:
    """决定用户可见的权限组（密级）。

    **优先用显式配置**：`users.perm_groups` 填了就以它为准，只追加 all（公共文档）。
    这条路径是必须的——否则"在 hr 空间工作的人"永远读不到 hr 密级文档，
    因为密级名和空间名恰好同名只是巧合，不能当作授权依据。

    未填时回退到"由空间名派生"，保持 P3 早期版本的行为不变：
      `*` 空间 → 通配权限组；否则 空间名 ∪ {all}。

    这里刻意**不做**"角色为 admin 就自动全密级"的推导：
    权限应该来自显式的数据授权，而不是代码里的隐式规则——
    隐式规则一旦写错，就是"所有人都能看薪资表"这种事故。
    管理员需要看全部密级时，把 perm_groups 显式设为 `*`。
    """
    raw = (explicit or "").strip()
    if raw:
        groups = {g.strip() for g in raw.split(",") if g.strip()}
        if not groups:
            groups = set()
        if "*" in groups:
            return ("*", "all")
        return tuple(sorted(groups | {"all"}))
    if not spaces:
        return ("all",)
    if spaces_wildcard(list(spaces)):
        return ("*", "all")
    return tuple(sorted(set(spaces) | {"all"}))


def _explicit_granted(spaces: Tuple[str, ...], explicit: str = "") -> Tuple[str, ...]:
    """写入侧授权范围：只认显式配置，**不补 all**。

    与 `_derive_perm_groups` 的差别只有一处，但方向必须相反：
      - 读取：补 all 是必需的，否则人人都看不到公开文档；
      - 写入：补 all 就是漏洞，受限用户能把密件标成 all 公开给全员。
    老 token（无 pg 字段）没有显式授权信息，按空间名派生——这是升级兼容所需，
    但派生的结果仍不补 all，宁可让老 token 不能投放 all，也不放开扩权口子。
    """
    raw = (explicit or "").strip()
    if raw:
        groups = {g.strip() for g in raw.split(",") if g.strip()}
        if "*" in groups:
            return ("*",)
        return tuple(sorted(groups))
    if spaces and spaces_wildcard(list(spaces)):
        return ("*",)
    return tuple(sorted(set(spaces or ())))


def _parse_spaces(raw) -> List[str]:
    """空间是「可见范围」，语义与权限组一致：空值保守为默认空间，`*` 表示全部。"""
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    return items or [settings.default_space_id]


def user_spaces(user: dict) -> List[str]:
    """该用户实际可检索的空间列表。`*` 原样返回，调用方据此跳过过滤。"""
    return _parse_spaces(user.get("spaces", settings.default_space_id))


# ---------------- 登录 / 刷新 / 注销 ----------------
def login(username: str, password: str, client_ip: str = "",
          tenant_id: str = "") -> dict:
    """密码登录，返回双 token 与用户画像。

    P4 起支持租户：`tenant_id` 为空时在**所有租户**里按用户名匹配。
    匹配到多条时优先"非默认租户"的记录不成立，因此规则是：
      - 给了租户 → 精确 (tenant_id, username) 命中；
      - 没给租户 → 取最早创建的记录（即升级前的存量账号）。
    为什么不做"同名则报错让你选租户"：登录页在拿到租户之前就要能用，
    强制先选租户会把"登录"和"多租户"耦合死，日常单租户用户每次都要多点一下。
    """
    username = (username or "").strip()
    if not username or not password:
        raise UnauthorizedError("用户名与密码不能为空")

    # 锁定键带上租户，避免 A 租户的暴力尝试把 B 租户的同名账号锁死
    lock_key = f"{tenant_id or '-'}:{username}" if tenant_id else username
    if _login_locked(lock_key):
        logger.warning("登录被锁定 username=%s tenant=%s ip=%s", username, tenant_id, client_ip)
        raise UnauthorizedError(f"登录失败次数过多，请 {LOGIN_LOCK_SEC} 秒后重试")

    user = None
    try:
        user = meta_store.get_user_by_name(username, tenant_id or "")
    except Exception as exc:
        logger.error("用户表查询失败：%s", exc)

    if not user:
        _burn_cycles(password)          # 时间对齐，防账号枚举
        _login_fail(lock_key)
        logger.warning("登录失败 username=%s tenant=%s ip=%s reason=not_found",
                       username, tenant_id or "-", client_ip)
        raise UnauthorizedError("用户名或密码错误")

    if not user.get("enabled", True):
        _login_fail(lock_key)
        logger.warning("登录失败 username=%s reason=disabled", username)
        raise UnauthorizedError("账号已被禁用")

    if not verify_password(password, user.get("password_hash", ""), user.get("salt", "")):
        _login_fail(lock_key)
        logger.warning("登录失败 username=%s ip=%s reason=bad_password", username, client_ip)
        raise UnauthorizedError("用户名或密码错误")

    # 租户停用时不允许登录：否则"停租户"就只是个显示开关
    try:
        from app_v2.services.tenant_service import tenant_service

        tenant_service.require_active(user.get("tenant_id") or settings.default_tenant_id)
    except Exception as exc:
        from app_v2.core.response import AppError as _AppError, ForbiddenError as _Forbidden

        if isinstance(exc, (_AppError, _Forbidden)):
            raise
        logger.warning("租户校验异常（放行）：%s", exc)

    mgr = jwt_manager()
    if not mgr.enabled:
        raise AppError("JWT 未启用，请改用 API Key 认证", code=503)

    _login_ok(lock_key)
    tokens = mgr.issue({
        "user_id": user.get("user_id", ""),
        "username": username,
        "role": user.get("role", DEFAULT_ROLE),
        "spaces": ",".join(user_spaces(user)),
        # 密级必须签进 token：principal_from_jwt 只信任 token 载荷（无状态），
        # 不带上就会退化成"按空间名派生"，显式授权的密级在下次刷新后静默丢失。
        "perm_groups": user.get("perm_groups", "") or "",
        # 租户同理：必须签进 token，否则下游只能靠"猜"。
        "tenant_id": user.get("tenant_id") or settings.default_tenant_id,
    })
    try:
        meta_store.touch_user_login(user.get("user_id", ""))
    except Exception:
        pass
    logger.info("登录成功 username=%s tenant=%s role=%s ip=%s",
                username, user.get("tenant_id"), user.get("role"), client_ip)
    return {**tokens, "user": public_user(user)}


def refresh(refresh_token: str) -> dict:
    """用 refresh token 换新的 access token。

    **轮换策略**：旧 refresh token 立即进黑名单（一次性使用）。
    否则被盗的 refresh token 可以无限续期，等于永不过期凭证。
    """
    mgr = jwt_manager()
    if not mgr.enabled:
        raise AppError("JWT 未启用", code=503)
    payload = mgr.verify(refresh_token, expect_type="refresh")
    jti = payload.get("jti", "")
    if cache_service.blacklist.contains(jti):
        logger.warning("refresh token 已被注销，疑似重放 uid=%s", payload.get("uid"))
        raise UnauthorizedError("Token 已注销，请重新登录")

    user = None
    try:
        user = meta_store.get_user(payload.get("uid", ""))
    except Exception:
        pass
    if not user or not user.get("enabled", True):
        raise UnauthorizedError("账号不存在或已禁用")

    # 旧 refresh 作废，剩余有效期即黑名单 TTL
    cache_service.blacklist.add(jti, mgr.remaining(payload))
    tokens = mgr.issue({
        "user_id": user.get("user_id", ""),
        "username": user.get("username", ""),
        "role": user.get("role", DEFAULT_ROLE),
        "spaces": ",".join(user_spaces(user)),
    })
    logger.info("刷新 token 成功 username=%s", user.get("username"))
    return {**tokens, "user": public_user(user)}


def logout(access_token: str, refresh_token: str = "") -> dict:
    """注销：把 access（以及可选的 refresh）的 jti 写进黑名单。

    只处理能解出 jti 的 token；已过期的无需登记（校验本来就会失败）。
    """
    mgr = jwt_manager()
    out = {"revoked": [], "backend": "-"}
    for label, token, expect in (("access", access_token, "access"),
                                 ("refresh", refresh_token, "refresh")):
        if not token:
            continue
        try:
            payload = mgr.verify(token, expect_type=expect)
        except jwt_utils.JWTError:
            continue
        jti = payload.get("jti", "")
        if not jti:
            continue
        out["backend"] = cache_service.blacklist.add(jti, mgr.remaining(payload))
        out["revoked"].append(label)
    logger.info("注销 token %s backend=%s", out["revoked"], out["backend"])
    return out


def principal_from_jwt(access_token: str) -> Principal:
    """校验 access token 并还原 Principal。黑名单命中一律拒绝。"""
    mgr = jwt_manager()
    if not mgr.enabled:
        raise UnauthorizedError("JWT 未启用")
    payload = mgr.verify(access_token, expect_type="access")
    jti = payload.get("jti", "")
    if cache_service.blacklist.contains(jti):
        raise UnauthorizedError("Token 已注销，请重新登录")

    # 由 API Key 换来的 token：必须回查来源密钥是否仍然有效。
    # 否则"吊销密钥"只是关掉了换票窗口，已发出的票据仍能用满整个 TTL。
    src_key = payload.get("sk", "")
    if src_key:
        rec = meta_store.get_api_key(src_key)
        if not rec or rec.get("revoked_at") or not rec.get("enabled", True):
            raise UnauthorizedError("签发该 Token 的 API Key 已失效，请重新登录")

    role = payload.get("role", DEFAULT_ROLE)
    spaces = tuple(_parse_spaces(payload.get("spaces", settings.default_space_id)))
    explicit_pg = payload.get("pg", "") or ""
    # P4：老 token 没有 tid → 归入默认租户。
    # 这里不做"按用户名反查真实租户"的猜测：token 是自包含的，
    # 一旦允许从库里补全，就等于把无状态校验变成了半有状态，且给了串户的可乘之机。
    tenant_id = (payload.get("tid") or "").strip() or settings.default_tenant_id
    return Principal(
        actor=payload.get("name") or payload.get("uid", "unknown"),
        role=role,
        key_id=f"jwt:{jti[:12]}",
        # pg 缺失（老 token）时退回空串 → _derive_perm_groups 走"按空间名派生"，
        # 与升级前完全一致；新签发的 token 则带上显式密级。
        perm_groups=_derive_perm_groups(spaces, explicit_pg),
        # 写入范围单独保存：不能用上面那份（它含自动补的 all）。
        # 老 token 没 pg 时按空间名派生，同样要去掉自动补的部分——
        # 直接取派生结果即可，因为派生分支里 all 是"可读公共文档"，
        # 而这里要的是"能不能往 all 投放"，对老 token 保守处理为不可投放 all。
        granted_perm_groups=_explicit_granted(spaces, explicit_pg),
        permissions=role_permissions(role),
        spaces=spaces,
        tenant_id=tenant_id,
    )


def is_jwt(token: str) -> bool:
    """JWT 恒为三段点分；API Key 是 `rag_` 前缀单段。用形状判别，不试错。"""
    token = (token or "").strip()
    return token.count(".") == 2 and not token.startswith("rag_")


# ---------------- 用户管理 ----------------
def public_user(rec: dict) -> dict:
    """对外用户画像：绝不含 password_hash / salt。"""
    return {
        "user_id": rec.get("user_id"),
        "username": rec.get("username"),
        "display_name": rec.get("display_name", ""),
        "role": rec.get("role", DEFAULT_ROLE),
        "spaces": _parse_spaces(rec.get("spaces")),
        # 显式密级；空串表示"按空间名派生"，前端可据此提示实际生效值
        "perm_groups": rec.get("perm_groups", "") or "",
        "enabled": bool(rec.get("enabled", True)),
        # P4：租户。前端据此显示"当前所在租户"，管理后台据此分组。
        "tenant_id": rec.get("tenant_id") or settings.default_tenant_id,
        "created_at": _iso(rec.get("created_at")),
        "last_login_at": _iso(rec.get("last_login_at")),
    }


def create_user(username: str, password: str, role: str = "user",
                spaces: str = "", display_name: str = "",
                perm_groups: str = "", created_by: str = "system",
                tenant_id: str = "") -> dict:
    username = (username or "").strip()
    if not username:
        raise AppError("username 不能为空", code=422)
    if len(password or "") < 8:
        raise AppError("密码至少 8 位", code=422)
    role = normalize_role(role)          # 兼容 P2 的 editor/viewer
    if role not in ROLES:
        raise AppError(f"未知角色：{role}，可选 {sorted(ROLES)}", code=422)

    tid = (tenant_id or "").strip() or settings.default_tenant_id
    # 租户必须存在且未停用，且用户数未超配额。
    # 这两条放在建号之前：否则会先写进库再报错，留下一个孤儿账号。
    from app_v2.services.tenant_service import tenant_service

    tenant_service.check_user_quota(tid)

    # 用户名唯一性只在租户内成立：两个租户各有自己的 admin 是正常的
    if meta_store.get_user_by_name(username, tid):
        raise AppError(f"用户名已存在：{username}（租户 {tid}）", code=409)

    hash_hex, salt = hash_password(password)
    rec = {
        "user_id": uuid.uuid4().hex[:16],
        "username": username,
        "display_name": display_name or username,
        "password_hash": hash_hex,
        "salt": salt,
        "role": role,
        "spaces": ",".join(_parse_spaces(spaces)),
        # 空串 = 按空间名派生（存量行为）；显式填写则原样保存，不做任何自动扩大
        "perm_groups": normalize_perm_group_input(perm_groups),
        "enabled": True,
        "tenant_id": tid,
    }
    meta_store.save_user(rec)
    logger.info("创建用户 username=%s tenant=%s role=%s spaces=%s perm_groups=%s by=%s",
                username, tid, role, rec["spaces"], rec["perm_groups"], created_by)
    return public_user(rec)


def normalize_perm_group_input(raw: str) -> str:
    """规范化密级输入：去空、去重、稳定排序；`*` 单独保留。

    这里**不追加 all**：写什么就是什么。追加 all 是读取侧
    （_derive_perm_groups）的职责，写入侧做扩大等于给用户偷偷提权。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    items = {x.strip() for x in raw.split(",") if x.strip()}
    if "*" in items:
        return "*"
    return ",".join(sorted(items))


def update_user(user_id: str, fields: dict, by: str = "system") -> dict:
    """更新用户。改密码时自动重算 hash，并让该用户所有旧 token 尽快失效。"""
    rec = meta_store.get_user(user_id)
    if not rec:
        raise AppError("用户不存在", code=404)

    patch: dict = {}
    if "role" in fields and fields["role"]:
        role = normalize_role(fields["role"])
        if role not in ROLES:
            raise AppError(f"未知角色：{fields['role']}", code=422)
        patch["role"] = role
    if "spaces" in fields and fields["spaces"] is not None:
        patch["spaces"] = ",".join(_parse_spaces(fields["spaces"]))
    if "perm_groups" in fields and fields["perm_groups"] is not None:
        patch["perm_groups"] = normalize_perm_group_input(fields["perm_groups"])
    if "display_name" in fields and fields["display_name"] is not None:
        patch["display_name"] = fields["display_name"]
    if "enabled" in fields and fields["enabled"] is not None:
        patch["enabled"] = bool(fields["enabled"])
    if fields.get("password"):
        if len(fields["password"]) < 8:
            raise AppError("密码至少 8 位", code=422)
        h, s = hash_password(fields["password"])
        patch["password_hash"] = h
        patch["salt"] = s

    if patch:
        meta_store.update_user(user_id, patch)
        logger.info("更新用户 user_id=%s fields=%s by=%s", user_id, sorted(patch), by)
    updated = meta_store.get_user(user_id) or rec
    return public_user(updated)


def list_users(tenant_id: str = "") -> List[dict]:
    """列出用户。租户管理员只看本租户；平台超管传空值看全部。"""
    return [public_user(u) for u in meta_store.list_users(tenant_id)]


def bootstrap_admin_user() -> Optional[dict]:
    """平台首次启动时创建管理员账号。密码来源优先级：env > 随机生成并打印。

    与 API Key 的 bootstrap 同理：全新环境必须有一条能进系统的路，
    否则部署完只能干瞪眼。生产要求首次登录后立即改密。

    **顺序陷阱**：create_user 内部会走 check_user_quota → require_active(tid)，
    而 require_active 对不存在的租户直接抛 404。全新环境（空库）里没有任何
    租户记录，于是"建第一个管理员"必然失败——表现为容器日志里一句
    「初始化管理员账号失败：租户不存在：default」，然后用户无法登录。
    所以这里必须**先把默认租户落库**，再建账号。
    这不是防御性冗余：bootstrap 就是全系统的"第 0 步"，它不能假设任何前置数据存在。
    """
    username = (settings.bootstrap_admin_user or "admin").strip()
    # P4：初始管理员归入默认租户。多租户下"平台超管"仍是一个租户内的账号，
    # 跨租户的运维能力靠 role=root 单独判定，而不是靠账号落在哪个租户里。
    tid = settings.default_tenant_id
    _ensure_bootstrap_tenant(tid)
    try:
        existing = meta_store.list_users(tid)
    except Exception as exc:
        logger.warning("用户表不可用，跳过管理员账号初始化：%s", exc)
        return None
    if existing:
        return None

    password = settings.bootstrap_admin_password or secrets.token_urlsafe(12)
    # 初始管理员显式给全密级：否则首次登录会遇到"我是管理员却看不到机密文档"，
    # 而这恰恰是最容易被误判成 bug 的行为。权限仍可后续按需收窄。
    rec = create_user(username=username, password=password, role="admin",
                      spaces="*", perm_groups="*", tenant_id=tid,
                      display_name="系统管理员", created_by="bootstrap")
    if not settings.bootstrap_admin_password:
        logger.warning("=" * 72)
        logger.warning("已创建初始管理员账号（密码仅本次输出，请立即修改）：")
        logger.warning("  用户名：%s", username)
        logger.warning("  密  码：%s", password)
        logger.warning("=" * 72)
    return {**rec, "initial_password": None if settings.bootstrap_admin_password else password}


def _ensure_bootstrap_tenant(tenant_id: str) -> None:
    """确保 bootstrap 用的默认租户存在。

    只负责"不存在就建一条最小记录"，不覆盖已存在的租户配置——
    运维可能已经手工调过配额或名字，bootstrap 不该把它重置回默认值。
    """
    try:
        if meta_store.get_tenant(tenant_id):
            return
        meta_store.save_tenant({
            "tenant_id": tenant_id,
            "name": settings.default_tenant_name,
            "description": "系统初始化时自动创建的默认租户",
            "max_users": 0,          # 0 = 不限额，避免首个管理员被自己的默认配额挡住
            "max_docs": 0,
            "max_qps": 0,
            "status": "active",
        })
        logger.info("已创建默认租户 tenant=%s", tenant_id)
    except Exception as exc:
        logger.warning("默认租户初始化失败 tenant=%s：%s", tenant_id, exc)


def ensure_default_space() -> None:
    """确保默认空间存在。文档的 space_id 默认指向它，缺了会导致列表页空。

    P4：默认空间按租户各建一份。space_id 在租户间可重复，但要显式落 tenant_id，
    否则新租户的文档会因为找不到同租户的空间而全部落进"默认租户的空间"。
    """
    tid = settings.default_tenant_id
    try:
        if meta_store.get_space(settings.default_space_id, tid):
            return
        meta_store.save_space({
            "space_id": settings.default_space_id,
            "name": settings.default_space_name,
            "description": "系统默认知识库空间，未指定空间的文档归入此处",
            "owner": "system",
            "tenant_id": tid,
            "status": "active",
        })
        logger.info("已创建默认知识库空间 tenant=%s space_id=%s", tid, settings.default_space_id)
    except Exception as exc:
        logger.warning("默认空间初始化失败（不影响启动）：%s", exc)


def ensure_tenant_baseline(tenant_id: str) -> None:
    """新租户开张时补齐最小可用集合：默认空间 + 预置角色说明。

    租户是隔离边界，不是"用户组"。没有默认空间的新租户登录后第一屏就是空列表，
    用户会以为是故障。所以开通即建。
    """
    try:
        if not meta_store.get_space(settings.default_space_id, tenant_id):
            meta_store.save_space({
                "space_id": settings.default_space_id,
                "name": settings.default_space_name,
                "description": "系统默认知识库空间，未指定空间的文档归入此处",
                "owner": "system",
                "tenant_id": tenant_id,
                "status": "active",
            })
            logger.info("已为新租户建立默认空间 tenant=%s", tenant_id)
    except Exception as exc:
        logger.warning("新租户基线初始化失败 tenant=%s：%s", tenant_id, exc)


def _iso(v) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v else None
