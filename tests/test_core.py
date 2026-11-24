"""Focused tests for persistence, command policy, parsers, routing, and indexing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.fast_router import FastRouter
from executor.cmd_runner import CommandRunner
from executor.policy_engine import PolicyEngine
from retrieval.index_worker import IndexWorker
from retrieval.parsers import parse_file
from storage.db import Database


ROOT = Path(__file__).resolve().parents[1]


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state.sqlite3")
        self.db.initialize()

    def test_dirty_queue_upsert_delete_and_retry(self) -> None:
        path = self.root / "notes.txt"
        self.db.enqueue_dirty(path, "upsert")
        self.assertEqual(self.db.pending_dirty()[0]["operation"], "upsert")
        self.db.record_dirty_error(path, "temporary parse issue")
        self.assertEqual(self.db.pending_dirty()[0]["attempts"], 1)
        self.db.enqueue_dirty(path, "delete")
        row = self.db.pending_dirty()[0]
        self.assertEqual(row["operation"], "delete")
        self.assertEqual(row["attempts"], 0)
        self.db.remove_dirty(path)
        self.assertEqual(self.db.pending_dirty(), [])

    def test_file_records_and_lexical_search(self) -> None:
        path = self.root / "project-notes.md"
        text = "Quarterly plan: replace the old storage engine."
        chunks = [{"text": text, "heading": "Plan", "page_or_line": "line 1"}]
        self.db.replace_file(path, content_hash="hash", size=len(text), mtime=1.0, chunks=chunks)
        self.assertEqual(self.db.get_file(path)["status"], "indexed")
        result = self.db.search_lexical("storage engine")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], str(path.resolve()))

    def test_command_claim_is_single_use(self) -> None:
        proposal = self.db.stage_command("whoami", str(self.root), "test", "read_only")
        self.assertTrue(self.db.decide_command(proposal, True))
        fake_result = SimpleNamespace(returncode=0, stdout="tester\n", stderr="")
        with patch("executor.cmd_runner.subprocess.run", return_value=fake_result) as run:
            code, stdout, _ = CommandRunner(self.db).execute(proposal)
            self.assertEqual((code, stdout), (0, "tester\n"))
            with self.assertRaises(PermissionError):
                CommandRunner(self.db).execute(proposal)
        run.assert_called_once()
        row = self.db.command_record(proposal)
        self.assertEqual(row["status"], "executed")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["stdout_tail"], "tester\n")


class PolicyAndParserTests(unittest.TestCase):
    def test_default_deny_policy_classifies_commands(self) -> None:
        policy = PolicyEngine(ROOT / "config" / "cmd_policy.yaml")
        self.assertEqual(policy.classify("whoami"), "read_only")
        self.assertEqual(policy.classify("mkdir new-folder"), "gated")
        self.assertEqual(policy.classify("format D:"), "blocked")
        self.assertEqual(policy.classify("dir & whoami"), "blocked")
        self.assertEqual(policy.classify("unknown-command"), "blocked")

    def test_text_chunking_preserves_overlap(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "long.txt"
            path.write_text("".join(chr(65 + i % 26) for i in range(250)), encoding="utf-8")
            chunks = parse_file(path, chunk_chars=100, overlap=20)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0].text[-20:], chunks[1].text[:20])
        self.assertEqual(chunks[1].text[-20:], chunks[2].text[:20])

    def test_fast_router_prioritizes_command_gate(self) -> None:
        router = FastRouter()
        self.assertEqual(router.route("Find and run a command"), "cmd")
        self.assertEqual(router.route("Find the meeting notes"), "search")
        self.assertEqual(router.route("Explain this concept"), "chat")


class IndexWorkerTests(unittest.TestCase):
    def test_worker_indexes_text_and_refreshes_lexical_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            file_path = root / "note.txt"
            file_path.write_text("Local search content", encoding="utf-8")
            db = Database(root / "state.sqlite3")
            db.initialize()
            db.enqueue_dirty(file_path, "upsert")

            class FakeEngine:
                def __init__(self) -> None:
                    self.upserts = []
                    self.refreshes = 0

                def upsert(self, path, chunks, *, refresh_lexical=True) -> None:
                    self.upserts.append((path, chunks, refresh_lexical))

                def delete(self, path, *, refresh_lexical=True) -> None:
                    raise AssertionError("unexpected delete")

                def refresh_lexical_index(self) -> None:
                    self.refreshes += 1

            engine = FakeEngine()
            settings = {"index": {"max_file_bytes": 10000, "chunk_chars": 1200, "chunk_overlap": 150}}
            processed = IndexWorker(settings, db, engine).drain()
            self.assertEqual(processed, 1)
            self.assertEqual(len(engine.upserts), 1)
            self.assertFalse(engine.upserts[0][2])
            self.assertEqual(engine.refreshes, 1)
            self.assertEqual(db.pending_dirty(), [])
            self.assertEqual(db.get_file(file_path)["status"], "indexed")


if __name__ == "__main__":
    unittest.main()
