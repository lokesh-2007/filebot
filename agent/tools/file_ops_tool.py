"""Safe file opening and indexed file search helpers."""
from __future__ import annotations

import os
from pathlib import Path
from filebot_config import expand_config_path, path_is_within_roots
from storage.db import Database


class FileOpsTool:
    """Read or open files only after verifying roots and current index membership."""
    name = "open_file"
    description = "Open an existing local file in its default Windows application."

    def __init__(self, db: Database | None = None, *, roots: list[str] | None = None) -> None:
        self.db = db
        self.roots = [expand_config_path(root) for root in roots] if roots is not None else None

    def open_file(self, path: str) -> None:
        file = Path(path).expanduser().resolve(strict=True)
        if not file.is_file():
            raise ValueError("Only existing files can be opened")
        if self.roots is not None and not path_is_within_roots(file, self.roots):
            raise PermissionError("File is outside FileBot's configured index roots")
        if self.db is not None and self.db.get_file(file) is None:
            raise PermissionError("File is not in FileBot's local index")
        os.startfile(str(file))  # type: ignore[attr-defined]
        record_open = getattr(self.db, "record_file_open", None)
        if callable(record_open):
            record_open(file)

    def read_file(self, path: str, *, max_chars: int = 12_000) -> str:
        """Read bounded parser output from an indexed file without opening it."""
        if not 1 <= max_chars <= 12_000:
            raise ValueError("max_chars must be between 1 and 12000")
        file = Path(path).expanduser().resolve(strict=True)
        if not file.is_file():
            raise ValueError("Only existing files can be read")
        if self.roots is not None and not path_is_within_roots(file, self.roots):
            raise PermissionError("File is outside FileBot's configured index roots")
        if self.db is None:
            raise PermissionError("File reading requires FileBot's local index")
        record = self.db.get_file(file)
        if record is None or record["status"] != "indexed":
            raise PermissionError("File is not in FileBot's current local index")
        chunks = self.db.indexed_file_chunks(
            file, limit=16, chars_per_chunk=max_chars,
        )
        if not chunks:
            raise ValueError("File has no readable indexed text")
        sections: list[str] = []
        remaining = max_chars
        for chunk in chunks:
            text = str(chunk["snippet"] or "")
            if not text:
                continue
            heading = str(chunk["heading"] or chunk["page_or_line"] or "").strip()
            prefix = f"[{heading}]\n" if heading else ""
            piece = text[:remaining]
            sections.append(prefix + piece)
            remaining -= len(piece)
            if remaining <= 0:
                break
        content = "\n\n".join(sections).strip()
        if not content:
            raise ValueError("File has no readable indexed text")
        suffix = "\n\n[Excerpt shortened to fit FileBot's local read limit.]" if remaining <= 0 else ""
        return f"File: {file}\n{content}{suffix}"
