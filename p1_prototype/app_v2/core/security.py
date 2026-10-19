"""安全模块：角色与权限模型、API Key 生成与校验。

设计要点（企业生产必需）：
  1. **密钥永不落明文**：只在创建时返回一次，库里只存 PBKDF2 哈希 + 盐。
  2. **权限最小化**：角色 -> 权限集合，接口按权限点声明，不在代码里写死角色名。
  3. **可轮换**：支持过期时间、启用/禁用、吊销，方便离职与泄露处置。
  4. **可追溯**：密钥带 owner 标识，审计日志能落到具体责任人。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Set

# ---------------- 权限点 ----------------
# 命名约定：<资源>:<动作>，*代表全部权限
PERM_KB_READ = "kb:read"            # 检索与问答
PERM_KB_WRITE = "kb:write"          # 上传/删除文档
PERM_KB_ADMIN = "kb:admin"          # 重建索引、清理
PERM_SPACE_ADMIN = "space:admin"    # P3：创建/删除知识库空间、配置空间成员
PERM_AUDIT_READ = "audit:read"      # 查看审计日志
PERM_USER_ADMIN = "user:admin"      # 管理密钥与用户
PERM_METRIC_READ = "metric:read"    # 查看监控指标

ALL_PERMISSIONS: FrozenSet[str] = frozenset({
    PERM_KB_READ, PERM_KB_WRITE, PERM_KB_ADMIN, PERM_SPACE_ADMIN,
    PERM_AUDIT_READ, PERM_USER_ADMIN, PERM_METRIC_READ,
})


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    permissions: FrozenSet[str]
    description: str


ROLES: Dict[str, Role] = {
    "admin": Role(
        name="admin", label="管理员",
        permissions=ALL_PERMISSIONS,
        description="全部权限：用户与密钥、知识库空间、审计、监控",
    ),
    "kb_admin": Role(
        name="kb_admin", label="知识库管理员",
        permissions=frozenset({PERM_KB_READ, PERM_KB_WRITE, PERM_KB_ADMIN, PERM_SPACE_ADMIN}),
        description="管理知识库内容与空间：上传/删除文档、重建索引、创建空间，不能管理用户与查看审计",
    ),
    "user": Role(
        name="user", label="普通用户",
        permissions=frozenset({PERM_KB_READ}),
        description="仅可在被授权的知识库空间内检索与问答",
    ),
}

# 旧角色名兼容：P2 的 editor/viewer 在 P3 归并到 kb_admin/user。
# 保留映射而不是直接删表，是为了让已发出的 API Key（库里存的是旧角色名）继续可用，
# 避免升级时全量作废密钥——这是生产环境升级的基本要求。
ROLE_ALIASES: Dict[str, str] = {
    "editor": "kb_admin",     # 知识编辑 → 知识库管理员
    "viewer": "user",         # 只读用户 → 普通用户
}


def normalize_role(role_name: str) -> str:
    """把可能的历史角色名归一化为当前角色名；未知角色回落到 'user'。"""
    name = (role_name or "").strip()
    name = ROLE_ALIASES.get(name, name)
    return name if name in ROLES else "user"


DEFAULT_ROLE = "user"


def role_permissions(role_name: str) -> FrozenSet[str]:
    role = ROLES.get(normalize_role(role_name))
    return role.permissions if role else frozenset()


def has_permission(role_name: str, permission: str) -> bool:
    perms = role_permissions(role_name)
    return "*" in perms or permission in perms


def list_roles() -> List[dict]:
    """角色清单（含标签与权限点），供 /auth/roles 与前端按钮级控制使用。"""
    return [
        {
            "name": r.name, "label": r.label, "description": r.description,
            "permissions": sorted(role_permissions(r.name)),
        }
        for r in ROLES.values()
    ]


# ---------------- 密钥 ----------------
KEY_PREFIX = "rag_"
PBKDF2_ROUNDS = 120_000


@dataclass
class ApiKeyRecord:
    """一条密钥记录。明文只在创建瞬间存在，之后只保留哈希。"""
    key_id: str
    owner: str
    role: str
    key_hash: str
    salt: str
    perm_group: str = "all"                 # 该密钥可访问的知识权限组，逗号分隔
    spaces: str = "*"                       # P3：可访问的知识库空间，"*" 表示全部
    enabled: bool = True
    created_at: str = ""
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    description: str = ""
    revoked_at: Optional[str] = None
    meta: Dict[str, str] = field(default_factory=dict)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return now > exp
        except ValueError:
            return False

    def is_active(self) -> bool:
        return self.enabled and not self.revoked_at and not self.is_expired()


def generate_api_key() -> str:
    """生成 32 字节随机密钥，带前缀便于日志与密钥扫描识别。"""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str, salt: Optional[str] = None) -> tuple[str, str]:
    """返回 (哈希, 盐)。PBKDF2-HMAC-SHA256，迭代次数与 OWASP 建议一致。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw_key.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def verify_api_key(raw_key: str, expected_hash: str, salt: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw_key.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return hmac.compare_digest(digest.hex(), expected_hash)


def key_id_of(raw_key: str) -> str:
    """从明文密钥派生短标识，用于日志与索引，不泄露密钥本身。"""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]


