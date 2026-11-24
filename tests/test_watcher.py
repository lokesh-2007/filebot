"""Filesystem watcher debounce and shutdown behavior tests."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from watchdog.events import DirCreatedEvent, DirDeletedEvent, FileCreatedEvent

from daemon.watcher import FileWatcher
from storage.db import Database


ROOT = Path(__file__).resolve().parents[1]


class FileWatcherTests(unittest.TestCase):
    def test_debounce_queues_supported_file_and_ignores_events_after_close(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            db.initialize()
            watcher = FileWatcher({"index": {
                "roots": [], "include_extensions": [".txt"], "exclude_dirs": [".git"],
                "debounce_seconds": 0.02,
            }}, db)
            first = root / "first.txt"
            watcher.handler.on_created(FileCreatedEvent(str(first)))
            time.sleep(0.08)
            self.assertEqual(len(db.pending_dirty()), 1)
            flush_on_exit = root / "flush-on-exit.txt"
            watcher.handler.on_created(FileCreatedEvent(str(flush_on_exit)))
            watcher.stop()
            watcher.handler.on_created(FileCreatedEvent(str(root / "after-close.txt")))
            time.sleep(0.05)
            rows = db.pending_dirty()
            self.assertEqual(len(rows), 2)
            self.assertIn(str(flush_on_exit.resolve()), [row["path"] for row in rows])

    def test_directory_create_and_delete_queue_supported_descendants(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            db.initialize()
            watcher = FileWatcher({"index": {
                "roots": [], "include_extensions": [".txt"], "exclude_dirs": [".git"],
                "debounce_seconds": 1.0,
            }}, db)
            new_tree = root / "new-tree"
            (new_tree / ".git").mkdir(parents=True)
            (new_tree / "note.txt").write_text("index me", encoding="utf-8")
            (new_tree / ".git" / "secret.txt").write_text("skip me", encoding="utf-8")
            watcher.handler.on_created(DirCreatedEvent(str(new_tree)))
            deadline = time.monotonic() + 2
            while not db.pending_dirty() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([row["path"] for row in db.pending_dirty()],
                             [str((new_tree / "note.txt").resolve())])

            old_tree = root / "old-tree"
            stale_file = old_tree / "stale.txt"
            old_tree.mkdir()
            db.replace_file(stale_file, content_hash="old", size=4, mtime=1,
                            chunks=[{"text": "stale"}])
            watcher.handler.on_deleted(DirDeletedEvent(str(old_tree)))
            deadline = time.monotonic() + 2
            while len(db.pending_dirty()) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            rows = {row["path"]: row["operation"] for row in db.pending_dirty()}
            self.assertEqual(rows[str(stale_file.resolve())], "delete")
            watcher.stop()


if __name__ == "__main__":
    unittest.main()

