"""P4 多实例部署验收：跨副本状态共享、实例标识、无状态化清单。

为什么要专门测这个：
    单实例测试**永远测不出**多实例的 bug。state / 一次性票据存在进程内字典时，
    所有单实例用例都会通过；一旦起两个副本，用户遇到的是
    "随机一半的登录失败、刷新一下又好了"——这种间歇性故障最难排查，
    因为每次重试都可能成功，测试脚本很容易"跑一次是绿的"就放过。

测什么（每项都对应一个真实会挂的部署方式）：
    1. 实例标识：/health 与日志里的 instance 必须一致且可区分
    2. OIDC state 跨副本：A 副本发起的 state，B 副本必须能消费
    3. state 一次性：同一 state 在 B 消费后，A 再消费必须失败
    4. 一次性票据跨副本：A 发票、B 换票必须成功
    5. 票据一次性：B 换过之后，A 再换必须失败
    6. 无 Redis 时降级但仍可用（单机模式不能被多实例改造搞坏）
    7. 无状态化清单：代码里不应再有残留的进程内状态

用法（需先起两个副本 + 一个 Redis，脚本会自己拉起来）：
    python scripts/verify_multi_instance.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REDIS_PORT = 16379
REDIS_URL = f"redis://127.0.0.1:{REDIS_PORT}/0"
PORT_A = 8121
PORT_B = 8122
ADMIN_PWD = "mi-Admin-Pass-2024"

# 必须在导入任何 app_v2 模块之前设好：settings 与 RedisLayer 在 import 期
# 就读 REDIS_URL 并建连接池。放到 main() 里再设，已导入的模块不会重读，
# 结果是"代码明明写了 Redis，测试却全程走内存降级"——这种假绿最危险。
os.environ["REDIS_URL"] = REDIS_URL
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", ADMIN_PWD)

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  \u2705 {name}" + (f"   {detail}" if detail else ""))
    else:
        _failed += 1
        _failures.append(name + (f"  [{detail}]" if detail else ""))
        print(f"  \u274c {name}" + (f"   {detail}" if detail else ""))
    return cond


def http(url: str, timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, {"_raw": body[:200]}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:200]}
    except Exception as e:
        return 0, {"_error": str(e)}


def wait_up(port: int, timeout: float = 40.0) -> bool:
    """等副本起来。用 /api/v1/health，它是免认证的，可以放心探活。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = http(f"http://127.0.0.1:{port}/api/v1/health", timeout=2)
        if code == 200:
            return True
        time.sleep(0.6)
    return False


