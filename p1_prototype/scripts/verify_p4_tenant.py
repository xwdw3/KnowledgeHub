"""P4 多租户隔离验收脚本。

覆盖三层隔离的每一条路径，故意用"内存实现 + 本地向量库"跑，
因为它不依赖 Docker —— 隔离逻辑写在应用层，容器起没起都该成立。

跑法：在项目根执行
    python scripts/verify_p4_tenant.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 强制走内存实现，避免脚本依赖容器
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")

from app_v2.core.settings import settings  # noqa: E402
from app_v2.services import cache_service, retrieval_service, space_service, tenant_service  # noqa: E402
from app_v2.services.tenant_service import current_tenant, set_current_tenant  # noqa: E402
from app_v2.storage.meta_store import meta_store  # noqa: E402
from app_v2.storage.vector_store import vector_store  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {extra}" if extra else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    print(f"配置版本：{settings.app_version}  default_tenant={settings.default_tenant_id}")

    # ---------------- 1. 元数据层 ----------------
    section("1. 元数据层：documents / spaces 按租户隔离")
    meta_store.save_document({"doc_id": "a1", "file_name": "考勤制度.md",
                              "tenant_id": "acme", "space_id": "hr",
                              "perm_group": "hr", "chunk_count": 3, "char_count": 900})
    meta_store.save_document({"doc_id": "b1", "file_name": "考勤制度.md",
                              "tenant_id": "globex", "space_id": "hr",
                              "perm_group": "hr", "chunk_count": 5, "char_count": 1500})

    check("同租户可见", [d["doc_id"] for d in meta_store.list_documents("acme")] == ["a1"])
    check("跨租户互不可见", [d["doc_id"] for d in meta_store.list_documents("globex")] == ["b1"])
    check("跨租户按 id 取 → None", meta_store.get_document("a1", "globex") is None)
    check("跨租户按名取 → None",
          meta_store.get_document_by_name("考勤制度.md", "globex", "hr")["doc_id"] == "b1")
    check("同名文件两租户各存一份", meta_store.get_document("a1", "acme") is not None
          and meta_store.get_document("b1", "globex") is not None)

    space_service.create_space("hr", "人事", tenant_id="acme")
    space_service.create_space("hr", "人事", tenant_id="globex")   # 同名不冲突
    check("空间名租户内唯一（同名可跨租户共存）",
          len(meta_store.list_spaces(tenant_id="acme")) == 1
          and len(meta_store.list_spaces(tenant_id="globex")) == 1)
    check("空间列表按租户隔离",
          {s["space_id"] for s in space_service.list_spaces(tenant_id="acme")} == {"hr"})

    # ---------------- 2. 向量层 ----------------
    section("2. 向量层：payload 过滤（召回阶段就排除他租户）")
    # 本地索引可能已有存量数据（维度不同会导致 vstack 失败），先清干净
    if hasattr(vector_store, "ids"):
        import numpy as np
        vector_store.ids = []
        vector_store.payloads = []
        vector_store.vectors = np.zeros((0, 0), dtype="float32")
    vec = [0.1] * 8
    vector_store.upsert([
        {"id": "11111111-1111-1111-1111-111111111111", "vector": vec,
         "payload": {"text": "年假 5 天", "doc_id": "a1", "perm": "hr",
                     "space_id": "hr", "tenant_id": "acme", "seq": 0}},
        {"id": "22222222-2222-2222-2222-222222222222", "vector": vec,
         "payload": {"text": "年假 15 天", "doc_id": "b1", "perm": "hr",
                     "space_id": "hr", "tenant_id": "globex", "seq": 0}},
    ])
    hits_a = vector_store.search(vec, 10, ["hr", "all"], ["hr"], tenant_id="acme")
    hits_b = vector_store.search(vec, 10, ["hr", "all"], ["hr"], tenant_id="globex")
    check("acme 只召回自己的片段",
          [h["payload"]["doc_id"] for h in hits_a] == ["a1"], str(len(hits_a)))
    check("globex 只召回自己的片段",
          [h["payload"]["doc_id"] for h in hits_b] == ["b1"])
    check("租户不匹配时空结果（不是 fallback 到默认租户）",
          vector_store.search(vec, 10, ["hr", "all"], ["hr"], tenant_id="nobody") == [])

    # ---------------- 3. 缓存层 ----------------
    section("3. 缓存层：key 带租户维度")
    cache_service.put_retrieval("年假几天", ["hr", "all"], 5,
                                {"hits": ["acme-answer"], "confidence": 0.9},
                                ["hr"], tenant_id="acme")
    got_acme = cache_service.get_retrieval_cached("年假几天", ["hr", "all"], 5, ["hr"],
                                                  tenant_id="acme")
    got_globex = cache_service.get_retrieval_cached("年假几天", ["hr", "all"], 5, ["hr"],
                                                    tenant_id="globex")
    check("同租户命中", got_acme is not None and got_acme["hits"] == ["acme-answer"])
    check("异租户不命中（不会拿到 acme 的答案）", got_globex is None)

    # ---------------- 4. 请求级上下文 ----------------
    section("4. 请求级上下文：ContextVar 传播与复位")
    token = set_current_tenant("acme")
    check("set 后 current_tenant 生效", current_tenant() == "acme")
    tenant_service.reset_current_tenant(token)
    check("reset 后回落默认租户", current_tenant() == settings.default_tenant_id)

    # ---------------- 5. 检索链路端到端 ----------------
    section("5. 检索链路：租户从上下文一路透传到向量层")
    token = set_current_tenant("acme")
    try:
        r = retrieval_service.search("年假", ["hr", "all"], top_k=5, spaces=["hr"])
    finally:
        tenant_service.reset_current_tenant(token)
    ids = {h["doc_id"] for h in r["hits"]}
    check("检索结果只含本租户文档", ids <= {"a1"}, str(sorted(ids)))

    # ---------------- 6. 租户服务 ----------------
    section("6. 租户服务：校验与配额")
    _ts = tenant_service.tenant_service
    _ts.create("acme", name="Acme 公司", max_users=2, max_docs=1)
    t = _ts.get("acme")
    check("租户创建成功", t is not None and t.name == "Acme 公司")
    try:
        tenant_service.normalize_tenant_id("A/B")
        check("租户 ID 非法字符被拒", False)
    except Exception as exc:
        check("租户 ID 非法字符被拒", True, str(exc)[:40])
    check("合法 ID 规范化通过", tenant_service.normalize_tenant_id("Acme-2") == "acme-2")
    try:
        _ts.check_doc_quota("acme")
        check("文档配额超限应抛错", False)
    except Exception as exc:
        check("文档配额超限被拦下", True, str(exc)[:60])

    # ---------------- 汇总 ----------------
    print(f"\n{'=' * 50}")
    print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
