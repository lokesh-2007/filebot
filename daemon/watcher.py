"""Filesystem watcher that queues changes without loading retrieval models."""
from __future__ import annotations

import logging
import threading
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from filebot_config import IndexScope
from storage.db import Database

log = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, db: Database, delay: float, extensions: set[str], scope: IndexScope,
                 max_pending: int = 50000, on_overflow: Callable[[], None] | None = None) -> None:
        self.db, self.delay, self.extensions, self.scope = db, delay, extensions, scope
        self.max_pending, self.on_overflow = max_pending, on_overflow
        self.overflowed = False
        self._condition = threading.Condition()
        self._pending: dict[str, tuple[str, float]] = {}
        self._directory_jobs: deque[tuple[str, str]] = deque()
        self._closed = False
        self._thread = threading.Thread(target=self._flush_loop, name="FileBotDebounceQueue", daemon=True)
        self._thread.start()

    def _queue(self, path: str, operation: str) -> None:
        target = Path(path)
        # Cheapest checks first: whole-drive watching sends many events from excluded
        # trees (Windows, AppData, node_modules) that must not cost a resolve() each.
        if operation == "upsert" and target.suffix.lower() not in self.extensions:
            return
        if self.scope.excluded(target) or not self.scope.contains(target):
            return
        if target.is_dir():
            return
        key = str(target.resolve())
        overflow = False
        with self._condition:
            if self._closed:
                return
            if key not in self._pending and len(self._pending) >= self.max_pending:
                self.overflowed = overflow = True  # bounded memory: a rescan will catch up
            else:
                self._pending[key] = (operation, time.monotonic() + max(0.0, self.delay))
                self._condition.notify()
        if overflow and self.on_overflow is not None:
            self.on_overflow()

    def _queue_directory(self, path: str, operation: str) -> None:
        """Schedule a directory scan or stale-path removal on the queue thread."""
        target = Path(path)
        if self.scope.excluded(target) or not self.scope.contains(target):
            return
        with self._condition:
            if self._closed:
                return
            self._directory_jobs.append((str(target.resolve()), operation))
            self._condition.notify()

    def _process_directory(self, path: str, operation: str) -> None:
        """Queue supported files for a new tree or remove entries from a lost tree."""
        target = Path(path)
        try:
            if operation == "delete":
                paths = self.db.paths_under(target)
                for start in range(0, len(paths), 500):
                    self._enqueue_batch([(item, "delete") for item in paths[start:start + 500]])
                return
            if not target.is_dir():
                return
            batch: list[tuple[str, str]] = []
            for current, dirs, files in os.walk(target):
                dirs[:] = self.scope.walk_filter(current, dirs)
                for name in files:
                    file = Path(current) / name
                    if file.suffix.lower() in self.extensions:
                        batch.append((str(file), "upsert"))
                        if len(batch) >= 500:
                            self._enqueue_batch(batch)
                            batch.clear()
            if batch:
                self._enqueue_batch(batch)
        except Exception:
            log.exception("Could not queue directory event for %s", path)

    def _enqueue_batch(self, events: list[tuple[str, str]]) -> None:
        """Persist a batch efficiently, falling back to individual retryable writes."""
        if not events:
            return
        try:
            self.db.enqueue_dirty_many(events)  # type: ignore[arg-type]
        except Exception:
            log.exception("Could not queue a %d-file scan batch; retrying events individually", len(events))
            for path, operation in events:
                try:
                    self.db.enqueue_dirty(path, operation)  # type: ignore[arg-type]
                except Exception:
                    log.exception("Could not queue filesystem path %s", path)

    def _flush_loop(self) -> None:
        while True:
            with self._condition:
                while not self._closed and not self._pending and not self._directory_jobs:
                    self._condition.wait()
                if self._closed and not self._pending and not self._directory_jobs:
                    return
                directory_job = self._directory_jobs.popleft() if self._directory_jobs else None
                if directory_job is not None:
                    events: list[tuple[str, str]] = []
                    remaining = 0.0
                else:
                    now = time.monotonic()
                    due = [path for path, (_, deadline) in self._pending.items()
                           if self._closed or deadline <= now][:500]
                    if due:
                        events = [(path, self._pending.pop(path)[0]) for path in due]
                        remaining = 0.0
                    else:
                        _, (_, deadline) = min(self._pending.items(), key=lambda item: item[1][1])
                        remaining = deadline - now
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
            if directory_job is not None:
                self._process_directory(*directory_job)
                continue
            try:
                self._enqueue_batch(events)
            except Exception:
                log.exception("Could not queue filesystem event batch")
                with self._condition:
                    if not self._closed:
                        retry_at = time.monotonic() + max(1.0, self.delay)
                        for path, operation in events:
                            if path not in self._pending:
                                self._pending[path] = (operation, retry_at)
                        self._condition.notify()

    def close(self) -> None:
        """Flush pending events immediately and wait for the debounce thread."""
        with self._condition:
            self._closed = True
            now = time.monotonic()
            self._pending = {path: (operation, now) for path, (operation, _) in self._pending.items()}
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._queue_directory(str(event.src_path), "upsert")
        else:
            self._queue(str(event.src_path), "upsert")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._queue(str(event.src_path), "upsert")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._queue_directory(str(event.src_path), "delete")
        else:
            self._queue(str(event.src_path), "delete")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._queue_directory(str(event.src_path), "delete")
            self._queue_directory(str(event.dest_path), "upsert")  # type: ignore[attr-defined]
        else:
            self._queue(str(event.src_path), "delete")
            self._queue(str(event.dest_path), "upsert")  # type: ignore[attr-defined]


