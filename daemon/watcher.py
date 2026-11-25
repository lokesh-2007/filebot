"""Filesystem watcher that queues changes without loading retrieval models."""
from __future__ import annotations

import logging
import threading
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from filebot_config import expand_config_path, path_is_within_roots
from storage.db import Database

log = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, db: Database, delay: float, extensions: set[str], excluded: set[str],
                 roots: list[Path]) -> None:
        self.db, self.delay, self.extensions, self.excluded = db, delay, extensions, excluded
        self.roots = roots
        self._condition = threading.Condition()
        self._pending: dict[str, tuple[str, float]] = {}
        self._directory_jobs: deque[tuple[str, str]] = deque()
        self._closed = False
        self._thread = threading.Thread(target=self._flush_loop, name="FileBotDebounceQueue", daemon=True)
        self._thread.start()

    def _queue(self, path: str, operation: str) -> None:
        target = Path(path)
        if not path_is_within_roots(target, self.roots):
            return
        if target.is_dir() or (operation == "upsert" and target.suffix.lower() not in self.extensions):
            return
        if any(part.lower() in self.excluded for part in target.parts):
            return
        key = str(target.resolve())
        with self._condition:
            if self._closed:
                return
            self._pending[key] = (operation, time.monotonic() + max(0.0, self.delay))
            self._condition.notify()

    def _queue_directory(self, path: str, operation: str) -> None:
        """Schedule a directory scan or stale-path removal on the queue thread."""
        target = Path(path)
        if not path_is_within_roots(target, self.roots):
            return
        if any(part.lower() in self.excluded for part in target.parts):
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
                dirs[:] = [name for name in dirs if name.lower() not in self.excluded]
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
    """Watch configured roots and persist debounced events in SQLite."""
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        config = settings["index"]
        self.roots = [expand_config_path(p) for p in config.get("roots", [])]
        self.handler = _DebouncedHandler(db, float(config.get("debounce_seconds", 1)),
            {str(x).lower() for x in config.get("include_extensions", [])},
            {str(x).lower() for x in config.get("exclude_dirs", [])}, self.roots)
        self.observer = Observer()
        self._scan_stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
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
        self._scan_thread = threading.Thread(target=self._seed_existing, name="FileBotInitialScan", daemon=True)
        self._scan_thread.start()

    def _seed_existing(self) -> None:
        """Queue existing supported files without parsing or loading models."""
        batch: list[tuple[str, str]] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root):
                if self._scan_stop.is_set():
                    if batch:
                        self.handler._enqueue_batch(batch)
                    return
                dirs[:] = [name for name in dirs if name.lower() not in self.handler.excluded]
                for name in files:
                    if self._scan_stop.is_set():
                        if batch:
                            self.handler._enqueue_batch(batch)
                        return
                    path = Path(current) / name
                    if path.suffix.lower() in self.handler.extensions:
                        batch.append((str(path), "upsert"))
                        if len(batch) >= 500:
                            self.handler._enqueue_batch(batch)
                            batch = []
        if batch and not self._scan_stop.is_set():
            self.handler._enqueue_batch(batch)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._scan_stop.set()
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5)
        if self._scan_thread is not None:
            self._scan_thread.join(timeout=5)
        # Stop producers first, then flush the final debounced events.
        self.handler.close()
