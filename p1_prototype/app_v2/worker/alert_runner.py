"""告警评估进程入口。

为什么需要这个独立进程：
  告警评估是"全局单点"语义——一条故障只应产生一条告警记录。
  如果让它跟着 api 进程跑，`--scale api=3` 之后就有三份评估器，
  各自采样、各自触发、各自投递，运营商那边会收到三份重复通知，
  告警列表里也会出现三条一模一样的记录。

  alert_silences 表能挡住"重复的外部通知"，但挡不住"重复落库"：
  三个副本几乎同时判定超标，同时查静默表、同时发现"没有静默"、
  同时写入、同时投递。这是一个典型的 check-then-act 竞态，
  在数据库层面无法用简单唯一约束解决（同一规则在窗口内本就可以再次触发）。
  因此正确的做法不是加锁，而是**只让一个进程评估**。

入口行为：
  - 不做 HTTP 服务，不起 uvicorn，只跑评估循环
  - 收到 SIGTERM 干净退出（容器 stop 时不会留下半截日志）
  - 启动时先做一次依赖自检并打日志，方便在 ELK 里确认"评估器活着"
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from app_v2.core.logging import setup_logging
from app_v2.core.settings import settings

logger = logging.getLogger("alert-runner")

_stop = False


def _on_signal(signum, frame):                       # noqa: ARG001
    global _stop
    _stop = True
    logger.info("收到信号 %s，准备退出", signum)


def main() -> int:
    setup_logging(settings.log_level, settings.log_format)
    logger.info("告警评估进程启动 version=%s interval=%ds silence=%ds",
                settings.app_version, settings.alert_eval_interval_sec,
                settings.alert_silence_sec)

    if not settings.alert_enabled:
        logger.warning("ALERT_ENABLED=false，评估器空转退出")
        return 0

    from app_v2.services.alert_service import alert_service
    from app_v2.storage.meta_store import meta_store

    # 依赖自检：业务库是写入告警事件的唯一落点，不可用则评估没有意义
    try:
        meta_store.init()
        logger.info("业务库就绪 backend=%s", meta_store.backend)
    except Exception as exc:
        logger.error("业务库不可用，评估器仍会运行但事件无法落库：%s", exc)

    channels = []
    if settings.alert_webhook_url:
        channels.append("webhook")
    if settings.alert_smtp_host and settings.alert_mail_to:
        channels.append("email")
    logger.info("告警渠道：%s", ", ".join(channels) or "仅落库（未配置任何外部渠道）")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    rules = alert_service.rules()
    logger.info("已装载 %d 条规则：%s", len(rules), [r.rule_id for r in rules])

    # 启动先跑一轮：容器刚起时如果等满一个周期，容器编排的健康检查
    # 会先看到"没有评估过"，容易被误判成没起来。
    rounds = 0
    last_beat = time.time()
    while not _stop:
        try:
            fired = alert_service.evaluate()
            rounds += 1
            if fired:
                logger.info("第 %d 轮触发 %d 条：%s", rounds, len(fired),
                            [f"{f['rule_id']}({f['value']}>{f['threshold']})" for f in fired])
            # 心跳日志按**时间**而非轮数打。
            # 原来写的是 `rounds % 20 == 1`：默认间隔 60 秒 → 20 分钟才一条心跳。
            # 这个组件最危险的失败模式是"进程活着但循环卡死/规则全部静默失效"，
            # 而外部监控判断它是否健康，唯一依据就是心跳日志。
            # 20 分钟的盲区意味着：告警器半夜挂了，早上才可能被发现，
            # 中间这段时间的所有故障都没有人知道——这比没有告警更危险，
            # 因为运维以为"没收到告警就是一切正常"。
            # 现在改为每 5 分钟一条，盲区压到可接受范围，
            # 也不会把日志量撑起来（一天 288 条）。
            if time.time() - last_beat >= 300:
                logger.info("第 %d 轮评估完成，无触发（心跳）", rounds)
                last_beat = time.time()
        except Exception as exc:
            logger.exception("评估异常（继续下一轮）：%s", exc)

        # 分片 sleep，让 SIGTERM 最多等 1 秒就能生效
        deadline = time.time() + settings.alert_eval_interval_sec
        while not _stop and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    logger.info("告警评估进程退出，共评估 %d 轮", rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
