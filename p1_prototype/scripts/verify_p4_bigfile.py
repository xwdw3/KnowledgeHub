"""P4 AC-01 验收：GB 级大文档后台异步处理。

这个脚本要回答一个问题，而不是一堆断言：

    **处理 1GB 文件时，进程峰值内存是不是也被撑到 1GB？**

所以判定核心不是"任务成功"（那是最低要求，全量路径也能成功——只是在小文件上），
而是进程 RSS 增量是否**远小于文件体积**。
为了让结论有说服力，用同一份数据分别跑全量路径和流式路径，
把两条曲线的差值直接打出来。

不依赖 MinIO / Redis / Celery：
  - 对象存储用内存替身（只需验证"上传时没把文件读进内存"）
  - 任务用 celery_eager 同步执行
这两处替换都不影响被测的关键代码：`_submit_ingest_task_stream`
（按块落盘）与 `ingest_stream`（流式解析+切分+入库）。
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("META_BACKEND", "memory")
os.environ.setdefault("VECTOR_BACKEND", "local")
os.environ["CELERY_EAGER"] = "true"
# ⚠ 必须强制离线向量，不能只靠"没配 key"来保证。
#
# 项目根目录（p1_prototype 的上一层）有 .env，里面有真实的模型 key，
# settings 会自动加载它。于是这个脚本在"看起来没配 key"的机器上
# 实际每片都在发 HTTPS 请求——被限流后 openai SDK 自行 sleep 重试，
# 实测 3MB 文件跑了 92 秒，其中 51 秒是 time.sleep、33 秒是 SSL read。
# 内存也随之被抬高（HTTP 层缓冲 + 重试期间的中间对象），
# 量出来的"入库峰值"根本不是内存模型的真实表现，而是网络重试的表现。
#
# 显式钉成 offline：这个脚本要的是"解析/切片/写库"三段的内存行为，
# 不是联网 embedding 的吞吐。生产用真实 API 时，向量化是批量流式的，
# 内存特征与此无关（见 README 的说明）。
os.environ["MODEL_MODE"] = "offline"
# 对象存储默认关闭：这段验证的是"内存模型"，不是 MinIO 本身。
# 开着会让每个用例先吃一次连接重试，跑一次要几分钟，还掩盖真正的被测点。
# 上传接口那节用内存替身单独覆盖。
os.environ["MINIO_ENABLED"] = "false"

from app_v2.core.settings import settings  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def make_txt(path: Path, target_mb: int) -> None:
    """生成 target_mb 大小的中文 txt。

    每行带编号与句末标点：切片器靠标点找句子边界，
    全角标点也顺带把"多字节字符跨块"这个坑暴露出来
    （纯 ASCII 数据测不出解码器是否复用了）。

    写文件本身也要**分批**：一次性 `"".join(全部行)` 会有
    O(文件大小) 的临时字符串，造 1GB 测试数据的动作自己就先 OOM 了，
    而这一步本该是"准备数据"，不该成为被测对象的一部分。
    """
    line_tpl = "第{no}条 本章程用于验证超大文档的流式解析与切片，包含中文标点以便断句。\n"
    # 先量一行多长，再算需要多少行——比"循环写入直到够大"少一次全量遍历
    unit = line_tpl.format(no=1).encode("utf-8")
    need = (target_mb * 1024 * 1024) // len(unit) + 1
    # 批大小压到 2000 行（约 1.6 万字符 / 数十 KB），
    # 在只剩几百 MB 可提交内存的机器上也能稳定造完 GB 级文件。
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            buf = []
            for i in range(1, need + 1):
                buf.append(line_tpl.format(no=i))
                if len(buf) >= 2000:
                    fh.write("".join(buf))
                    buf = []
            if buf:
                fh.write("".join(buf))
    except MemoryError:
        # 直接把"是环境内存不够"说出来。否则 traceback 指向 join/write，
        # 看起来像脚本的 bug，实际是机器被别的进程占满了。
        raise SystemExit(
            f"造 {target_mb}MB 测试文件时内存不足（当前可提交 {_avail_mb():.0f}MB）。\n"
            "请释放内存后重跑，或用 BIGFILE_MB=256 降低规模。")


def _win_counters():
    """Windows: 返回 (PMC 结构体实例, 是否成功)。失败返回 (None, False)。

    单独抽出来是因为当前值和峰值走的是同一个结构体的两个字段，
    重复写一遍 ctypes 声明只会让"漏声明 restype"这个坑多出现一次。
    """
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    # **必须声明 restype/argtypes**。默认返回 c_int，64 位下进程句柄
    # 会被截断成垃圾值，GetProcessMemoryInfo 静默返回 0（失败），
    # 结构体保持全 0 —— 表现为"峰值内存 0.0 MB"，
    # 一个看起来像"内存占用极低"的好消息，实际是根本没测到。
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = PMC()
    counters.cb = ctypes.sizeof(PMC)
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                      ctypes.byref(counters), counters.cb):
        return None
    return counters


def _rss_hwm_mb() -> float:
    """进程生命周期内的**峰值** RSS（MB）。

    与 _rss_current_mb 的区别：这个值只增不减，由内核在缺页时更新，
    **不会因为采样线程抢不到 GIL 而漏采**。用它兜住用户态采样的漏采。
    """
    if sys.platform == "win32":
        try:
            c = _win_counters()
            return 0.0 if c is None else c.PeakWorkingSetSize / 1024 / 1024
        except Exception:
            return 0.0
    try:
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024
    except Exception:
        return 0.0


def _rss_current_mb() -> float:
    """当前进程 RSS（MB）——不是峰值。

    差值测量必须用瞬时值：`ru_maxrss` / `PeakWorkingSetSize` 都是
    **单调递增的生命周期峰值**，拿来做"操作前后差值"会得到 0 或负值，
    看起来像"这次操作没占内存"，实际上只是峰值早就在前面某个阶段顶到了。
    """
    try:
        import os as _os

        # Linux：/proc/self/statm 第二列是常驻页数。
        # 用 os.sysconf 取真实页大小，不要硬编码 4096——
        # arm64 上页大小常见 16K/64K，写死会让读数差 4~16 倍。
        try:
            with open("/proc/self/statm") as fh:
                pages = int(fh.read().split()[1])
            return pages * _os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
        except (OSError, ValueError, AttributeError):
            pass
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024
    except ImportError:
        pass
    try:  # Windows
        try:
            counters = _win_counters()
        except Exception:
            return 0.0
        if counters is None:
            return 0.0
        # 取 WorkingSetSize（当前）而不是 PeakWorkingSetSize（峰值）
        return counters.WorkingSetSize / 1024 / 1024
    except Exception:
        return 0.0


_PROC_BASE_MB = None


def _proc_base_mb() -> float:
    """本进程刚启动时的 RSS（MB），只在首次调用时记录。

    用来把 HWM（进程生命周期峰值）换算成"相对空进程的增量"。
    没有它的话，同一进程里跑第二个用例时 HWM 已包含上一个用例的开销，
    读出来的数会被前面用例的残留抬高——而那个数与被测代码无关。
    """
    global _PROC_BASE_MB
    if _PROC_BASE_MB is None:
        _PROC_BASE_MB = _rss_current_mb()
    return _PROC_BASE_MB


def _avail_mb() -> float:
    """系统当前可提交内存（MB）。

    为什么需要它：本脚本要在同一次运行里造 GB 级源文件、逐块解析出数亿字符、
    再起子进程量内存。在一台 16GB 但已用掉 85% 的机器上，
    父进程会在某次看似无关的分配上直接 MemoryError——
    实测崩在 `Path.write_text`（写 1KB 的探针脚本）。
    那种崩溃没有任何指向性，看起来像"脚本有 bug"，
    实际是环境内存不足。宁可先探测并优雅降级，也不要给出误导性的失败。
    """
    if sys.platform != "win32":
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
        except OSError:
            pass
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEM()
        m.dwLength = ctypes.sizeof(MEM)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return 0.0
        # 用"可提交页文件"而不是"可用物理内存"：Windows 会为 malloc
        # 预留提交额度，物理内存还够但提交额度用完时同样会 MemoryError。
        return m.ullAvailPageFile / 1024 / 1024
    except Exception:
        return 0.0


def measure_rss_in_child(mode: str, src_path: str) -> dict | None:
    """在**全新的子进程**里跑一次入库，返回该进程的峰值内存。

    为什么必须开子进程，而不是在同一个进程里 measure_rss：

      HWM（PeakWorkingSetSize / ru_maxrss）是**进程生命周期**的峰值，只增不减。
      本脚本前面几节为了做"全量 vs 流式"对拍，已经在这个进程里跑过
      257MB 的全量路径。此后 HWM 永远停在 257MB 以上，
      再量任何东西的"峰值增量"都是 0——
      实测得到过"峰值 130MB < 常驻 175MB"这种自相矛盾的读数。

      子进程从空状态开始，HWM 就是它的真实峰值。代价是多一次
      Python 启动（约 1 秒）和重新生成测试文件，换来的是**可自证**的数字。

    返回 {"peak_mb", "resident_mb", "chunk_count", "doc_id"}。
    """
    import json
    import subprocess

    script = r'''
import json, os, sys
sys.path.insert(0, os.environ["_P4_ROOT"])
os.environ["MODEL_MODE"] = "offline"
os.environ["MINIO_ENABLED"] = "false"
os.environ["META_BACKEND"] = "memory"
os.environ["VECTOR_BACKEND"] = "local"
import gc
from pathlib import Path
from app_v2.services import ingest_service
from scripts.verify_p4_bigfile import _rss_current_mb, _rss_hwm_mb

src = Path(os.environ["_P4_SRC"])
# 分阶段采样，全部记**绝对值**，基线相减留给调用方按需做。
# 只看 "峰值-基线" 一个数会丢掉关键信息：本进程 import 完服务栈
# 就已经占 174MB（jieba 词典 + numpy + fastapi），
# 而所有增量都必须相对"import 之后"来算才有意义。
after_import = _rss_current_mb()
base_hwm = _rss_hwm_mb()
r = ingest_service.ingest_stream(
    file_name=src.name, source_path=src, perm_group="all",
    uploader="tester", space_id="default", tenant_id="acme")
gc.collect()
cur = _rss_current_mb()
hwm = _rss_hwm_mb()
print("@@" + json.dumps({
    "after_import_mb": after_import,
    "base_hwm_mb": base_hwm,
    "resident_mb": cur,
    "hwm_mb": hwm,
    "chunk_count": r.get("chunk_count"),
    "doc_id": r.get("doc_id"),
    "file_mb": src.stat().st_size / 1048576,
}))
'''
    # 可提交内存不够时直接返回 None，由调用方记一条 SKIP。
    # 子进程峰值约 300MB、父进程当时也可能还留着上百 MB，
    # 留 600MB 余量才起得来；低于这个值强行跑只会得到
    # 一个更难读的 MemoryError。
    #
    # 门槛允许用 BIGFILE_MIN_AVAIL_MB 覆盖：机器负载不是脚本能决定的事，
    # 同一份代码在空载机器上需要 600MB、在已被别的进程占满的机器上
    # 可能需要更多或（若余量恰好够）更少。硬编码会让"跳过"变成常态。
    gate = int(os.environ.get("BIGFILE_MIN_AVAIL_MB", "600"))
    avail = _avail_mb()
    if 0 < avail < gate:
        print(f"  SKIP 可提交内存不足（{avail:.0f}MB < {gate}MB），跳过独立进程测量")
        return None
    script_path = Path(src_path).with_suffix(".probe.py")
    script_path.write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["_P4_ROOT"] = str(Path(__file__).resolve().parent.parent)
    env["_P4_SRC"] = str(src_path)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, text=True, env=env, timeout=1800,
    )
    script_path.unlink(missing_ok=True)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("@@"):
            data = json.loads(line[2:])
            # 三个派生量，语义要分清楚，混用会得出自相矛盾的结论：
            #   peak_mb     本次入库把进程水位推到的位置（相对 import 之后）
            #   resident_mb 入库结束后仍占用的量（本地向量库=向量+正文，无法释放）
            #   transient_mb 纯处理开销 = 峰值 - 结束时常驻，这才是与文件大小解耦的量
            base = data["after_import_mb"]
            data["peak_mb"] = max(data["hwm_mb"] - base, 0)
            data["resident_mb"] = max(data["resident_mb"] - base, 0)
            data["transient_mb"] = max(data["peak_mb"] - data["resident_mb"], 0)
            return data
    raise RuntimeError(
        f"子进程未输出结果（exit={proc.returncode}）："
        f"{(proc.stderr or proc.stdout or '')[-800:]}")


def measure_rss(fn):
    """跑 fn，返回 (结果, 档案峰值字节 - 基线字节)。

    这里同时用两条独立的量，交叉校验：

      A. **系统记录的峰值工作集**（Windows: PeakWorkingSetSize；
         Linux: VmHWM）。它由内核在每次缺页时更新，**不会漏采**，
         是"这个进程最多占过多少内存"的权威答案。
      B. 20ms 一次的用户态采样。它能看到"当前值"曲线，
         但 Python 线程要抢 GIL，纯 Python 循环里几乎抢不到，
         容易漏掉真正的峰值——实测出现过"峰值 129MB < 常驻 173MB"
         这种自相矛盾的读数。

    最终取两者较大值。用 A 兜住漏采，用 B 兜住"进程复用了上一次的峰值
    水位"（A 是进程级累计峰值，同一进程跑多个用例时会被上一个用例抬高）。

    fn 内部如果自己会改变常驻量（比如入库把数据留在内存里），
    调用方应当用"峰值 - 结束时常驻"来算临时开销，而不是减去进入时的基线。
    """
    import gc
    import threading
    import time as _time

    gc.collect()
    base = _rss_current_mb()
    peak = {"v": base}
    stop = threading.Event()

    def _sample() -> None:
        while not stop.is_set():
            cur = _rss_current_mb()
            if cur > peak["v"]:
                peak["v"] = cur
            _time.sleep(0.02)

    hwm_before = _rss_hwm_mb()
    t = threading.Thread(target=_sample, daemon=True)
    t.start()
    try:
        out = fn()
    finally:
        # 必须先停采样、再 gc.collect()。
        # 顺序反了的话，gc 释放的内存发生在采样停止之后，
        # 峰值与常驻的读数会互相矛盾。
        stop.set()
        t.join(timeout=1.0)
        cur = _rss_current_mb()
        if cur > peak["v"]:
            peak["v"] = cur
        gc.collect()

    # 进程级峰值（HWM）在这里**不能直接用增量**：HWM 是整个进程生命周期的
    # 最高水位，同一进程里跑完第 4 节的全量对拍（257MB）之后，
    # HWM 就永远停在 257MB 以上。此后无论再跑什么，增量都是 0。
    # 实测过：入库结束时 cur=167MB、hwm=216MB（进入时就是 216），
    # 增量算出来 0，于是"临时开销 0.0 MB"——一个看起来很漂亮的假数据。
    #
    # 正确处理：只有当本轮把 HWM 推高了（说明本轮确实创造了新峰值），
    # 才认为 HWM 反映了本轮的真实峰值，此时用它的**绝对值相对进程启动基线**
    # 的增量更可信；否则退回采样值（采样至少不会低估到 0）。
    hwm_after = _rss_hwm_mb()
    if hwm_after > hwm_before + 1.0:
        peak_mb = hwm_after - _proc_base_mb()
    else:
        peak_mb = peak["v"] - base
    peak_mb = max(peak_mb, 0)
    return out, peak_mb * 1024 * 1024


def main() -> int:
    from app_v2.services import ingest_service
    from app_v2.services import embedder as embed_service
    from app_v2.services.loader import iter_parse_file
    from app_v2.services.splitter import StreamingSplitter
    from app_v2.storage.vector_store import vector_store

    def reset_vector_store() -> None:
        """把本地向量库恢复到"全新实例"的状态。

        必须按 LocalVectorStore 的**真实状态契约**逐项重置，不能只清
        ids/payloads/vectors 这三个"看起来像全部"的字段。踩过的两个坑：

          1. 漏 _id_pos：旧 id 还在映射表里，upsert 认为"已存在"，
             去写 self.vectors[pos] —— 矩阵刚被清空，直接 IndexError。
          2. 漏 _pending_vecs：上一轮残留的待写入向量会在下一轮
             第一次 upsert 时被当作"本批新增"，写入坐标按 len(ids) 计算，
             与实际行数错位 → broadcast 报 shape (16,1024) into (0,1024)。

        两个错误都只在"同一进程内跑第二段用例"时出现。
        所以把它提成函数，每次入库前显式调用，而不是指望开头清一次就够。
        """
        vector_store.ids = []
        vector_store.payloads = []
        vector_store.vectors = np.zeros((0, settings.embed_dim), dtype=np.float32)
        for attr, empty in (("_id_pos", {}), ("_pending_vecs", []),
                            ("_dirty_batches", 0), ("_alloc", 0)):
            if hasattr(vector_store, attr):
                setattr(vector_store, attr, empty)

    import numpy as np

    reset_vector_store()

    tmpdir = Path(tempfile.mkdtemp(prefix="p4_big_"))
    size_mb = int(os.environ.get("BIGFILE_MB", "1024"))

    section("0. 配置：同步与异步上限必须分开")
    check("max_upload_mb 保持小值（同步路径不放开）",
          settings.max_upload_mb <= 64, f"{settings.max_upload_mb} MB")
    check("max_async_upload_mb 达到 GB 级",
          settings.max_async_upload_mb >= 1024, f"{settings.max_async_upload_mb} MB")
    check("流式阈值小于异步上限（否则流式永不启用）",
          settings.stream_ingest_threshold_mb < settings.max_async_upload_mb,
          f"threshold={settings.stream_ingest_threshold_mb}MB")

    section(f"1. 造一份 {size_mb}MB 文本")
    src = tmpdir / f"big_{size_mb}mb.txt"
    make_txt(src, size_mb)
    real_mb = src.stat().st_size / 1024 / 1024
    check("文件已生成且大小符合预期", real_mb >= size_mb * 0.95,
          f"实际 {real_mb:.1f} MB")

    section("2. 流式解析：峰值内存是否与文件大小解耦")
    counter = {"chars": 0}

    def _consume():
        # 只消费，不累积：一旦把块拼成一个 list 再 join，
        # 被测量的就不再是解析器的内存模型，而是测试代码自己的。
        for block in iter_parse_file(src):
            counter["chars"] += len(block)
        return counter["chars"]

    _, peak_parse = measure_rss(_consume)
    check("流式解析产出了内容", counter["chars"] > 0, f"{counter['chars']:,} 字符")
    # 阈值取 64MB：解析器的真实峰值只取决于块大小(1MB)与解码器缓冲，
    # 与文件是 1GB 还是 10GB 无关。若这里接近文件大小，说明某处退回了全量读取。
    check("解析峰值内存 < 64MB（与文件大小无关）", peak_parse < 64 * 1024 * 1024,
          f"峰值 {mb(peak_parse)} / 文件 {real_mb:.0f}MB")

    section("3. 流式切片：片数与长度受控")
    def _split():
        # 不累积全文，只统计：把 10 万片拼进一个 list 再测量，
        # 量到的是测试代码自己的内存，不是切片器的内存模型。
        sp = StreamingSplitter()
        stat = {"n": 0, "longest": 0, "chars": 0}
        for block in iter_parse_file(src):
            for c in sp.feed(block):
                stat["n"] += 1
                stat["chars"] += len(c)
                stat["longest"] = max(stat["longest"], len(c))
        for c in sp.flush():
            stat["n"] += 1
            stat["chars"] += len(c)
            stat["longest"] = max(stat["longest"], len(c))
        return stat

    stat, peak_split = measure_rss(_split)
    check("切片结果非空", stat["n"] > 0, f"{stat['n']} 片")
    check("切片峰值内存 < 160MB", peak_split < 160 * 1024 * 1024,
          f"峰值 {mb(peak_split)}")
    check("单片长度受控（未出现整篇成一片）",
          0 < stat["longest"] <= settings.chunk_size + settings.chunk_overlap + 64,
          f"最长 {stat['longest']} / 上限 {settings.chunk_size}+{settings.chunk_overlap}")
    # 切片是有损压缩的对照组：总字符数应接近原文（overlap 会让它略多）。
    # 若少了一个量级，说明某处静默丢内容——这正是流式改造最容易出的错。
    check("切片总字符量级与原文相符",
          stat["chars"] >= counter["chars"] * 0.9,
          f"切片 {stat['chars']:,} / 原文 {counter['chars']:,}")

    section("4. 全量 vs 流式：同一文件的内存对比")
    from app_v2.services.splitter import split_text as _full_split

    def _full():
        text = src.read_text(encoding="utf-8")
        out = _full_split(text)
        return {"n": len(out), "longest": max((len(c) for c in out), default=0)}

    full_exc = None
    try:
        full_stat, peak_full = measure_rss(_full)
    except Exception as exc:               # 内存不足等
        full_stat, peak_full = None, 0
        full_exc = exc
        print(f"  （全量路径执行失败，正是预期中的风险：{type(exc).__name__}）")

    if full_stat and peak_full:
        check("流式片数与全量一致（语义等价，不是简化版）",
              stat["n"] == full_stat["n"],
              f"流式 {stat['n']} / 全量 {full_stat['n']}")
        ratio = peak_full / max(peak_split, 1)
        check("流式峰值显著低于全量",
              peak_split < peak_full,
              f"全量 {mb(peak_full)} → 流式 {mb(peak_split)}（降 {ratio:.1f}x）")
    elif full_exc is not None and real_mb >= 512:
        # 全量路径在 GB 级文件上**直接 OOM 失败**，这本身就是结论：
        # 同一份文件、同一台机器，全量连跑都跑不完，流式却正常产出，
        # 「必须流式」不再是一个优化选项，而是可用性前提。
        # 这是比"峰值 284MB vs 14MB"更强的证据——后者还有可能调大内存糊过去。
        check("GB 级文件上全量路径直接失败，流式正常完成（流式是可用性前提）",
              stat["n"] > 0,
              f"全量 {type(full_exc).__name__} / 流式 {stat['n']:,} 片，峰值 {mb(peak_split)}")
    else:
        # 显式记 SKIP 而不是静默跳过：不给 PASS 是避免假绿，
        # 但必须让人看到"这一节没测到"，否则总表 35/35 会让人误以为等价性已验证
        # （4b 会独立验证逐字节等价，但少了"同一文件"这个前提）。
        print("  SKIP 本机内存不足以跑全量路径，跳过同文件内存对比"
              "（等价性由 4b 节独立覆盖）")

    section("4b. 逐字节等价对拍（片数相同还不够）")
    # 片数相同不代表切点相同：两份文档可以切出同样数量的片段、
    # 但边界全部错开。真正要验的是**每一片的内容都一模一样**。
    # 这里用多种文本形态 × 多种块大小交叉验证，
    # 因为流式切片最容易在"块边界 + 无空行"的组合上出问题。
    from app_v2.services.splitter import StreamingSplitter as _SS, split_text as _sp

    def _stream_all(text: str, block: int):
        sp = _SS()
        out: list[str] = []
        for i in range(0, len(text), block):
            out.extend(sp.feed(text[i:i + block]))
        out.extend(sp.flush())
        return out

    import random
    random.seed(7)
    corpus: dict[str, str] = {}
    corpus["密集短行"] = "\n".join(f"第{i}条 短行内容示例" for i in range(3000))
    corpus["超长无标点"] = "甲" * 20000
    corpus["正常段落"] = "\n\n".join(
        "".join(random.choice("文档内容测试句子。") for _ in range(random.randint(80, 900)))
        for _ in range(400))
    mix = []
    for i in range(300):
        if i % 7 == 0:
            mix.append(f"短行{i}")
        elif i % 5 == 0:
            mix.append("")
        else:
            mix.append(f"段{i} " + "内容。" * random.randint(10, 120))
    corpus["混合结构"] = "\n\n".join(mix)

    identical = 0
    total = 0
    for name, text in corpus.items():
        expected = _sp(text)
        for block in (1024, 4096, 65536):
            total += 1
            got = _stream_all(text, block)
            if got == expected:
                identical += 1
            else:
                print(f"    ↳ 不一致：{name} block={block} "
                      f"全量 {len(expected)} 片 / 流式 {len(got)} 片")
    check("所有文本形态 × 块大小组合均逐字节一致",
          identical == total, f"{identical}/{total}")

    # 大文件相关的结论已经全部拿到，回收临时对象：
    # 第 4 节为了对比全量路径，把整篇 2600 万字符读进过内存，
    # 虽然已释放，但堆上的内存块还留在 arena 里。
    # 本机可用内存紧张时（实测可提交只剩 1GB），
    # 不主动回收会让后续某次看似无关的分配直接 MemoryError。
    # 注意不能删 src：第 6 节要拿它当"大文件上传"的样本。
    gc.collect()

    section("5. 端到端：ingest_stream 落库")
    # 端到端单独用小文件，原因要写清楚，否则看起来像"绕开大文件"：
    #
    #   本用例要证明的是"流式入库链路能跑通、且内存受控"，
    #   不是吞吐。而离线兜底向量（无 EMBED_API_KEY 时的哈希向量）
    #   每片都要跑一次 jieba 分词 + crc32，13701 片实测约 100 秒。
    #   跑 270MB 会切出 23 万片，需要半小时——那是**兜底实现的性能问题**，
    #   不是 AC-01 的验收点（生产用真实 embedding API，批量推理，快两个量级）。
    #
    #   内存结论由第 2、3 节在 270MB 上量得（峰值 34MB），
    #   链路正确性由本节在小文件上验证，两者合起来才是完整结论。
    e2e_mb = int(os.environ.get("BIGFILE_E2E_MB", "10"))
    e2e_src = tmpdir / f"e2e_{e2e_mb}mb.txt"
    make_txt(e2e_src, max(e2e_mb, settings.stream_ingest_threshold_mb + 4))
    e2e_real = e2e_src.stat().st_size
    os.environ.setdefault("DEFAULT_TENANT_ID", "acme")
    # 第 4 节跑过全量路径，可能留下未写入的批次状态；入库前必须清干净
    reset_vector_store()
    result, peak_ingest = measure_rss(lambda: ingest_service.ingest_stream(
        file_name=e2e_src.name, source_path=e2e_src,
        perm_group="all", uploader="tester",
        space_id="default", tenant_id="acme",
    ))
    check("入库成功", bool(result.get("doc_id")), str(result.get("doc_id")))
    check("标记为流式路径", bool(result.get("streamed")), str(result.get("streamed")))
    check("片段数 > 0", int(result.get("chunk_count") or 0) > 0,
          str(result.get("chunk_count")))

    # 这里必须把两笔内存分开算，否则断言是误导性的。
    #
    #   A. 入库后的**常驻数据**：本地向量库把向量矩阵和全部正文都放在进程里。
    #      它是 O(语料总量) 的，一个 20MB 文档就有 67MB 矩阵 + 30MB 正文。
    #      这是"本地兜底实现"的固有性质——生产用 Qdrant，数据在服务端，
    #      进程里不持有索引（见 README）。
    #
    #   B. 处理过程中的**临时开销**：峰值 - 常驻。
    #      这才是 AC-01 要证明的东西，也是能被代码改动影响的部分。
    #
    # 混在一起断言"峰值 < 文件一半"会得出错误结论：20MB 文件光矩阵就 67MB，
    # 永远不可能通过；而把阈值放宽到"通过"又会掩盖真正的临时开销膨胀。
    #
    # 内存数字取子进程的（本进程 HWM 已被第 4 节顶高，量不准）；
    # 检索正确性用本进程刚入库的这份数据验证。两者各取所长。
    child5 = measure_rss_in_child("ingest", str(e2e_src))
    if child5 is None:
        # 环境内存不足时**不做任何内存断言**，只记 SKIP。
        # 不用本进程的采样值兜底：本进程的 HWM 已被第 4 节顶高，
        # 读出来的"峰值"可能低于"结束时常驻"，得出"临时开销 0MB"这种
        # 自相矛盾的结论。宁可承认这次没测到，也不要给一个假数字——
        # 误报比漏报更难排查。
        print("  SKIP 本机可提交内存不足，跳过内存量化（见第 2/3/4 节的同规模结论）")
    else:
        print(f"  独立进程量得：文件 {child5['file_mb']:.1f}MB / "
              f"峰值 {child5['peak_mb']:.1f}MB / 常驻 {child5['resident_mb']:.1f}MB / "
              f"临时 {child5['transient_mb']:.1f}MB / {child5['chunk_count']} 片")
        matrix_mb = child5["chunk_count"] * settings.embed_dim * 4 / 1024 / 1024
        check("入库峰值在独立进程中可复现（片段数一致）",
              child5["chunk_count"] == result.get("chunk_count"),
              f"{child5['chunk_count']} vs {result.get('chunk_count')}")
        # 常驻里主要是"向量矩阵 + 全部正文 + 每个点的 dict 开销"。
        # 矩阵是硬成本（n × dim × 4B），正文与 dict 再加一倍左右属正常。
        # 断言的是"没有数量级级别的额外泄漏"，不是"越小越好"——
        # 本地后端的设计目标就是"没有 Qdrant 时能跑"，不是"省内存"。
        check("常驻内存不超过向量矩阵的 4 倍（无数量级级别的泄漏）",
              child5["resident_mb"] < matrix_mb * 4,
              f"常驻 {child5['resident_mb']:.1f} MB / 矩阵 {matrix_mb:.1f} MB "
              f"（{child5['resident_mb'] / matrix_mb:.1f}x）")
        check("处理过程临时开销是常数级（< 256MB，与文件大小无关）",
              child5["transient_mb"] < 256,
              f"临时 {child5['transient_mb']:.1f} MB "
              f"（峰值 {child5['peak_mb']:.1f} - 常驻 {child5['resident_mb']:.1f}）")
    # 说清楚这个数字是什么构成的，否则读者会以为"流式化没起作用"：
    #   临时开销 = 峰值 - 结束时常驻，主要是**每片的固定开销**
    #   （jieba 精确分词 + crc32 建桶，每片约几十 KB 中间对象），
    #   而不是文件本身的一次性读入量。
    #   实测 12.5MB 文件（10274 片）临时 36.5MB，
    #   27MB 文件（22268 片）临时 86MB —— 片数翻倍，开销也大致翻倍，
    #   但**都远小于**同规模文件走全量路径的峰值（第 4 节：67MB 文件全量 286MB）。
    #   换成真实 embedding API 后，每片变成批量请求里的一个元素，
    #   这部分开销会小一个量级。
    # 落盘后能被搜到，才是真的"入库"而不是"跑完了"
    hits = vector_store.search(
        embed_service.embed_query("本章程用于验证超大文档的流式解析与切片"),
        limit=3, perm_groups=["all"], spaces=["*"], tenant_id="acme")
    check("入库后可按内容检索到", bool(hits), f"{len(hits)} 条命中")
    if hits:
        check("命中的是本次入库的文档",
              hits[0]["payload"].get("file_name") == e2e_src.name,
              str(hits[0]["payload"].get("file_name")))

    # 父进程里这份数据已经没用了，立刻释放。
    # 不释放的话，后面每启动一个测量子进程都要和这份常驻数据抢内存——
    # 实测出现过父进程被系统静默杀掉（无 traceback、退出码 1），
    # 日志停在两节之间，完全看不出是内存问题。
    reset_vector_store()
    gc.collect()

    # ---- 5b. 规模对比：这才是"解耦"的硬证据 ----
    # 单个规模的绝对值说明不了问题（常数可能很大但确实是常数）。
    # 真正的判据是：文件放大 2 倍时，临时开销**不跟着放大**。
    # 全量路径在这里会直接翻倍，甚至 MemoryError（见第 4 节）。
    section("5b. 规模对比：文件翻倍时临时开销不翻倍")
    # 注意 e2e_mb 可能被 max() 抬到阈值之上，所以"小文件"的真实尺寸
    # 必须取 e2e_src 的实际大小，不能拿 e2e_mb 推算——
    # 之前就踩过：e2e_mb=6 被抬成 12，5b 又算成 12，两个文件一样大。
    small_real_mb = e2e_real / 1024 / 1024
    big_mb = int(small_real_mb * 2) + 1
    big_src = tmpdir / "scale_big.txt"

    # 两级规模要**连续起两个子进程**，本机内存紧张时父进程会先被系统杀掉
    # （表现是日志停在这一节、无 traceback）。所以先看门槛，
    # 不够就整节跳过，而不是先花两分钟造文件再崩在子进程启动处。
    avail5b = _avail_mb()
    gate5b = int(os.environ.get("BIGFILE_MIN_AVAIL_MB", "900"))
    if 0 < avail5b < gate5b:
        print(f"  SKIP 可提交内存不足（{avail5b:.0f}MB < {gate5b}MB），跳过两档规模对比")
        # 这里**不写死任何实测数字**：上一版把一次运行的读数硬编码进了提示语，
        # 换一台机器跑就会看到与本次完全无关的"结论"，比不打印更糟。
        # 只指向真正支撑结论的章节，数字请从本节日志里取。
        print(f"       解耦结论请见第 2/3/4 节：本次实测 {real_mb:.0f}MB 文件 "
              f"→ 解析峰值 {mb(peak_parse)} / 切片峰值 {mb(peak_split)}")
        t_small = t_big = None
        child_small = None
        result2 = {}
    else:
        make_txt(big_src, big_mb)
        # 换到子进程里量：本进程的 HWM 已被第 4 节的全量对拍顶到 257MB，
        # 再在这里量任何"峰值增量"都会得到 0（详见 measure_rss_in_child 的注释）。
        child_small = measure_rss_in_child("ingest", str(e2e_src))
        child_big = measure_rss_in_child("ingest", str(big_src))
        if child_small is None or child_big is None:
            print("  SKIP 内存不足，无法完成两档规模的独立进程对比")
            print(f"       （第 2/3/4 节已在 {real_mb:.0f}MB 文件上量得 "
                  f"解析 {mb(peak_parse)}、切片 {mb(peak_split)}；")
            print("        解耦结论以那几节的实测为准，此处不再给数字）")
            t_small = t_big = None
        else:
            print(f"  小文件 {child_small['file_mb']:.1f}MB 峰值 {child_small['peak_mb']:.1f}MB "
                  f"常驻 {child_small['resident_mb']:.1f}MB 临时 {child_small['transient_mb']:.1f}MB")
            print(f"  大文件 {child_big['file_mb']:.1f}MB 峰值 {child_big['peak_mb']:.1f}MB "
                  f"常驻 {child_big['resident_mb']:.1f}MB 临时 {child_big['transient_mb']:.1f}MB")
            t_small = child_small["transient_mb"]
            t_big = child_big["transient_mb"]

        # 上一份文档的向量要先清掉，否则常驻基数里混着两次的数据。
        # 用 reset_vector_store 而不是 delete_by_doc：后者只删这一个 doc 的点，
        # 前面小节可能还留有别的残留，会让"常驻基数"算不准。
        reset_vector_store()
        result2, _peak2_unused = measure_rss(lambda: ingest_service.ingest_stream(
            file_name=big_src.name, source_path=big_src,
            perm_group="all", uploader="tester",
            space_id="default", tenant_id="acme",
        ))
        ratio_file = big_src.stat().st_size / e2e_real
        check("大文件入库成功", bool(result2.get("doc_id")),
              f"{result2.get('chunk_count')} 片")
        if t_small is not None and t_big is not None:
            ratio_transient = (t_big / t_small) if t_small > 1 else 0.0
            check("大文件片段数约为小文件的 2 倍（两档规模确实翻倍）",
                  1.7 < (result2.get("chunk_count", 0)
                         / max(child_small["chunk_count"] or 1, 1)) < 2.3,
                  f"{result2.get('chunk_count')} 片（小文件 {child_small['chunk_count']} 片）")
            # 先确认"临时开销"这个量本身测得到。为 0 或极小说明测量失效，
            # 后面那条比值断言就会变成恒真——宁可在这里失败，也不要假绿。
            check("临时开销可测（> 5MB，排除测量失效导致的假绿）",
                  t_small > 5, f"小文件临时开销 {t_small:.1f} MB")
            check("文件放大后临时开销没有同步放大（解耦的直接证据）",
                  t_small > 1 and ratio_transient < ratio_file * 0.75,
                  f"文件 ×{ratio_file:.1f} → 临时开销 ×{ratio_transient:.2f}"
                  f"（{t_small:.1f} MB → {t_big:.1f} MB）")
        # 大文件落盘后同样要能被检索到，否则"入库"只是空转
        hits_big = vector_store.search(
            embed_service.embed_query("本章程用于验证超大文档的流式解析与切片"),
            limit=3, perm_groups=["all"], spaces=["*"], tenant_id="acme")
        check("大文件入库后可按内容检索到", bool(hits_big), f"{len(hits_big)} 条命中")
        if hits_big:
            check("命中的是本次入库的大文档",
                  hits_big[0]["payload"].get("file_name") == big_src.name,
                  str(hits_big[0]["payload"].get("file_name")))

    section("6. 上传接口：按块落盘而非整块读入")
    # 用内存替身接管对象存储，只关心 put_file 收到的是"磁盘上的文件"还是"内存 bytes"
    import app_v2.api.routes.documents as docs_mod
    from app_v2.storage import object_store as os_mod

    captured = {}

    class _FakeStorage:
        def put_file(self, key, path, content_type=""):
            p = Path(path)
            captured["path"] = str(p)
            captured["exists_during_call"] = p.exists()
            captured["size"] = p.stat().st_size if p.exists() else 0
            captured["content_type"] = content_type
            return True

    class _FakeUpload:
        filename = "big_upload.txt"

        def __init__(self, path: Path):
            self._fh = open(path, "rb")
            self.read_calls = []
            self.file = self          # 路由里通过 upload.file.read 读取

        def read(self, n=-1):
            self.read_calls.append(n)
            return self._fh.read(n)

    orig_storage = os_mod.object_storage
    orig_enabled = settings.minio_enabled
    try:
        os_mod.object_storage = _FakeStorage()
        settings.minio_enabled = True

        # 拦下任务提交：只验证"原件如何落盘"，不在这里跑入库。
        # 直接替换模块级单例，比 mock 更贴近真实调用路径
        # （路由里是 `from app_v2.services import task_service as ts` 后调 ts.task_service.submit）。
        import app_v2.services.task_service as real_ts_mod

        orig_ts_service = real_ts_mod.task_service

        class _FakeTaskService:
            @staticmethod
            def submit(*a, **kw):
                captured["payload"] = kw.get("payload") or {}
                return {"task_id": "fake-task-1", "status": "pending"}

        real_ts_mod.task_service = _FakeTaskService()

        from app_v2.services.auth_service import Principal

        principal = Principal(actor="tester", role="admin", key_id="verify-key",
                              perm_groups=("*",), permissions=frozenset({"*"}),
                              granted_perm_groups=("*",), spaces=("*",),
                              tenant_id="acme")
        upload = _FakeUpload(src)

        class _Req:
            class state:
                client_ip = "127.0.0.1"
            headers = {"User-Agent": "p4-bigfile-verify"}

        from app_v2.schemas.models import IngestData

        resp = docs_mod._submit_ingest_task_stream(
            request=_Req(), principal=principal, upload=upload,
            file_name="big_upload.txt", perm_group="all",
            uploader="tester", space_id="default",
        )
        # 异步上传返回 IngestData 说明字段映射也对得上（不只是"没抛异常"）
        check("异步上传返回了 task_id", bool(resp.task_id), str(resp.task_id))
        check("返回体标记 async_mode 与原件 key",
              bool(resp.async_mode) and bool(resp.object_key),
              f"key={resp.object_key}")
        check("原件以磁盘文件形式交给对象存储（不是内存 bytes）",
              bool(captured.get("exists_during_call")),
              f"put_file 收到 {captured.get('path')}")
        check("对象存储收到的文件大小正确",
              captured.get("size") == src.stat().st_size,
              f"{captured.get('size')} vs {src.stat().st_size}")
        check("按块读取（read 参数不是 -1）",
              upload.read_calls and all(n and n > 0 for n in upload.read_calls),
              f"read 次数 {len(upload.read_calls)}，块大小 {upload.read_calls[:1]}")
        check("MIME 类型按扩展名推断",
              captured.get("content_type", "").startswith("text/plain"),
              str(captured.get("content_type")))
        check("任务 payload 携带 object_key 与大小",
              bool(captured.get("payload", {}).get("object_key"))
              and int(captured["payload"].get("size") or 0) == src.stat().st_size,
              f"size={captured.get('payload', {}).get('size')}")
        check("payload 同时写 size 与 file_size（防版本错配）",
              captured.get("payload", {}).get("file_size")
              == captured.get("payload", {}).get("size"))
        check("上传用临时文件已清理",
              not Path(captured.get("path", "/nonexistent")).exists(),
              captured.get("path", ""))
        upload._fh.close()
    finally:
        os_mod.object_storage = orig_storage
        settings.minio_enabled = orig_enabled
        real_ts_mod.task_service = orig_ts_service

    section("7. 超限拒绝：不能先读完再报错")
    orig_max = settings.max_async_upload_mb
    orig_enabled_7 = settings.minio_enabled
    orig_storage_7 = os_mod.object_storage
    try:
        settings.max_async_upload_mb = 1        # 1MB 上限，喂 8MB 文件
        small = tmpdir / "eight_mb.txt"
        make_txt(small, 8)
        upload2 = _FakeUpload(small)
        os_mod.object_storage = _FakeStorage()
        settings.minio_enabled = True
        from app_v2.core.response import AppError
        rejected, code = False, None
        try:
            docs_mod._submit_ingest_task_stream(
                request=_Req(), principal=principal, upload=upload2,
                file_name="eight_mb.txt", perm_group="all",
                uploader="tester", space_id="default",
            )
        except AppError as exc:
            rejected, code = True, getattr(exc, "code", None) or getattr(exc, "status_code", None)
        check("超过异步上限被拒绝", rejected, f"code={code}")
        # 关键：只读到 1MB 附近就停，不应把 8MB 全部读完
        read_total = sum(n for n in upload2.read_calls if n and n > 0)
        check("超限时提前终止读取（未读完整个文件）",
              read_total <= 3 * 1024 * 1024,
              f"已读 {mb(read_total)} / 文件 {mb(small.stat().st_size)}")
        upload2._fh.close()
    finally:
        settings.max_async_upload_mb = orig_max
        # 必须把对象存储的全局状态复原：本节把 minio_enabled 打开、
        # 又把 object_storage 换成内存替身，不还原的话后面每一节都会
        # 以为自己连着 MinIO——真实建连会吃 5 秒超时 + 30 秒冷却，
        # 表现为"备份用例莫名其妙失败"，而且失败原因和备份毫无关系。
        settings.minio_enabled = orig_enabled_7
        os_mod.object_storage = orig_storage_7

    section("8. 对象存储：备份能力必须有租户边界")
    # 这一节不连真 MinIO（本机拉不到镜像），而是直接打 `backup_prefix` 的
    # **准入判断**——它才是安全边界所在。
    #
    # 为什么备份要单列一节：备份功能天生"能读全量数据"，
    # 它是整个系统里最容易变成越权出口的接口。
    # 判定点不是"备份能不能跑通"，而是"能不能只备份自己租户的"。
    from app_v2.storage.object_store import ObjectStorage
    from app_v2.core.response import AppError

    store = ObjectStorage()

    def _expect_error(kwargs, code):
        try:
            store.backup_prefix(**kwargs)
        except AppError as exc:
            return getattr(exc, "code", None) or getattr(exc, "status_code", None)
        except Exception as exc:                      # noqa: BLE001
            return f"unexpected:{type(exc).__name__}"
        return None

    # 空 prefix == 整个桶。如果不拦，一次调用就能把所有企业的原件打包下载
    check("拒绝整桶备份（空前缀）",
          _expect_error({"prefix": "", "dest_dir": str(tmpdir)}, 400) == 400)
    check("拒绝跨租户前缀（acme 身份备份 globex 目录）",
          _expect_error({"prefix": "globex/", "dest_dir": str(tmpdir),
                         "tenant_id": "acme"}, 403) == 403)
    check("前缀与租户一致时放行（不再被准入拦下）",
          _expect_error({"prefix": "acme/", "dest_dir": str(tmpdir),
                         "tenant_id": "acme"}, None) is None,
          "放行后因无 MinIO 拿不到对象，属预期")

    section("9. 任务队列：大文件必须进独立队列")
    # 大文档和小文档混在同一个队列里，会出现"3 个 1GB 任务把队列占满，
    # 后面 500 个小文档集体等半小时"的排队反转。
    # 这里的判定点是**队列名由文件大小决定**，而不是写死。
    from app_v2.services import task_service as ts_mod

    big = settings.stream_ingest_threshold_mb * 1024 * 1024 + 1
    small = settings.stream_ingest_threshold_mb * 1024 * 1024 - 1
    check("大文件进 large 队列",
          ts_mod._pick_queue(ts_mod.TASK_INGEST, {"size": big}) == ts_mod.QUEUE_LARGE,
          ts_mod.QUEUE_LARGE)
    check("小文件进默认队列",
          ts_mod._pick_queue(ts_mod.TASK_INGEST, {"size": small}) == ts_mod.QUEUE_DEFAULT,
          ts_mod.QUEUE_DEFAULT)
    check("缺字段时保守进默认队列（不抛异常）",
          ts_mod._pick_queue(ts_mod.TASK_INGEST, {}) == ts_mod.QUEUE_DEFAULT)
    check("非入库任务不进 large 队列",
          ts_mod._pick_queue("reindex", {"size": big}) == ts_mod.QUEUE_DEFAULT)
    # task_routes 若存在且匹配 `app_v2.worker.tasks.*`，会**覆盖** send_task(queue=...)
    # 显式传入的队列名——分流静默失效，且没有任何报错。
    # 所以这里断言"没有配置任何 task_routes"，而不是断言"配置正确"。
    try:
        from app_v2.worker.tasks import celery_app
    except Exception:                                  # noqa: BLE001
        celery_app = None
    if celery_app is None:
        print("  SKIP  celery 未安装，跳过 task_routes 检查（不影响分流逻辑本身）")
    else:
        check("未配置 task_routes（否则会覆盖显式 queue）",
              not getattr(celery_app.conf, "task_routes", None),
              str(getattr(celery_app.conf, "task_routes", None)))

    section("10. 清理")
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
    check("临时目录已清理", not tmpdir.exists())

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
