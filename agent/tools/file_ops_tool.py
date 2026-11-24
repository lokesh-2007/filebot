"""Safe file opening and indexed file search helpers."""
from __future__ import annotations

import os
from pathlib import Path
from storage.db import Database


class FileOpsTool:
    """Open files through the platform shell after verifying the path exists."""
    name = "open_file"
    description = "Open an existing local file in its default Windows application."

    def __init__(self, db: Database | None = None) -> None:
        self.db = db

    def open_file(self, path: str) -> None:
        file = Path(path).expanduser().resolve(strict=True)
        if not file.is_file():
            raise ValueError("Only existing files can be opened")
        if self.db is not None and self.db.get_file(file) is None:
            raise PermissionError("File is not in FileBot's local index")
        os.startfile(str(file))  # type: ignore[attr-defined]
