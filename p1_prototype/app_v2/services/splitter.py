"""文本切片与中文分词。

切片：递归按"段落 → 换行 → 句号 → 分号 → 逗号"切分，保证语义完整。
分词：jieba 搜索引擎模式，兼顾 BM25 的召回率（未登录词也能切开）。
"""
from __future__ import annotations

import re

import jieba

from app_v2.core.settings import settings

CHUNK_SIZE = settings.chunk_size
CHUNK_OVERLAP = settings.chunk_overlap

SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " "]

_STOPWORDS = {
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "它", "这", "那", "吗", "呢", "吧",
    "请问", "怎么", "什么", "如何", "一个", "以及", "可以", "需要", "进行", "对于", "关于",
    "是什", "么", "哪些", "多少", "是否", "能否", "应该", "我们", "你们",
}


def _split_by(text: str, sep: str) -> list[str]:
    """按分隔符切分并保留分隔符，避免句子粘在一起。"""
    pieces = text.split(sep)
    out = [p + sep for p in pieces[:-1]]
    if pieces[-1]:
        out.append(pieces[-1])
    return out


def _recursive_split(text: str, size: int, sep_index: int = 0) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    if sep_index >= len(SEPARATORS):
        # 兜底：硬切
        return [text[i:i + size] for i in range(0, len(text), size)]

    sep = SEPARATORS[sep_index]
    pieces = _split_by(text, sep)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if len(buf) + len(piece) <= size:
            buf += piece
        else:
            if buf:
                chunks.append(buf)
            if len(piece) > size:
                chunks.extend(_recursive_split(piece, size, sep_index + 1))
                buf = ""
            else:
                buf = piece
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按"章节 + 段落"切片。

    做法：先按空行切段，并对超长段按句子递归切分，保证每个片段只讲一个主题；
    再贪心地把相邻短段合并到 size 以内，避免碎片化。
    这样得到的片段既语义独立，又不会短到丢失上下文。
    """
    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            paragraphs.extend(_recursive_split(para, size, sep_index=2))   # 从"句号"级开始切
        else:
            paragraphs.append(para)

    # 贪心合并：短段（如表格行、列表项）拼到同一片
    merged: list[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) <= size:
            buf = candidate
        else:
            if buf:
                merged.append(buf)
            buf = para
    if buf:
        merged.append(buf)

    # 只有当片段确实被 size 切断时，才补重叠，避免重复内容污染检索
    result: list[str] = []
    prev_tail = ""
    for chunk in merged:
        chunk = chunk.strip()
        if not chunk:
            continue
        result.append((prev_tail + chunk) if prev_tail else chunk)
        prev_tail = chunk[-overlap:] if len(chunk) >= size else ""
    return result


class StreamingSplitter:
    """增量切片器：喂文本块，产出切片。**任何时候都不持有全文。**

    为什么要单独一个类而不是 `split_text("".join(blocks))`：
    后者又把全文拼回来了，等于流式白做。

    实现要点是维护两个跨块状态：
      - `_buf`：还没攒够一个切片的段落（有界，最多 size + 一个段落）
      - `_prev_tail`：上一片用于重叠的尾巴

    **边界正确性**是最容易出错的地方：一个段落可能被块边界切成两半。
    如果每块独立切片，"年假为 5 天，入职满三年" 和 "增至 10 天" 会变成
    两个互不相关的碎片，检索时都答不对。所以 `_buf` 必须跨块保留，
    直到遇到段落分隔（空行）才认为"这个段落完整了"。
    """

    def __init__(self, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> None:
        self.size = max(int(size), 64)
        self.overlap = max(int(overlap), 0)
        self._buf = ""            # 未完成的段落（还没等到空行）
        self._prev_tail = ""      # 上一片的尾巴，用于重叠
        self._partial_line = ""   # 上一块末尾被切断的半行
        self._pending: list[str] = []   # 已完整、待合并的段落
        # 正在合并中的那一批（贪心 buf）。**必须是实例状态，不能是局部变量**：
        # 全量版 `split_text` 的 buf 跨越整个文档，一个 buf 能连续吞下几十个段落；
        # 若每次 drain 都从空 buf 重新开始，上一轮留下的候选片会被当成"第一段"
        # 立刻与新段落比较——它已经接近 size，几乎必然超限，于是被提前推出。
        # 实测后果：270MB 文档流式切出 231765 片，全量只有 231666 片，多出 99 片。
        # 差异不会报错，只会让同一份文档在两种路径下切出不同片段，
        # 检索结果因此漂移——最难定位的一类回归。
        self._merge_buf = ""
        self.count = 0            # 已产出的切片数（避免外部再存一份列表）

    def _drain_ready(self) -> list[str]:
        """把已完整的段落按贪心策略合并成切片。

        与全量版 `split_text` 的贪心合并必须**逐字节等价**，否则同一份文档
        走流式与走全量会切出不同的片段。

        关键点：贪心合并的"最后一片"永远不能立即产出。
        因为下一个段落（可能还在后续文本块里）如果足够短，
        它应该被拼进这一片。全量版能看到全文所以能做出正确判断，
        流式版只能"留一片在手"——合并中的 `_merge_buf` 就是那一片。
        """
        out: list[str] = []
        # 注意这里不再有局部 buf：合并状态存在 self._merge_buf 上，
        # 跨多次 drain 连续累积，才与全量版的单个 buf 语义一致。
        while self._pending:
            para = self._pending.pop(0)
            buf = self._merge_buf
            candidate = f"{buf}\n{para}" if buf else para
            if len(candidate) <= self.size:
                self._merge_buf = candidate
                continue
            if buf:
                out.append(buf)
            self._merge_buf = para
        return out

    def feed(self, text: str) -> list[str]:
        """喂一块文本，返回**本块内新产出**的切片（可能为空）。

        `text` 不要求以换行或段落分隔结尾——跨块的段落由 `_buf` 累积拼接，
        与全量版 `split_text` 的处理方式一致。

        为什么不额外维护"半行退回"：块边界可能落在任何位置，
        若把半行单独存起来再拼回去，会与 `_buf` 的段落累积语义打架，
        导致切点与全量版不同（内容不丢，但片段边界漂移）。
        让 `_buf` 统一承担跨块累积是唯一能保证等价的做法。
        """
        if not text:
            return []

        buf = self._buf + text
        out: list[str] = []

        # 找到最后一个"段落分隔"的位置：它之前的内容都已完整，结尾留在 _buf。
        # 用 rfind 而不是 split，避免为整块文本再复制一份列表。
        last_sep_end = -1
        for m in re.finditer(r"\n\s*\n", buf):
            last_sep_end = m.end()
        if last_sep_end <= 0:
            complete, rest = "", buf
        else:
            complete, rest = buf[:last_sep_end], buf[last_sep_end:]

        if complete:
            for para in re.split(r"\n\s*\n", complete):
                para = para.strip()
                if not para:
                    continue
                if len(para) > self.size:
                    self._pending.extend(_recursive_split(para, self.size, sep_index=2))
                else:
                    self._pending.append(para)
            # **只在整块入队完之后 drain 一次**。
            # 若在每个段落之后都 drain，早期段落会在后续段落还没进队时
            # 就被判定为"无法再合并"而提前产出，与全量版本的贪心结果不同。
            out.extend(self._drain_ready())

        self._buf = rest
        # 兜底：一直等不到空行时（无空行的导出文本、整篇一个自然段），
        # `_buf` 会无限增长，必须强制处理。
        #
        # **难点在于"怎么切"，而不是"要不要切"。** 全量版把整个超长段
        # 一次性交给 `_recursive_split(段, size, sep_index=2)`，由它逐句累加封片。
        # 流式版如果先按窗口硬切一刀再分别处理，两刀之间"跨刀的那一行/那句话"
        # 就会被拆到两个片里，片数与切点全部漂移——
        # 实测曾出现：20000 字无标点长段全量 40 片、流式 49 片；
        # 3000 行密集短行全量 84 片、流式 98 片。内容没丢，但两种路径
        # 切出的片段不同，同一份文档的检索结果因此不可复现。
        #
        # 正确做法：**整段交给 `_recursive_split`**，只把"已确定封片"的
        # 前 N-1 片推出，最后一片退回 `_buf` 继续累积——
        # 因为它后面还可能接上后续块的内容，提前推出就和全量版不一致了。
        if len(self._buf) > self.size * 4:
            pieces = _recursive_split(self._buf, self.size, sep_index=2)
            if len(pieces) > 1:
                # 最后一片可能还能和后续文本拼接，留在缓冲里
                self._buf = pieces[-1]
                for piece in pieces[:-1]:
                    if piece.strip():
                        self._pending.append(piece.strip())
                out.extend(self._drain_ready())
            else:
                # 只有一片说明这一整段仍短于 size 的切分结果（理论上不会到这里，
                # 因为进来时已经 > 4×size），保守起见原样留回缓冲。
                self._buf = "".join(pieces)

        out = self._with_overlap(out)
        self.count += len(out)
        return out

    def flush(self) -> list[str]:
        """收尾：把残留段落全部产出。"""
        out: list[str] = []
        if self._buf.strip():
            tail = self._buf.strip()
            self._buf = ""
            if len(tail) > self.size:
                self._pending.extend(_recursive_split(tail, self.size, sep_index=2))
            else:
                self._pending.append(tail)

        # 收尾时必须**完整地**做一次贪心合并，并且要从 `_merge_buf` 接着做。
        # 之前这里从空 buf 开始，等于把"正在合并的那一批"丢掉重来，
        # 于是 flush 产出的最后一片比全量版短、甚至多出额外一片。
        # flush 之后不会再有内容，所以 `_merge_buf` 里剩下的必须直接产出，不能留。
        out.extend(self._drain_ready())
        tail = self._merge_buf
        self._merge_buf = ""
        if tail and tail.strip():
            out.append(tail)

        out = [c.strip() for c in out if c and c.strip()]
        out = self._with_overlap(out)
        self.count += len(out)
        return out

    def _with_overlap(self, chunks: list[str]) -> list[str]:
        """给切片补重叠。逻辑与 `split_text` 尾部一致，保证两种入口产出一致。"""
        result: list[str] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            result.append((self._prev_tail + chunk) if self._prev_tail else chunk)
            self._prev_tail = chunk[-self.overlap:] if (self.overlap and len(chunk) >= self.size) else ""
        return result


def tokenize(text: str) -> list[str]:
    """中文分词（BM25 用），过滤停用词与空白，英文统一小写。"""
    tokens = jieba.lcut_for_search(text.lower())
    return [t.strip() for t in tokens if t.strip() and t.strip() not in _STOPWORDS and not re.fullmatch(r"\W+", t)]