class FileWatcher:
    """Watch configured roots and persist debounced events in SQLite.

    A background scan queues new or changed files (skipping ones whose size and
    mtime already match the index), removes entries for files deleted while
    FileBot was off, and repeats periodically or after the event buffer overflows.
    """
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        config = settings["index"]
        self.db = db
        self.scope = IndexScope.from_settings(settings)
        self.roots = self.scope.roots
        self.extensions = {str(x).lower() for x in config.get("include_extensions", [])}
        self.rescan_seconds = float(config.get("rescan_seconds", 3600))
        self._rescan = threading.Event()
        self.handler = _DebouncedHandler(db, float(config.get("debounce_seconds", 1)), self.extensions,
                                         self.scope, int(config.get("max_pending_events", 50000)),
                                         on_overflow=self._rescan.set)
        self.observer = Observer()
        self._scan_stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self.progress: dict[str, Any] = {"state": "idle", "seen": 0, "queued": 0, "skipped": 0}
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started or self._stopped:
            raise RuntimeError("FileWatcher can only be started once")
        self._started = True
        for root in self.roots:
            if root.is_dir():
                self.observer.schedule(self.handler, str(root), recursive=True)
            else:
                log.warning("Configured index root does not exist: %s", root)
        self.observer.start()
        self._scan_thread = threading.Thread(target=self._scan_loop, name="FileBotInitialScan", daemon=True)
        self._scan_thread.start()

    def _scan_loop(self) -> None:
        while not self._scan_stop.is_set():
            try:
                self._scan_once()
            except Exception:
                log.exception("Index scan failed")
            self._rescan.wait(self.rescan_seconds if self.rescan_seconds > 0 else None)
            if self.handler.overflowed:
                log.info("Watcher event buffer overflowed; rescanning")
                self.handler.overflowed = False
            self._rescan.clear()

    def _walk_files(self, root: Path) -> Iterator[os.DirEntry[str]]:
        """Iterative scandir walk that never enters excluded directories."""
        stack = [str(root)]
        while stack:
            directory = stack.pop()
            try:
                iterator = os.scandir(directory)
            except OSError:
                continue
            with iterator:
                for entry in iterator:
                    if self._scan_stop.is_set():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not self.scope.dir_excluded(entry.name, entry.path):
                                stack.append(entry.path)
                        elif entry.is_file() and os.path.splitext(entry.name)[1].lower() in self.extensions:
                            yield entry
                    except OSError:
                        continue

    def _scan_once(self) -> None:
        """Queue new/changed supported files, then drop index entries for vanished files."""
        progress = self.progress = {"state": "scanning", "seen": 0, "queued": 0, "skipped": 0}
        batch: list[tuple[str, int, float]] = []

        def flush() -> None:
            if not batch:
                return
            unchanged = self.db.unchanged_paths(batch)
            events = [(path, "upsert") for path, _, _ in batch if path not in unchanged]
            self.handler._enqueue_batch(events)
            progress["queued"] += len(events)
            progress["skipped"] += len(batch) - len(events)
            batch.clear()

        scanned: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            scanned.append(root)
            for entry in self._walk_files(root):
                try:
                    info = entry.stat()
                except OSError:
                    continue
                batch.append((entry.path, info.st_size, info.st_mtime))
                progress["seen"] += 1
                if len(batch) >= 500:
                    flush()
                    if progress["seen"] % 20000 < 500:
                        log.info("Index scan: %d files seen, %d queued", progress["seen"], progress["queued"])
        flush()
        if self._scan_stop.is_set():
            progress["state"] = "stopped"
            return
        removed = self._sweep_missing(scanned)
        progress["state"] = "idle"
        log.info("Index scan finished: %d seen, %d queued, %d unchanged, %d stale removed",
                 progress["seen"], progress["queued"], progress["skipped"], removed)

    def _sweep_missing(self, scanned_roots: list[Path]) -> int:
        """Queue deletes for indexed files that vanished while FileBot was not watching.

        Only paths under roots that were reachable this scan are considered, so an
        unplugged drive does not wipe its entries.
        """
        prefixes = [os.path.normcase(str(root)).rstrip("\\/") + os.sep for root in scanned_roots]
        removed, after = 0, 0
        while not self._scan_stop.is_set():
            rows = self.db.iter_file_paths(after, 1000)
            if not rows:
                break
            after = rows[-1][0]
            gone = [path for _, path in rows
                    if os.path.normcase(path).startswith(tuple(prefixes)) and not os.path.exists(path)]
            if gone:
                self.handler._enqueue_batch([(path, "delete") for path in gone])
                removed += len(gone)
        return removed

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._scan_stop.set()
        self._rescan.set()
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5)
        if self._scan_thread is not None:
            self._scan_thread.join(timeout=5)
        # Stop producers first, then flush the final debounced events.
        self.handler.close()
