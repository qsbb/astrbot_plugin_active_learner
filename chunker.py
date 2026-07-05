"""文档分块器：把长文本/Markdown/PDF/DOCX 拆成可嵌入的 chunk。

设计要点：
1. 中文按字数切，英文按词数切（混合时按字数兜底）
2. 滑动窗口 + overlap，避免边界信息丢失
3. Markdown 优先按 ## 拆 section，保留标题
4. PDF/DOCX 按需导入依赖（pypdf, python-docx），缺失时抛友好错误
"""

from __future__ import annotations

import io
from typing import Optional


def chunk_text(text: str, max_size: int = 500, overlap: int = 50) -> list[str]:
    """滑动窗口分块。

    Args:
        text: 原始文本
        max_size: 每块最大字符数（中文按字，英文按词）
        overlap: 重叠字符数
    Returns:
        chunks 列表，空文本返回 []
    """
    if not text or not text.strip():
        return []
    text = text.strip()
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    step = max_size - overlap
    if step <= 0:
        step = max_size
    i = 0
    while i < len(text):
        chunk = text[i : i + max_size]
        if chunk.strip():
            chunks.append(chunk.strip())
        i += step
    return chunks


def chunk_markdown(content: str, max_size: int = 500, overlap: int = 50) -> list[str]:
    """Markdown 分块：先按 ## 拆 section，section 超长再 chunk_text。

    保留 section 标题作为 chunk 的前缀，便于检索。
    """
    if not content or not content.strip():
        return []
    # 去 YAML frontmatter
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4 :].lstrip("\n")

    # 按 ## 或 ### 拆 section
    sections: list[tuple[str, str]] = []
    current_title = ""
    current_body: list[str] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            if current_body:
                sections.append((current_title, "\n".join(current_body)))
            current_title = stripped
            current_body = [line]
        else:
            current_body.append(line)
    if current_body:
        sections.append((current_title, "\n".join(current_body)))

    # 每个 section 切 chunk
    chunks: list[str] = []
    for title, body in sections:
        body_clean = body.strip()
        if not body_clean:
            continue
        if len(body_clean) <= max_size:
            chunks.append(body_clean)
        else:
            for sub in chunk_text(body_clean, max_size, overlap):
                # 把标题前缀加到每个子 chunk（除非已经是 section 第一行）
                if title and not sub.startswith(title):
                    chunks.append(f"{title}\n{sub}")
                else:
                    chunks.append(sub)
    return chunks if chunks else chunk_text(content, max_size, overlap)


def chunk_pdf(file_bytes: bytes, max_size: int = 500, overlap: int = 50) -> list[str]:
    """PDF 分块：用 pypdf 提取文本，再 chunk_text。

    Raises:
        ImportError: 未安装 pypdf
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ImportError("PDF 导入需要 pypdf，请 pip install pypdf") from e

    reader = PdfReader(io.BytesIO(file_bytes))
    pages_text: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
            if txt.strip():
                pages_text.append(txt.strip())
        except Exception:
            continue
    full_text = "\n\n".join(pages_text)
    return chunk_text(full_text, max_size, overlap)


def chunk_docx(file_bytes: bytes, max_size: int = 500, overlap: int = 50) -> list[str]:
    """DOCX 分块：用 python-docx 提取段落，再 chunk_text。

    Raises:
        ImportError: 未安装 python-docx
    """
    try:
        from docx import Document
    except ImportError as e:
        raise ImportError("DOCX 导入需要 python-docx，请 pip install python-docx") from e

    doc = Document(io.BytesIO(file_bytes))
    paragraphs: list[str] = []
    for p in doc.paragraphs:
        txt = p.text.strip()
        if txt:
            paragraphs.append(txt)
    full_text = "\n\n".join(paragraphs)
    return chunk_text(full_text, max_size, overlap)
