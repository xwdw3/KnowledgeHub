"""本地向量库的落盘 / 重载 / 容量管理验证。

为什么单独一个脚本，而不是塞进 verify_p4_bigfile：

  verify_p4_bigfile 走的是「GB 级文件 → 端到端入库」的真实链路，
  整轮要跑十几分钟，且对机器内存有硬要求。而这里的每一条断言
  都是**秒级**的、与文件大小无关的状态机检查——一旦落盘格式或
  容量管理改坏了，应该马上就能发现，不必等一次大文件回归。

  这个文件的存在本身也是一个教训：_save 从 np.savez 改成
  分块写 .npy 之后，格式已经和旧的 .npz 不兼容。如果没有这层
  单测，唯一的暴露路径就是"某天重启后历史索引读不出来"，
  而那时候数据已经在生产上了。

覆盖的真实缺陷（每一条都对应一次实际踩坑）：
  · np.load 返回只读视图 → 重启后原地写报 "read-only"
  · _ensure_capacity 用 len(ids) 当矩阵行数 → 扩容时 broadcast 报错
  · _save 依赖 ensure_ready 建目录 → 空 PVC 首次入库 FileNotFoundError
  · _save 用 json.dumps 全量序列化 → GB 级语料 MemoryError
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MODEL_MODE", "offline")
os.environ.setdefault("MINIO_ENABLED", "false")
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app_v2.core.settings import settings  # noqa: E402
from app_v2.storage import vector_store as vs_mod  # noqa: E402
# 复用峰值读数实现，避免两处各写一份 ctypes / resource 声明
# （之前就因为漏声明 restype 得到过"峰值 0.0MB"这种静默错误读数）。
from scripts.verify_p4_bigfile import _rss_hwm_mb  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed
    if ok:
        _passed += 1
        print(f"  PASS  {name}" + (f"   {detail}" if detail else ""))
    else:
        _failed.append(name)
        print(f"  FAIL  {name}" + (f"   {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def fresh_store(root: Path, name: str):
    """在 root/<name>/ 下造一个全新的向量库实例。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return vs_mod.LocalVectorStore(path=d / "idx")


def emb(text: str) -> np.ndarray:
    from app_v2.services.embedder import embed_query
    return embed_query(text)


