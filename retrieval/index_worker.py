"""Process the durable SQLite dirty queue when FileBot is awake."""
from __future__ import annotations

import hashlib
import ctypes
import logging
import os
from pathlib import Path
import threading
from typing import Any

from filebot_config import expand_config_path, path_is_within_roots
from retrieval.parsers import parse_file
from storage.db import Database

log = logging.getLogger(__name__)

_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_THREAD_MODE_BACKGROUND_END = 0x00020000


def _set_current_thread_background_mode(enabled: bool) -> bool:
    """Lower this worker's Windows CPU and I/O priority while it indexes."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_thread = kernel32.GetCurrentThread
        get_current_thread.restype = ctypes.c_void_p
        set_thread_priority = kernel32.SetThreadPriority
        set_thread_priority.argtypes = (ctypes.c_void_p, ctypes.c_int)
        set_thread_priority.restype = ctypes.c_int
        mode = _THREAD_MODE_BACKGROUND_BEGIN if enabled else _THREAD_MODE_BACKGROUND_END
        if set_thread_priority(get_current_thread(), mode):
            return True
        error = ctypes.get_last_error()
        log.warning("Could not change index worker background priority (Windows error %d)", error)
    except (AttributeError, OSError) as exc:
        log.warning("Could not change index worker background priority: %s", exc)
    return False


def _same_file_revision(left: Any, right: Any) -> bool:
    """Compare metadata that should remain stable across one indexing pass."""
    return (left.st_size, left.st_mtime_ns, getattr(left, "st_ino", None)) == (
        right.st_size, right.st_mtime_ns, getattr(right, "st_ino", None)
    )


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
        roots = [expand_config_path(root) for root in config.get("roots", [])]
        for row in self.db.pending_dirty(limit):
            if stop_event is not None and stop_event.is_set():
                break
            path = Path(row["path"])
            try:
                if (row["operation"] != "delete" and "roots" in config
                        and not path_is_within_roots(path, roots)):
                    log.warning("Discarding queued index path outside configured roots: %s", path)
                    self.db.remove_dirty(path, float(row["queued_at"]))
                    processed += 1
                    continue
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
                    digest_builder = hashlib.sha256()
                    with path.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            digest_builder.update(block)
                    digest = digest_builder.hexdigest()
                    hashed_stat = path.stat()
                    if not _same_file_revision(stat, hashed_stat):
                        log.debug("File changed while hashing; requeuing %s", path)
                        self.db.enqueue_dirty(path, "upsert")
                        continue
                    current = self.db.get_file(path)
                    if current and current["status"] == "indexed" and current["content_hash"] == digest:
                        if not _same_file_revision(hashed_stat, path.stat()):
                            log.debug("File changed before acknowledging index event; requeuing %s", path)
                            self.db.enqueue_dirty(path, "upsert")
                            continue
                        self.engine.delete_obsolete(path, self.db.vector_ids_for_file(path))
                        self.db.remove_dirty(path, float(row["queued_at"]))
                        processed += 1
                        continue
                    parsed = parse_file(
                        path,
                        int(config.get("chunk_chars", 1200)),
                        int(config.get("chunk_overlap", 150)),
                        max_chars=int(config.get("max_extracted_chars", 5_000_000)),
                        max_docx_uncompressed_bytes=int(
                            config.get("max_docx_uncompressed_bytes", 100_000_000)
                        ),
                        source_parse_timeout_ms=min(5_000, max(
                            1, int(config.get("source_parse_timeout_ms", 250)),
                        )),
                    )
                    parsed_stat = path.stat()
                    if not _same_file_revision(hashed_stat, parsed_stat):
                        log.debug("File changed while parsing; requeuing %s", path)
                        self.db.enqueue_dirty(path, "upsert")
                        continue
                    records = [{"text": item.text, "page_or_line": item.page_or_line, "heading": item.heading} for item in parsed]
                    # Commit vectors before marking the SQLite digest current. If the
                    # process stops between these operations, the durable dirty row
                    # causes a safe vector upsert on the next wake instead of falsely
                    # treating an incomplete index operation as complete.
                    self.engine.upsert(path, parsed, content_hash=digest, refresh_lexical=False)
                    self.db.replace_file(path, content_hash=digest, size=hashed_stat.st_size,
                                         mtime=hashed_stat.st_mtime, chunks=records)
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
        background_mode = _set_current_thread_background_mode(True)
        try:
            while not self._stop.is_set():
                try:
                    processed = self.worker.drain(self.batch_size, stop_event=self._stop)
                except Exception:
                    log.exception("Background index batch failed")
                    processed = 0
                self._stop.wait(0.05 if processed else self.poll_seconds)
        finally:
            if background_mode:
                _set_current_thread_background_mode(False)

    def stop(self) -> None:
        """Finish the current bounded batch and wait for the queue thread to stop."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
