"""Process the durable SQLite dirty queue when FileBot is awake."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from retrieval.parsers import parse_file
from storage.db import Database

log = logging.getLogger(__name__)


class IndexWorker:
    """Drain queued filesystem updates, preserving failed jobs for later retry."""
    def __init__(self, settings: dict[str, Any], db: Database, engine: Any) -> None:
        self.settings, self.db, self.engine = settings, db, engine

    def drain(self, limit: int = 100) -> int:
        processed = 0
        config = self.settings["index"]
        for row in self.db.pending_dirty(limit):
            path = Path(row["path"])
            try:
                if row["operation"] == "delete" or not path.exists():
                    self.engine.delete(path)
                    self.db.delete_file(path)
                else:
                    stat = path.stat()
                    if stat.st_size > int(config.get("max_file_bytes", 50 * 1024 * 1024)):
                        self.engine.delete(path)
                        self.db.delete_file(path)
                        self.db.remove_dirty(path)
                        processed += 1
                        continue
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    current = self.db.get_file(path)
                    if current and current["status"] == "indexed" and current["content_hash"] == digest:
                        self.db.remove_dirty(path)
                        processed += 1
                        continue
                    parsed = parse_file(path, int(config.get("chunk_chars", 1200)), int(config.get("chunk_overlap", 150)))
                    records = [{"text": item.text, "page_or_line": item.page_or_line, "heading": item.heading} for item in parsed]
                    self.db.replace_file(path, content_hash=digest, size=stat.st_size,
                                         mtime=stat.st_mtime, chunks=records)
                    self.engine.upsert(path, parsed)
                self.db.remove_dirty(path)
                processed += 1
            except Exception as exc:
                log.exception("Could not index %s", path)
                try:
                    self.db.record_dirty_error(path, str(exc))
                except Exception:
                    log.exception("Could not persist index error for %s", path)
        return processed
