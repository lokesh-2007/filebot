"""Small, bounded-memory parsers for supported local documents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ParsedChunk:
    """Text chunk plus optional source location metadata."""
    text: str
    page_or_line: str | None = None
    heading: str | None = None


def _split(text: str, size: int, overlap: int) -> Iterator[str]:
    if size < 100 or overlap < 0 or overlap >= size:
        raise ValueError("chunk size must be >=100 and overlap smaller than chunk size")
    text = text.strip()
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            yield piece
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)


def parse_file(path: str | Path, chunk_chars: int = 1200, overlap: int = 150) -> list[ParsedChunk]:
    """Extract text from TXT/Markdown, PDF, or DOCX without loading all dependencies at startup."""
    file = Path(path)
    ext = file.suffix.lower()
    out: list[ParsedChunk] = []
    if ext in {".txt", ".md"}:
        text = file.read_text(encoding="utf-8", errors="replace")
        heading = None
        for piece in _split(text, chunk_chars, overlap):
            for line in piece.splitlines():
                if line.lstrip().startswith("#"):
                    heading = line.lstrip("# ").strip()[:200]
            out.append(ParsedChunk(piece, heading=heading))
    elif ext == ".pdf":
        import fitz
        with fitz.open(file) as doc:
            for page_number, page in enumerate(doc, start=1):
                for piece in _split(page.get_text("text"), chunk_chars, overlap):
                    out.append(ParsedChunk(piece, page_or_line=f"page {page_number}"))
    elif ext == ".docx":
        from docx import Document
        doc = Document(file)
        heading = None
        buffer: list[str] = []
        for paragraph in doc.paragraphs:
            value = paragraph.text.strip()
            if not value:
                continue
            if paragraph.style and paragraph.style.name.startswith("Heading"):
                heading = value[:200]
            buffer.append(value)
        for piece in _split("\n".join(buffer), chunk_chars, overlap):
            out.append(ParsedChunk(piece, heading=heading))
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    return out
