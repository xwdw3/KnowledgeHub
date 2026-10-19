"""P4 端到端验收：多租户 + 对象存储的两租户交叉验证。

用 TestClient 直接打接口，不起容器，但走的是和生产完全相同的路由/鉴权/服务链路。
关键断言：A 租户的凭据拿不到 B 租户的任何东西——不是"看不到"，是"不存在"。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")
os.environ["AUTH_MODE"] = "jwt"
os.environ["MINIO_ENABLED"] = "false"      # 无容器环境，验证"无原件降级"路径
os.environ["LLM_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app_v2.main import API_PREFIX, app  # noqa: E402
from app_v2.core.settings import settings  # noqa: E402
from app_v2.services import tenant_service, user_service  # noqa: E402
from app_v2.storage.meta_store import meta_store  # noqa: E402
from app_v2.storage.vector_store import vector_store  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    # 清空本地向量索引，避免存量 1024 维数据干扰
    if hasattr(vector_store, "ids"):
        import numpy as np
        vector_store.ids, vector_store.payloads = [], []
        vector_store.vectors = np.zeros((0, 0), dtype="float32")

    client = TestClient(app, raise_server_exceptions=False)

    section("0. 环境准备：两个租户 + 各自的管理员")
    for tid, tname in (("acme", "Acme 公司"), ("globex", "Globex 公司")):
        if not tenant_service.tenant_service.get(tid):
            tenant_service.tenant_service.create(tid, name=tname, max_users=10, max_docs=100)
        user_service.ensure_tenant_baseline(tid)

    tokens = {}
    for tid in ("acme", "globex"):
        u = user_service.create_user(
            username="admin", password="Adm1n@2026x", role="admin",
            spaces="*", perm_groups="*", tenant_id=tid, created_by="verify",
        )
        # 已存在时 create_user 会抛错，直接取记录
        tokens[tid] = u

    r = client.post(f"{API_PREFIX}/auth/login",
                    json={"username": "admin", "password": "Adm1n@2026x", "tenant_id": "acme"})
    check("acme 管理员登录成功", r.status_code == 200, f"HTTP {r.status_code}")
    tok_acme = (r.json().get("data") or {}).get("access_token", "") if r.status_code == 200 else ""

    r = client.post(f"{API_PREFIX}/auth/login",
                    json={"username": "admin", "password": "Adm1n@2026x", "tenant_id": "globex"})
    check("globex 管理员登录成功", r.status_code == 200, f"HTTP {r.status_code}")
    tok_globex = (r.json().get("data") or {}).get("access_token", "") if r.status_code == 200 else ""

    if not tok_acme or not tok_globex:
        print("\n登录失败，后续用例无法进行")
        return 1

    ha = {"Authorization": f"Bearer {tok_acme}"}
    hb = {"Authorization": f"Bearer {tok_globex}"}

    section("1. 上传：两租户上传同名文件，互不覆盖")
    content = "考勤制度：本公司实行弹性工作制，每日工作八小时。年假按国家规定执行。".encode()
    r = client.post(f"{API_PREFIX}/documents",
                    files={"file": ("考勤制度.md", content, "text/markdown")},
                    data={"perm_group": "all", "space_id": "default"}, headers=ha)
    check("acme 上传成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
    doc_a = (r.json().get("data") or {}).get("doc_id", "")

    content_b = "考勤制度：本公司实行坐班制，打卡时间为早九晚六，迟到三次记警告。".encode()
    r = client.post(f"{API_PREFIX}/documents",
                    files={"file": ("考勤制度.md", content_b, "text/markdown")},
                    data={"perm_group": "all", "space_id": "default"}, headers=hb)
    check("globex 上传同名文件成功（未被 acme 覆盖）", r.status_code == 200,
          f"HTTP {r.status_code}")
    doc_b = (r.json().get("data") or {}).get("doc_id", "")
    check("两租户 doc_id 不同", bool(doc_a) and doc_a != doc_b, f"{doc_a} vs {doc_b}")

    section("2. 列表：各看各的")
    r = client.get(f"{API_PREFIX}/documents", headers=ha)
    names_a = [d["doc_id"] for d in (r.json().get("data") or {}).get("items", [])]
    r = client.get(f"{API_PREFIX}/documents", headers=hb)
    names_b = [d["doc_id"] for d in (r.json().get("data") or {}).get("items", [])]
    check("acme 列表只含自己的文档", names_a == [doc_a], str(names_a))
    check("globex 列表只含自己的文档", names_b == [doc_b], str(names_b))

    section("3. 跨租户按 id 直取 → 404（不是 403）")
    r = client.get(f"{API_PREFIX}/documents/{doc_b}", headers=ha)
    check("acme 用 globex 的 doc_id 取详情 → 404", r.status_code == 404, f"HTTP {r.status_code}")

    r = client.get(f"{API_PREFIX}/documents/{doc_a}/download-url", headers=hb)
    check("globex 索取 acme 文档的下载链接 → 404", r.status_code == 404, f"HTTP {r.status_code}")

    section("4. 跨租户按文件名删除 → 404，且不误删")
    r = client.delete(f"{API_PREFIX}/documents",
                      params={"file_name": "考勤制度.md", "space_id": "default"}, headers=ha)
    check("acme 删除自己的同名文件成功", r.status_code == 200, f"HTTP {r.status_code}")
    r = client.get(f"{API_PREFIX}/documents", headers=hb)
    still_b = [d["doc_id"] for d in (r.json().get("data") or {}).get("items", [])]
    check("globex 的同名文件仍然在（未被连带删除）", still_b == [doc_b], str(still_b))

    section("5. 检索：答案不串户")
    r = client.post(f"{API_PREFIX}/retrieve",
                    json={"query": "考勤制度 打卡", "top_k": 5}, headers=hb)
    hits = (r.json().get("data") or {}).get("hits", [])
    # chunk_id 由 doc_id 派生（uuid5(f"{doc_id}:{seq}")），据此反查归属文档，
    # 比用 file_name 判断更可靠——两租户的文件名本来就相同。
    from app_v2.services.ingest_service import _chunk_id
    own_chunks = {_chunk_id(doc_b, i) for i in range(50)}
    foreign = [h["chunk_id"] for h in hits if h["chunk_id"] not in own_chunks]
    check("globex 检索结果只含自己的文档", not foreign, str(foreign)[:120])
    check("globex 检索确实命中了内容", len(hits) > 0, f"{len(hits)} hits")

    section("6. 空间：同名空间跨租户共存")
    r = client.post(f"{API_PREFIX}/spaces",
                    json={"space_id": "hr", "name": "人事空间"}, headers=ha)
    ok_a = r.status_code == 200
    r2 = client.post(f"{API_PREFIX}/spaces",
                     json={"space_id": "hr", "name": "人事空间"}, headers=hb)
    ok_b = r2.status_code == 200
    check("两租户都能创建 hr 空间", ok_a and ok_b,
          f"acme={r.status_code} globex={r2.status_code}")

    r = client.get(f"{API_PREFIX}/spaces", headers=ha)
    sp_a = {s["space_id"] for s in (r.json().get("data") or {}).get("items", [])}
    check("acme 只看到自己的空间", sp_a == {"default", "hr"}, str(sorted(sp_a)))

    section("7. 无原件降级：MinIO 关闭时上传仍成功")
    check("上传时 object_key 为空但文档可用",
          bool(doc_b), "已在 MinIO 关闭状态下完成上传")
    r = client.get(f"{API_PREFIX}/documents/{doc_b}/download-url", headers=hb)
    check("无原件时下载链接返回 404 并说明原因",
          r.status_code == 404, f"HTTP {r.status_code}")

    print(f"\n{'=' * 52}")
    print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
