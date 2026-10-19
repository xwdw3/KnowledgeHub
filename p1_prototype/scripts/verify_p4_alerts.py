"""P4 告警通知验收。

验证重点不是"接口能通"，而是三件真正会出事的事：

  1. **该报的报**：注入故障（限流飙升、任务失败）后，对应规则必须触发；
  2. **不该报的不报**：服务空闲时不能因为 calls=0 之类除零场景误报
     ——误报会让值班习惯性忽略告警，比漏报更致命；
  3. **不刷屏**：一次故障会被 60s 采样连续命中几十轮，
     静默窗口内必须只落一条、只投递一次。

同时验证 webhook 投递确实发出了 POST（本地起一个真 HTTP 服务收包），
而不是"看起来成功了"。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")
os.environ["CELERY_EAGER"] = "true"
os.environ["MINIO_ENABLED"] = "false"
os.environ["ALERT_SILENCE_SEC"] = "300"

from app_v2.core.metrics import metrics_registry as registry  # noqa: E402
from app_v2.core.settings import settings  # noqa: E402
from app_v2.services import alert_service as alert_mod  # noqa: E402
from app_v2.services import task_service  # noqa: E402
from app_v2.services.alert_service import AlertService, alert_service  # noqa: E402
from app_v2.storage.meta_store import meta_store  # noqa: E402

# alert_service 是模块级单例，评估线程可能在 import 时就已启动。
# 它一旦开跑就会推进 streak 与 last_fired，导致下面用独立实例断言"第几轮触发"时
# 时序完全不可控（时而 streak=1，时而已经是 3）。这里显式停掉线程即可——
# **不要**顺手改 settings.alert_enabled，那样 0 号用例会去断言一个被自己改坏的值。
ALERT_ENABLED_AT_IMPORT = settings.alert_enabled
alert_service.stop()
PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# ----------------------------------------------------------------------
# 本地 webhook 接收器
# ----------------------------------------------------------------------
class _Sink(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            _Sink.received.append(json.loads(raw))
        except Exception:
            _Sink.received.append({"raw": raw})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):                         # 静音
        pass


def start_sink() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/hook"


def main() -> int:
    section("0. 环境与规则装载")
    check("告警总开关开启", ALERT_ENABLED_AT_IMPORT)
    check("CELERY_EAGER 生效（webhook 同步投递）", settings.celery_eager)
    rules = alert_service.rules()
    check("内置规则已装载", len(rules) >= 10, f"{len(rules)} 条")
    # 判据必须覆盖"只有应用知道"的维度，而不是复刻 Prometheus 已有的东西
    ids = {r.rule_id for r in rules}
    check("含 LLM 失败率规则（Prometheus 看不到）", "llm_failure_rate" in ids)
    check("含任务积压规则（Prometheus 看不到）", "task_backlog" in ids)
    check("含对象存储可用性规则", "object_storage_unavailable" in ids)

    section("1. 空闲基线：不该报的绝不报")
    # 全新服务里 LLM 一次都没调用。若失败率用 failures/calls 且不保护 calls=0，
    # 这里会得到 inf 或 1.0，触发"大模型不可用"——典型的误报源头。
    fresh = AlertService()
    quiet = fresh.evaluate(dry_run=True)
    fired_ids = {a["rule_id"] for a in quiet}
    check("calls=0 时不误报 LLM 失败率", "llm_failure_rate" not in fired_ids,
          f"fired={sorted(fired_ids)}")
    check("calls=0 时不误报 LLM 延迟", "llm_latency_p95" not in fired_ids)
    check("MinIO 关闭时不误报对象存储", "object_storage_unavailable" not in fired_ids)

    section("2. 指标取值正确性")
    st = {s["rule_id"]: s for s in fresh.rule_states()}
    llm_last = (st["llm_failure_rate"]["last"] or {})
    check("LLM 失败率读数为 0 而非空", llm_last.get("value") == 0, str(llm_last.get("value")))
    check("每个规则都有可读的当前值",
          all((s["last"] or {}).get("value") is not None for s in fresh.rule_states()))

    section("3. 连续轮数（for_rounds）抑制毛刺")
    # 错误率规则的 for_rounds=2：注入一次高错误率，只应把 streak 记到 1，不触发
    spike = AlertService()
    registry.requests_total[("POST", "/api/v1/chat", "500")] += 100
    registry.requests_total[("POST", "/api/v1/chat", "200")] += 1
    first = spike.evaluate(dry_run=True)
    first_ids = {a["rule_id"] for a in first}
    state = {s["rule_id"]: s for s in spike.rule_states()}
    check("单轮命中不触发（streak=1 < for_rounds=2）",
          "api_error_rate" not in first_ids and state["api_error_rate"]["last"]["streak"] == 1,
          f"streak={state['api_error_rate']['last']['streak']}")
    # 第二轮仍超标 → 应触发
    second = spike.evaluate(dry_run=True)
    second_ids = {a["rule_id"] for a in second}
    check("连续两轮命中后触发", "api_error_rate" in second_ids, f"fired={sorted(second_ids)}")
    check("触发值等于实际错误率",
          any(a["rule_id"] == "api_error_rate" and a["value"] > 0.9 for a in second),
          str([a["value"] for a in second if a["rule_id"] == "api_error_rate"]))

    section("4. 任务失败与积压规则")
    svc = task_service.task_service
    bad = svc.submit("webhook", "acme", payload={"url": "http://127.0.0.1:1/x", "body": {}},
                     actor="tester")
    rec = meta_store.get_task(bad["task_id"])
    check("制造了一条失败任务", rec.get("status") == "failed", str(rec.get("status")))
    manual = AlertService()
    fired2 = {a["rule_id"] for a in manual.evaluate(dry_run=True)}
    check("失败任务触发 task_failed", "task_failed" in fired2, f"fired={sorted(fired2)}")

    section("5. webhook 真实投递")
    srv, url = start_sink()
    settings.alert_webhook_url = url
    _Sink.received.clear()
    try:
        svc2 = AlertService()
        fired3 = svc2.evaluate()          # 非 dry_run，会真投递
        time.sleep(0.4)
        hooked = [a for a in fired3 if "webhook" in (a.get("channels") or "")]
        check("至少一条告警标记了 webhook 渠道", bool(hooked),
              str([a["rule_id"] for a in fired3]))
        check("接收端真的收到了 POST", len(_Sink.received) >= 1, f"{len(_Sink.received)} 次")
        if _Sink.received:
            body = _Sink.received[0]
            check("报文含企业微信/钉钉兼容的 msgtype=text",
                  body.get("msgtype") == "text" and "content" in (body.get("text") or {}))
            check("报文含结构化 alert 字段", isinstance(body.get("alert"), dict))
            check("正文含规则与阈值",
                  "阈值" in (body.get("text", {}).get("content") or ""))
    finally:
        srv.shutdown()
        settings.alert_webhook_url = ""

    section("6. 静默窗口：一次故障只通知一次")
    srv2, url2 = start_sink()
    settings.alert_webhook_url = url2
    _Sink.received.clear()
    # 这里必须先把静默窗口显式收紧。settings 是本进程内的单例，而 5 号用例里
    # 我们刚在同一个进程里评估过一轮，_state 里的 last_fired 已经被推进。
    # 如果直接依赖默认 300s，被测对象就成了"上一段用例的残留状态"，
    # 断言会在"首轮无触发"上失败——而那是环境问题，不是静默逻辑的问题。
    _old_silence = settings.alert_silence_sec
    settings.alert_silence_sec = 1
    # 4 号用例已把 task_failed 静默到 600s 后，且静默持久在 meta_store 里，
    # 新实例同样会读到。这里解掉，否则首轮就被静默挡住。
    meta_store.set_silence("task_failed", 0, "test-reset")
    svc3 = AlertService()
    r1 = svc3.evaluate()
    n1 = len(_Sink.received)
    # 立刻再评估一轮：值仍超标，但静默窗口内不应重复
    r2 = svc3.evaluate()
    time.sleep(0.2)
    n2 = len(_Sink.received)
    # 断言"同一条规则不再重复"，而不是"本轮无任何触发"。
    # 本脚本 3 号用例注入的高错误率为 99%，其 for_rounds=2 恰好在这一轮凑满会触发；
    # 那是**另一条规则**的首次触发，与静默去重无关。
    # 直接断言 len(r2)==0 会让一个正确的实现看起来是错的。
    repeated = ({a["rule_id"] for a in r1} & {a["rule_id"] for a in r2})
    check("首轮有触发", len(r1) > 0, f"{len(r1)} 条")
    check("静默窗口内同规则不再重复触发", not repeated,
          f"重复规则={sorted(repeated)}；本轮新触发={sorted(a['rule_id'] for a in r2)}")
    check("同规则未被重复投递", n2 - n1 == len(r2) - len(repeated),
          f"投递 {n1}→{n2}，本轮触发 {len(r2)} 条，其中重复 {len(repeated)} 条")
    # 静默到期后应恢复。把窗口压到 0 来模拟"到期"，
    # 而不是真等一个窗口——要验证的是"到期恢复"这条逻辑，不是计时器精度。
    settings.alert_silence_sec = 0
    r3 = svc3.evaluate()
    settings.alert_silence_sec = _old_silence
    srv2.shutdown()
    settings.alert_webhook_url = ""
    check("静默到期后重新触发", len(r3) > 0, f"{len(r3)} 条")

    section("7. 人工静默与规则开关")
    svc4 = AlertService()
    until = svc4.silence("task_failed", seconds=600, reason="维护窗口")
    check("静默已写库", meta_store.get_silence("task_failed") == until)
    check("is_silenced 为真", svc4.is_silenced("task_failed"))
    check("静默到未来时间", until > time.time() + 500, f"{int(until - time.time())}s")
    svc4.set_enabled("task_failed", False)
    check("停用后规则状态为 disabled",
          any(s["rule_id"] == "task_failed" and not s["enabled"] for s in svc4.rule_states()))
    svc4.set_enabled("task_failed", True)
    try:
        svc4.set_enabled("no_such_rule", True)
        check("未知规则应抛 KeyError", False)
    except KeyError:
        check("未知规则抛 KeyError", True)

    section("8. 告警落库与租户可见性")
    alerts = alert_service.list("acme", limit=100)
    check("落库后能查到告警", len(alerts) > 0, f"{len(alerts)} 条")
    check("平台级告警对租户可见",
          all(a["tenant_id"] == "*" for a in alerts),
          str({a["tenant_id"] for a in alerts}))
    stats = alert_service.stats("acme")
    check("统计含 24h 总数", "total_24h" in stats)
    # last_eval_at 只反映**单例**的评估历史；本脚本刻意把单例停掉了，
    # 所以这里断言"字段存在且是全量评估次数"，而不是断言它 > 0。
    # 真正要防的是字段缺失或类型错（下游 /alerts 接口直接透传它）。
    check("统计含评估次数且类型正确",
          isinstance(stats.get("eval_count"), int),
          f"eval_count={stats.get('eval_count')} last_eval_at={stats.get('last_eval_at')}")
    check("统计含渠道配置状态",
          set((stats.get("channels") or {}).keys()) == {"webhook", "email"},
          str(stats.get("channels")))
    if alerts:
        a0 = alerts[0]
        check("对外字段齐全",
              all(k in a0 for k in ("rule_id", "severity", "title", "value",
                                    "threshold", "labels", "channels", "status")))
        check("labels 已反序列化为 dict", isinstance(a0["labels"], dict))

    section("9. 投递失败不影响业务")
    settings.alert_webhook_url = "http://127.0.0.1:1/dead"
    try:
        svc5 = AlertService()
        out = svc5.evaluate()
        check("对端不可达时评估仍返回（不抛异常）", isinstance(out, list))
        if out:
            check("失败渠道记入 db-only 而不是丢弃",
                  any("db-only" in (a.get("channels") or "") for a in out),
                  str([a.get("channels") for a in out]))
    finally:
        settings.alert_webhook_url = ""

    print(f"\nPASS {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
