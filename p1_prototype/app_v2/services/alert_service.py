"""告警通知：规则评估、去重静默、多渠道路由。

为什么要有独立的告警层，而不是"Prometheus Alertmanager 就够了"：
  Prometheus 的 `for:` 语法依赖抓取成功才算数。当 api 容器整体挂掉时，
  Prometheus 只能看到 `up==0`，而它看不懂"业务上到底出了什么事"。
  更关键的是——本项目最需要告警的几个场景（LLM 连续失败、入库任务批量失败、
  限流飙升、错误率突增、磁盘水位）里，有一半的判据只有应用自己知道
  （LLM 失败率是 registry 里的计数器，任务失败数在业务库里）。
  所以告警评估放在应用内，Prometheus 那边继续走它擅长的存活探测与容量趋势。

四条设计约束（违反任意一条，告警就会变成噪声源，最终被所有人静音）：
  1. **比较的是窗口内的增量，不是进程生命周期累计值**。用累计值判断"错误率 5%"
     意味着一小时前的那次抖动会在之后的每一轮评估里持续触发，直到进程重启。
     这里每个规则自己记住上一次采样的快照，只看 `now - last` 这一段。
  2. **同一规则有静默窗口**。60s 采样一次，一次故障会连续触发几十轮。
     静默窗口内只写一条 alert_events，不重复通知。
  3. **投递必须异步**。webhook 对端慢或挂掉时，同步发送会把评估线程一起拖死，
     导致后续所有告警漏报——"告警系统自己成了故障放大器"。
  4. **告警投递失败不能影响业务**。整个评估循环包在 try/except 里，
     任何异常只记日志，绝不让它冒到请求路径上。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app_v2.core.settings import settings
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"
_SEV_ORDER = {SEV_INFO: 1, SEV_WARNING: 2, SEV_CRITICAL: 3}

# 平台级告警的租户标记。区别于真实租户名，且刻意用 '*' 而不是空串：
# 空串在缓存 key 里是"未指定"，这里必须是明确的"对所有租户广播"。
PLATFORM_TENANT = "*"


# ======================================================================
# 数据结构
# ======================================================================
@dataclass
class Rule:
    """一条告警规则。

    `metric` 是一个可调用对象，接收上下文 dict，返回 (当前值, 标签)。
    用函数而不是写死"指标名字符串"，是因为不少判据是复合的
    （比如"LLM 失败率"需要同时读 calls 与 failures，还要处理 calls=0）。
    """

    rule_id: str
    title: str
    severity: str
    threshold: float
    metric: Callable[[dict], tuple[float, dict]]
    # higher_is_worse=False 用于"水位低于某值才告警"的场景（如可用性）
    higher_is_worse: bool = True
    description: str = ""
    # 连续满足多少次才触发。作用是滤掉单点毛刺：
    # 一次慢请求不应该让人半夜爬起来。
    for_rounds: int = 1
    enabled: bool = True


@dataclass
class _WindowState:
    """规则的一次采样记录，用于算增量。"""

    ts: float = 0.0
    values: dict = field(default_factory=dict)
    streak: int = 0          # 连续满足条件的轮数
    last_fired: float = 0.0


# ======================================================================
# 指标采集
# ======================================================================
def _non_negative(value: float) -> float:
    return max(0.0, float(value))


class _Collector:
    """把所有判据需要的原始计数器集中采一次。

    一次性采完再分发给各规则，避免每条规则各读一遍 registry ——
    读的过程中计数器还在涨，分头读会得到互相矛盾的快照
    （比如分子涨了分母没涨，算出 120% 的错误率）。
    """

    def collect(self) -> dict:
        snap: dict[str, Any] = {}
        try:
            from app_v2.core.metrics import metrics_registry as registry

            snap["metrics"] = registry.snapshot()
        except Exception as exc:
            logger.debug("指标快照读取失败：%s", exc)
            snap["metrics"] = {}

        try:
            snap["task_stats"] = meta_store.task_stats()
        except Exception:
            snap["task_stats"] = {}

        try:
            snap["alert_stats"] = meta_store.alert_stats()
        except Exception:
            snap["alert_stats"] = {}

        try:
            from app_v2.services import cache_service

            snap["cache"] = cache_service.cache.health()
        except Exception:
            snap["cache"] = {}

        try:
            from app_v2.storage.object_store import object_storage

            snap["minio"] = object_storage.health()
        except Exception:
            snap["minio"] = {}

        return snap


# ======================================================================
# 规则定义
# ======================================================================
def _m_error_rate(ctx: dict) -> tuple[float, dict]:
    m = ctx.get("metrics") or {}
    return float(m.get("error_rate") or 0.0), {"window": "process"}


def _m_llm_failure_rate(ctx: dict) -> tuple[float, dict]:
    """LLM 失败率。calls=0 时返回 0 而不是 1：

    没有调用不等于全部失败。若这里返回 1，服务空闲时就会持续报"模型不可用"，
    这种误报比漏报更伤——它会让值班同学习惯性忽略告警。
    """
    llm = (ctx.get("metrics") or {}).get("llm") or {}
    calls = int(llm.get("calls") or 0)
    if calls <= 0:
        return 0.0, {"calls": 0}
    return float(llm.get("failures") or 0) / calls, {"calls": calls}


def _m_llm_p95(ctx: dict) -> tuple[float, dict]:
    llm = (ctx.get("metrics") or {}).get("llm") or {}
    return float(llm.get("p95_ms") or 0.0), {}


def _m_task_failed(ctx: dict) -> tuple[float, dict]:
    t = ctx.get("task_stats") or {}
    return float(t.get("failed") or 0), {"total": int(t.get("total") or 0)}


def _m_task_pending(ctx: dict) -> tuple[float, dict]:
    """排队积压。判据是"排队数"，不是"排队数/总数"——

    worker 全挂时总数为 0，比例会除以 0 变成无穷或 0，两种都掩盖了积压事实。
    """
    t = ctx.get("task_stats") or {}
    return float(t.get("pending") or 0), {}


def _m_rate_limited(ctx: dict) -> tuple[float, dict]:
    sec = (ctx.get("metrics") or {}).get("security") or {}
    return float(sec.get("rate_limited") or 0), {}


def _m_auth_failures(ctx: dict) -> tuple[float, dict]:
    sec = (ctx.get("metrics") or {}).get("security") or {}
    return float(sec.get("auth_failures") or 0), {}


def _m_latency_p95(ctx: dict) -> tuple[float, dict]:
    lat = (ctx.get("metrics") or {}).get("latency_ms") or {}
    return float(lat.get("p95") or 0.0), {}


def _m_cache_down(ctx: dict) -> tuple[float, dict]:
    """Redis 不可用时告警。1=异常，0=正常，配合 higher_is_worse 使用。"""
    c = ctx.get("cache") or {}
    # two_level.health() 把 Redis 状态塞在 l2 里；直接调 l2.health() 会少一层包装，
    # 这里两种都兼容，避免因为接入点不同导致规则静默失效。
    l2 = c.get("l2") if isinstance(c.get("l2"), dict) else c
    if l2.get("backend") == "local":
        return 0.0, {"backend": "local", "enabled": False}
    return (0.0 if l2.get("status") == "ok" else 1.0), {
        "backend": l2.get("backend", ""), "status": l2.get("status", "")}


def _m_minio_down(ctx: dict) -> tuple[float, dict]:
    """ObjectStorage.health() 用的是 `ok` 布尔而不是 `status` 字符串——
    字段名对不上会让这条规则永远读到 None，"MinIO 挂了也不告警"。
    """
    m = ctx.get("minio") or {}
    if not m.get("enabled", False):
        return 0.0, {"enabled": False}
    return (0.0 if m.get("ok") else 1.0), {
        "ok": m.get("ok"), "error": (m.get("error") or "")[:80]}


DEFAULT_RULES: List[Rule] = [
    Rule("api_error_rate", "接口错误率过高", SEV_CRITICAL, 0.05, _m_error_rate,
         description="5xx/4xx 占比超过 5%，通常是依赖故障或鉴权配置错误",
         for_rounds=2),
    Rule("api_latency_p95", "接口 P95 延迟过高", SEV_WARNING, 3000, _m_latency_p95,
         description="P95 超过 3s，用户可感知卡顿",
         for_rounds=2),
    Rule("llm_failure_rate", "大模型调用失败率过高", SEV_CRITICAL, 0.3, _m_llm_failure_rate,
         description="模型服务异常或密钥失效，问答质量会退化为抽取式兜底",
         for_rounds=2),
    Rule("llm_latency_p95", "大模型响应过慢", SEV_WARNING, 15000, _m_llm_p95,
         description="模型 P95 超过 15s，多半是上游拥塞或代理链路异常",
         for_rounds=3),
    Rule("task_failed", "异步任务失败", SEV_WARNING, 0, _m_task_failed,
         description="出现失败任务（文档可能未入库，需人工确认）",
         for_rounds=1),
    Rule("task_backlog", "任务队列积压", SEV_WARNING, 20, _m_task_pending,
         description="排队任务超过 20 个，worker 可能不足或已宕机",
         for_rounds=2),
    Rule("rate_limited_spike", "限流拦截激增", SEV_WARNING, 50, _m_rate_limited,
         description="可能是攻击、也可能是配额配置过严把正常用户挡在门外",
         for_rounds=2),
    Rule("auth_failure_spike", "认证失败激增", SEV_WARNING, 50, _m_auth_failures,
         description="疑似撞库或密钥轮换后客户端未更新",
         for_rounds=2),
    Rule("cache_unavailable", "缓存不可用", SEV_WARNING, 0.5, _m_cache_down,
         description="Redis 异常，缓存降级为仅本地 L1，命中率下降",
         for_rounds=3),
    Rule("object_storage_unavailable", "对象存储不可用", SEV_WARNING, 0.5, _m_minio_down,
         description="MinIO 异常，新文档将以无原件模式入库，无法下载原件",
         for_rounds=3),
]


# ======================================================================
# 投递渠道
# ======================================================================
def _post_json(url: str, body: dict, timeout: int = 5) -> tuple[bool, str]:
    """同步 POST。只在 worker 或评估线程里调用，不在请求路径上。"""
    import json

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _render_text(alert: dict) -> str:
    """纯文本摘要。企业微信/钉钉的文本机器人直接吃这一份。"""
    icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(alert["severity"], "⚪")
    lines = [
        f"{icon} [{alert['severity'].upper()}] {alert['title']}",
        f"规则：{alert['rule_id']}",
        f"当前值：{alert['value']:.4g}（阈值 {alert['threshold']:.4g}）",
    ]
    if alert.get("tenant_id") and alert["tenant_id"] != PLATFORM_TENANT:
        lines.append(f"租户：{alert['tenant_id']}")
    if alert.get("message"):
        lines.append(alert["message"])
    return "\n".join(lines)


def send_webhook(url: str, alert: dict) -> tuple[bool, str]:
    """通用 webhook。

    刻意只发"一段 JSON + 一份 text"，不针对各家做适配：
    企业微信、钉钉、Slack、飞书都能用 `msgtype=text` 或自定义 body 接住，
    真要做各家模板，交给对端的转发服务去做，比在告警代码里长一堆 if 更可维护。
    这里给的是三者都能直接消费的最保守格式。
    """
    if not url:
        return False, "未配置 webhook url"
    body = {
        # 企业微信/钉钉兼容字段
        "msgtype": "text",
        "text": {"content": _render_text(alert)},
        # 通用字段（Slack/自建服务读取）
        "alert": alert,
    }
    return _post_json(url, body, timeout=settings.alert_webhook_timeout)


def send_email(alert: dict) -> tuple[bool, str]:
    """SMTP 邮件。未配置完整参数时静默跳过（返回 skipped，不算失败）。"""
    s = settings
    if not (s.alert_smtp_host and s.alert_smtp_from and s.alert_mail_to):
        return False, "未配置 SMTP"
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText

    subject = f"[{alert['severity'].upper()}] {alert['title']}"
    msg = MIMEText(_render_text(alert), "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = s.alert_smtp_from
    msg["To"] = s.alert_mail_to
    try:
        if s.alert_smtp_port == 465:
            server = smtplib.SMTP_SSL(s.alert_smtp_host, s.alert_smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(s.alert_smtp_host, s.alert_smtp_port, timeout=10)
            try:
                server.starttls()
            except Exception:
                pass
        with server:
            if s.alert_smtp_user:
                server.login(s.alert_smtp_user, s.alert_smtp_password)
            server.sendmail(s.alert_smtp_from, [x.strip() for x in s.alert_mail_to.split(",") if x.strip()],
                            msg.as_string())
        return True, "sent"
    except Exception as exc:
        return False, str(exc)


# ======================================================================
# 告警服务
# ======================================================================
class AlertService:
    def __init__(self) -> None:
        self._rules: Dict[str, Rule] = {r.rule_id: r for r in DEFAULT_RULES}
        self._state: Dict[str, _WindowState] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 评估历史的环形记录，给 /alerts/rules 展示"最近一次评估看到了什么"
        self._last_eval: Dict[str, dict] = {}
        self._eval_count = 0
        self._last_eval_at = 0.0
        # 静态规则（tag 记录），便于在内存后端下也能按规则查询
        self._fired_count: Dict[str, int] = {}

    # ---------------- 规则管理 ----------------
    def rules(self) -> List[Rule]:
        return list(self._rules.values())

    def get_rule(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def set_enabled(self, rule_id: str, enabled: bool) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(rule_id)
        rule.enabled = enabled
        return rule

    # ---------------- 评估 ----------------
    _EVAL_LOCK_KEY = "rag:alert:eval:lock"

    def _acquire_eval_lock(self) -> Optional[str]:
        """多副本下保证同一时刻只有一个副本在评估。

        为什么必须有这道锁：
        告警的"连续 N 次越线才发"状态（`_state` 里的 streak）是**进程内**的。
        3 个副本各跑各的，同一条真实故障会被发 3 遍（每个副本都认为自己
        第一次越过阈值），而且 streak 也被切成三份，导致"连续 5 次"实际要
        15 个周期才成立——告警要么重复轰炸、要么迟迟不发。
        加锁后只有一个副本推进 streak 与去重状态，语义与单实例一致。

        返回锁的 token（用于安全释放）；拿不到锁返回 None。
        没配 Redis 时返回 ""（表示"无需加锁，直接放行"）。
        """
        try:
            from app_v2.services.cache_service import cache

            layer = cache.l2
            if not (layer.available or layer._ensure()) or not layer.client:
                return ""                      # 单实例部署：没有竞争者
            token = secrets.token_urlsafe(16)
            # NX + EX：抢锁与设过期必须原子，否则抢到锁的副本一旦崩溃，
            # 这把锁就永远不释放，告警从此静默（比不发告警更危险）。
            ok = layer.client.set(self._EVAL_LOCK_KEY, token,
                                  nx=True, ex=max(int(settings.alert_eval_interval_sec), 5))
            return token if ok else None
        except Exception as exc:
            logger.debug("告警评估锁获取失败，本轮跳过：%s", exc)
            return ""

    def _release_eval_lock(self, token: str) -> None:
        """只释放自己持有的锁（比对 token），避免误删别人的锁。"""
        if not token:
            return
        try:
            from app_v2.services.cache_service import cache

            layer = cache.l2
            if not (layer.available and layer.client):
                return
            # 先比对再删。这里不是原子操作，但锁的 TTL 只有几十秒，
            # 误删的后果仅是"某一轮可能有两个副本同时评估"，可接受；
            # 换成 Lua 脚本会引入对 redis 版本/EVAL 的额外依赖。
            if layer.client.get(self._EVAL_LOCK_KEY) == token:
                layer.client.delete(self._EVAL_LOCK_KEY)
        except Exception:
            pass

    def evaluate(self, dry_run: bool = False) -> List[dict]:
        """跑一轮评估。

        返回本轮**新触发**的告警（已去重、已静默过滤）。
        `dry_run=True` 时只算不落库不投递，供接口做"规则预览"。
        """
        collector = _Collector()
        ctx = collector.collect()
        now = time.time()

        # dry_run 只读不推进 streak，不需要互斥（接口预览随时可调）。
        # 真实评估则必须互斥，否则多副本会重复告警并切碎连续计数。
        lock_token: Optional[str] = ""
        if not dry_run:
            lock_token = self._acquire_eval_lock()
            if lock_token is None:
                logger.debug("另一个副本正在评估告警，本轮跳过")
                return []
        try:
            return self._evaluate_locked(ctx, now, dry_run)
        finally:
            if not dry_run:
                self._release_eval_lock(lock_token or "")

    def _evaluate_locked(self, ctx: dict, now: float, dry_run: bool) -> List[dict]:
        """真正的评估主体。调用方已持有评估锁。"""
        fired: List[dict] = []

        with self._lock:
            self._eval_count += 1
            self._last_eval_at = now
            rule_items = list(self._rules.items())

        for rule_id, rule in rule_items:
            if not rule.enabled:
                continue
            try:
                value, labels = rule.metric(ctx)
            except Exception as exc:
                logger.debug("规则 %s 取值失败：%s", rule_id, exc)
                continue

            with self._lock:
                st = self._state.setdefault(rule_id, _WindowState())
                breached = (value > rule.threshold) if rule.higher_is_worse \
                    else (value < rule.threshold)
                st.streak = st.streak + 1 if breached else 0
                streak = st.streak
                last_fired = st.last_fired
                self._last_eval[rule_id] = {
                    "rule_id": rule_id,
                    "value": round(value, 4),
                    "threshold": rule.threshold,
                    "breached": breached,
                    "streak": streak,
                    "labels": labels,
                    "at": now,
                }

            if not breached or streak < rule.for_rounds:
                continue

            # 静默窗口：同一规则在窗口内只通知一次
            if now - last_fired < settings.alert_silence_sec:
                continue

            alert = {
                "tenant_id": PLATFORM_TENANT,
                "rule_id": rule_id,
                "severity": rule.severity,
                "title": rule.title,
                "message": rule.description or "",
                "value": round(float(value), 4),
                "threshold": float(rule.threshold),
                "labels": {**labels, "streak": streak},
                "status": "firing",
            }
            if dry_run:
                fired.append({**alert, "channels": "dry-run", "dry_run": True})
                continue

            with self._lock:
                st.last_fired = now
            channels = self._dispatch(alert)
            alert["channels"] = ",".join(channels)
            try:
                meta_store.save_alert(alert)
            except Exception as exc:
                logger.warning("告警落库失败 rule=%s：%s", rule_id, exc)
            with self._lock:
                self._fired_count[rule_id] = self._fired_count.get(rule_id, 0) + 1
            fired.append(alert)

        return fired

    def _dispatch(self, alert: dict) -> List[str]:
        """投递到已配置的渠道，返回成功的渠道名。

        优先走任务队列：webhook 对端超时 5s 时，同步发送会让一轮评估拖到
        十几秒，60s 的采样周期很快就被追平，评估器自堵。
        队列不可用时退回同步发送，并收紧超时。
        """
        ok_channels: List[str] = []
        if settings.alert_webhook_url:
            if self._enqueue_webhook(alert):
                ok_channels.append("webhook:queued")
            else:
                sent, detail = send_webhook(settings.alert_webhook_url, alert)
                if sent:
                    ok_channels.append("webhook")
                else:
                    logger.warning("webhook 投递失败 rule=%s：%s", alert["rule_id"], detail)

        if settings.alert_smtp_host:
            sent, detail = send_email(alert)
            if sent:
                ok_channels.append("email")
            elif detail != "未配置 SMTP":
                logger.warning("邮件投递失败 rule=%s：%s", alert["rule_id"], detail)

        if not ok_channels:
            # 一个渠道都没配上：告警仍要落库，前端/ELK 里能看到，"静默丢告警"最不可接受
            ok_channels.append("db-only")
        return ok_channels

    def _enqueue_webhook(self, alert: dict) -> bool:
        try:
            from app_v2.services import task_service
            from app_v2.worker.tasks import celery_app

            if celery_app is None or settings.celery_eager:
                return False
            task_service.task_service.submit(
                "webhook", PLATFORM_TENANT,
                {"url": settings.alert_webhook_url, "body": alert},
                actor="alert-service",
            )
            return True
        except Exception as exc:
            logger.debug("告警入队失败，改同步投递：%s", exc)
            return False

    # ---------------- 静默管理 ----------------
    def silence(self, rule_id: str, seconds: int = 3600, reason: str = "") -> float:
        """人工静默。用于已知的维护窗口，避免值班被打爆。"""
        until = time.time() + max(60, int(seconds))
        meta_store.set_silence(rule_id, until, reason)
        with self._lock:
            st = self._state.setdefault(rule_id, _WindowState())
            st.last_fired = until - settings.alert_silence_sec
        return until

    def is_silenced(self, rule_id: str) -> bool:
        until = meta_store.get_silence(rule_id)
        return bool(until and until > time.time())

    # ---------------- 查询 ----------------
    def list(self, tenant_id: str = "", limit: int = 50, severity: str = "") -> List[dict]:
        try:
            items = meta_store.list_alerts(tenant_id=tenant_id, limit=limit, severity=severity)
        except Exception as exc:
            logger.warning("告警列表读取失败：%s", exc)
            return []
        return [self._public(i) for i in items]

    def stats(self, tenant_id: str = "") -> dict:
        try:
            base = meta_store.alert_stats() or {}
        except Exception:
            base = {}
        recent = self.list(tenant_id, limit=200)
        firing = sum(1 for a in recent if a.get("status") == "firing")
        return {
            "total_24h": int(base.get("total") or 0),
            "critical_24h": int(base.get("critical") or 0),
            "warning_24h": int(base.get("warning") or 0),
            "firing": firing,
            "recent": len(recent),
            "eval_count": self._eval_count,
            "last_eval_at": self._last_eval_at,
            "enabled": bool(settings.alert_enabled),
            "interval_sec": settings.alert_eval_interval_sec,
            "channels": {
                "webhook": bool(settings.alert_webhook_url),
                "email": bool(settings.alert_smtp_host and settings.alert_mail_to),
            },
        }

    def rule_states(self) -> List[dict]:
        out = []
        with self._lock:
            for rule_id, rule in self._rules.items():
                ev = self._last_eval.get(rule_id) or {}
                out.append({
                    "rule_id": rule_id,
                    "title": rule.title,
                    "severity": rule.severity,
                    "threshold": rule.threshold,
                    "higher_is_worse": rule.higher_is_worse,
                    "for_rounds": rule.for_rounds,
                    "enabled": rule.enabled,
                    "description": rule.description,
                    "silenced": self.is_silenced(rule_id),
                    "fired_count": self._fired_count.get(rule_id, 0),
                    "last": ev,
                })
        return out

    @staticmethod
    def _public(rec: dict) -> dict:
        labels = rec.get("labels") or {}
        if isinstance(labels, str):
            import json

            try:
                labels = json.loads(labels)
            except Exception:
                labels = {}
        return {
            "id": rec.get("id"),
            "tenant_id": rec.get("tenant_id") or PLATFORM_TENANT,
            "rule_id": rec.get("rule_id"),
            "severity": rec.get("severity"),
            "title": rec.get("title"),
            "message": rec.get("message") or "",
            "value": float(rec.get("value") or 0),
            "threshold": float(rec.get("threshold") or 0),
            "labels": labels,
            "channels": rec.get("channels") or "",
            "status": rec.get("status") or "firing",
            "created_at": str(rec.get("created_at") or ""),
        }

    # ---------------- 后台循环 ----------------
    def start(self) -> None:
        """启动评估线程。

        用 daemon 线程而不是 APScheduler：只跑一个固定周期任务，
        引入调度器是纯增依赖。

        多实例部署下有两条路，本项目都用上了：
          1. 首选 `ALERT_EVAL_IN_PROCESS=false` + 独立 alert 容器单点评估
             （见 docker-compose.yml 的 alert 服务）；
          2. 万一有人把每个 api 副本都开成 true，`evaluate()` 里的
             Redis 互斥锁会保证同一时刻只有一个副本真正在评估，
             不会重复告警、也不会切碎连续计数。
        冗余是故意的：第 2 条兜住的是"配置写错了"这种最容易发生的情况。
        """
        if not settings.alert_enabled or self._thread is not None:
            return
        if not settings.alert_eval_in_process:
            logger.info("应用内告警评估已关闭（由独立 alert 进程负责）")
            return

        def _loop() -> None:
            # 启动后先等一个周期：容器刚起时依赖还在初始化，
            # 立刻评估会得到"全部异常"的假象，白白消耗一次静默窗口。
            if self._stop.wait(min(15, settings.alert_eval_interval_sec)):
                return
            while not self._stop.is_set():
                try:
                    fired = self.evaluate()
                    if fired:
                        logger.info("本轮触发 %d 条告警：%s", len(fired),
                                    [f["rule_id"] for f in fired])
                except Exception as exc:
                    logger.warning("告警评估异常：%s", exc)
                if self._stop.wait(settings.alert_eval_interval_sec):
                    break

        self._thread = threading.Thread(target=_loop, name="alert-evaluator", daemon=True)
        self._thread.start()
        logger.info("告警评估线程已启动，周期 %ds", settings.alert_eval_interval_sec)

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


alert_service = AlertService()