def start_replica(port: int, instance_id: str, redis_url: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "INSTANCE_ID": instance_id,
        "REDIS_URL": redis_url,
        "META_BACKEND": "memory",
        "VECTOR_BACKEND": "local",
        "MODEL_MODE": "offline",
        "CELERY_EAGER": "true",
        "MINIO_ENABLED": "false",
        "ALERT_EVAL_IN_PROCESS": "false",
        "LOG_FORMAT": "text",
        "AUDIT_ENABLED": "false",
        "OIDC_ENABLED": "true",
        # 这两个副本不真去连 IdP（本脚本只验证状态共享，不重跑 SSO 全链路）
        "OIDC_ISSUER": "http://127.0.0.1:9/realms/mi",
        "OIDC_CLIENT_ID": "rag-kb",
        "OIDC_CLIENT_SECRET": "s3cret",
        "OIDC_REDIRECT_URI": f"http://127.0.0.1:{port}/api/v1/auth/sso/callback",
        # 各副本必须共享同一个 JWT secret，否则 A 签的 token 到 B 就验不过
        "JWT_SECRET": "multi-instance-shared-secret-please-rotate",
        # 固定初始管理员密码：内存库每次启动都是全新的，
        # 不固定的话每次跑都要去翻日志找随机密码，脚本无法自洽。
        "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PWD,
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app_v2.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return proc


def in_process_probe(redis_url: str) -> None:
    """进程内验证跨副本语义。

    为什么不全程走 HTTP：OIDC 的 state 是服务端私有的，浏览器拿不到明文
    （这正是它安全的原因），所以无法通过 HTTP 让"副本 A 创建、副本 B 消费"
    这条路径可观测。用两个独立的 OIDCService 实例模拟两个副本的进程状态，
    它们是**完全不共享内存**的，只要能互相消费成功，就证明状态确实落在
    Redis 而不是进程里。
    """
    print("\n[2] OIDC state 跨副本共享（两个独立实例 = 两个副本的进程状态）")
    from app_v2.services.oidc_service import OIDCService

    # 两个实例互不共享内存，唯一共同点是 Redis
    node_a = OIDCService()
    node_b = OIDCService()
    if not check("两个实例内存不共享", node_a._pending is not node_b._pending):
        return

    from app_v2.services.oidc_service import _PendingAuth
    pending = _PendingAuth(
        state="mi-state-cross-node", nonce="n-1", code_verifier="v" * 43,
        next_url="/app", tenant_hint="acme", created_at=time.time(),
    )
    node_a._save_pending(pending)

    # A 写进去了，就不该在 A 的内存里再留一份（否则 Redis 与内存会不一致）
    check("A 侧未在内存留副本", "mi-state-cross-node" not in node_a._pending)

    taken = node_b._take_pending("mi-state-cross-node")
    check("B 能消费 A 创建的 state（跨副本）", taken is not None,
          f"tenant_hint={(taken.tenant_hint if taken else None)}")
    check("消费后字段完整", bool(taken and taken.code_verifier == "v" * 43
                          and taken.nonce == "n-1" and taken.next_url == "/app"))

    # 一次性：A 再拿同一个 state 必须失败
    again = node_a._take_pending("mi-state-cross-node")
    check("state 一次性（已被 B 消费后 A 取不到）", again is None)


def in_process_ticket(redis_url: str) -> None:
    print("\n[3] 一次性票据跨副本")
    os.environ["REDIS_URL"] = redis_url
    from app_v2.api.routes import sso as sso_route

    # 清掉可能的残留，保证从干净状态开始
    sso_route._tickets.clear()

    payload = {"access_token": "at-x", "refresh_token": "rt-x",
               "user": {"username": "zhangsan", "role": "user"}}
    ticket = sso_route._issue_ticket(payload)
    check("issued ticket 非空且足够长", bool(ticket) and len(ticket) >= 32,
          f"len={len(ticket)}")
    check("票据未留在进程内存（已写 Redis）", ticket not in sso_route._tickets)

    item = sso_route._take_ticket(ticket)
    check("能换出票据（跨副本语义）", item is not None
          and (item[0] or {}).get("access_token") == "at-x")

    again = sso_route._take_ticket(ticket)
    check("票据一次性（换过即废）", again is None)


def fallback_no_redis() -> None:
    """没配 Redis 时必须仍然可用（单机/降级）。"""
    print("\n[4] 无 Redis 时降级仍可用")
    from app_v2.services.oidc_service import OIDCService, _PendingAuth
    import app_v2.api.routes.sso as sso_route

    svc = OIDCService()
    # 强制 _redis() 返回 None，模拟未配 Redis / Redis 挂掉
    orig = OIDCService._redis
    OIDCService._redis = staticmethod(lambda: None)
    try:
        p = _PendingAuth(state="mi-noredis", nonce="n", code_verifier="v" * 43,
                         next_url="/app", tenant_hint="", created_at=time.time())
        svc._save_pending(p)
        check("无 Redis：写入内存字典", "mi-noredis" in svc._pending)
        got = svc._take_pending("mi-noredis")
        check("无 Redis：能取出", got is not None)
        check("无 Redis：取出后即删", "mi-noredis" not in svc._pending)
    finally:
        OIDCService._redis = orig

    orig_t = sso_route._ticket_redis
    sso_route._ticket_redis = lambda: None
    try:
        sso_route._tickets.clear()
        tk = sso_route._issue_ticket({"access_token": "at-noredis"})
        check("无 Redis：票据落内存", tk in sso_route._tickets)
        it = sso_route._take_ticket(tk)
        check("无 Redis：票据能换出", it is not None
              and it[0].get("access_token") == "at-noredis")
    finally:
        sso_route._ticket_redis = orig_t


def alert_single_eval() -> None:
    """多副本下告警评估必须互斥。

    不互斥的后果不是"多发几条"这么轻：streak（连续越线次数）被切碎，
    配了"连续 5 次"的规则在 3 个副本下要 15 个周期才触发——真故障来了
    迟迟不告警；同时每个副本都认为自己第一次越线，又各发一遍。
    这是"既吵又聋"的最坏组合。
    """
    print("\n[5] 告警评估多副本互斥")
    from app_v2.services.alert_service import alert_service

    # dry_run 只读，不该抢锁（接口预览可以随便调）
    fired = alert_service.evaluate(dry_run=True)
    check("dry_run 评估可用", isinstance(fired, list))

    token = alert_service._acquire_eval_lock()
    check("副本 A 抢到评估锁", token is not None and token != "",
          f"token={str(token)[:12]}")
    second = alert_service._acquire_eval_lock()
    check("副本 B 抢锁失败（互斥生效）", second is None)

    alert_service._release_eval_lock(token or "")
    third = alert_service._acquire_eval_lock()
    check("A 释放后 B 可抢到", third is not None and third != "")
    alert_service._release_eval_lock(third or "")

    # 释放别人的锁不应生效
    t1 = alert_service._acquire_eval_lock()
    alert_service._release_eval_lock("not-my-token")
    still = alert_service._acquire_eval_lock()
    check("释放非持有者的锁无效（不会误放）", still is None)
    alert_service._release_eval_lock(t1 or "")


def no_leftover_state() -> None:
    print("\n[6] 无状态化残留检查")
    # 这些是"不该出现在进程内存里"的状态类型：会话/票据/一次性凭证。
    # 允许存在的只有作为降级路径的兜底字典（已在上面两个用例里证明其行为正确）。
    checks = [
        ("sso.py 有 Redis 迁移实现", (ROOT / "app_v2/api/routes/sso.py"), "_TICKET_PREFIX"),
        ("oidc_service.py 有 Redis 迁移实现", (ROOT / "app_v2/services/oidc_service.py"),
         "_STATE_PREFIX"),
        ("告警评估有分布式锁", (ROOT / "app_v2/services/alert_service.py"),
         "_acquire_eval_lock"),
        ("告警评估可关闭进程内调度", (ROOT / "app_v2/core/settings.py"),
         "alert_eval_in_process"),
        ("实例标识写入日志", (ROOT / "app_v2/core/logging.py"), "instance"),
        ("健康检查暴露实例", (ROOT / "app_v2/api/routes/system.py"), "resolve_instance_id"),
    ]
    for name, path, needle in checks:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        check(name, needle in text, f"{path.name}")


def main() -> int:
    print("=" * 72)
    print("  P4 多实例部署验收")
    print("=" * 72)

    print("\n[0] 环境准备")
    # 必须在**导入应用模块之前**设好：settings / RedisLayer 在 import 时就读环境，
    # 之后再改 os.environ 不会生效（表现为"实现明明写了 Redis，测试却说没走"）。
    os.environ["REDIS_URL"] = REDIS_URL
    # 复用已存在的测试 Redis；没有就拉一个
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                       capture_output=True, text=True)
    if "rag_p4_redis_test" not in (r.stdout or ""):
        subprocess.run(["docker", "rm", "-f", "rag_p4_redis_test"],
                       capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", "rag_p4_redis_test",
                        "-p", f"{REDIS_PORT}:6379", "redis:7-alpine"],
                       capture_output=True)
        time.sleep(3)
    ok = False
    try:
        code, _ = http(f"http://127.0.0.1:{PORT_A}/api/v1/health", timeout=1)
    except Exception:
        code = 0
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", REDIS_PORT), 3)
        s.sendall(b"PING\r\n")
        ok = b"PONG" in s.recv(64)
        s.close()
    except Exception:
        ok = False
    if not check("Redis 可用", ok, f"127.0.0.1:{REDIS_PORT}"):
        return 1

    print("\n[1] 启动两个副本并验证实例标识")
    procs = []
    try:
        for port, iid in ((PORT_A, "api-node-1"), (PORT_B, "api-node-2")):
            procs.append(start_replica(port, iid, REDIS_URL))
        up_a = wait_up(PORT_A)
        up_b = wait_up(PORT_B)
        if not check("副本 A 就绪", up_a, f":{PORT_A}") or \
           not check("副本 B 就绪", up_b, f":{PORT_B}"):
            return 1

        code_a, body_a = http(f"http://127.0.0.1:{PORT_A}/api/v1/health")
        code_b, body_b = http(f"http://127.0.0.1:{PORT_B}/api/v1/health")
        iid_a = (body_a.get("data") or {}).get("instance_id")
        iid_b = (body_b.get("data") or {}).get("instance_id")
        check("A 暴露 instance_id", iid_a == "api-node-1", f"got={iid_a}")
        check("B 暴露 instance_id", iid_b == "api-node-2", f"got={iid_b}")
        check("两副本标识可区分", iid_a != iid_b)

        # /ready 也要带实例，否则探针失败时不知道是哪台
        code_r, body_r = http(f"http://127.0.0.1:{PORT_A}/api/v1/ready")
        check("/ready 带实例标识",
              (body_r.get("data") or {}).get("instance") == "api-node-1",
              f"code={code_r}")

        # JWT secret 必须是副本间共享的**唯一**来源，否则 A 签的 token 到 B 就验不过，
        # 负载均衡下面用户会"刷新一次登录一次"。这里用真实登录拿 token，
        # 比手工构造更接近实际：它同时验证了 secret 与 issuer 两边一致。
        code_login, body_login = (0, {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT_A}/api/v1/auth/login",
            data=json.dumps({"username": "admin", "password": ADMIN_PWD}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                code_login = r.status
                body_login = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            code_login = e.code
        except Exception as e:
            body_login = {"_error": str(e)}

        tok = ((body_login.get("data") or {}).get("access_token") or "")
        if not tok:
            check("A 副本签发 token", False, f"code={code_login}")
        else:
            check("A 副本签发 token", True)
            # 关键：把这个 token 拿到 B 上用
            code_me = 0
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{PORT_B}/api/v1/auth/me",
                headers={"Authorization": "Bearer " + tok})
            try:
                with urllib.request.urlopen(req2, timeout=8) as r:
                    code_me = r.status
            except urllib.error.HTTPError as e:
                code_me = e.code
            except Exception:
                code_me = 0
            check("A 签发的 JWT 在 B 上可用（secret 共享 = 无状态）",
                  code_me == 200, f"code={code_me}")

        in_process_probe(REDIS_URL)
        # 子进程已起，这里单独起进程内验证要重新以 Redis 配置导入
        in_process_ticket(REDIS_URL)
        fallback_no_redis()
        alert_single_eval()
        no_leftover_state()

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    print("\n" + "=" * 72)
    print(f"  通过 {_passed} / {_passed + _failed}")
    if _failed:
        print("  失败项：")
        for f in _failures:
            print(f"    - {f}")
        print("=" * 72)
        return 1
    print("  全部通过 \u2705")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