def mask_key(raw_key: str) -> str:
    """日志脱敏：保留前缀与后 4 位。"""
    if len(raw_key) <= 12:
        return "***"
    return f"{raw_key[:8]}...{raw_key[-4:]}"


def normalize_perm_groups(perm_group: str) -> List[str]:
    """解析权限组字符串，调用方总能访问公共知识（all）。

    ⚠ 本函数语义是「**可见范围**」，仅用于鉴权读取侧（判断 principal 能看什么）。
    不要用它处理写入目标组——那会把用户选择的 `hr` 悄悄改写成 `all,hr`，
    导致密件被扩大为全员可见。写入侧请用 `parse_perm_groups`。
    """
    groups = parse_perm_groups(perm_group)
    groups.add("all")
    return sorted(groups)


def parse_perm_groups(perm_group: str) -> Set[str]:
    """解析权限组字符串为「**目标组**」集合（不加 all、不做任何扩大）。

    写入路径（上传/更新文档的 perm_group）必须用这个，
    否则权限组隔离会在数据落库那一刻被静默破坏。
    """
    raw = (perm_group or "").strip()
    if not raw:
        return {"all"}
    return {g.strip() for g in raw.split(",") if g.strip()} or {"all"}


# ---------------- P3：知识库空间 ----------------
DEFAULT_SPACE = "default"


def parse_spaces(spaces: str) -> List[str]:
    """解析空间白名单字符串。

    "*" 表示全部空间（管理员默认）；空值回落到 default 空间。
    与 perm_group 同样的原则：**不做静默扩大**，写什么就是什么。
    """
    raw = (spaces or "").strip()
    if raw == "*":
        return ["*"]
    if not raw:
        return [DEFAULT_SPACE]
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or [DEFAULT_SPACE]


SPACE_WILDCARDS = frozenset({"*", "all"})


def space_allowed(space_id: str, allowed: List[str]) -> bool:
    """判断某个空间是否在可见范围内。

    两个通配符都接受，因为它们在不同入口表达同一件事：
      - `*`   ：账号的 spaces 字段写 `*`（"我有全部空间的成员资格"）
      - `all` ：API Key 的 perm_group 写 `all`（P2 的历史约定，"全部"）
    只认 `*` 会导致管理员密钥（perm_group=all）反而无权往任何空间投放文档——
    权限系统最忌讳的就是"最高权限者被自己挡住"。
    """
    if not allowed:
        return False
    if SPACE_WILDCARDS & set(allowed):
        return True
    return space_id in allowed


# ---------------- 密码 ----------------
PASSWORD_MIN_LEN = 8


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """账号密码同样用 PBKDF2，与 API Key 一致的强度与常量时间比较。"""
    return hash_api_key(password, salt)


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    return verify_api_key(password, expected_hash, salt)


def check_password_strength(password: str) -> Optional[str]:
    """返回错误信息，None 表示通过。企业内网系统不追求复杂策略，但拒绝弱口令。"""
    if len(password or "") < PASSWORD_MIN_LEN:
        return f"密码长度至少 {PASSWORD_MIN_LEN} 位"
    if password.isdigit() or password.isalpha():
        return "密码不能是纯数字或纯字母，需混合"
    common = {"12345678", "password", "admin123", "qwerty123", "123456789"}
    if password.lower() in common:
        return "密码过于常见，请更换"
    return None


def spaces_wildcard(spaces) -> bool:
    """space 列表是否代表"全部空间"。

    所有下推层（元数据 SQL、Qdrant 过滤、进程内兜底、缓存 key）都必须用同一判据，
    否则会出现"某条链路以为要过滤、另一条不过滤"的半隔离状态——
    这类不一致比完全不过滤更危险，因为测试很容易只覆盖到其中一条。
    """
    if spaces is None:
        return True
    return bool(SPACE_WILDCARDS & set(spaces))
