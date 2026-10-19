"""文本切片与中文分词。

切片：递归按"段落 → 换行 → 句号 → 分号 → 逗号"切分，保证语义完整。
分词：jieba 搜索引擎模式，兼顾 BM25 的召回率（未登录词也能切开）。
"""
from __future__ import annotations

import re

import jieba

from app.config import CHUNK_OVERLAP, CHUNK_SIZE

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


def tokenize(text: str) -> list[str]:
    """中文分词（BM25 用），过滤停用词与空白，英文统一小写。"""
    tokens = jieba.lcut_for_search(text.lower())
    return [t.strip() for t in tokens if t.strip() and t.strip() not in _STOPWORDS and not re.fullmatch(r"\W+", t)]
