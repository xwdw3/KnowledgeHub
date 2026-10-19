"""诊断：对同一问题，dump 出 RRF 原始序与最终序、各分项分数。

用途：当三档 Rerank 的 Hit@K 完全一样时，需要回答"是 Rerank 没生效，
还是 Rerank 生效了但这批题目本来就分得开"。区别在于：
  - 若 rerank_score 与 rrf_score 的排序完全一致 → 重排没改变任何东西
  - 若排序有变化但命中率不变 → 重排生效，只是题目不够难
只有把这层看清楚，才能诚实地写结论，而不是含糊地说"提升不明显"。
"""
import json
import os
import sys
import urllib.error
import urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
API = os.getenv("BASE", "http://127.0.0.1:8012").rstrip("/") + "/api/v1"
SPACE = os.getenv("EVAL_SPACE", "")


def call(path, tok="", body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=d, headers=h, method="POST" if d else "GET")
    try:
        with OP.open(r, timeout=180) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


s, b = call("/auth/login", body={"username": "admin",
                                 "password": os.getenv("ADMIN_PASS", "")})
tok = b.get("data", {}).get("access_token", "")
if not tok:
    print("登录失败", b.get("message"))
    sys.exit(1)

queries = [
    "一线城市出差住宿一晚最多能报多少钱",
    "我出差住酒店的钱能报多少",
    "干了三年的话假期能有几天",
    "今年工资会涨吗",
]

for q in queries:
    body = {"query": q, "top_k": 10}
    if SPACE:
        body["space_id"] = SPACE
    s, b = call("/retrieve", tok, body)
    d = b.get("data") or {}
    hits = d.get("hits") or []
    print(f"\n{'=' * 78}\n[/retrieve] Q: {q}\n{'=' * 78}")
    print(f"  {'文件':<26}{'vec':>6}{'bm25':>7}{'rrf':>9}{'rerank':>9}  {'vrk':>4}{'brk':>4}")
    for h in hits:
        def f(k, d=0.0):
            v = h.get(k)
            return d if v is None else float(v)
        print(f"  {str(h.get('file_name', ''))[:24]:<26}"
              f"{f('vec_score'):>6.3f}{f('bm25_score'):>7.3f}"
              f"{f('rrf_score'):>9.4f}{f('rerank_score'):>9.4f}"
              f"{int(f('vec_rank')):>6}{int(f('bm25_rank')):>4}")
    # 关键判断：最终返回顺序是否等于 RRF 顺序
    rrf_order = sorted(hits, key=lambda x: x.get("rrf_score") or 0, reverse=True)
    final_order = [h.get("file_name") for h in hits]
    rrf_order_names = [h.get("file_name") for h in rrf_order]
    same = rrf_order_names == final_order
    print(f"  最终序 == RRF 序 ? {same}  →  {'重排未改变排序' if same else '重排改变了排序'}")

    # 对照：/chat 的最终 top_k=3，看它实际给了什么顺序
    body2 = {"question": q, "top_k": 3, "mode": "rag"}
    if SPACE:
        body2["space_id"] = SPACE
    s2, b2 = call("/chat", tok, body2)
    d2 = b2.get("data") or {}
    print(f"  [/chat top3] rejected={d2.get('rejected')} conf={d2.get('confidence')}")
    for i, src in enumerate(d2.get("sources") or [], 1):
        print(f"    {i}. {src.get('file_name')}  rerank={(src.get('rerank_score') or 0):.4f} "
              f"rrf={(src.get('rrf_score') or 0):.4f}")

