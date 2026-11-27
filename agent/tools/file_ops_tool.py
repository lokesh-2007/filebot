"""Safe file opening and indexed file search helpers."""
from __future__ import annotations

import os
from pathlib import Path
from filebot_config import expand_config_path, path_is_within_roots
from storage.db import Database


class FileOpsTool:
    """List, read, or open files only within configured roots and the current index."""
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

    def list_directory(self, path: str | None = None, *, limit: int = 100) -> str:
        """List one configured folder without following links outside its roots."""
        if self.roots is None or not self.roots:
            raise PermissionError("Folder listing requires configured FileBot roots")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        requested = (path or "").strip().strip('"').strip("'")
        if not requested:
            folder = self.roots[0].resolve(strict=True)
        else:
            candidate = Path(requested).expanduser()
            if candidate.is_absolute():
                folder = candidate.resolve(strict=True)
            else:
                root_match = next(
                    (root for root in self.roots if root.name.casefold() == requested.casefold()),
                    None,
                )
                folder = root_match.resolve(strict=True) if root_match else None
                if folder is None:
                    for root in self.roots:
                        possible = (root / candidate)
                        try:
                            if possible.is_dir():
                                folder = possible.resolve(strict=True)
                                break
                        except (OSError, RuntimeError):
                            continue
                if folder is None:
                    raise FileNotFoundError(f"No configured folder matches '{requested}'")
        if not folder.is_dir():
            raise ValueError("The selected path is not a folder")
        if not path_is_within_roots(folder, self.roots):
            raise PermissionError("Folder is outside FileBot's configured roots")
        entries = []
        skipped_links = 0
        scanned = 0
        scan_limit = 1000
        scan_capped = False
        for item in folder.iterdir():
            if scanned >= scan_limit:
                scan_capped = True
                break
            scanned += 1
            try:
                resolved = item.resolve(strict=True)
                if not path_is_within_roots(resolved, self.roots):
                    skipped_links += 1
                    continue
                is_dir = resolved.is_dir()
                name = "".join(character if character.isprintable() else " " for character in item.name)
                entries.append((not is_dir, name.casefold(), f"{name}{'/' if is_dir else ''}"))
            except (OSError, RuntimeError):
                continue
        entries.sort()
        visible: list[str] = []
        used_chars = 0
        for entry in entries[:limit]:
            line = entry[2][:255]
            if used_chars + len(line) + 1 > 6000:
                break
            visible.append(line)
            used_chars += len(line) + 1
        if not visible:
            listing = "This folder is empty."
        else:
            listing = "\n".join(visible)
            if len(entries) > len(visible) or scan_capped:
                listing += "\n… additional items omitted from this bounded listing."
        if skipped_links:
            listing += f"\n{skipped_links} link(s) outside configured folders hidden."
        return f"Folder: {str(folder)[:1024]}\n{listing}"
