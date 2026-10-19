"""文档解析：txt / md / pdf / docx → 纯文本。

**流式优先**：本模块提供两套接口。

  1. `parse_file(path)` —— 一次性返回完整字符串。**仅适用于小文件**。
  2. `iter_parse_file(path)` —— 增量产出文本块，内存占用与文件大小**无关**。

为什么必须有第二套：GB 级文档（大 PDF、几十万行的 txt 导出）如果把全文
读进内存再做切片，峰值内存 ≈ 文件大小的数倍（原始字节 + 解码后的 str
（Python str 对中文是 2 字节/字符）+ 切片列表 + 向量矩阵）。一份 1GB 的
txt 轻松吃掉 4~6GB 内存，容器直接被 OOMKill。

OOMKill 的表现极具误导性：任务状态停在 running、日志里没有任何异常、
`dmesg` 里只有一行 `Killed process ... (python)`。运维第一反应往往是
"worker 资源不够，加内存"——加了内存只会让下一次更大的文件再挂一次。

所以正确的解法不是加内存，是让**峰值内存与文件大小解耦**。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

ALLOW_EXT = {".txt", ".md", ".markdown", ".pdf", ".docx"}

# 流式读取的块大小。1MB 是个平衡点：
# 太小 → 系统调用与解码开销占比上升；太大 → 单块内存放大，失去流式意义。
STREAM_BLOCK_SIZE = 1024 * 1024


class ParseError(Exception):
    """文档解析失败。"""


# ======================================================================
# 非流式（保留给小程序、配置片段等小文本）
# ======================================================================
def _read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:          # 单页异常不应导致整篇失败
            pages.append("")
    return "\n".join(pages)


def _read_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:       # 表格内容也是知识，按行拼接
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def clean_text(raw: str) -> str:
    """清洗：统一换行、压缩空白、去除控制字符与重复空行。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_file(file_path: str | Path) -> tuple[str, str]:
    """返回 (文件名, 清洗后的文本)。**大文件请改用 `iter_parse_file`。**"""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext not in ALLOW_EXT:
        raise ParseError(f"不支持的文件类型：{ext}，仅支持 {'/'.join(sorted(ALLOW_EXT))}")

    if ext in {".txt", ".md", ".markdown"}:
        raw = _read_text(path)
    elif ext == ".pdf":
        raw = _read_pdf(path)
    else:
        raw = _read_docx(path)

    text = clean_text(raw)
    if not text:
        raise ParseError("文档解析成功，但未提取到任何文本（可能是扫描件或加密文档）")
    return path.name, text


# ======================================================================
# 流式解析（GB 级文档的唯一正确入口）
# ======================================================================
def _iter_text_blocks(path: Path, block: int = STREAM_BLOCK_SIZE) -> Iterator[str]:
    """按字节块流式读取纯文本文件。

    三个必须处理好的细节：

    1. **编码探测只做一次**。逐块猜编码会在块边界上误判——同一份 GBK 文件，
       前 1MB 可能是合法 UTF-8（纯 ASCII 部分），后 1MB 才出现非 ASCII 字节。
       所以先用首块探明编码，后续沿用。

    2. **块边界不能切断多字节字符**。直接用增量解码器（`codecs.getincrementaldecoder`），
       它会把不完整的多字节序列留到下一块。手工按字节切再用 `errors="ignore"`
       会静默吞掉每个边界上的字符——GB 级文件切上万次，就是上万处内容缺失，
       而且日志里毫无异常。

    3. **按行对齐**。切片是按段落/句子做的，如果一块的结尾正好落在句子中间，
       流式切片器需要自己兜住跨块的长句。这里在块尾保留"未完成的行"，
       让每块都尽量以完整行结束，显著降低切片器的负担。
    """
    import codecs

    # 先读首块探明编码。这一步是必要的额外 IO（1MB），
    # 换来的是整份文件用同一个编码解码，不会在中途出现乱码。
    with open(path, "rb") as fh:
        sample = fh.read(block)
    if not sample:
        return
    encoding = _detect_encoding(sample)

    # decoder 必须跨块复用：它内部持有"未完成的多字节序列"，
    # 每块新建一个会把边界字符当不完整数据丢掉。
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending_line = ""

    with open(path, "rb") as fh:
        while True:
            raw_chunk = fh.read(block)
            if not raw_chunk:
                tail = decoder.decode(b"", final=True)
                if tail:
                    pending_line += tail
                break
            text = decoder.decode(raw_chunk)
            if not text:
                continue
            text = pending_line + text
            cut = text.rfind("\n")
            if cut < 0:
                # 整块都没有换行（超长单行，如某些导出文件）：直接交出，
                # 由切片器兜底硬切，避免 pending_line 无限增长吃内存。
                if len(text) > block * 4:
                    pending_line = ""
                    yield text
                else:
                    pending_line = text
                continue
            pending_line = text[cut + 1:]
            yield text[:cut + 1]
    if pending_line.strip():
        yield pending_line


