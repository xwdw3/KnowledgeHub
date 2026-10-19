"""标准 3 取证：相同问题命中缓存后，LLM 调用次数是否真的下降。

取证方法不依赖服务端自述的 cached 字段：
  1. 先读一次 rag_llm_calls_total 的当前值
  2. 同一问题连问 N 次
  3. 再读一次，差值 = 这段时间真实经过 LLM 的次数
  4. 再连问 N 次，差值应为 0

为什么读进程内实时值而不是 Prometheus 查询：
Prometheus 有 15s 抓取间隔，一轮只跑几秒的实验会读到"上一刻"的值（甚至全 0），
把"没抓到"误判成"没调用"。这里直接读 /metrics 端点，拿到的是进程内 counter 真值。
counter 本身仍是旁路观测——即使应用把响应里的 cached 字段写反，counter 也不会骗人。
"""
import json
import os
import urllib.error
import urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
API = "http://127.0.0.1:8012/api/v1"
METRICS = "http://127.0.0.1:8012/metrics"
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "pk-metrics-5c1f8a7d2e9b4c60")
QUESTION = os.getenv("Q", "住宿费标准是多少")


def call(path, tok="", body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=d, headers=h, method="POST" if d else "GET")
    try:
        with OP.open(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def metric(name):
    """从 /metrics 读单个 counter 的实时值。"""
    try:
        r = urllib.request.Request(METRICS, headers={"Authorization": "Bearer " + METRICS_TOKEN})
        with OP.open(r, timeout=15) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith(name + " "):
                    return float(line.split()[1])
    except Exception as exc:
        print("  (指标读取失败:", exc, ")")
    return -1.0


def ask(tok, q, tag):
    s, b = call("/chat", tok, {"question": q, "top_k": 3, "space_id": "default", "mode": "rag"})
    d = b.get("data") or {}
    t = d.get("timings") or {}
    srcs = d.get("sources") or []
    print(f"  {tag:22s} status={s} cached={t.get('cached')} "
          f"rejected={d.get('rejected')} sources={len(srcs)} "
          f"latency={t.get('latency_ms')}ms")
    return s, d


print("=" * 72)
print("标准 3：缓存命中减少 LLM 调用")
print("=" * 72)

s, b = call("/auth/login", body={"username": "admin", "password": os.getenv("ADMIN_PASS", "Adm1n@2026kb")})
tok = (b.get("data") or {}).get("access_token", "")
if not tok:
    print("登录失败：", s, b.get("message"))
    raise SystemExit(1)
print(f"已登录，question = {QUESTION!r}\n")

before = metric("rag_llm_calls_total")
hit_before = metric("rag_chat_cache_hit_total")
print(f"基线  llm_calls={before:.0f}  chat_cache_hit={hit_before:.0f}\n")

print("-- 第 1 轮：连问 3 次（冷/热混合）--")
for i in range(3):
    ask(tok, QUESTION, f"round1 #{i + 1}")
mid = metric("rag_llm_calls_total")
hit_mid = metric("rag_chat_cache_hit_total")
print(f"\n第 1 轮结束  llm_calls={mid:.0f} (+{mid - before:.0f})  "
      f"cache_hit={hit_mid:.0f} (+{hit_mid - hit_before:.0f})\n")

print("-- 第 2 轮：同一问题再连问 5 次（应全部命中缓存）--")
for i in range(5):
    ask(tok, QUESTION, f"round2 #{i + 1}")
after = metric("rag_llm_calls_total")
hit_after = metric("rag_chat_cache_hit_total")
print(f"\n第 2 轮结束  llm_calls={after:.0f} (+{after - mid:.0f})  "
      f"cache_hit={hit_after:.0f} (+{hit_after - hit_mid:.0f})\n")

print("=" * 72)
delta2 = after - mid
print(f"结论：第 2 轮 5 次同名提问，LLM 实际新增调用 {delta2:.0f} 次"
      f" → {'PASS 缓存消除重复 LLM 调用' if delta2 <= 0 else 'FAIL 仍有 LLM 调用'}")
print(f"      缓存命中新增 {hit_after - hit_mid:.0f} 次（期望 5）")
