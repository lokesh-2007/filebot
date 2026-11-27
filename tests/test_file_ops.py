"""File-open scope checks for configured roots and the active index."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.file_ops_tool import FileOpsTool
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class FileOpsTests(unittest.TestCase):
    def test_reads_bounded_content_only_from_indexed_files_inside_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            root = base / "allowed"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            indexed = root / "notes.txt"
            external = outside / "private.txt"
            unindexed = root / "unindexed.txt"
            indexed.write_text("indexed file", encoding="utf-8")
            external.write_text("external private file", encoding="utf-8")
            unindexed.write_text("not in the index", encoding="utf-8")
            db = Database(base / "state.sqlite3")
            db.initialize()
            for path, text in ((indexed, "A" * 5000), (external, "External content")):
                db.replace_file(path, content_hash=path.name, size=len(text), mtime=1.0,
                                chunks=[{"text": text, "page_or_line": "line 1"}])

            tool = FileOpsTool(db, roots=[str(root)])
            content = tool.read_file(str(indexed), max_chars=200)

            self.assertIn(str(indexed.resolve()), content)
            self.assertIn("[line 1]", content)
            self.assertEqual(content.split("[line 1]\n", 1)[1].split("\n\n", 1)[0], "A" * 200)
            self.assertIn("Excerpt shortened", content)
            with self.assertRaisesRegex(PermissionError, "outside FileBot"):
                tool.read_file(str(external))
            with self.assertRaisesRegex(PermissionError, "current local index"):
                tool.read_file(str(unindexed))

    def test_opens_only_existing_indexed_files_inside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory) / "allowed"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            indexed = root / "notes.txt"
            external = outside / "private.txt"
            unindexed = root / "unindexed.txt"
            indexed.write_text("indexed", encoding="utf-8")
            external.write_text("external", encoding="utf-8")
            unindexed.write_text("not indexed", encoding="utf-8")

            class FakeDatabase:
                def get_file(self, path):
                    return {"path": str(indexed)} if Path(path) == indexed.resolve() else None

            tool = FileOpsTool(FakeDatabase(), roots=[str(root)])
            with patch("agent.tools.file_ops_tool.os.startfile", create=True) as startfile:
                tool.open_file(str(indexed))
                startfile.assert_called_once_with(str(indexed.resolve()))
                with self.assertRaisesRegex(PermissionError, "outside FileBot"):
                    tool.open_file(str(external))
                with self.assertRaisesRegex(PermissionError, "not in FileBot's local index"):
                    tool.open_file(str(unindexed))
                startfile.assert_called_once()


if __name__ == "__main__":
    unittest.main()
