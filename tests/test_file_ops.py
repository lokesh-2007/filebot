"""File access checks for configured roots and the active index."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.file_ops_tool import FileOpsTool
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class FileOpsTests(unittest.TestCase):
    def test_create_is_root_scoped_text_only_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            root, outside = base / "allowed", base / "outside"
            root.mkdir()
            outside.mkdir()
            db = Database(base / "state.sqlite3")
            db.initialize()
            tool = FileOpsTool(db, roots=[str(root)])

            created = Path(tool.create_file("notes.md", "New local note"))
            self.assertEqual(created.read_text(encoding="utf-8"), "New local note")
            self.assertEqual(db.pending_dirty()[0]["path"], str(created.resolve()))
            with self.assertRaisesRegex(FileExistsError, "will not overwrite"):
                tool.create_file("notes.md", "replacement")
            with self.assertRaisesRegex(PermissionError, "outside FileBot"):
                tool.create_file(str(outside / "private.txt"), "private")
            with self.assertRaisesRegex(ValueError, "text extension"):
                tool.create_file("run.ps1", "Write-Host unsafe")

    def test_delete_requires_one_time_code_and_rechecks_the_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            root = base / "allowed"
            root.mkdir()
            target = root / "note.txt"
            target.write_text("keep unless confirmed", encoding="utf-8")
            db = Database(base / "state.sqlite3")
            db.initialize()
            tool = FileOpsTool(db, roots=[str(root)])

            token = tool.stage_delete(str(target))
            self.assertTrue(target.exists())
            row = db.claim_file_delete(token)
            self.assertIsNotNone(row)
            moved: list[Path] = []
            with patch("agent.tools.file_ops_tool._move_file_to_recycle_bin", side_effect=moved.append):
                tool.delete_to_recycle_bin(
                    str(target), expected_size=row["file_size"], expected_mtime_ns=row["file_mtime_ns"],
                )
            db.finish_file_delete(token)
            self.assertEqual(moved, [target.resolve()])
            self.assertTrue(target.exists())  # the test stub models a successful OS recycle-bin call
            self.assertIsNone(db.claim_file_delete(token))

            changed = root / "changed.txt"
            changed.write_text("original", encoding="utf-8")
            changed_token = tool.stage_delete(str(changed))
            expected = db.claim_file_delete(changed_token)
            changed.write_text("changed since confirmation", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after confirmation"):
                tool.delete_to_recycle_bin(
                    str(changed), expected_size=expected["file_size"],
                    expected_mtime_ns=expected["file_mtime_ns"],
                )
            self.assertTrue(changed.exists())

    def test_summary_can_read_bounded_unindexed_file_inside_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            root, outside = base / "allowed", base / "outside"
            root.mkdir()
            outside.mkdir()
            file = root / "fresh.txt"
            file.write_text("A fresh document with release notes.", encoding="utf-8")
            private_file = outside / "private.txt"
            private_file.write_text("not in roots", encoding="utf-8")
            tool = FileOpsTool(db=Database(base / "unused.sqlite3"), roots=[str(root)])

            excerpt = tool.read_file_for_summary(str(file), max_chars=200)
            self.assertIn(str(file.resolve()), excerpt)
            self.assertIn("release notes", excerpt)
            with self.assertRaisesRegex(PermissionError, "outside FileBot"):
                tool.read_file_for_summary(str(private_file))

    def test_lists_only_configured_folders_and_bounds_the_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            root = base / "allowed"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "Reports").mkdir()
            for index in range(4):
                (root / f"note-{index}.txt").write_text("local", encoding="utf-8")
            (outside / "private.txt").write_text("private", encoding="utf-8")

            tool = FileOpsTool(db=None, roots=[str(root)])
            result = tool.list_directory(str(root), limit=2)

            self.assertIn("Reports/", result)
            self.assertIn("additional items omitted", result)
            self.assertNotIn("private.txt", result)
            with self.assertRaisesRegex(PermissionError, "outside FileBot"):
                tool.list_directory(str(outside))

    def test_open_directory_resolves_named_child_inside_configured_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            documents = Path(directory) / "Documents"
            project = documents / "ChatGPT"
            project.mkdir(parents=True)
            tool = FileOpsTool(db=None, roots=[str(documents)])

            with patch("agent.tools.file_ops_tool.os.startfile") as startfile:
                result = tool.open_directory("ChatGPT")

            self.assertEqual(result, str(project.resolve()))
            startfile.assert_called_once_with(str(project.resolve()))

    def test_open_and_list_directory_match_spaced_names_at_bounded_depth(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            documents = Path(directory) / "Documents"
            project = documents / "ChatGPT" / "file bot"
            project.mkdir(parents=True)
            (project / "README.md").write_text("local", encoding="utf-8")
            tool = FileOpsTool(db=None, roots=[str(documents)])

            with patch("agent.tools.file_ops_tool.os.startfile") as startfile:
                self.assertEqual(tool.open_directory("chat gpt"), str((documents / "ChatGPT").resolve()))
                self.assertEqual(tool.open_directory("file bot"), str(project.resolve()))
            self.assertIn("Folder:", tool.list_directory("chat gpt"))
            startfile.assert_any_call(str((documents / "ChatGPT").resolve()))
            startfile.assert_any_call(str(project.resolve()))

    def test_fuzzy_folder_resolution_reports_ambiguity_without_opening(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            documents = Path(directory) / "Documents"
            (documents / "ChatGPT").mkdir(parents=True)
            (documents / "ChatGPT!").mkdir()
            tool = FileOpsTool(db=None, roots=[str(documents)])

            with patch("agent.tools.file_ops_tool.os.startfile") as startfile:
                with self.assertRaisesRegex(ValueError, "More than one configured folder"):
                    tool.open_directory("chat gpt")
                startfile.assert_not_called()

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
