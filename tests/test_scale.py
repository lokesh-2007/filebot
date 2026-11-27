"""Whole-system search scale features: FTS5, scope rules, scan skipping, parking bad files."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from daemon.watcher import FileWatcher
from filebot_config import IndexScope
from retrieval.parsers import SUPPORTED_EXTENSIONS, parse_file
from storage.db import MAX_DIRTY_ATTEMPTS, Database

ROOT = Path(__file__).resolve().parents[1]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.db = Database(self.root / "state.sqlite3")
        self.db.initialize()

    def index(self, name: str, text: str = "hello", *, subdir: str = "docs") -> Path:
        path = self.root / subdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stat = path.stat()
        self.db.replace_file(path, content_hash="h" + name, size=stat.st_size, mtime=stat.st_mtime,
                             chunks=[{"text": text, "page_or_line": None, "heading": None}])
        return path


class FtsTests(_Base):
    def test_fts_follows_replace_and_cascade_delete(self) -> None:
        self.assertTrue(self.db.fts_enabled)
        path = self.index("a.txt", "quarterly storage budget")
        self.assertEqual(len(self.db.search_lexical("budget")), 1)
        self.index("a.txt", "something else entirely")  # replace
        self.assertEqual(self.db.search_lexical("budget"), [])
        self.assertEqual(len(self.db.search_lexical("entirely")), 1)
        self.db.delete_file(path)
        self.assertEqual(self.db.search_lexical("entirely"), [])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0], 0)

    def test_file_name_is_searchable_and_special_characters_are_safe(self) -> None:
        self.index("invoice-2026.txt", "plain body")
        self.assertEqual(len(self.db.search_lexical("invoice")), 1)
        for hostile in ('"', "AND OR NOT", "a* NEAR(b)", "col:val", "'; DROP TABLE files;--"):
            self.db.search_lexical(hostile)  # must not raise

    def test_existing_database_is_backfilled_on_initialize(self) -> None:
        self.index("old.txt", "legacy indexed content")
        with self.db.connect() as connection:
            connection.executescript("DROP TRIGGER chunks_fts_ai; DROP TRIGGER chunks_fts_ad; DROP TABLE chunks_fts;")
        fresh = Database(self.root / "state.sqlite3")
        fresh.initialize()
        self.assertEqual(len(fresh.search_lexical("legacy")), 1)

    def test_out_of_sync_index_is_rebuilt(self) -> None:
        self.index("a.txt", "alpha content")
        with self.db.connect() as connection:
            connection.execute("DELETE FROM chunks_fts")
        self.db.initialize()
        self.assertEqual(len(self.db.search_lexical("alpha")), 1)

    def test_path_prefix_restricts_results(self) -> None:
        self.index("a.txt", "shared words", subdir="one")
        self.index("b.txt", "shared words", subdir="two")
        prefix = [str(self.root / "one") + os.sep]
        self.assertEqual([Path(r["path"]).name for r in self.db.search_lexical("shared", 10, prefix)], ["a.txt"])
        self.assertEqual(self.db.search_lexical("shared", 10, []), [])


class ScanSupportTests(_Base):
    def test_unchanged_paths_compares_size_and_mtime(self) -> None:
        path = self.index("a.txt", "abc")
        stat = path.stat()
        key = str(path)
        self.assertEqual(self.db.unchanged_paths([(key, stat.st_size, stat.st_mtime)]), {key})
        self.assertEqual(self.db.unchanged_paths([(key, stat.st_size + 1, stat.st_mtime)]), set())
        self.assertEqual(self.db.unchanged_paths([(key, stat.st_size, stat.st_mtime + 5)]), set())
        self.assertEqual(self.db.unchanged_paths([(str(self.root / "unknown.txt"), 1, 1.0)]), set())

    def test_requeued_files_are_never_treated_as_unchanged(self) -> None:
        path = self.index("a.txt", "abc")
        stat = path.stat()
        self.db.requeue_all_files()
        self.assertEqual(self.db.unchanged_paths([(str(path), stat.st_size, stat.st_mtime)]), set())

    def test_file_is_parked_after_max_attempts_and_not_rescanned(self) -> None:
        path = self.root / "bad.pdf"
        path.write_bytes(b"not a pdf")
        self.db.enqueue_dirty(path, "upsert")
        with self.db.connect() as connection:
            connection.execute("UPDATE dirty_queue SET attempts=?", (MAX_DIRTY_ATTEMPTS - 1,))
        self.db.record_dirty_error(path, "corrupt")
        self.assertIsNone(self.db.dirty_record(path))
        row = self.db.get_file(path)
        self.assertEqual((row["status"], row["error"]), ("failed", "corrupt"))
        stat = path.stat()
        self.assertEqual(self.db.unchanged_paths([(str(path), stat.st_size, stat.st_mtime)]), {str(path)})
        self.assertEqual(self.db.search_lexical("pdf"), [])  # failed rows are not searchable
        self.db.enqueue_dirty(path, "upsert")  # a new event retries from scratch
        self.assertEqual(self.db.dirty_record(path)["attempts"], 0)

    def test_parking_keeps_previously_indexed_content_searchable(self) -> None:
        path = self.index("keep.txt", "still searchable text")
        self.db.enqueue_dirty(path, "upsert")
        with self.db.connect() as connection:
            connection.execute("UPDATE dirty_queue SET attempts=?", (MAX_DIRTY_ATTEMPTS - 1,))
        self.db.record_dirty_error(path, "later edit unreadable")
        self.assertEqual(self.db.get_file(path)["status"], "indexed")
        self.assertEqual(len(self.db.search_lexical("searchable")), 1)


class IndexScopeTests(_Base):
    def test_exclusion_by_name_glob_and_path_prefix(self) -> None:
        scope = IndexScope([self.root], ["node_modules"], [str(self.root / "private"), "%UNDEFINED_VAR_X%/y"],
                           ["cache*"])
        self.assertTrue(scope.excluded(self.root / "app" / "node_modules" / "x.js"))
        self.assertTrue(scope.excluded(self.root / "CacheStore" / "x.txt"))
        self.assertTrue(scope.excluded(self.root / "private" / "deep" / "x.txt"))
        self.assertFalse(scope.excluded(self.root / "privateer" / "x.txt"))
        self.assertFalse(scope.excluded(self.root / "docs" / "x.txt"))
        self.assertEqual(len(scope.path_prefixes), 1)  # undefined %VAR% entry ignored

    def test_contains_resolves_candidate_and_prefixes_end_with_separator(self) -> None:
        scope = IndexScope([self.root])
        self.assertTrue(scope.contains(self.root / "a" / ".." / "b.txt"))
        self.assertFalse(scope.contains(self.root.parent / "elsewhere.txt"))
        self.assertTrue(all(p.endswith(os.sep) for p in scope.prefixes()))


class WatcherScanTests(_Base):
    def make(self, **index) -> FileWatcher:
        settings = {"index": {"roots": [str(self.root)], "include_extensions": [".txt"],
                              "exclude_dirs": ["skipme"], "debounce_seconds": 0.01, **index}}
        return FileWatcher(settings, self.db)

    def test_scan_queues_new_files_skips_unchanged_and_excluded(self) -> None:
        unchanged = self.index("same.txt", "already indexed")
        changed = self.index("edited.txt", "old")
        changed.write_text("new longer content", encoding="utf-8")
        new = self.root / "docs" / "fresh.txt"
        new.write_text("fresh", encoding="utf-8")
        hidden = self.root / "skipme" / "hidden.txt"
        hidden.parent.mkdir()
        hidden.write_text("x", encoding="utf-8")
        (self.root / "docs" / "ignored.bin").write_bytes(b"x")
        watcher = self.make()
        watcher._scan_once()
        queued = {Path(row["path"]).name for row in self.db.pending_dirty(100)}
        self.assertEqual(queued, {"edited.txt", "fresh.txt"})
        self.assertEqual(watcher.progress["skipped"], 1)
        self.assertNotIn(str(unchanged), {row["path"] for row in self.db.pending_dirty(100)})

    def test_scan_queues_deletes_for_files_removed_while_offline(self) -> None:
        gone = self.index("gone.txt", "bye")
        gone.unlink()
        self.make()._scan_once()
        rows = {Path(r["path"]).name: r["operation"] for r in self.db.pending_dirty(100)}
        self.assertEqual(rows.get("gone.txt"), "delete")

    def test_missing_root_does_not_wipe_its_entries(self) -> None:
        self.index("kept.txt", "on an unplugged drive")
        watcher = FileWatcher({"index": {"roots": [str(self.root / "unplugged")],
                                         "include_extensions": [".txt"]}}, self.db)
        watcher._scan_once()
        self.assertEqual(self.db.pending_dirty(100), [])

    def test_event_buffer_is_bounded_and_overflow_requests_rescan(self) -> None:
        watcher = self.make(max_pending_events=100)
        watcher.handler.delay = 3600  # keep events pending
        from watchdog.events import FileCreatedEvent
        for index in range(150):
            watcher.handler.on_created(FileCreatedEvent(str(self.root / f"f{index}.txt")))
        self.assertLessEqual(len(watcher.handler._pending), 100)
        self.assertTrue(watcher.handler.overflowed)
        self.assertTrue(watcher._rescan.is_set())
        watcher.handler.close()

    def test_excluded_directory_events_are_ignored_cheaply(self) -> None:
        watcher = self.make()
        from watchdog.events import FileCreatedEvent
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve must not run")):
            watcher.handler.on_created(FileCreatedEvent(str(self.root / "skipme" / "x.txt")))
        watcher.handler.close()


class ParserTests(_Base):
    def test_html_text_excludes_scripts_and_styles(self) -> None:
        page = self.root / "p.html"
        page.write_text("<style>.x{}</style><script>secret()</script><h1>Title</h1><p>Hello <b>world</b></p>",
                        encoding="utf-8")
        text = " ".join(chunk.text for chunk in parse_file(page))
        self.assertIn("Hello world", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(".x{}", text)

    def test_code_and_csv_are_plain_text(self) -> None:
        code = self.root / "main.py"
        code.write_text("def quarterly_report():\n    pass\n", encoding="utf-8")
        self.assertIn("quarterly_report", parse_file(code)[0].text)
        self.assertIn(".csv", SUPPORTED_EXTENSIONS)

    def test_pptx_slides_are_ordered_and_labelled(self) -> None:
        deck = self.root / "d.pptx"
        with zipfile.ZipFile(deck, "w") as archive:
            archive.writestr("ppt/slides/slide10.xml", '<s xmlns:a="a"><a:t>Tenth</a:t></s>')
            archive.writestr("ppt/slides/slide2.xml", '<s xmlns:a="a"><a:t>Second</a:t></s>')
        self.assertEqual([(c.page_or_line, c.text) for c in parse_file(deck)],
                         [("slide 2", "Second"), ("slide 10", "Tenth")])

    def test_xlsx_shared_strings_are_extracted_and_archive_budget_enforced(self) -> None:
        sheet = self.root / "s.xlsx"
        with zipfile.ZipFile(sheet, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/sharedStrings.xml", "<sst xmlns='x'><si><t>Revenue</t></si><si><t>Q3</t></si></sst>")
        self.assertEqual(parse_file(sheet)[0].text, "Revenue Q3")
        with self.assertRaisesRegex(ValueError, "XLSX expands"):
            parse_file(sheet, max_docx_uncompressed_bytes=10)

    def test_corrupt_office_file_raises_value_error(self) -> None:
        bad = self.root / "bad.pptx"
        bad.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "Invalid PPTX"):
            parse_file(bad)


if __name__ == "__main__":
    unittest.main()
