"""Focused tests for persistence, command policy, parsers, routing, and indexing."""
from __future__ import annotations

import tempfile
import asyncio
import unittest
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.fast_router import FastRouter
from agent.bot_runtime import BotRuntime
from executor.cmd_runner import CommandRunner
from executor.policy_engine import PolicyEngine
from retrieval.index_worker import IndexQueueService, IndexWorker
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

    def test_stale_index_ack_does_not_remove_newer_event(self) -> None:
        path = self.root / "changing.txt"
        self.db.enqueue_dirty(path, "upsert")
        stale_revision = float(self.db.pending_dirty()[0]["queued_at"])
        self.db.enqueue_dirty(path, "delete")
        self.db.remove_dirty(path, stale_revision)
        self.db.record_dirty_error(path, "stale parse error", stale_revision)
        latest = self.db.pending_dirty()[0]
        self.assertEqual(latest["operation"], "delete")
        self.assertEqual(latest["attempts"], 0)
        self.assertGreater(float(latest["queued_at"]), stale_revision)

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

    def test_command_is_rechecked_against_current_policy_after_approval(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            policy_path = Path(directory) / "cmd_policy.yaml"
            policy_path.write_text("version: 1\ndefault: blocked\nblocked: []\nread_only: []\ngated: []\n", encoding="utf-8")
            proposal = self.db.stage_command("mkdir new-folder", str(self.root), "create folder", "gated")
            self.assertTrue(self.db.decide_command(proposal, True))
            with patch("executor.cmd_runner.subprocess.run") as run:
                code, _, error = CommandRunner(self.db, policy_path=policy_path).execute(proposal)
            self.assertEqual(code, 126)
            self.assertIn("current policy", error)
            run.assert_not_called()
            self.assertEqual(self.db.command_record(proposal)["status"], "failed")

    def test_bot_runtime_reuses_one_asyncio_loop_until_close(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.loops = []

            async def run(self, prompt, **kwargs):
                self.loops.append(asyncio.get_running_loop())
                return SimpleNamespace(content=prompt)

            async def aclose(self):
                self.loops.append(asyncio.get_running_loop())

        bot = FakeBot()
        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        runtime.bot = bot
        runtime._start_async_loop()
        try:
            self.assertEqual(runtime.ask("Explain local indexing"), "Explain local indexing")
            self.assertEqual(runtime.ask("Explain retrieval fusion"), "Explain retrieval fusion")
        finally:
            runtime._model_used = False
            runtime.close()
        self.assertEqual(len(bot.loops), 3)
        self.assertTrue(all(loop is bot.loops[0] for loop in bot.loops))

    def test_cancel_pending_unblocks_a_running_model_turn(self) -> None:
        class WaitingBot:
            async def run(self, prompt, **kwargs):
                await asyncio.Event().wait()

            async def aclose(self):
                return None

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        runtime.bot = WaitingBot()
        runtime._start_async_loop()
        errors: list[Exception] = []

        def ask() -> None:
            try:
                runtime.ask("Explain local indexing")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=ask)
        worker.start()
        deadline = time.monotonic() + 2
        while runtime._active_future is None and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self.assertTrue(runtime.cancel_pending())
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
        finally:
            runtime._model_used = False
            runtime.close()


class PolicyAndParserTests(unittest.TestCase):
    def test_default_deny_policy_classifies_commands(self) -> None:
        policy = PolicyEngine(ROOT / "config" / "cmd_policy.yaml")
        self.assertEqual(policy.classify("whoami"), "read_only")
        self.assertEqual(policy.classify("mkdir new-folder"), "gated")
        self.assertEqual(policy.classify("format D:"), "blocked")
        self.assertEqual(policy.classify("dir & whoami"), "blocked")
        self.assertEqual(policy.classify(r"mkdir %TEMP%\FileBot"), "blocked")
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

    def test_engine_failure_keeps_previous_database_index_and_dirty_item(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            file_path = root / "note.txt"
            file_path.write_text("Updated content", encoding="utf-8")
            db = Database(root / "state.sqlite3")
            db.initialize()
            db.replace_file(file_path, content_hash="old-hash", size=12, mtime=1.0,
                            chunks=[{"text": "Original content"}])
            db.enqueue_dirty(file_path, "upsert")

            class FailingEngine:
                def upsert(self, path, chunks, *, refresh_lexical=True) -> None:
                    raise RuntimeError("vector store unavailable")

                def delete(self, path, *, refresh_lexical=True) -> None:
                    raise AssertionError("unexpected delete")

                def refresh_lexical_index(self) -> None:
                    raise AssertionError("failed update should not refresh")

            with self.assertLogs("retrieval.index_worker", level="ERROR"):
                IndexWorker({"index": {"max_file_bytes": 10000, "chunk_chars": 1200,
                                        "chunk_overlap": 150}}, db, FailingEngine()).drain()
            self.assertEqual(db.get_file(file_path)["content_hash"], "old-hash")
            self.assertEqual(db.pending_dirty()[0]["attempts"], 1)

    def test_background_queue_service_processes_updates_and_stops(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            file_path = root / "note.txt"
            file_path.write_text("Background indexing", encoding="utf-8")
            db = Database(root / "state.sqlite3")
            db.initialize()
            db.enqueue_dirty(file_path, "upsert")
            completed = threading.Event()

            class FakeEngine:
                def upsert(self, path, chunks, *, refresh_lexical=True) -> None:
                    completed.set()

                def delete(self, path, *, refresh_lexical=True) -> None:
                    raise AssertionError("unexpected delete")

                def refresh_lexical_index(self) -> None:
                    pass

            worker = IndexWorker({"index": {"max_file_bytes": 10000, "chunk_chars": 1200,
                                             "chunk_overlap": 150}}, db, FakeEngine())
            service = IndexQueueService(worker, batch_size=1, poll_seconds=0.02)
            service.start()
            self.assertTrue(completed.wait(timeout=2))
            service.stop()
            self.assertEqual(db.pending_dirty(), [])
            self.assertEqual(db.get_file(file_path)["status"], "indexed")


if __name__ == "__main__":
    unittest.main()

