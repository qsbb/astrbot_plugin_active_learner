"""chunker 分块器单元测试。

覆盖：滑动窗口边界、空/超长输入、overlap 逻辑、Markdown section 拆分，
以及 PDF/DOCX 的离线解析（用内存构造的文件字节，不读磁盘、不联网）。
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_active_learner.chunker import (
    chunk_docx,
    chunk_markdown,
    chunk_pdf,
    chunk_text,
)


# ---------- chunk_text ----------


def test_empty_and_blank_text_returns_empty_list():
    # 空输入必须返回 []，而不是 [""]：下游会把每个 chunk 当成一条记忆入库，
    # 空 chunk 会污染知识库并浪费一次 embedding 调用。
    assert chunk_text("") == [], "空字符串不应产生任何 chunk"
    assert chunk_text("   \n\t ") == [], "纯空白同样视为空输入"


def test_short_text_returns_single_stripped_chunk():
    # 未超过 max_size 时走短路分支，只做 strip，不进滑动窗口。
    assert chunk_text("hello", max_size=10) == ["hello"]
    assert chunk_text("   padded   ", max_size=100) == ["padded"], (
        "短文本分支应已 strip 首尾空白"
    )


def test_sliding_window_overlap_repeats_tail_of_previous_chunk():
    text = "".join(str(i % 10) for i in range(25))  # 25 个字符
    chunks = chunk_text(text, max_size=10, overlap=3)
    # step = max_size - overlap = 7，故起点为 0/7/14/21
    assert chunks == ["0123456789", "7890123456", "4567890123", "1234"]
    # 相邻 chunk 必须共享 overlap 个字符，否则跨边界的句子会被切断丢失语义
    for prev, curr in zip(chunks, chunks[1:]):
        assert prev[-3:] == curr[:3], "相邻 chunk 应重叠 overlap=3 个字符"


def test_full_text_is_covered_without_gap():
    text = "".join(str(i % 10) for i in range(25))
    chunks = chunk_text(text, max_size=10, overlap=2)
    # 拼接去重后必须能还原原文，确认滑动窗口没有漏掉中间片段
    rebuilt = chunks[0]
    for c in chunks[1:]:
        rebuilt += c[2:]
    assert rebuilt == text, "去掉重叠部分后应能无缝还原原文，说明无内容丢失"


def test_overlap_not_smaller_than_max_size_falls_back_to_no_overlap():
    text = "".join(str(i % 10) for i in range(25))
    # step <= 0 会死循环，实现里回退成 step = max_size（即退化为无重叠切分）
    assert chunk_text(text, max_size=10, overlap=10) == [
        "0123456789",
        "0123456789",
        "01234",
    ], "overlap == max_size 时应退化为 step=max_size，避免 while 死循环"
    assert chunk_text(text, max_size=10, overlap=20) == chunk_text(
        text, max_size=10, overlap=10
    ), "overlap > max_size 与 overlap == max_size 应走同一条兜底分支"


def test_negative_overlap_skips_content():
    """负 overlap 会让 step > max_size，实现未做校验，中间内容被跳过。

    这是被测代码的现状（疑似缺陷），此处固化行为以便重构时被察觉。
    """
    text = "".join(str(i % 10) for i in range(25))
    chunks = chunk_text(text, max_size=10, overlap=-5)
    assert chunks == ["0123456789", "5678901234"], (
        "负 overlap 时 step=15 > max_size=10，第 10-14 字符被跳过"
    )


def test_whitespace_only_window_is_dropped():
    # 中间恰好切出全空白的窗口时该 chunk 被丢弃，不会产出空串
    text = "ab" + " " * 20 + "cd"
    chunks = chunk_text(text, max_size=5, overlap=0)
    assert all(c.strip() for c in chunks), "全空白窗口应被丢弃"
    assert chunks[0] == "ab"


# ---------- chunk_markdown ----------


def test_markdown_empty_returns_empty_list():
    assert chunk_markdown("") == []
    assert chunk_markdown("  \n ") == []


def test_markdown_splits_by_heading_and_strips_frontmatter():
    md = "---\ntitle: x\n---\nintro text\n## Sec A\nbody a\n### Sec B\nbody b\n"
    chunks = chunk_markdown(md, max_size=100)
    # frontmatter 被剥离；## 与 ### 都作为 section 边界；标题随正文保留便于检索
    assert chunks == ["intro text", "## Sec A\nbody a", "### Sec B\nbody b"]
    assert "title: x" not in "\n".join(chunks), "YAML frontmatter 不应进入 chunk"


def test_markdown_long_section_prefixes_title_on_every_subchunk():
    md = "## Big\n" + "x" * 250
    chunks = chunk_markdown(md, max_size=100, overlap=10)
    assert len(chunks) > 1, "超长 section 应被继续切分"
    # 除首个（本身以标题开头）外，其余子块都要补标题前缀，避免检索时丢失上下文
    assert all(c.startswith("## Big") for c in chunks), (
        "每个子 chunk 都应带 section 标题前缀"
    )


def test_markdown_without_heading_falls_back_to_plain_chunking():
    assert chunk_markdown("just plain text here", max_size=100) == [
        "just plain text here"
    ]


def test_markdown_frontmatter_without_terminator_is_kept():
    # 只有起始 --- 没有结束标记时不应误删正文
    chunks = chunk_markdown("---\nbroken frontmatter\n", max_size=100)
    assert chunks == ["---\nbroken frontmatter"]


# ---------- chunk_pdf / chunk_docx ----------


def test_chunk_pdf_blank_page_yields_no_chunk():
    pypdf = pytest.importorskip("pypdf", reason="未安装 pypdf 时跳过 PDF 分块测试")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    # 空白页提取不到文本，应返回 []，而不是抛异常
    assert chunk_pdf(buf.getvalue()) == []


def test_chunk_pdf_invalid_bytes_raises():
    pytest.importorskip("pypdf", reason="未安装 pypdf 时跳过")
    # 非法字节由 pypdf 抛错；chunker 不吞异常，交由上层提示用户
    with pytest.raises(Exception):
        chunk_pdf(b"not a pdf at all")


def test_chunk_docx_joins_paragraphs_and_drops_blank_ones():
    docx = pytest.importorskip("docx", reason="未安装 python-docx 时跳过 DOCX 分块测试")
    doc = docx.Document()
    doc.add_paragraph("first para")
    doc.add_paragraph("   ")  # 空白段应被过滤
    doc.add_paragraph("second para")
    buf = io.BytesIO()
    doc.save(buf)
    assert chunk_docx(buf.getvalue(), max_size=100) == ["first para\n\nsecond para"], (
        "段落间以空行连接，空白段被丢弃"
    )
