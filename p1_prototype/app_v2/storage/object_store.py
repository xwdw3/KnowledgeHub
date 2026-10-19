"""MinIO 对象存储：文档原件的权威落点。

为什么要对象存储而不是继续放本地磁盘：
  - **多实例**：P4 要求同一服务能水平扩容到多副本，本地磁盘意味着
    "传到 A 实例的文件，从 B 实例下载 404"。对象存储是所有副本共享的同一份。
  - **原件与元数据分离**：PostgreSQL 只存路径和校验和，正文与二进制不落库。
    把 20MB 的 PDF 塞进 bytea 会把备份、迁移、行缓存全部拖慢。
  - **预签名直传**：下载走 MinIO 的短时签名 URL，不经过 API 进程，
    省带宽也避免大文件占满工作进程。

Key 布局（顺序即隔离边界，不可调换）：
    {tenant_id}/{space_id}/{doc_id}/{file_name}

  租户在最前：即使日后被人用前缀遍历列出对象，也只能看到自己租户的目录。
  路径里的每一层都做了字符清洗，防止 `../` 把 key 跳出当前租户前缀。
"""
from __future__ import annotations

import io
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app_v2.core.response import AppError
from app_v2.core.settings import settings

logger = logging.getLogger(__name__)

# key 里允许的字符：字母数字、中文、常见安全符号。其余一律替换为下划线。
# 不做严格白名单会出事：`?` `#` `%` 会破坏预签名 URL，`/` 会伪造目录层级。
_UNSAFE_KEY_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5._\-]+")

# 扩展名 → MIME。用途只有一个：浏览器点"下载"时不要一律变成
# application/octet-stream（那会让 PDF 被下成 .bin）。类型猜错不影响入库，
# 所以这里不追求完备，覆盖本项目允许的四种格式即可。
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def guess_content_type(file_name: str) -> str:
    """按扩展名推断 MIME；未知类型退回二进制流。"""
    return _CONTENT_TYPES.get(Path(file_name).suffix.lower(),
                              "application/octet-stream")


def _safe_segment(raw: str, fallback: str = "_") -> str:
    """清洗 key 中的一段路径。

    必须显式干掉 `.` 与 `..`：它们本身符合"安全字符"定义，
    但拼进 path 后可以跳出当前前缀，是最典型的路径穿越。
    """
    s = (raw or "").strip()
    if not s or s in (".", ".."):
        return fallback
    s = _UNSAFE_KEY_CHARS.sub("_", s)
    # 去掉首尾的点：`.hidden` 在某些 S3 兼容实现里会被忽略
    s = s.strip("._") or fallback
    return s[:200]


def build_key(tenant_id: str, space_id: str, doc_id: str, file_name: str) -> str:
    """构造对象 key。租户必须是最外层前缀。"""
    return "/".join([
        _safe_segment(tenant_id, "default"),
        _safe_segment(space_id, "default"),
        _safe_segment(doc_id, "unknown"),
        _safe_segment(file_name, "unnamed"),
    ])


