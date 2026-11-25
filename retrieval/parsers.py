"""Small, bounded-memory parsers for supported local documents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from zipfile import BadZipFile, ZipFile


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


def parse_file(path: str | Path, chunk_chars: int = 1200, overlap: int = 150, *,
               max_chars: int = 5_000_000,
               max_docx_uncompressed_bytes: int = 100_000_000) -> list[ParsedChunk]:
    """Extract bounded text from TXT/Markdown, PDF, or DOCX.

    The DOCX archive is checked before python-docx expands its XML parts. The
    output text budget bounds chunk-list growth for every supported format.
    """
    if max_chars < 1 or max_docx_uncompressed_bytes < 1:
        raise ValueError("parser resource limits must be positive")
    file = Path(path)
    ext = file.suffix.lower()
    out: list[ParsedChunk] = []
    if ext in {".txt", ".md"}:
        with file.open("r", encoding="utf-8", errors="replace") as source:
            text = source.read(max_chars)
        heading = None
        for piece in _split(text, chunk_chars, overlap):
            for line in piece.splitlines():
                if line.lstrip().startswith("#"):
                    heading = line.lstrip("# ").strip()[:200]
            out.append(ParsedChunk(piece, heading=heading))
    elif ext == ".pdf":
        import fitz
        remaining = max_chars
        with fitz.open(file) as doc:
            for page_number, page in enumerate(doc, start=1):
                if remaining <= 0:
                    break
                page_text = page.get_text("text")[:remaining]
                remaining -= len(page_text)
                for piece in _split(page_text, chunk_chars, overlap):
                    out.append(ParsedChunk(piece, page_or_line=f"page {page_number}"))
    elif ext == ".docx":
        try:
            with ZipFile(file) as archive:
                uncompressed_bytes = sum(info.file_size for info in archive.infolist())
        except (BadZipFile, OSError) as exc:
            raise ValueError(f"Invalid DOCX archive: {exc}") from exc
        if uncompressed_bytes > max_docx_uncompressed_bytes:
            raise ValueError(
                f"DOCX expands to {uncompressed_bytes} bytes; limit is "
                f"{max_docx_uncompressed_bytes} bytes"
            )
        from docx import Document
        doc = Document(file)
        heading = None
        buffer: list[str] = []
        remaining = max_chars
        for paragraph in doc.paragraphs:
            value = paragraph.text.strip()
            if not value:
                continue
            if paragraph.style and paragraph.style.name.startswith("Heading"):
                heading = value[:200]
            clipped = value[:remaining]
            buffer.append(clipped)
            remaining -= len(clipped)
            if remaining <= 0:
                break
        for piece in _split("\n".join(buffer), chunk_chars, overlap):
            out.append(ParsedChunk(piece, heading=heading))
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    return out
