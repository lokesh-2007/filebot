"""Small, bounded-memory parsers for supported local documents."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Iterator
from zipfile import BadZipFile, ZipFile

log = logging.getLogger(__name__)

_SOURCE_LANGUAGES = {
    ".py": "python", ".pyi": "python", ".rs": "rust", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".java": "java",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".cs": "csharp", ".rb": "ruby",
    ".php": "php", ".sh": "bash", ".bash": "bash", ".ps1": "powershell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sql": "sql",
}
_missing_language_warnings: set[str] = set()
_missing_language_lock = threading.Lock()


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


def _parse_source_code(path: Path, text: str, language: str, chunk_chars: int,
                       overlap: int, timeout_ms: int) -> list[ParsedChunk]:
    """Split source on Tree-sitter syntax boundaries with a bounded fallback."""
    try:
        from tree_sitter_language_pack import LanguageRegistry, ProcessConfig
        registry = LanguageRegistry.default()
        # Registry.has_parser is deliberately used before process(): unlike
        # get_parser/process, it never downloads a grammar during a wake/index.
        if not registry.has_parser(language):
            with _missing_language_lock:
                if language not in _missing_language_warnings:
                    log.warning(
                        "Tree-sitter grammar '%s' is not cached; indexing %s as bounded plain text. "
                        "Run scripts/setup.ps1 to install parser grammars.", language, path.suffix,
                    )
                    _missing_language_warnings.add(language)
            return [ParsedChunk(piece, page_or_line="source")
                    for piece in _split(text, chunk_chars, overlap)]
        result = registry.process(text, ProcessConfig(
            language=language,
            structure=False,
            imports=False,
            exports=False,
            # The pack's chunk limit is measured in UTF-8 bytes; use the same
            # budget as our text chunker so ASCII source stays near chunk_chars.
            chunk_max_size=max(1, chunk_chars),
            max_source_bytes=max(1, len(text.encode("utf-8"))),
            parse_timeout_ms=max(1, timeout_ms),
        ))
        out = [
            ParsedChunk(
                chunk.content.strip(),
                page_or_line=f"lines {chunk.start_line + 1}-{chunk.end_line + 1}",
                heading=(
                    ", ".join(getattr(chunk.metadata, "symbols_defined", [])[:3])
                    or "source code"
                ),
            )
            for chunk in result.chunks
            if chunk.content.strip()
        ]
        if out:
            return out
    except Exception:
        log.exception("Tree-sitter parsing failed for %s; using bounded source chunks", path)
    return [ParsedChunk(piece, page_or_line="source")
            for piece in _split(text, chunk_chars, overlap)]


def parse_file(path: str | Path, chunk_chars: int = 1200, overlap: int = 150, *,
               max_chars: int = 5_000_000,
               max_docx_uncompressed_bytes: int = 100_000_000,
               source_parse_timeout_ms: int = 250) -> list[ParsedChunk]:
    """Extract bounded text from documents and Tree-sitter supported source files.

    The DOCX archive is checked before python-docx expands its XML parts. The
    output text budget bounds chunk-list growth for every supported format.
    """
    if max_chars < 1 or max_docx_uncompressed_bytes < 1:
        raise ValueError("parser resource limits must be positive")
    if source_parse_timeout_ms < 1:
        raise ValueError("source parse timeout must be positive")
    file = Path(path)
    ext = file.suffix.lower()
    out: list[ParsedChunk] = []
    if ext in _SOURCE_LANGUAGES:
        with file.open("r", encoding="utf-8", errors="replace") as source:
            text = source.read(max_chars)
        return _parse_source_code(
            file, text, _SOURCE_LANGUAGES[ext], chunk_chars, overlap, source_parse_timeout_ms,
        )
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
