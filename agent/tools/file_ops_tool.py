"""Safe file opening and indexed file search helpers."""
from __future__ import annotations

import os
import ctypes
import logging
from pathlib import Path
from ctypes import wintypes
from filebot_config import expand_config_path, path_is_within_roots
from storage.db import Database

log = logging.getLogger(__name__)
_CREATABLE_TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log",
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go",
    ".java", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs",
    ".rb", ".php", ".rs", ".html", ".css", ".xml", ".sql",
}
_MAX_CREATE_BYTES = 256_000
_FOLDER_SEARCH_DEPTH = 3
_FOLDER_SEARCH_LIMIT = 2_000
_FOLDER_SEARCH_SKIP = frozenset({".git", ".venv", "appdata", "node_modules", "windows"})


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
        ("fFlags", wintypes.UINT), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


class FileOpsTool:
    """Handle files inside configured roots with bounded, explicit operations."""
    name = "open_file"
    description = "Open an existing local file in its default Windows application."

    def __init__(self, db: Database | None = None, *, roots: list[str] | None = None) -> None:
        self.db = db
        self.roots = [expand_config_path(root) for root in roots] if roots is not None else None

    def _resolve_new_target(self, path: str) -> Path:
        """Resolve a target's existing parent and enforce root containment."""
        if self.roots is None or not self.roots:
            raise PermissionError("File creation requires configured FileBot roots")
        requested_text = path.strip().strip('"').strip("'")
        if not requested_text or "\x00" in requested_text:
            raise ValueError("Provide a valid file path")
        requested = Path(requested_text).expanduser()
        if not requested.is_absolute():
            root_match = next(
                (root for root in self.roots if requested.parts
                 and requested.parts[0].casefold() == root.name.casefold()),
                None,
            )
            root = root_match or self.roots[0]
            relative = Path(*requested.parts[1:]) if root_match else requested
            requested = root / relative
        if requested.name in {"", ".", ".."}:
            raise ValueError("Provide a filename, not only a folder")
        parent = requested.parent.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError("The parent path is not a folder")
        if not path_is_within_roots(parent, self.roots):
            raise PermissionError("File path is outside FileBot's configured roots")
        return parent / requested.name

    @staticmethod
    def _folder_key(name: str) -> str:
        """Normalize spacing and punctuation for a user's natural folder name."""
        return "".join(character.casefold() for character in name if character.isalnum())

    def _resolve_configured_folder(self, requested: str) -> Path:
        """Resolve exact paths first, then a bounded unambiguous name under configured roots."""
        if self.roots is None or not self.roots:
            raise PermissionError("Folder access requires configured FileBot roots")
        candidate = Path(requested).expanduser()
        if candidate.is_absolute():
            folder = candidate.resolve(strict=True)
            if not folder.is_dir():
                raise ValueError("The selected path is not a folder")
            if not path_is_within_roots(folder, self.roots):
                raise PermissionError("Folder is outside FileBot's configured roots")
            return folder

        exact: dict[str, Path] = {}
        for root in self.roots:
            try:
                resolved_root = root.resolve(strict=True)
                if not resolved_root.is_dir():
                    continue
                if candidate.parts and len(candidate.parts) == 1 and (
                    resolved_root.name.casefold() == requested.casefold()
                ):
                    exact[str(resolved_root).casefold()] = resolved_root
                possible = (resolved_root / candidate).resolve(strict=True)
                if possible.is_dir() and path_is_within_roots(possible, self.roots):
                    exact[str(possible).casefold()] = possible
            except (OSError, RuntimeError):
                continue
        if len(exact) == 1:
            return next(iter(exact.values()))
        if len(exact) > 1:
            options = "\n".join(
                f"- {path}" for path in sorted(exact.values(), key=lambda item: str(item).casefold())[:10]
            )
            raise ValueError(f"More than one configured folder matches '{requested}':\n{options}")
        if len(candidate.parts) != 1:
            raise FileNotFoundError(f"No configured folder matches '{requested}'")

        query_key = self._folder_key(requested)
        if not query_key:
            raise FileNotFoundError(f"No configured folder matches '{requested}'")
        matches: dict[str, Path] = {}
        queue: list[tuple[Path, int]] = []
        for root in self.roots:
            try:
                resolved_root = root.resolve(strict=True)
                if resolved_root.is_dir() and path_is_within_roots(resolved_root, self.roots):
                    queue.append((resolved_root, 0))
            except (OSError, RuntimeError):
                continue

        visited = 0
        position = 0
        while position < len(queue) and visited < _FOLDER_SEARCH_LIMIT:
            folder, depth = queue[position]
            position += 1
            visited += 1
            if self._folder_key(folder.name) == query_key:
                matches[str(folder).casefold()] = folder
            if depth >= _FOLDER_SEARCH_DEPTH:
                continue
            try:
                with os.scandir(folder) as entries:
                    children = sorted(entries, key=lambda entry: entry.name.casefold())
                for entry in children:
                    if entry.name.casefold() in _FOLDER_SEARCH_SKIP:
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        child = Path(entry.path).resolve(strict=True)
                        if (child.is_dir() and path_is_within_roots(child, self.roots)):
                            queue.append((child, depth + 1))
                    except (OSError, RuntimeError):
                        continue
            except OSError:
                continue

        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            options = "\n".join(
                f"- {path}" for path in sorted(matches.values(), key=lambda item: str(item).casefold())[:10]
            )
            raise ValueError(f"More than one configured folder matches '{requested}':\n{options}")
        raise FileNotFoundError(f"No configured folder matches '{requested}'")

    def create_file(self, path: str, content: str) -> str:
        """Create a new, bounded UTF-8 text file without overwriting anything."""
        target = self._resolve_new_target(path)
        if target.suffix.casefold() not in _CREATABLE_TEXT_EXTENSIONS:
            raise ValueError("FileBot can create text and source files with a supported text extension")
        if not isinstance(content, str):
            raise ValueError("File content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_CREATE_BYTES:
            raise ValueError(f"New file content is limited to {_MAX_CREATE_BYTES:,} bytes")
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise FileExistsError(f"FileBot will not overwrite an existing file: {target}") from exc
        if self.db is not None:
            try:
                self.db.enqueue_dirty(target, "upsert")
            except Exception:
                # The watcher will still index the new file; do not misreport a
                # successful create as a failed write because queueing failed.
                log.exception("Created file but could not enqueue immediate indexing: %s", target)
        return str(target)

    def stage_delete(self, path: str) -> str:
        """Stage one exact file and return its short-lived confirmation token."""
        if self.db is None:
            raise PermissionError("File deletion requires FileBot's local audit database")
        file = Path(path).expanduser().resolve(strict=True)
        if not file.is_file():
            raise ValueError("Only an existing file can be deleted")
        if self.roots is None or not path_is_within_roots(file, self.roots):
            raise PermissionError("File is outside FileBot's configured roots")
        metadata = file.stat()
        return self.db.stage_file_delete(
            file, file_size=metadata.st_size, file_mtime_ns=metadata.st_mtime_ns,
        )

    def delete_to_recycle_bin(self, path: str, *, expected_size: int, expected_mtime_ns: int) -> str:
        """Move the unchanged approved file to Windows Recycle Bin, never unlink it."""
        file = Path(path).expanduser().resolve(strict=True)
        if not file.is_file():
            raise ValueError("The approved path is no longer a file")
        if self.roots is None or not path_is_within_roots(file, self.roots):
            raise PermissionError("File is outside FileBot's configured roots")
        metadata = file.stat()
        if metadata.st_size != expected_size or metadata.st_mtime_ns != expected_mtime_ns:
            raise RuntimeError("The file changed after confirmation was requested; nothing was deleted")
        _move_file_to_recycle_bin(file)
        if self.db is not None:
            try:
                self.db.enqueue_dirty(file, "delete")
            except Exception:
                log.exception("Moved file to Recycle Bin but could not enqueue index cleanup: %s", file)
        return str(file)

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
            folder = self._resolve_configured_folder(requested)
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

    def open_directory(self, path: str) -> str:
        """Open a named or absolute folder only when it is inside configured roots."""
        requested = path.strip().strip('"').strip("'")
        if not requested or "\x00" in requested:
            raise ValueError("Provide a valid folder path")
        folder = self._resolve_configured_folder(requested)
        if os.name != "nt":
            raise OSError("Opening a folder in Explorer is available only on Windows")
        os.startfile(str(folder))  # type: ignore[attr-defined]
        return str(folder)


def _move_file_to_recycle_bin(file: Path) -> None:
    """Use the Windows shell undo path, with no permanent-delete fallback."""
    if os.name != "nt":
        raise OSError("Recycle Bin file deletion is available only on Windows")
    source = ctypes.create_unicode_buffer(str(file) + "\0\0")
    flags = 0x0004 | 0x0010 | 0x0040 | 0x0400  # silent, confirm in chat, allow undo, no error UI
    operation = _SHFILEOPSTRUCTW(
        None, 3, ctypes.cast(source, ctypes.c_wchar_p), None, flags, False, None, None,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"Windows could not move {file.name} to the Recycle Bin (error {result})")
