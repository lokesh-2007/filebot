"""Filesystem watcher that queues changes without loading retrieval models."""
from __future__ import annotations

import logging
import threading
import os
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from storage.db import Database

log = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, db: Database, delay: float, extensions: set[str], excluded: set[str]) -> None:
        self.db, self.delay, self.extensions, self.excluded = db, delay, extensions, excluded
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _queue(self, path: str, operation: str) -> None:
        target = Path(path)
        if target.is_dir() or (operation == "upsert" and target.suffix.lower() not in self.extensions):
            return
        if any(part.lower() in self.excluded for part in target.parts):
            return
        key = str(target.resolve())
        with self._lock:
            previous = self._timers.pop(key, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(self.delay, self._write, (key, operation))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _write(self, path: str, operation: str) -> None:
        try:
            self.db.enqueue_dirty(path, operation)  # type: ignore[arg-type]
        except Exception:
            log.exception("Could not queue filesystem event for %s", path)
        finally:
            with self._lock:
                self._timers.pop(path, None)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._queue(str(event.src_path), "upsert")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._queue(str(event.src_path), "upsert")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._queue(str(event.src_path), "delete")

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._queue(str(event.src_path), "delete")
            self._queue(str(event.dest_path), "upsert")  # type: ignore[attr-defined]


class FileWatcher:
    """Watch configured roots and persist debounced events in SQLite."""
    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        config = settings["index"]
        self.roots = [Path(p).expanduser() for p in config.get("roots", [])]
        self.handler = _DebouncedHandler(db, float(config.get("debounce_seconds", 1)),
            {str(x).lower() for x in config.get("include_extensions", [])},
            {str(x).lower() for x in config.get("exclude_dirs", [])})
        self.observer = Observer()

    def start(self) -> None:
        for root in self.roots:
            if root.is_dir():
                self.observer.schedule(self.handler, str(root), recursive=True)
            else:
                log.warning("Configured index root does not exist: %s", root)
        self.observer.start()
        threading.Thread(target=self._seed_existing, name="FileBotInitialScan", daemon=True).start()

    def _seed_existing(self) -> None:
        """Queue existing supported files without parsing or loading models."""
        for root in self.roots:
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name.lower() not in self.handler.excluded]
                for name in files:
                    path = Path(current) / name
                    if path.suffix.lower() in self.handler.extensions:
                        try:
                            self.handler.db.enqueue_dirty(path, "upsert")
                        except Exception:
                            log.exception("Could not queue initial scan file %s", path)

    def stop(self) -> None:
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join(timeout=5)
