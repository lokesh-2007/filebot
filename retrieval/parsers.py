"""Small, bounded-memory parsers for supported local documents."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

# Plain-text formats read as UTF-8 (code, data, config). Markdown headings are tracked.
PLAIN_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".ini", ".yaml", ".yml", ".toml", ".xml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".sql", ".sh", ".ps1", ".bat", ".css", ".rb", ".php", ".kt", ".swift",
})
HTML_EXTENSIONS = frozenset({".html", ".htm"})
OFFICE_ZIP_EXTENSIONS = frozenset({".xlsx", ".pptx"})
SUPPORTED_EXTENSIONS = (PLAIN_TEXT_EXTENSIONS | HTML_EXTENSIONS | OFFICE_ZIP_EXTENSIONS
                        | {".pdf", ".docx"})


class _HtmlText(HTMLParser):
    """Collect visible text, skipping scripts and styles."""

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.size, self.limit, self._skip = 0, limit, 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and self.size < self.limit:
            self.parts.append(data)
            self.size += len(data)


def _check_zip_budget(file: Path, limit: int, label: str) -> None:
    """Reject archives that would expand past ``limit`` bytes before reading any part."""
    try:
        with ZipFile(file) as archive:
            uncompressed_bytes = sum(info.file_size for info in archive.infolist())
    except (BadZipFile, OSError) as exc:
        raise ValueError(f"Invalid {label} archive: {exc}") from exc
    if uncompressed_bytes > limit:
        raise ValueError(f"{label} expands to {uncompressed_bytes} bytes; limit is {limit} bytes")


def _xml_texts(data: bytes, tag_suffix: str) -> Iterator[str]:
    """Yield text of every element whose tag ends with ``tag_suffix`` (namespace-agnostic)."""
    for _, element in ElementTree.iterparse(io.BytesIO(data)):
        if element.tag.endswith(tag_suffix) and element.text:
            yield element.text
        element.clear()


def _parse_xlsx(file: Path, max_chars: int) -> list[tuple[str, str | None]]:
    """Return (text, sheet label) pairs from shared strings and inline cell values."""
    out: list[tuple[str, str | None]] = []
    with ZipFile(file) as archive:
        names = archive.namelist()
        remaining = max_chars
        if "xl/sharedStrings.xml" in names:
            text = " ".join(_xml_texts(archive.read("xl/sharedStrings.xml"), "}t"))[:remaining]
            out.append((text, "shared strings"))
    return out


def _parse_pptx(file: Path, max_chars: int) -> list[tuple[str, str | None]]:
    """Return (text, slide label) pairs for each slide, in slide order."""
    out: list[tuple[str, str | None]] = []
    with ZipFile(file) as archive:
        slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))  # type: ignore[union-attr]
        remaining = max_chars
        for name in slides:
            if remaining <= 0:
                break
            text = " ".join(_xml_texts(archive.read(name), "}t"))[:remaining]
            remaining -= len(text)
            if text.strip():
                out.append((text, f"slide {int(re.search(r'(\d+)\.xml$', name).group(1))}"))  # type: ignore[union-attr]
    return out


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
    """Extract bounded text from plain-text/code, HTML, PDF, DOCX, XLSX, or PPTX.

    Office archives are size-checked before any part is read. The output text
    budget bounds chunk-list growth for every supported format.
    """
    if max_chars < 1 or max_docx_uncompressed_bytes < 1:
        raise ValueError("parser resource limits must be positive")
    file = Path(path)
    ext = file.suffix.lower()
    out: list[ParsedChunk] = []
    if ext in PLAIN_TEXT_EXTENSIONS:
        with file.open("r", encoding="utf-8", errors="replace") as source:
            text = source.read(max_chars)
        heading = None
        for piece in _split(text, chunk_chars, overlap):
            if ext == ".md":
                for line in piece.splitlines():
                    if line.lstrip().startswith("#"):
                        heading = line.lstrip("# ").strip()[:200]
            out.append(ParsedChunk(piece, heading=heading))
    elif ext in HTML_EXTENSIONS:
        with file.open("r", encoding="utf-8", errors="replace") as source:
            raw = source.read(max_chars * 4)  # markup overhead: read more than the text budget
        collector = _HtmlText(max_chars)
        collector.feed(raw)
        collector.close()
        text = re.sub(r"\n\s*\n+", "\n", "".join(collector.parts))[:max_chars]
        for piece in _split(text, chunk_chars, overlap):
            out.append(ParsedChunk(piece))
    elif ext in OFFICE_ZIP_EXTENSIONS:
        _check_zip_budget(file, max_docx_uncompressed_bytes, ext[1:].upper())
        pairs = _parse_xlsx(file, max_chars) if ext == ".xlsx" else _parse_pptx(file, max_chars)
        for text, label in pairs:
            for piece in _split(text, chunk_chars, overlap):
                out.append(ParsedChunk(piece, page_or_line=label))
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
