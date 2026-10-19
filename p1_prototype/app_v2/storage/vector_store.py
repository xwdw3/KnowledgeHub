"""向量库抽象。

生产使用 Qdrant，无 Docker 环境下自动降级为本地 numpy 索引（同一接口）。
权限过滤在**检索阶段下推**，而不是召回后过滤，避免无权限内容进入上下文。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from app_v2.core.settings import DATA_DIR, settings
from app_v2.core.security import spaces_wildcard

logger = logging.getLogger(__name__)

# 本地后端的落盘节流窗口（批数）。
# 取值理由：太小则 GB 级入库反复重写整个 npz（纯浪费），
# 太大则进程异常退出时丢的批数多。20 批 = 最多丢 320 片索引，
# 而这个后端本身就是"没有数据库时的兜底"，可接受。
_LOCAL_SAVE_EVERY = 20


class VectorStore(ABC):
    backend: str = "base"

    @abstractmethod
    def ensure_ready(self) -> None: ...

    @abstractmethod
    def upsert(self, points: list[dict]) -> int:
        """points: [{id, vector, payload}]"""

    @abstractmethod
    def search(self, vector: list[float], limit: int, perm_groups: list[str],
               spaces: list[str] | None = None,
               tenant_id: str = "") -> list[dict]:
        """返回 [{id, score, payload}]，已按租户/空间/权限组过滤。

        tenant_id 没有"通配"语义：空串表示"只匹配 tenant_id 为空的历史数据"，
        跨租户检索不被支持。这是刻意的——向量库里唯一能阻止越界的
        就是 payload 过滤，一旦允许 tenant 通配，整个隔离就只剩一层应用层自觉。
        """

    @abstractmethod
    def delete_by_doc(self, doc_id: str) -> None: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def health(self) -> dict: ...


class QdrantVectorStore(VectorStore):
    backend = "qdrant"

    def __init__(self) -> None:
        from qdrant_client import QdrantClient

        kwargs = {"url": settings.qdrant_url, "timeout": 10}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        self.client = QdrantClient(**kwargs)
        self.collection = settings.collection_name

    def ensure_ready(self) -> None:
        from qdrant_client import models

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=settings.embed_dim, distance=models.Distance.COSINE),
            )
            logger.info("已创建集合 %s（%d 维）", self.collection, settings.embed_dim)
            # payload 索引能显著提升过滤检索性能
            for field in ("doc_id", "perm", "space_id"):
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
        else:
            # 兼容 P2 存量集合：缺少 space_id 索引会导致空间过滤退化为全表扫描
            from qdrant_client import models

            try:
                for field in ("space_id",):
                    self.client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
            except Exception as exc:
                logger.debug("payload 索引已存在或创建失败：%s", exc)

    def upsert(self, points: list[dict]) -> int:
        from qdrant_client import models

        if not points:
            return 0
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
                for p in points
            ],
            wait=True,
        )
        return len(points)

    def search(self, vector: list[float], limit: int, perm_groups: list[str],
               spaces: list[str] | None = None,
               tenant_id: str = "") -> list[dict]:
        from qdrant_client import models

        must = [models.FieldCondition(key="perm", match=models.MatchAny(any=list(perm_groups)))]
        # 租户条件下推到 Qdrant，且恒定加上（不是可选）：
        # 召回阶段就排除他租户的点，避免 top_k 名额被别人的片段占掉，
        # 也避免"先召回后过滤"导致的结果数不足。
        must.append(models.FieldCondition(
            key="tenant_id", match=models.MatchValue(value=tenant_id or "")))
        # 空间隔离同样下推到 Qdrant：召回阶段就把其他空间的点排除，
        # 而不是召回后再在应用层过滤——后者会让 top_k 名额被无权片段占掉。
        if not spaces_wildcard(spaces):
            must.append(models.FieldCondition(key="space_id", match=models.MatchAny(any=list(spaces))))
        flt = models.Filter(must=must)
        res = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=limit,
            query_filter=flt,
            with_payload=True,
        )
        return [{"id": str(p.id), "score": float(p.score), "payload": p.payload or {}} for p in res.points]

    def delete_by_doc(self, doc_id: str) -> None:
        from qdrant_client import models

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))
            ])),
            wait=True,
        )

    def count(self) -> int:
        try:
            return int(self.client.count(self.collection, exact=True).count)
        except Exception:
            return -1

    def health(self) -> dict:
        try:
            self.client.get_collections()
            return {"backend": self.backend, "status": "ok", "url": settings.qdrant_url, "points": self.count()}
        except Exception as exc:
            return {"backend": self.backend, "status": "error", "error": str(exc)}

    def load(self) -> dict:
        """P3 监控探针：可用性 + 点数 + 探活耗时。

        用 get_collections（轻量元数据接口）而不是 count(exact=True)：
        exact 计数在大集合上会全量扫描，被 Prometheus 每 15 秒打一次
        等于自己给自己制造负载尖峰。点数用近似值即可满足"负载趋势"的观察需求。
        """
        start = time.perf_counter()
        try:
            info = self.client.get_collection(self.collection)
            points = int(getattr(info, "points_count", 0) or 0)
            latency = round((time.perf_counter() - start) * 1000, 2)
            return {"up": 1, "points": points, "latency_ms": latency}
        except Exception:
            return {"up": 0, "points": 0, "latency_ms": -1.0}


class LocalVectorStore(VectorStore):
    """本地降级实现：numpy 矩阵 + json 元数据，供无 Docker 环境验证。"""

    backend = "local"

    def __init__(self, path: str | Path | None = None) -> None:
        # 允许注入路径，默认仍取配置。
        # 注入的价值在于可测：容量管理、落盘格式这类状态机问题
        # 必须能在临时目录里反复构造"空目录 / 重载 / 覆盖"场景，
        # 否则只能靠 GB 级大文件回归去碰运气。
        #
        # 路径**统一带上 .npy 后缀**，不依赖调用方怎么写。
        # 原因：向量数据用 self.path 本身，元数据用 self.path.with_suffix(".json")。
        # 传 "idx"      → 数据写 idx，     元数据写 idx.json
        # 传 "idx.npy"  → 数据写 idx.npy， 元数据写 idx.json
        # 两种写法落在不同文件上，一旦部署配置从一种换成另一种，
        # 重启后就会"读到空索引但磁盘上明明有数据"。
        # 固定后缀让"路径"与"实际文件"一一对应，没有歧义。
        raw = Path(path) if path is not None else Path(
            settings.local_index_path or DATA_DIR / "local_index.npy")
        self.path = raw if raw.suffix == ".npy" else raw.with_suffix(".npy")
        self.lock = threading.RLock()
        self.ids: list[str] = []
        self.payloads: list[dict] = []
        self.vectors = np.zeros((0, settings.embed_dim), dtype=np.float32)
        # 矩阵的**已分配行数**。alloc > len(ids) 时说明尾部是预留空间，
        # 下次追加可以直接原地写，不必复制整张表（见 _ensure_capacity）。
        self._alloc = 0
        # id → 行号。与 ids 同步维护，避免每次 upsert 都线性扫描。
        self._id_pos: dict[str, int] = {}
        # 本次 upsert 待拼接的向量，批末尾一次 vstack。
        self._pending_vecs: list[np.ndarray] = []
        # 距上次落盘的批数，用于节流（见 upsert 里的说明）。
        self._dirty_batches = 0
        self._load()

    def _load(self) -> None:
        meta = self.path.with_suffix(".json")
        if not self.path.exists() or not meta.exists():
            return
        try:
            # 用 mmap 读取，而不是 np.load(path) 直接解出数组。
            # 两个原因：
            #   1) 写入侧为了不让峰值翻倍改成了裸 .npy（见 _save 的说明），
            #      这里必须按对应的格式读，不能再当 .npz 取 data["vectors"]。
            #   2) mmap 让向量数据**不进进程常驻内存**：GB 级语料下矩阵几百 MB，
            #      一启动就全读进来会把 Pod 的内存 requests 顶穿。
            #      下面立刻 copy 出一份可写数组——upsert 要原地写，
            #      mmap 视图是只读的（"assignment destination is read-only"）。
            mapped = np.load(self.path, mmap_mode="r")
            self.vectors = np.array(mapped, dtype=np.float32)
            del mapped
            self._alloc = len(self.vectors)
            payload = json.loads(meta.read_text(encoding="utf-8"))
            self.ids = payload["ids"]
            self.payloads = payload["payloads"]
            self._id_pos = {iid: i for i, iid in enumerate(self.ids)}
            self._check_embed_scheme(payload.get("embed_scheme", ""))
            logger.info("本地向量索引已加载：%d 条", len(self.ids))
        except Exception as exc:
            logger.warning("本地索引加载失败，从空开始：%s", exc)

    def _check_embed_scheme(self, stored: str) -> None:
        """校验索引里存的是哪一版离线向量算法。

        `_hash_embedding` 的哈希函数一旦更换（例如 md5 → crc32），
        旧索引里的向量与新查询算出的向量就落在**完全不同的方向**上。
        此时检索不报错、不返回空，而是稳定地返回一堆不相关的片段——
        用户看到的是"答案突然不准了"，运维在日志里什么都查不到。

        所以把算法标识写进索引元数据，加载时比对；不一致就大声说清楚
        处置方式，而不是假装没事继续用。
        """
        from app_v2.services.embedder import HASH_SCHEME

        if not stored:
            logger.warning(
                "本地索引缺少 embed_scheme 标记（旧版本写入），"
                "若检索结果明显变差，请删除 %s 后重新入库", self.path)
            return
        if stored != HASH_SCHEME:
            logger.error(
                "本地索引的离线向量算法已变更（索引 %s ≠ 当前 %s）："
                "旧向量与新查询不可比，检索会返回不相关结果。"
                "请删除 %s 与同名 .json 后重新入库。",
                stored, HASH_SCHEME, self.path)

    def _save(self) -> None:
        meta = self.path.with_suffix(".json")
        from app_v2.services.embedder import HASH_SCHEME

        # 自己保证目录存在，不依赖调用方先调 ensure_ready()。
        # 缺这一步的表现：容器里挂载卷目录已存在所以看不出问题，
        # 而全新的空 PVC / 全新环境首次入库会在写盘时抛 FileNotFoundError——
        # 报错位置在 _save，看起来像"磁盘故障"，实际只是少了一次 mkdir。
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 逐点流式写，而不是 json.dumps 整个结构。
        #
        # json.dumps 会把「全部 id + 全部正文 + 全部 payload」一次性
        # 拼成一个巨大的字符串：GB 级语料意味着几百 MB 的纯文本，
        # 峰值 = 字符串本身 + encoder 内部缓冲，比数据本身还大 1~2 倍。
        # 本机实测：内存紧张时（可提交 < 1GB）这一步直接 MemoryError，
        # 而且崩在"写 64 字节的元数据"这种看起来不可能失败的位置。
        # 流式写把峰值降到"一个 point 的 JSON"（约 1KB），与语料规模无关。
        with meta.open("w", encoding="utf-8") as fh:
            fh.write('{"ids":[')
            for i, iid in enumerate(self.ids):
                if i:
                    fh.write(",")
                fh.write(json.dumps(iid, ensure_ascii=False))
            fh.write('],"payloads":[')
            for i, p in enumerate(self.payloads):
                if i:
                    fh.write(",")
                fh.write(json.dumps(p, ensure_ascii=False))
            fh.write('],"embed_scheme":')
            fh.write(json.dumps(HASH_SCHEME, ensure_ascii=False))
            fh.write("}")
        # 用 savez 而不是 savez_compressed。
        # 压缩版在 zlib 上花了 44 秒 / 43 次落盘（实测 16MB 文档），
        # 而它换来的只是几十 MB 磁盘空间——对"没有数据库时的兜底实现"来说
        # 这个交易是亏的：磁盘不紧张，加载慢才是真问题。
        # 存入的是 float32 随机向量，压缩率本来就低，压缩意义更小。
        #
        # 但 savez 也不能直接把整个矩阵交给它：numpy 内部会 `arr.tobytes()`
        # 再写文件，即**把矩阵完整复制成一份 bytes**。
        # GB 级语料下矩阵本身几百 MB，这一步等于又申请一份同样大的内存，
        # 峰值直接翻倍；本机在可提交内存不足 600MB 时实测 OOM 在
        # `format.write_array` 的 tobytes 上。
        # 所以自己写 .npy 头 + **分块写数据**：
        # 每块只有几 MB，峰值与矩阵规模无关，和流式解析是同一个思路。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        live = self.vectors[:len(self.ids)]
        with self.path.open("wb") as fh:
            np.lib.format.write_array_header_1_0(
                fh, {"descr": "|f4", "fortran_order": False,
                     "shape": (int(live.shape[0]), int(live.shape[1]))})
            # 每块 1024 行（1024×1024×4B = 4MB），大小固定，与语料规模无关
            step = 1024
            for start in range(0, live.shape[0], step):
                fh.write(live[start:start + step].tobytes(order="C"))

    def ensure_ready(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _ensure_capacity(self, need: int) -> None:
        """确保矩阵能**原地**写下 need 行。

        以前每次追加都写 `np.vstack([self.vectors, new])`，这是复制整张表的操作：
        分配新矩阵的同时旧矩阵还活着，峰值约等于 **3 倍**矩阵大小。
        16MB 文档就能量到 200MB 的峰值放大；GB 级文档的向量矩阵本身
        就是 GB 量级，3 倍放大足以让"已经流式化了的内存"重新变成 OOM。

        改成按 1.5 倍增长预留容量后，扩容只发生在少数几次，
        平时追加是纯原地写入，峰值 = 矩阵本身 + 一个批次的向量。
        预留的尾部行不计入 `len(self.ids)`，落盘时按 ids 长度截断，
        所以"预留"不会污染数据。

        注意 `live` 取的是**矩阵行数**而不是 `len(self.ids)`：
        upsert 是先 append ids（逻辑上的"已存在"）再调这里扩容的，
        两者在扩容那一刻必然不等（ids 多、矩阵少）。
        用 len(ids) 拷数据会拿不到对应的旧行，直接 broadcast 报错。
        """
        live = len(self.vectors)
        if need <= self._alloc:
            return
        # 预留后仍不够时按 1.5 倍翻，至少让 need 有座位
        new_alloc = max(self._alloc * 3 // 2, need, 64)
        grown = np.zeros((new_alloc, self.vectors.shape[1]), dtype=np.float32)
        if live:
            grown[:live] = self.vectors
        self.vectors = grown
        self._alloc = new_alloc

    def upsert(self, points: list[dict]) -> int:
        if not points:
            return 0
        with self.lock:
            for p in points:
                vec = np.asarray(p["vector"], dtype=np.float32)
                # 用 id→下标 的字典查位置。原来写的是 `self.ids.index(...)`，
                # 在列表上是线性扫描：GB 级文档一次入库要写 10 万+ 片，
                # 每次 upsert 都扫一遍全表，整体退化成 O(n²)——
                # 实测 16MB 文档（约 1.7 万片）在本地后端上要跑几分钟。
                pos = self._id_pos.get(p["id"])
                if pos is not None:
                    self.vectors[pos] = vec
                    self.payloads[pos] = p["payload"]
                else:
                    self._id_pos[p["id"]] = len(self.ids)
                    self.ids.append(p["id"])
                    self.payloads.append(p["payload"])
                    self._pending_vecs.append(vec)
            # 向量累积到批次末尾一次性写入，而不是每片都扩一次表。
            if self._pending_vecs:
                need = len(self.ids)
                self._ensure_capacity(need)
                self.vectors[need - len(self._pending_vecs):need] = np.stack(
                    self._pending_vecs)
                self._pending_vecs.clear()
            # 落盘节流：本地后端是"无数据库环境"的兜底实现，
            # 每批都全量重写 npz 在 GB 级文档下是纯浪费（一次入库会重写上千次）。
            # 超过窗口才真正落盘；进程退出时由 flush() 兜底。
            self._dirty_batches += 1
            if self._dirty_batches >= _LOCAL_SAVE_EVERY:
                self._save()
                self._dirty_batches = 0
        return len(points)

    def flush(self) -> None:
        """把未落盘的变更写出去。批量入库结束、或进程退出前调用。"""
        with self.lock:
            if self._dirty_batches:
                self._save()
                self._dirty_batches = 0

    def search(self, vector: list[float], limit: int, perm_groups: list[str],
               spaces: list[str] | None = None,
               tenant_id: str = "") -> list[dict]:
        with self.lock:
            if not len(self.ids):
                return []
            q = np.asarray(vector, dtype=np.float32)
            if q.shape[0] != self.vectors.shape[1]:
                logger.warning("向量维度不一致（索引 %d / 查询 %d），跳过向量检索",
                               self.vectors.shape[1], q.shape[0])
                return []
            # 只乘有效行：矩阵尾部可能有 _ensure_capacity 预留的空行，
            # 一起乘会让 sims 比 payloads 长，下面按 i 索引 payloads 时越界。
            sims = self.vectors[:len(self.ids)] @ q
            wildcard = spaces_wildcard(spaces)
            space_set = set(spaces or [])
            allowed = [
                (i, float(s)) for i, s in enumerate(sims)
                if self.payloads[i].get("tenant_id", "") == (tenant_id or "")
                and self.payloads[i].get("perm") in perm_groups
                and (wildcard or self.payloads[i].get("space_id", "default") in space_set)
            ]
            allowed.sort(key=lambda x: -x[1])
            return [{"id": self.ids[i], "score": s, "payload": self.payloads[i]} for i, s in allowed[:limit]]

    def delete_by_doc(self, doc_id: str) -> None:
        with self.lock:
            keep = [i for i, p in enumerate(self.payloads) if p.get("doc_id") != doc_id]
            self.ids = [self.ids[i] for i in keep]
            self.payloads = [self.payloads[i] for i in keep]
            _dim = self.vectors.shape[1]
            if keep:
                # np.take 比 self.vectors[keep] 少一次中间索引数组的分配
                self.vectors = np.ascontiguousarray(np.take(self.vectors, keep, axis=0))
                self._alloc = len(self.vectors)
            else:
                self.vectors = np.zeros((0, _dim), dtype=np.float32)
                self._alloc = 0
            # 删除会让所有下标平移，映射表必须整体重建；
            # 忘了这一步会让后续 upsert 写到错行的位置——静默的数据错乱。
            self._id_pos = {iid: i for i, iid in enumerate(self.ids)}
            self._pending_vecs.clear()
            # 删除是破坏性操作，不走节流：必须立刻落盘，
            # 否则进程崩在节流窗口内会"删了又回来"。
            self._save()
            self._dirty_batches = 0

    def count(self) -> int:
        return len(self.ids)

    def health(self) -> dict:
        return {"backend": self.backend, "status": "ok", "path": str(self.path), "points": self.count()}

    def load(self) -> dict:
        """本地实现恒可用：内存里的矩阵本身就是"库"。"""
        return {"up": 1, "points": self.count(), "latency_ms": 0.0}


def build_vector_store() -> VectorStore:
    mode = settings.vector_backend.lower()
    if mode == "local":
        store: VectorStore = LocalVectorStore()
    elif mode == "qdrant":
        store = QdrantVectorStore()
    else:
        try:
            candidate = QdrantVectorStore()
            candidate.ensure_ready()
            store = candidate
            logger.info("向量库：Qdrant（%s）", settings.qdrant_url)
        except Exception as exc:
            logger.warning("Qdrant 不可用，降级为本地向量索引：%s", exc)
            store = LocalVectorStore()
    store.ensure_ready()
    return store


vector_store: VectorStore = build_vector_store()