class ObjectStorage:
    """MinIO 客户端封装。

    设计取舍：所有方法**永不抛异常到业务层**（除了显式调用 require 的写入），
    存储不可用时降级为"无原件"模式——上传仍成功，只是不能下载原件。
    理由：对象存储是辅助能力，不该因为它挂掉就让整条入库链路失败。
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self._init_error: str = ""
        self._checked = False
        self._bucket_ready = False
        # 桶不可用后的冷却截止时间（monotonic）。
        # 没有它的话，对象存储长时间宕机时**每个入库任务都要白等一轮超时**：
        # 100 个文件排队，就多花 100 × 5 秒，而且日志里看不出是同一个原因。
        self._bucket_retry_after: float = 0.0

    # ---------------- 连接 ----------------
    @property
    def enabled(self) -> bool:
        return bool(settings.minio_enabled)

    def _ensure_client(self):
        """惰性建连。构造时连不上不报错，等到真正用的时候再暴露。"""
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                from minio import Minio
                from urllib3 import PoolManager, Retry, Timeout

                timeout = int(settings.minio_timeout or 5)
                retry = Retry(total=int(settings.minio_retries or 1),
                              backoff_factor=0.2,
                              # 只重试"可能是瞬时"的状态码。连接超时/拒绝不重试：
                              # 对象存储没起来的时候，重试只是把等待时间乘以次数。
                              status_forcelist=[500, 502, 503, 504])
                http_client = PoolManager(
                    timeout=Timeout(connect=timeout, read=timeout),
                    retries=retry,
                    maxsize=10,
                )
                self._client = Minio(
                    settings.effective_minio_endpoint,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=bool(settings.minio_secure),
                    http_client=http_client,
                )
                self._init_error = ""
            except ImportError:
                # 没装 urllib3（理论上 minio 的依赖）时退回默认客户端，
                # 不能因为拿不到超时配置就把整个对象存储禁用掉。
                from minio import Minio

                self._client = Minio(
                    settings.effective_minio_endpoint,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=bool(settings.minio_secure),
                )
                self._init_error = ""
            except Exception as exc:
                self._init_error = str(exc)
                logger.warning("MinIO 客户端初始化失败：%s", exc)
                self._client = None
            return self._client

    def _ensure_bucket(self) -> bool:
        if self._bucket_ready:
            return True
        # 冷却期内直接返回 False，不再发起连接。
        # 这里用"快速失败"而不是"等它恢复"：调用方都有无原件降级路径，
        # 让它立刻降级比让它挂在超时上更符合业务预期。
        if time.monotonic() < self._bucket_retry_after:
            return False
        client = self._ensure_client()
        if client is None:
            return False
        try:
            if not client.bucket_exists(settings.minio_bucket):
                client.make_bucket(settings.minio_bucket)
                logger.info("已创建对象存储桶 bucket=%s", settings.minio_bucket)
            self._bucket_ready = True
            self._bucket_retry_after = 0.0
            return True
        except Exception as exc:
            self._bucket_retry_after = time.monotonic() + max(
                1, int(settings.minio_failure_cooldown))
            logger.warning("对象存储桶不可用 bucket=%s（%ds 内快速失败）：%s",
                           settings.minio_bucket, settings.minio_failure_cooldown, exc)
            return False

    # ---------------- 写入 ----------------
    def put(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> bool:
        """上传对象。返回是否成功——失败不抛异常，由调用方决定是否在意。"""
        client = self._ensure_client()
        if client is None or not self._ensure_bucket():
            return False
        try:
            client.put_object(
                settings.minio_bucket, key,
                io.BytesIO(content), length=len(content),
                content_type=content_type or "application/octet-stream",
            )
            return True
        except Exception as exc:
            logger.error("对象上传失败 key=%s：%s", key, exc)
            return False

    def put_file(self, key: str, path: str, content_type: str = "") -> bool:
        client = self._ensure_client()
        if client is None or not self._ensure_bucket():
            return False
        try:
            client.fput_object(settings.minio_bucket, key, path,
                               content_type=content_type or None)
            return True
        except Exception as exc:
            logger.error("对象上传失败 key=%s path=%s：%s", key, path, exc)
            return False

    # ---------------- 读取 ----------------
    def get(self, key: str) -> Optional[bytes]:
        client = self._ensure_client()
        if client is None:
            return None
        try:
            resp = client.get_object(settings.minio_bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception as exc:
            logger.warning("对象读取失败 key=%s：%s", key, exc)
            return None

    def download_to_file(self, key: str, local_path: str, chunk: int = 1024 * 1024) -> bool:
        """流式下载到本地文件。

        与 `get()` 的区别是内存模型：`get()` 把整个对象读进内存，
        1GB 的原件就吃 1GB+ 内存——对 GB 级文档这条路径必须避开。
        这里按 chunk 循环读写，峰值内存 = chunk 大小（默认 1MB），
        与对象大小无关。

        为什么不直接用官方 `fget_object`：它会先查 stat 再下载，
        且中间层有自己的 buffer 与重试逻辑，内存峰值不可控；
        对"要喂给流式解析器"的场景，手工分块更可预测。
        """
        client = self._ensure_client()
        if client is None:
            return False
        resp = None
        try:
            resp = client.get_object(settings.minio_bucket, key)
            with open(local_path, "wb") as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
            return True
        except Exception as exc:
            logger.warning("对象流式下载失败 key=%s：%s", key, exc)
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception:
                pass
            return False
        finally:
            if resp is not None:
                try:
                    resp.close()
                    resp.release_conn()
                except Exception:
                    pass

    def exists(self, key: str) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            client.stat_object(settings.minio_bucket, key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            client.remove_object(settings.minio_bucket, key)
            return True
        except Exception as exc:
            logger.warning("对象删除失败 key=%s：%s", key, exc)
            return False

    def list_objects(self, prefix: str = "", limit: int = 0) -> list[dict]:
        """按前缀列出对象（key / size / last_modified）。

        `limit=0` 表示不限制。注意 MinIO 的 list 是**分页**的，
        这里用生成器展开而不是 `list(...)` 一把梭，
        否则一个租户有几万个对象时会把响应撑爆。
        """
        client = self._ensure_client()
        if client is None:
            return []
        out: list[dict] = []
        try:
            for obj in client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True):
                out.append({
                    "key": obj.object_name,
                    "size": int(obj.size or 0),
                    "last_modified": obj.last_modified.isoformat() if obj.last_modified else "",
                })
                if limit and len(out) >= limit:
                    break
        except Exception as exc:
            logger.warning("对象列举失败 prefix=%s：%s", prefix, exc)
        return out

    def backup_prefix(self, prefix: str, dest_dir: str, tenant_id: str = "") -> dict:
        """把某前缀下的对象备份到本地目录（流式，不占内存）。

        `tenant_id` 只用于校验：**备份路径必须落在该租户前缀内**。
        这个参数存在的意义是防止调用方传一个空 prefix（= 整个桶）
        把其他企业的原件一起打包出去——备份功能天生能"看全量数据"，
        必须有租户边界，否则它就是一个越权出口。

        返回 {objects, bytes, files, skipped}。skipped 用于记录
        因越界被拒绝的 key，便于审计时发现"有人试图跨租户备份"。
        """
        if not prefix:
            raise AppError("备份必须指定租户前缀，不允许整桶备份", code=400)
        if tenant_id and not prefix.startswith(f"{tenant_id}/"):
            raise AppError("备份前缀与租户不匹配", code=403)

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        objects = self.list_objects(prefix)
        total = 0
        files = 0
        skipped: list[str] = []
        for obj in objects:
            key = obj["key"]
            if tenant_id and not key.startswith(f"{tenant_id}/"):
                skipped.append(key)
                continue
            # 目录结构按 key 原样保留：恢复时可以直接按 key 回传，
            # 不需要额外的映射表（映射表本身就是新的故障点）。
            local = dest / key
            local.parent.mkdir(parents=True, exist_ok=True)
            if self.download_to_file(key, str(local)):
                total += obj["size"]
                files += 1
            else:
                skipped.append(key)
        return {"objects": len(objects), "files": files,
                "bytes": total, "skipped": skipped}


        """按前缀批量删除（删文档时清理其全部原件/衍生文件）。"""
        client = self._ensure_client()
        if client is None:
            return 0
        n = 0
        try:
            from minio.deleteobjects import DeleteObject

            errors = client.remove_objects(
                settings.minio_bucket,
                (DeleteObject(o.object_name)
                 for o in client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)),
            )
            for _ in errors:
                n += 1
        except Exception as exc:
            logger.warning("按前缀删除失败 prefix=%s：%s", prefix, exc)
        return n

    def presigned_get_url(self, key: str, expire: int = 0,
                          download_name: str = "") -> str:
        """生成下载用预签名 URL。

        对外域名与内网 endpoint 常常不同（MinIO 在容器网内是 minio:9000，
        浏览器只认 https://files.example.com），所以用 public_endpoint 覆盖
        签名结果里的 host —— 否则签出来的 URL 在浏览器里根本连不上。
        """
        client = self._ensure_client()
        if client is None:
            raise AppError("对象存储不可用，无法生成下载链接", code=503)
        ttl = int(expire or settings.minio_presign_expire)
        # S3 协议上限 7 天，超了签名直接失效
        ttl = max(60, min(ttl, 7 * 24 * 3600))
        try:
            from datetime import timedelta

            extra = {}
            if download_name:
                # 用 RFC 5987 编码，避免中文文件名让部分浏览器乱码
                extra["response-content-disposition"] = (
                    f"attachment; filename*=UTF-8''{quote(download_name)}")
            url = client.presigned_get_object(
                settings.minio_bucket, key, expires=timedelta(seconds=ttl),
                response_headers=extra or None,
            )
        except Exception as exc:
            logger.error("预签名生成失败 key=%s：%s", key, exc)
            raise AppError(f"生成下载链接失败：{exc}", code=503)

        public = (settings.minio_public_endpoint or "").rstrip("/")
        if public:
            # 替换 scheme://host[:port] 部分，保留 path 与签名 query
            m = re.match(r"^https?://[^/]+", url)
            if m:
                scheme = "https" if public.startswith("https://") else "http"
                host = public.split("://", 1)[-1]
                url = f"{scheme}://{host}" + url[m.end():]
        # ⚠ 这里有一个必须知道的约束：**S3 V4 签名把 Host 计入签名内容**。
        # 直接替换 host 之后，若对象存储按原 host 校验，签名会失效并返回
        # SignatureDoesNotMatch —— 现象是"链接能打开但下载 403"。
        #
        # 能正常工作的前提是二者之一成立：
        #   a) 对外域名与签发时用的 endpoint 指向**同一个** MinIO，
        #      且 MinIO 配置了对应的 server-side 域名（MINIO_SERVER_URL）；
        #   b) 前置反向代理把对外域名转成内网 Host 转发。
        # 生产部署推荐 a)：给 MinIO 设 MINIO_SERVER_URL=https://files.example.com，
        # 让服务端自己就用对外域名签名，就不存在替换 host 这一步了。
        # 这种情况下把 MINIO_PUBLIC_ENDPOINT 留空即可。
        return url

    # ---------------- 健康 ----------------
    def health(self) -> dict:
        out = {
            "enabled": self.enabled,
            "endpoint": settings.effective_minio_endpoint,
            "bucket": settings.minio_bucket,
            "backend": "minio",
            "ok": False,
            "error": self._init_error,
        }
        if not self.enabled:
            out["ok"] = True
            out["backend"] = "disabled"
            return out
        try:
            client = self._ensure_client()
            if client is None:
                return out
            out["ok"] = bool(client.bucket_exists(settings.minio_bucket))
            if not out["ok"]:
                out["error"] = "bucket 不存在"
        except Exception as exc:
            out["error"] = str(exc)
        return out


object_storage = ObjectStorage()