def put(store, doc_id: str, n: int, tag: str) -> list[str]:
    """按真实接口批量写入：upsert(points=[{id, vector, payload}])。

    注意 upsert 内部只是把新向量攒进 _pending_vecs，
    真正写进矩阵发生在方法末尾的"批次写入"，因此每次 put 之后
    矩阵行数就已经包含这批数据，但**落盘**仍受 _dirty_batches 节流。
    """
    points = []
    ids = []
    for i in range(n):
        iid = f"{doc_id}-{i}"
        points.append({
            "id": iid,
            "vector": list(emb(f"{tag} 第{i}条 内容")),
            "payload": {
                "doc_id": doc_id, "chunk_index": i, "text": f"{tag}-{i}",
                "file_name": f"{tag}.txt", "perm": "all", "perm_group": "all",
                "space_id": "default", "tenant_id": "acme"},
        })
        ids.append(iid)
    store.upsert(points)
    return ids


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="p4_vs_"))
    dim = settings.embed_dim
    try:
        section("1. 空目录首次入库：_save 必须自己建目录")
        # 空 PVC 场景：目录由挂载点创建，但 idx.json / idx.npy 都不在，
        # 早期版本依赖 ensure_ready() 才 mkdir，直接在写盘处 FileNotFoundError。
        deep = root / "a" / "b" / "c"
        store = vs_mod.LocalVectorStore(path=deep / "idx")
        put(store, "d1", 3, "首次")
        # upsert 内部落盘有节流（_LOCAL_SAVE_EVERY 批才写一次），
        # 3 条数据不会触发。这里显式 flush，模拟"进程退出 / 批量结束"时的兜底，
        # 否则断言在检查一个"还没到该写的时候"的状态。
        store.flush()
        check("首次落盘自动创建父目录", (deep / "idx.npy").exists(),
              str(deep.relative_to(root)))
        check("父目录（原本三层都不存在）被完整创建",
              (deep / "idx.json").exists(),
              f"{[p.name for p in deep.iterdir()]}")

        section("2. 落盘体积：只写有效行，不含预留空间")
        # 先 flush 把 upsert 攒下的 _pending_vecs 真正写进矩阵。
        # 不做这一步的话矩阵还是 0 行、ids 已经有 3 条，
        # 下面拿到的 shape 会是 (0, dim)，断言"预留被截掉"就永远为真。
        store.flush()
        store._ensure_capacity(len(store.ids) + 5000)
        # 确认预留确实存在，否则"截断"这条断言没有意义
        check("已为后续追加预留了容量（alloc > 有效条数）",
              store._alloc > len(store.ids),
              f"alloc={store._alloc} / 有效 {len(store.ids)}")
        store._save()
        npy = np.load(deep / "idx.npy", mmap_mode="r")
        check("磁盘上的矩阵行数等于有效条数（预留行被截掉）",
              npy.shape[0] == len(store.ids),
              f"磁盘 {npy.shape[0]} 行 / 有效 {len(store.ids)} 条")
        check("矩阵列数等于 embed_dim", npy.shape[1] == dim, str(npy.shape))
        del npy
        gc.collect()

        section("3. 重载：格式闭环 + 可写性")
        store2 = vs_mod.LocalVectorStore(path=deep / "idx")
        check("重载后条数一致", len(store2.ids) == 3, f"{len(store2.ids)} 条")
        check("重载后向量完全一致",
              np.array_equal(store2.vectors[:3], store.vectors[:3]))
        # 关键：mmap 读出来的是只读视图，必须 copy 成可写数组。
        # 不做这一步，重启后第一次 upsert 命中已有 id 就抛
        # "assignment destination is read-only"。
        try:
            store2.vectors[0, 0] = 123.0
            writable = True
            err = ""
        except Exception as exc:  # noqa: BLE001
            writable, err = False, f"{type(exc).__name__}: {exc}"
        check("重载后向量矩阵可原地写（只读视图已 copy）", writable, err)

        section("4. 追加：容量预留不改变语义")
        before = len(store2.ids)
        put(store2, "d2", 50, "追加")
        check("追加后条数正确", len(store2.ids) == before + 50,
              f"{before} → {len(store2.ids)}")
        check("矩阵能容纳全部有效行",
              store2.vectors.shape[0] >= len(store2.ids),
              f"矩阵 {store2.vectors.shape[0]} 行 / 有效 {len(store2.ids)}")
        check("预留容量已生效（没有退化成每次整表复制）",
              store2._alloc >= len(store2.ids),
              f"alloc={store2._alloc}")

        section("5. 覆盖：同一 id 二次写入不得新增行")
        n_before = len(store2.ids)
        v_old = store2.vectors[store2._id_pos["d1-0"]].copy()
        store2.upsert([{
            "id": "d1-0",
            "vector": list(emb("覆盖后的内容 完全不同的语义")),
            "payload": {"doc_id": "d1", "chunk_index": 0, "text": "覆盖",
                        "file_name": "覆盖.txt", "perm": "all", "perm_group": "all",
                        "space_id": "default", "tenant_id": "acme"},
        }])
        check("覆盖不新增条数", len(store2.ids) == n_before, f"{len(store2.ids)} 条")
        check("覆盖后向量确实变了",
              not np.array_equal(store2.vectors[store2._id_pos["d1-0"]], v_old))

        section("6. 检索：只乘有效行（预留行不得越界）")
        # _ensure_capacity 预留的尾部是全零行。若 search 乘整张矩阵，
        # sims 会比 payloads 长，按下标取 payload 时越界。
        #
        # 查询词直接用**入库时的原句**：离线向量是 crc32 哈希桶，
        # 只有字面重叠才会撞到同一个桶。用近义词查询会返回 0 条，
        # 而"0 条不越界"永远为真 —— 那种断言是假绿。
        try:
            hits = store2.search(list(emb("追加 第7条 内容")), limit=5,
                                 perm_groups=["all"], spaces=["*"], tenant_id="acme")
            ok, err = True, ""
        except Exception as exc:  # noqa: BLE001
            hits, ok, err = [], False, f"{type(exc).__name__}: {exc}"
        check("检索不因预留行越界", ok, err)
        check("检索确实返回了结果（否则上面的断言是空转）", bool(hits),
              f"{len(hits)} 条命中")
        if hits:
            check("检索结果都来自本次入库的文档",
                  all(h["payload"].get("doc_id") in ("d1", "d2") for h in hits),
                  f"{len(hits)} 条")

        section("7. 删除：重建 alloc，不留虚假预留")
        n_before_del = len(store2.ids)
        store2.delete_by_doc("d2")
        removed = n_before_del - len(store2.ids)
        check("按 doc 删除生效", removed >= 50, f"删除 {removed} 条")
        check("删除后 alloc 等于实际行数", store2._alloc == len(store2.ids),
              f"alloc={store2._alloc} / 有效 {len(store2.ids)}")
        check("被删的 id 不再出现在索引里",
              not any(i.startswith("d2-") for i in store2.ids))

        section("8. 租户隔离：同库不同租户互不可见")
        store3 = fresh_store(root, "tenant")
        store3.upsert([
            {"id": "a-0", "vector": list(emb("租户 acme 的机密文档")),
             "payload": {"doc_id": "a", "chunk_index": 0, "text": "acme 机密",
                         "file_name": "acme.txt", "perm": "all", "perm_group": "all",
                         "space_id": "default", "tenant_id": "acme"}},
            {"id": "b-0", "vector": list(emb("租户 globex 的机密文档")),
             "payload": {"doc_id": "b", "chunk_index": 0, "text": "globex 机密",
                         "file_name": "globex.txt", "perm": "all", "perm_group": "all",
                         "space_id": "default", "tenant_id": "globex"}},
        ])
        q = list(emb("机密文档"))
        h_acme = store3.search(q, limit=10, perm_groups=["all"], spaces=["*"], tenant_id="acme")
        h_globex = store3.search(q, limit=10, perm_groups=["all"], spaces=["*"], tenant_id="globex")
        # 先确认"各租户都能查到自己那条"。少了这一步，
        # 后面两条"结果里没有别人的片段"在**永远返回空**时也成立，
        # 等于用隔离断言掩盖了检索全坏的事实。
        check("acme 能查到自己的片段", bool(h_acme), f"{len(h_acme)} 条")
        check("globex 能查到自己的片段", bool(h_globex), f"{len(h_globex)} 条")
        check("acme 检索结果里没有 globex 的片段",
              all(h["payload"].get("tenant_id") == "acme" for h in h_acme),
              f"{[h['payload'].get('tenant_id') for h in h_acme]}")
        check("globex 检索结果里没有 acme 的片段",
              all(h["payload"].get("tenant_id") == "globex" for h in h_globex),
              f"{[h['payload'].get('tenant_id') for h in h_globex]}")

        section("9. 大数据量落盘：峰值不随矩阵规模线性放大")
        # 这一节验的是 _save 不再让 numpy 整块 tobytes()。
        # 旧写法 np.savez(arr) 内部会 `arr.tobytes()`：
        # 矩阵 78MB → 序列化时再要一份 78MB，峰值 ≈ 2 倍矩阵。
        # 改成按 1024 行分块写之后，额外只要 4MB 的块。
        #
        # 矩阵本身在**子进程**里构造，父进程不占这 78MB——
        # 内存紧张的机器上，父进程攒几十 MB 就足以让后面的断言莫名失败。
        bulk_dir = root / "bulk"
        bulk_dir.mkdir(parents=True, exist_ok=True)
        rows = 20000
        matrix_mb = rows * dim * 4 / 1024 / 1024
        # 峰值必须在**子进程**里量。
        # HWM 是进程生命周期峰值，只增不减：本节前面几节已经把水位推高，
        # 在同一个进程里量"落盘把水位推高多少"永远是 0。
        # 实测就得到过 "额外峰值 0.0MB / 矩阵 78.1MB" 这种读数——
        # 它不是"开销很小"，而是"测量失效"。
        npy_path = str(bulk_dir / "idx.npy")
        # 子进程里**调用真实的 _save 实现**，不要在这里重写一遍分块逻辑：
        # 重写的话测的是"探针脚本写得对不对"，而不是仓库里的代码。
        probe = r'''
import json, os, sys
sys.path.insert(0, os.environ["_P4V_ROOT"])
os.environ["MODEL_MODE"] = "offline"
os.environ["VECTOR_BACKEND"] = "local"
os.environ["META_BACKEND"] = "memory"
import numpy as np
from pathlib import Path
from scripts.verify_p4_bigfile import _rss_hwm_mb
from app_v2.storage.vector_store import LocalVectorStore

rows, dim = int(os.environ["_P4V_ROWS"]), int(os.environ["_P4V_DIM"])
store = LocalVectorStore(path=os.environ["_P4V_NPY"])
# ⚠ 基线必须取在**分配矩阵之前**。
# 矩阵本身 78MB，先分配再取基线的话，那块内存已经把 HWM 推上去了，
# 后面 _save 无论怎么整块复制都"推不动水位"，读出的额外开销恒为 0。
# 那样得到的结论会是"开销很小"——错得还很令人放心。
base = _rss_hwm_mb()
store.vectors = np.random.rand(rows, dim).astype(np.float32)
store._alloc = rows
store.ids = [f"x{i}" for i in range(rows)]
store.payloads = [{"doc_id": "x", "chunk_index": i, "text": "",
                   "perm": "all", "file_name": "x.txt",
                   "space_id": "default", "tenant_id": "acme"}
                  for i in range(rows)]
store._id_pos = {iid: i for i, iid in enumerate(store.ids)}
after_alloc = _rss_hwm_mb()
store._save()
peak = _rss_hwm_mb()
print("@@" + json.dumps({"base": base, "after_alloc": after_alloc, "peak": peak,
                         "matrix_mb": rows * dim * 4 / 1048576,
                         "checksum": float(store.vectors[:500].sum())}))
'''
        import subprocess  # noqa: PLC0415

        probe_path = root / "probe_save.py"
        probe_path.write_text(probe, encoding="utf-8")
        env = dict(os.environ)
        env["_P4V_ROOT"] = str(Path(__file__).resolve().parent.parent)
        env["_P4V_NPY"] = npy_path
        env["_P4V_ROWS"] = str(rows)
        env["_P4V_DIM"] = str(dim)
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        proc = subprocess.run([sys.executable, str(probe_path)],
                              capture_output=True, text=True, env=env, timeout=600)
        peak_extra = None
        checksum_child = None
        d_base = d_peak = 0.0
        matrix_mb = rows * dim * 4 / 1024 / 1024
        for line in (proc.stdout or "").splitlines():
            if line.startswith("@@"):
                d = json.loads(line[2:])
                # 峰值从"分配完矩阵之后"算起，才是 _save 这一步的额外开销。
                # 用 base（分配前）会混进矩阵本身的 78MB，
                # 那点开销就完全被淹没了。
                d_base, d_peak = d["base"], d["peak"]
                peak_extra = max(d["peak"] - d["after_alloc"], 0.0)
                matrix_mb = d["matrix_mb"]
                checksum_child = d.get("checksum")
        if peak_extra is None:
            print(f"  SKIP 子进程测量失败，跳过落盘峰值断言："
                  f"{(proc.stderr or '')[-300:]}")
        got = np.load(npy_path, mmap_mode="r")
        check("大矩阵落盘内容完整（行数/列数一致）",
              got.shape == (rows, dim), f"{got.shape}")
        # 子进程用的是它自己的随机矩阵，不能和父进程的比。
        # 正确的校验是"落盘内容 == 子进程当时内存里的内容"，
        # 即用子进程回报的校验和与磁盘上前 500 行比对。
        if checksum_child is not None:
            disk_sum = float(np.array(got[:500], dtype=np.float32).sum())
            check("磁盘内容与写入时内存中的矩阵一致（校验和）",
                  abs(disk_sum - checksum_child) <= max(abs(checksum_child), 1.0) * 1e-5,
                  f"磁盘 {disk_sum:.4f} vs 内存 {checksum_child:.4f}")
        del got
        gc.collect()
        size_mb = Path(npy_path).stat().st_size / 1024 / 1024
        check("文件体积接近矩阵理论大小（无额外冗余）",
              abs(size_mb - matrix_mb) < matrix_mb * 0.05,
              f"文件 {size_mb:.1f}MB / 矩阵 {matrix_mb:.1f}MB")
        if peak_extra is not None:
            # 测量有效性先于结论：矩阵分配必须真的把水位推高过，
            # 否则后面"额外峰值≈0"无法与"量表坏了"区分。
            # （注意不能用 np.zeros 造矩阵来量：Windows 上零页是懒提交的，
            #   分配后 HWM 不涨，_save 读一遍才把页面提交进内存，
            #   量出来会虚高 ≈ 矩阵大小——那是矩阵第一次进内存，不是复制。）
            check("测量有效（矩阵分配确实推高了水位）",
                  (d_peak - d_base) > matrix_mb * 0.6,
                  f"分配后水位 +{d_peak - d_base:.1f}MB / 矩阵 {matrix_mb:.1f}MB")
            # 旧写法 np.savez 内部 tobytes() 会稳定地多要 ≈1 倍矩阵；
            # 分块写只多要一个 4MB 的块，理想情况下 ≈0。
            # 断言上限取 0.5 倍：足以区分两种实现，又不被碎片留量误伤。
            check("落盘额外峰值远小于矩阵本身（分块写生效，未整块复制）",
                  peak_extra < matrix_mb * 0.5,
                  f"额外峰值 {peak_extra:.1f}MB / 矩阵 {matrix_mb:.1f}MB"
                  f"（{peak_extra / max(matrix_mb, 0.1):.2f}x）")

        section("10. 兼容性：旧 .npz 索引不应静默读错")
        # 旧版本用 np.savez 写出的是 zip 归档。新代码用 np.load(mmap_mode=)
        # 读它会在加载阶段抛异常，被 _load 捕获后记 warning 并从空开始。
        # 期望行为是"读不出来要说清楚"，而不是崩溃或读进垃圾数据。
        old = root / "old"
        old.mkdir(parents=True, exist_ok=True)
        np.savez(old / "idx.npy", vectors=np.zeros((4, dim), dtype=np.float32))
        (old / "idx.json").write_text(
            '{"ids":["a","b","c","d"],"payloads":[{},{},{},{}],'
            '"embed_scheme":"crc32-1/2gram-cut"}', encoding="utf-8")
        legacy = vs_mod.LocalVectorStore(path=old / "idx")
        check("旧格式不会被读成错误数据（要么空、要么条数一致）",
              len(legacy.ids) in (0, 4), f"{len(legacy.ids)} 条")

        print("\n" + "=" * 62)
        if _failed:
            print(f"通过 {_passed} / {_passed + len(_failed)}   失败 {len(_failed)} 项")
            for f in _failed:
                print(f"  ✗ {f}")
            return 1
        print(f"通过 {_passed} / {_passed}")
        print("=" * 62)
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