def _detect_encoding(sample: bytes) -> str:
    """用首个字节块判断编码。

    顺序很重要：先 UTF-8 BOM → UTF-8 → GB18030。
    GB18030 能解码绝大多数字节序列（它几乎是"万能解码器"），
    放在前面会让真正的 UTF-8 文件被误判成 GB18030 而出现乱码。
    """
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        sample.decode("gb18030")
        return "gb18030"
    except UnicodeDecodeError:
        return "utf-8"          # 兜底：用 replace 容错


def _iter_pdf_pages(path: Path) -> Iterator[str]:
    """按页产出 PDF 文本。

    PDF 没法真正"流式"读——文件结构决定了必须随机访问交叉引用表。
    但按页产出已经足够：pypdf 是惰性加载页对象的，不会一次性把所有页
    的文本都提取到内存。真正吃内存的是"把所有页文本 join 成一个大字符串"，
    那一步在流式接口里被彻底去掉了。
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total = len(reader.pages)
    for idx, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:          # 单页异常不应导致整篇失败
            text = ""
        if text:
            # 页之间补空行，让切片器把不同页当作不同段落
            yield text + "\n\n"
        # 每页处理完显式释放页对象引用，让 GC 有机会回收
        del page
        if idx and idx % 100 == 0:
            import gc

            gc.collect()


def _iter_docx_blocks(path: Path) -> Iterator[str]:
    """按段落批量产出 docx 文本。

    python-docx 会把整个 document.xml 解析成树，这一步无法避免。
    但我们可以按 batch 交出文本并及时让已处理的段落被回收，
    避免"树 + 全量文本 + 切片 + 向量"四份数据同时驻留。
    """
    from docx import Document

    doc = Document(str(path))
    batch: list[str] = []
    for para in doc.paragraphs:
        batch.append(para.text)
        if len(batch) >= 500:
            yield "\n".join(batch) + "\n"
            batch = []
    if batch:
        yield "\n".join(batch) + "\n"

    for table in doc.tables:
        rows: list[str] = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
            if len(rows) >= 500:
                yield "\n".join(rows) + "\n\n"
                rows = []
        if rows:
            yield "\n".join(rows) + "\n\n"


def iter_parse_file(path: str | Path) -> Iterator[str]:
    """流式解析：增量产出**已清洗**的文本块。

    调用方应当逐块切片、逐批向量化、逐批写库，任何时候都不持有全文。
    这是 GB 级文档能跑通的前提。

    与 `parse_file` 的语义差异：
      - 不对空文档抛错，而是产出 0 块。是否算失败由调用方根据"总收到 0 块"判断
        （只有调用方知道它一共期待多少）。
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in ALLOW_EXT:
        raise ParseError(f"不支持的文件类型：{ext}，仅支持 {'/'.join(sorted(ALLOW_EXT))}")

    if ext in {".txt", ".md", ".markdown"}:
        source = _iter_text_blocks(p)
    elif ext == ".pdf":
        source = _iter_pdf_pages(p)
    else:
        source = _iter_docx_blocks(p)

    carry = ""          # 上一块尾部可能被切断的段落
    for raw in source:
        # 逐块清洗：clean_text 里没有跨块状态，唯一要注意的是
        # "\n{3,}" 压缩可能把一个跨块的连续空行压不干净，
        # 因此把块尾的换行先留着，交给下一块一起处理。
        text = carry + raw
        cut = len(text)
        # 若块尾是空白，保留到下一块（可能在下一块形成 \n\n\n）
        stripped = text.rstrip("\n")
        cut = len(stripped)
        carry = text[cut:]
        head = text[:cut]
        if not head:
            continue
        cleaned = clean_text(head)
        if cleaned:
            yield cleaned
        # carry 只保留尾部换行，长度有界（不会被撑大）
        if len(carry) > 8:
            carry = carry[-8:]

    if carry.strip():
        cleaned = clean_text(carry)
        if cleaned:
            yield cleaned
