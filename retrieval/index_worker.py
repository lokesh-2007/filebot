"""Process the durable SQLite dirty queue when FileBot is awake."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import threading
from typing import Any

from retrieval.parsers import parse_file
from storage.db import Database

log = logging.getLogger(__name__)


class IndexWorker:
    """Drain queued filesystem updates, preserving failed jobs for later retry."""
    def __init__(self, settings: dict[str, Any], db: Database, engine: Any) -> None:
        self.settings, self.db, self.engine = settings, db, engine
        self._drain_lock = threading.Lock()

    def drain(self, limit: int = 100, stop_event: threading.Event | None = None) -> int:
        """Process a bounded batch, allowing only one queue drainer at a time."""
        if not self._drain_lock.acquire(blocking=False):
            return 0
        try:
            return self._drain_locked(limit, stop_event)
        finally:
            self._drain_lock.release()

    def _drain_locked(self, limit: int, stop_event: threading.Event | None) -> int:
        processed = 0
        corpus_changed = False
        config = self.settings["index"]
        for row in self.db.pending_dirty(limit):
            if stop_event is not None and stop_event.is_set():
                break
            path = Path(row["path"])
            try:
                if row["operation"] == "delete" or not path.exists():
                    self.engine.delete(path, refresh_lexical=False)
                    self.db.delete_file(path)
                    corpus_changed = True
                else:
                    stat = path.stat()
                    if stat.st_size > int(config.get("max_file_bytes", 50 * 1024 * 1024)):
                        self.engine.delete(path, refresh_lexical=False)
                        self.db.delete_file(path)
                        corpus_changed = True
                        self.db.remove_dirty(path, float(row["queued_at"]))
                        processed += 1
                        continue
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    del raw
                    current = self.db.get_file(path)
                    if current and current["status"] == "indexed" and current["content_hash"] == digest:
                        self.engine.delete_obsolete(path, self.db.vector_ids_for_file(path))
                        self.db.remove_dirty(path, float(row["queued_at"]))
                        processed += 1
                        continue
                    parsed = parse_file(path, int(config.get("chunk_chars", 1200)), int(config.get("chunk_overlap", 150)))
                    records = [{"text": item.text, "page_or_line": item.page_or_line, "heading": item.heading} for item in parsed]
                    # Commit vectors before marking the SQLite digest current. If the
                    # process stops between these operations, the durable dirty row
                    # causes a safe vector upsert on the next wake instead of falsely
                    # treating an incomplete index operation as complete.
                    self.engine.upsert(path, parsed, content_hash=digest, refresh_lexical=False)
                    self.db.replace_file(path, content_hash=digest, size=stat.st_size,
                                         mtime=stat.st_mtime, chunks=records)
                    self.engine.delete_obsolete(path, self.db.vector_ids_for_file(path))
                    corpus_changed = True
                self.db.remove_dirty(path, float(row["queued_at"]))
                processed += 1
            except Exception as exc:
                log.exception("Could not index %s", path)
                try:
                    self.db.record_dirty_error(path, str(exc), float(row["queued_at"]))
                except Exception:
                    log.exception("Could not persist index error for %s", path)
        if corpus_changed:
            self.engine.refresh_lexical_index()
        return processed


class IndexQueueService:
    """Drain filesystem events in small batches while retrieval is awake."""

    def __init__(self, worker: IndexWorker, *, batch_size: int = 10, poll_seconds: float = 1.0) -> None:
        if batch_size < 1 or poll_seconds <= 0:
            raise ValueError("batch_size and poll_seconds must be positive")
        self.worker = worker
        self.batch_size = batch_size
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the single background queue thread once."""
        if self._thread is not None:
            raise RuntimeError("IndexQueueService can only be started once")
        self._thread = threading.Thread(target=self._run, name="FileBotIndexQueue", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.worker.drain(self.batch_size, stop_event=self._stop)
            except Exception:
                log.exception("Background index batch failed")
                processed = 0
            self._stop.wait(0.05 if processed else self.poll_seconds)

    def stop(self) -> None:
        """Finish the current bounded batch and wait for the queue thread to stop."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

