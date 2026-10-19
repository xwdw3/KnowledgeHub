"""P1 原型端到端验收脚本。

用法：
    python scripts/verify.py            # 使用当前索引
    python scripts/verify.py --rebuild  # 清空索引并从 data/docs 重新入库

覆盖：文档入库 → 切片质量 → 双路检索 → 答案生成 → 引用溯源 → 无答案拒答 → 幂等覆盖 → 删除
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, ingest                      # noqa: E402
from app.rag import ask                             # noqa: E402
from app.retriever import relevance, search         # noqa: E402
from app.store import index                         # noqa: E402

IN_KG = [
    ("去深圳出差住宿最多能报多少？", "600"),
    ("请假要提前几天申请？", "工作日"),
    ("差旅报销流程有哪几步？", "审核"),
    ("出差坐飞机有什么标准？", "经济舱"),
]

OUT_KG = [
    "公司的股票期权怎么分配？",
    "年终奖什么时候发？",
    "今天天气怎么样？",
]

PASS, FAIL = "✅", "❌"
results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str, detail: str = "") -> None:
    results.append((ok, msg))
    print(f"{PASS if ok else FAIL} {msg}" + (f"\n     {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="清空索引并重新入库")
    args = parser.parse_args()

    print("=" * 72)
    print(f"P1 原型验收　运行模式：{config.MODE}　知识库：{config.DOCS_DIR}")
    print("=" * 72)

    # ---------- 1. 入库 ----------
    if args.rebuild or not index.chunks:
        for f in config.INDEX_FILE.parent.glob("*"):
            f.unlink()
        t0 = time.time()
        res = ingest.ingest_dir()
        cost = time.time() - t0
        check(len(res["success"]) >= 3, f"入库 {len(res['success'])} 个文档（{cost:.1f}s，失败 {len(res['failed'])}）")
        for f in res["failed"]:
            print(f"     - {f['file_name']}: {f['reason']}")

    stats = index.stats()
    check(stats["chunk_count"] > 0, f"索引规模：{stats['doc_count']} 文档 / {stats['chunk_count']} 片段 / {stats['char_count']} 字")

    # ---------- 2. 切片质量 ----------
    lens = [len(c.text) for c in index.chunks]
    over = [n for n in lens if n > config.CHUNK_SIZE * 1.5]
    check(not over, f"切片长度在合理区间（{min(lens)}~{max(lens)} 字，阈值 {config.CHUNK_SIZE}）")

    # ---------- 3. 双路检索确实生效 ----------
    q = IN_KG[0][0]
    hits = search(q, top_k=3)
    both = [h for h in hits if h.vec_rank and h.bm25_rank]
    check(bool(both), f"向量与 BM25 双路均有召回（双路命中 {len(both)}/{len(hits)} 段）",
          "  ".join(f"#{h.chunk_index}:vec{h.vec_rank}/bm25{h.bm25_rank}" for h in hits))

    # ---------- 4. 库内问题：答得出 + 有引用 ----------
    print("\n--- 库内问题 ---")
    for question, expect in IN_KG:
        t0 = time.time()
        r = ask(question)
        cost = (time.time() - t0) * 1000
        answer = r["answer"]
        ok = (not r["rejected"]) and bool(r["sources"]) and (expect in answer or expect in "".join(s["text"] for s in r["sources"]))
        check(ok, f"{question}（{cost:.0f}ms，相关度 {r['confidence']}）",
              answer.replace("\n", " ")[:110])

    # ---------- 5. 库外问题：必须拒答 ----------
    print("\n--- 库外问题（应拒答）---")
    for question in OUT_KG:
        r = ask(question)
        check(r["rejected"], f"{question}（相关度 {r['confidence']}）",
              r["answer"].replace("\n", " ")[:110])

    # ---------- 6. 阈值边界 ----------
    scores = {q: relevance(search(q), q) for q in [x[0] for x in IN_KG] + OUT_KG}
    in_min = min(scores[q] for q, _ in IN_KG)
    out_max = max(scores[q] for q in OUT_KG)
    check(in_min > out_max, f"相关度阈值可分离（库内最低 {in_min} > 库外最高 {out_max}，阈值 {config.MIN_SCORE}）")

    # ---------- 7. 幂等覆盖 ----------
    before = index.stats()["chunk_count"]
    sample = next(config.DOCS_DIR.glob("*.md"))
    ingest.ingest_path(sample, keep_copy=False)
    after = index.stats()["chunk_count"]
    check(before == after, f"同名文档重复入库不产生重复片段（{before} → {after}）")

    # ---------- 8. 删除一致性 ----------
    sample = next(config.DOCS_DIR.glob("*.md"))
    backup = sample.read_bytes()                 # delete_doc 会一并删除原始文件，先备份
    removed = ingest.delete_doc(sample.name)
    check(removed > 0 and not sample.exists() and sample.name not in [d["file_name"] for d in index.list_docs()],
          f"删除文档同时清理片段、元数据与原始文件（移除 {removed} 段）")
    sample.write_bytes(backup)                   # 还原，便于演示
    ingest.ingest_path(sample, keep_copy=False)

    print("\n" + "=" * 72)
    failed = [m for ok, m in results if not ok]
    print(f"验收结果：{len(results) - len(failed)}/{len(results)} 通过")
    for m in failed:
        print(f"  {FAIL} {m}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
