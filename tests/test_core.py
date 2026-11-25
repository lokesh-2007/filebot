"""Focused tests for persistence, command policy, parsers, routing, and indexing."""
from __future__ import annotations

import tempfile
import asyncio
from io import BytesIO
import os
import sqlite3
import subprocess
import unittest
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.fast_router import FastRouter
from agent.bot_runtime import BotRuntime, _ensure_filebot_instructions
from executor.cmd_runner import CommandRunner, _read_output_tail
from executor.policy_engine import PolicyEngine
from retrieval.index_worker import IndexQueueService, IndexWorker
from retrieval.parsers import parse_file
from storage.db import Database


ROOT = Path(__file__).resolve().parents[1]


class _FakeCommandProcess:
    def __init__(self, stdout: bytes = b"tester\n", stderr: bytes = b"") -> None:
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        raise AssertionError("successful fake process should not be killed")


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
        deferred = self.db.dirty_record(path)
        self.assertEqual(deferred["attempts"], 1)
        self.assertIn("temporary parse issue", deferred["last_error"])
        self.assertGreater(float(deferred["available_at"]), float(deferred["queued_at"]))
        self.assertEqual(self.db.pending_dirty(), [])
        self.db.enqueue_dirty(path, "delete")
        row = self.db.pending_dirty()[0]
        self.assertEqual(row["operation"], "delete")
        self.assertEqual(row["attempts"], 0)
        self.db.remove_dirty(path)
        self.assertEqual(self.db.pending_dirty(), [])

    def test_deferred_retry_does_not_block_newer_healthy_queue_items(self) -> None:
        failing = self.root / "failing.pdf"
        healthy = self.root / "healthy.txt"
        self.db.enqueue_dirty(failing, "upsert")
        self.db.record_dirty_error(failing, "corrupt document")
        self.db.enqueue_dirty(healthy, "upsert")

        ready = self.db.pending_dirty()
        self.assertEqual([row["path"] for row in ready], [str(healthy.resolve())])

    def test_dirty_retry_backoff_is_capped_at_one_hour(self) -> None:
        path = self.root / "persistent-error.pdf"
        self.db.enqueue_dirty(path, "upsert")
        with self.db.connect() as connection:
            connection.execute("UPDATE dirty_queue SET attempts=10 WHERE path=?", (str(path.resolve()),))
        self.db.record_dirty_error(path, "still invalid")
        row = self.db.dirty_record(path)

        delay = float(row["available_at"]) - time.time()
        self.assertGreater(delay, 3590)
        self.assertLessEqual(delay, 3600)

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

    def test_initialize_migrates_legacy_dirty_queue_without_losing_rows(self) -> None:
        legacy_path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("""CREATE TABLE dirty_queue (
                path TEXT PRIMARY KEY, event_type TEXT NOT NULL, queued_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)""")
            connection.execute(
                "INSERT INTO dirty_queue(path,event_type,queued_at,attempts,last_error) VALUES(?,?,?,?,?)",
                (str((self.root / "old.txt").resolve()), "upsert", 1.0, 2, "previous error"),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = Database(legacy_path)
        migrated.initialize()

        with migrated.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(dirty_queue)")}
        self.assertIn("available_at", columns)
        row = migrated.dirty_record(self.root / "old.txt")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["last_error"], "previous error")
        self.assertEqual(migrated.pending_dirty()[0]["path"], str((self.root / "old.txt").resolve()))

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
        with patch("executor.cmd_runner.subprocess.Popen", return_value=_FakeCommandProcess()) as run:
            code, stdout, _ = CommandRunner(self.db).execute(proposal)
            self.assertEqual((code, stdout), (0, "tester\n"))
            with self.assertRaises(PermissionError):
                CommandRunner(self.db).execute(proposal)
        run.assert_called_once()
        system32 = Path(os.environ["SystemRoot"]) / "System32"
        self.assertEqual(Path(run.call_args.args[0][0]), system32 / "cmd.exe")
        row = self.db.command_record(proposal)
        self.assertEqual(row["status"], "executed")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["stdout_tail"], "tester\n")

    def test_crash_recovery_requires_reapproval_and_never_replays_claimed_command(self) -> None:
        approved_not_claimed = self.db.stage_command(
            "mkdir not-started", str(self.root), "create a folder", "gated",
        )
        self.assertTrue(self.db.decide_command(approved_not_claimed, True))
        claimed = self.db.stage_command("mkdir possibly-partial", str(self.root), "create another folder", "gated")
        self.assertTrue(self.db.decide_command(claimed, True))
        self.assertIsNotNone(self.db.claim_approved_command(claimed))

        self.assertEqual(self.db.recover_command_states(), (1, 1))
        pending = self.db.command_record(approved_not_claimed)
        interrupted = self.db.command_record(claimed)
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["approved_at"])
        self.assertEqual(interrupted["status"], "failed")
        self.assertEqual(interrupted["exit_code"], 125)
        self.assertIn("outcome may be partial", interrupted["error"])
        self.assertIsNone(self.db.claim_approved_command(claimed))
        self.assertEqual(self.db.recover_command_states(), (0, 0))

    def test_command_timeout_kills_process_and_records_timeout(self) -> None:
        class HangingProcess(_FakeCommandProcess):
            def __init__(self) -> None:
                super().__init__(stdout=b"partial output")
                self.wait_calls = 0
                self.killed = False

            def wait(self, timeout=None) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("cmd", timeout)
                return 0

            def kill(self) -> None:
                self.killed = True

        proposal = self.db.stage_command("whoami", str(self.root), "test timeout", "read_only")
        self.assertTrue(self.db.decide_command(proposal, True))
        process = HangingProcess()
        with patch("executor.cmd_runner.subprocess.Popen", return_value=process):
            code, stdout, stderr = CommandRunner(self.db, timeout=1).execute(proposal)

        self.assertTrue(process.killed)
        self.assertEqual(code, 124)
        self.assertEqual(stdout, "partial output")
        self.assertIn("timed out after 1s", stderr)
        audit = self.db.command_record(proposal)
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["exit_code"], 124)

    def test_command_is_rechecked_against_current_policy_after_approval(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            policy_path = Path(directory) / "cmd_policy.yaml"
            policy_path.write_text("version: 1\ndefault: blocked\nblocked: []\nread_only: []\ngated: []\n", encoding="utf-8")
            proposal = self.db.stage_command("mkdir new-folder", str(self.root), "create folder", "gated")
            self.assertTrue(self.db.decide_command(proposal, True))
            with patch("executor.cmd_runner.subprocess.Popen") as run:
                code, _, error = CommandRunner(self.db, policy_path=policy_path).execute(proposal)
            self.assertEqual(code, 126)
            self.assertIn("current policy", error)
            run.assert_not_called()
            self.assertEqual(self.db.command_record(proposal)["status"], "failed")

    def test_command_runner_rejects_approved_multiline_command(self) -> None:
        command = "whoami\r\ndel important.txt"
        proposal = self.db.stage_command(command, str(self.root), "malformed stored proposal", "read_only")
        self.assertTrue(self.db.decide_command(proposal, True))
        with patch("executor.cmd_runner.subprocess.Popen") as process:
            code, _, error = CommandRunner(self.db).execute(proposal)

        self.assertEqual(code, 126)
        self.assertIn("no longer allowed", error)
        process.assert_not_called()
        audit = self.db.command_record(proposal)
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["exit_code"], 126)

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

        runtime = BotRuntime({"index": {}, "models": {"ollama": {
            "base_url": "http://127.0.0.1:11434", "model": "test-model",
        }}}, self.db, FakeEngine())
        runtime.bot = bot
        runtime._start_async_loop()
        try:
            with patch.object(runtime, "_check_ollama_ready") as check_ollama:
                self.assertEqual(runtime.ask("Explain local indexing"), "Explain local indexing")
                self.assertEqual(runtime.ask("Explain retrieval fusion"), "Explain retrieval fusion")
                self.assertEqual(check_ollama.call_count, 2)
        finally:
            runtime._model_used = False
            runtime.close()
        self.assertEqual(len(bot.loops), 3)
        self.assertTrue(all(loop is bot.loops[0] for loop in bot.loops))

    def test_plain_chat_does_not_wait_for_background_file_indexing(self) -> None:
        class FakeBot:
            async def run(self, prompt, **kwargs):
                return SimpleNamespace(content="Hello")

            async def aclose(self):
                return None

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        runtime.bot = FakeBot()
        runtime._start_async_loop()
        try:
            with patch.object(runtime.indexer, "drain") as drain:
                self.assertEqual(runtime.ask("Hello, how are you?"), "Hello")
            drain.assert_not_called()
        finally:
            runtime._model_used = False
            runtime.close()

    def test_bot_runtime_scopes_tools_by_intent_and_restores_full_registry(self) -> None:
        class Registry:
            def __init__(self, names) -> None:
                self.tool_names = list(names)

        class FakeLoop:
            def __init__(self) -> None:
                self.tools = Registry(["search_files", "open_file", "propose_cmd"])

        class FakeBot:
            def __init__(self) -> None:
                self._loop = FakeLoop()
                self.seen_tools = []

            async def run(self, prompt, **kwargs):
                self.seen_tools.append(list(self._loop.tools.tool_names))
                return SimpleNamespace(content="Hello")

            async def aclose(self):
                return None

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        bot = FakeBot()
        runtime.bot = bot
        full_registry = bot._loop.tools
        runtime._tool_registries = {
            "chat": Registry([]),
            "search": Registry(["search_files"]),
            "open": Registry(["open_file"]),
            "cmd": Registry(["propose_cmd"]),
        }
        runtime._start_async_loop()
        try:
            self.assertEqual(runtime.ask("Say hello"), "Hello")
            self.assertEqual(runtime.ask("Find the meeting notes"), "Hello")
            self.assertEqual(runtime.ask("Open the PDF document"), "Hello")
            self.assertEqual(runtime.ask("Run command whoami"), "Hello")
        finally:
            runtime._model_used = False
            runtime.close()

        self.assertEqual(bot.seen_tools, [
            [], ["search_files"], ["open_file"], ["propose_cmd"],
        ])
        self.assertIs(bot._loop.tools, full_registry)

    def test_ollama_preflight_accepts_the_configured_installed_model(self) -> None:
        runtime = BotRuntime({"models": {"ollama": {
            "base_url": "http://127.0.0.1:11434", "model": "qwen2.5-coder:3b",
        }}}, self.db, object())
        response = BytesIO(b'{"models":[{"name":"qwen2.5-coder:3b"}]}')
        with patch("urllib.request.urlopen", return_value=response) as request:
            runtime._check_ollama_ready()

        request.assert_called_once_with("http://127.0.0.1:11434/api/tags", timeout=4)

    def test_ollama_preflight_explains_missing_model_and_service(self) -> None:
        runtime = BotRuntime({"models": {"ollama": {
            "base_url": "http://127.0.0.1:11434", "model": "qwen2.5-coder:3b",
        }}}, self.db, object())
        with patch("urllib.request.urlopen", return_value=BytesIO(b'{"models":[]}')):
            with self.assertRaisesRegex(RuntimeError, "ollama pull qwen2.5-coder:3b"):
                runtime._check_ollama_ready()

        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
            with self.assertRaisesRegex(RuntimeError, "Start Ollama and try again"):
                runtime._check_ollama_ready()

    def test_bot_runtime_streams_text_deltas_and_returns_final_result(self) -> None:
        class StreamingBot:
            async def stream(self, prompt, **kwargs):
                yield SimpleNamespace(type="run.started", delta="")
                yield SimpleNamespace(type="text.delta", delta="Hello ")
                yield SimpleNamespace(type="text.delta", delta="from FileBot")
                yield SimpleNamespace(
                    type="run.completed",
                    result=SimpleNamespace(content="Hello from FileBot", error=None),
                )

            async def aclose(self):
                return None

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        runtime.bot = StreamingBot()
        runtime._start_async_loop()
        chunks: list[str] = []
        try:
            result = runtime.ask_streaming("Hello", chunks.append)
        finally:
            runtime._model_used = False
            runtime.close()

        self.assertEqual(chunks, ["Hello ", "from FileBot"])
        self.assertEqual(result, "Hello from FileBot")

    def test_bot_runtime_replaces_pseudo_tool_json_with_clear_capability_message(self) -> None:
        class FakeBot:
            async def run(self, prompt, **kwargs):
                return SimpleNamespace(
                    content='```json\n{"name":"open_file","arguments":{"path":"new.html"}}\n```',
                    tools_used=[], error=None, stop_reason="completed",
                )

            async def aclose(self):
                return None

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime({"index": {}}, self.db, FakeEngine())
        runtime.bot = FakeBot()
        runtime._start_async_loop()
        try:
            answer = runtime.ask("Create a new HTML file")
        finally:
            runtime._model_used = False
            runtime.close()

        self.assertIn("did not run that action", answer)
        self.assertIn("File creation and editing are not supported", answer)
        self.assertNotIn('"name"', answer)

    def test_filebot_workspace_instructions_are_created_once_and_user_owned(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            workspace = Path(directory)
            _ensure_filebot_instructions(workspace)
            instructions = (workspace / "SOUL.md").read_text(encoding="utf-8")
            self.assertIn("Do not pretend to create", instructions)
            self.assertIn("propose_cmd", instructions)
            (workspace / "SOUL.md").write_text("User customized instructions", encoding="utf-8")
            _ensure_filebot_instructions(workspace)
            self.assertEqual((workspace / "SOUL.md").read_text(encoding="utf-8"),
                             "User customized instructions")

    def test_bot_runtime_closes_ollama_client_after_idle_unload(self) -> None:
        class FakeOllamaClient:
            def __init__(self) -> None:
                self.generate_args = None
                self.closed = False

            def generate(self, **kwargs):
                self.generate_args = kwargs

            def close(self):
                self.closed = True

        class FakeEngine:
            def search(self, query, limit=10):
                return []

        runtime = BotRuntime(
            {"models": {"ollama": {"base_url": "http://127.0.0.1:11434",
                                     "model": "qwen2.5-coder:3b", "keep_alive": "2m"}}},
            self.db, FakeEngine(),
        )
        runtime._model_used = True
        client = FakeOllamaClient()
        fake_ollama = SimpleNamespace(Client=lambda **kwargs: client)
        with patch.dict("sys.modules", {"ollama": fake_ollama}):
            runtime.close()

        self.assertTrue(client.closed)
        self.assertEqual(client.generate_args["prompt"], "")
        self.assertEqual(client.generate_args["keep_alive"], "2m")
        self.assertFalse(runtime._model_used)

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
    def test_command_output_capture_keeps_only_a_bounded_tail(self) -> None:
        payload = b"a" * 20000 + b"last-bytes"
        tail = _read_output_tail(BytesIO(payload), limit=1024)
        self.assertEqual(len(tail), 1024)
        self.assertEqual(tail[-10:], b"last-bytes")

    def test_default_deny_policy_classifies_commands(self) -> None:
        policy = PolicyEngine(ROOT / "config" / "cmd_policy.yaml")
        self.assertEqual(policy.classify("whoami"), "read_only")
        self.assertEqual(policy.classify("mkdir new-folder"), "gated")
        self.assertEqual(policy.classify("mkdir " + "x" * 4096), "blocked")
        self.assertEqual(policy.classify(" " * 4091 + "whoami"), "blocked")
        self.assertEqual(policy.classify("format D:"), "blocked")
        self.assertEqual(policy.classify("dir & whoami"), "blocked")
        self.assertEqual(policy.classify("whoami\r\ndel important.txt"), "blocked")
        self.assertEqual(policy.classify("where\nwhoami"), "blocked")
        self.assertEqual(policy.classify("mkdir folder\nformat D:"), "blocked")
        self.assertEqual(policy.classify("whoami\x7f"), "blocked")
        self.assertEqual(policy.classify(r"mkdir %TEMP%\FileBot"), "blocked")
        self.assertEqual(policy.classify("whoami.exe"), "blocked")
        self.assertEqual(policy.classify("mkdir.exe new-folder"), "blocked")
        self.assertEqual(policy.classify("unknown-command"), "blocked")

    def test_policy_rejects_non_blocked_default_and_malformed_patterns(self) -> None:
        invalid_policies = (
            "version: 1\ndefault: read_only\nread_only: []\ngated: []\nblocked: []\n",
            "version: 1\ndefault: blocked\nread_only: whoami\ngated: []\nblocked: []\n",
            "version: 1\ndefault: blocked\nread_only: ['[']\ngated: []\nblocked: []\n",
        )
        for contents in invalid_policies:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
                policy_path = Path(directory) / "cmd_policy.yaml"
                policy_path.write_text(contents, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    PolicyEngine(policy_path)

    def test_read_only_windows_utility_uses_system32_working_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            db = Database(Path(directory) / "state.sqlite3")
            db.initialize()
            proposal = db.stage_command("whoami", directory, "show current account", "read_only")
            self.assertTrue(db.decide_command(proposal, True))
            with patch("executor.cmd_runner.subprocess.Popen", return_value=_FakeCommandProcess()) as run:
                CommandRunner(db).execute(proposal)
            system32 = Path(os.environ["SystemRoot"]) / "System32"
            self.assertEqual(Path(run.call_args.kwargs["cwd"]), system32)

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
        self.assertEqual(router.route("Open the PDF document"), "open")
        self.assertEqual(router.route("Explain this concept"), "chat")


class IndexWorkerTests(unittest.TestCase):
    def test_worker_requeues_a_file_changed_during_parsing(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            root = Path(directory)
            file_path = root / "changing.txt"
            file_path.write_text("before", encoding="utf-8")
            db = Database(root / "state.sqlite3")
            db.initialize()
            db.enqueue_dirty(file_path, "upsert")

            class FakeEngine:
                def __init__(self) -> None:
                    self.upserts = []

                def upsert(self, path, chunks, *, content_hash, refresh_lexical=True) -> None:
                    self.upserts.append((content_hash, [chunk.text for chunk in chunks]))

                def delete_obsolete(self, path, keep_ids) -> None:
                    pass

                def delete(self, path, *, refresh_lexical=True) -> None:
                    pass

                def refresh_lexical_index(self) -> None:
                    pass

            def mutate_while_parsing(path, *args, **kwargs):
                path.write_text("new revision after save", encoding="utf-8")
                return [SimpleNamespace(text="before", page_or_line=None, heading=None)]

            engine = FakeEngine()
            worker = IndexWorker({"index": {
                "max_file_bytes": 10000, "chunk_chars": 1200, "chunk_overlap": 150,
            }}, db, engine)
            with patch("retrieval.index_worker.parse_file", side_effect=mutate_while_parsing):
                self.assertEqual(worker.drain(), 0)

            self.assertEqual(engine.upserts, [])
            self.assertEqual(len(db.pending_dirty()), 1)
            self.assertIsNone(db.get_file(file_path))

            self.assertEqual(worker.drain(), 1)
            self.assertEqual(engine.upserts[0][1], ["new revision after save"])

    def test_worker_discards_persisted_upsert_outside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            base = Path(directory)
            allowed, outside = base / "allowed", base / "outside"
            allowed.mkdir()
            outside.mkdir()
            file_path = outside / "private.txt"
            file_path.write_text("must not be read", encoding="utf-8")
            db = Database(base / "state.sqlite3")
            db.initialize()
            db.enqueue_dirty(file_path, "upsert")

            class FakeEngine:
                def upsert(self, *args, **kwargs) -> None:
                    raise AssertionError("out-of-scope paths must not reach embeddings")

            worker = IndexWorker({"index": {
                "roots": [str(allowed)], "max_file_bytes": 10000,
                "chunk_chars": 1200, "chunk_overlap": 150,
            }}, db, FakeEngine())
            with self.assertLogs("retrieval.index_worker", level="WARNING"):
                self.assertEqual(worker.drain(), 1)
            self.assertEqual(db.pending_dirty(), [])
            self.assertIsNone(db.get_file(file_path))

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

                def upsert(self, path, chunks, *, content_hash, refresh_lexical=True) -> None:
                    self.upserts.append((path, chunks, refresh_lexical))

                def delete_obsolete(self, path, keep_ids) -> None:
                    pass

                def delete(self, path, *, refresh_lexical=True) -> None:
                    raise AssertionError("unexpected delete")

                def refresh_lexical_index(self) -> None:
                    self.refreshes += 1

            engine = FakeEngine()
            settings = {"index": {"max_file_bytes": 10000, "chunk_chars": 1200, "chunk_overlap": 150}}
            with patch.object(Path, "read_bytes", side_effect=AssertionError("file hashing must be streamed")):
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
                def upsert(self, path, chunks, *, content_hash, refresh_lexical=True) -> None:
                    raise RuntimeError("vector store unavailable")

                def delete_obsolete(self, path, keep_ids) -> None:
                    pass

                def delete(self, path, *, refresh_lexical=True) -> None:
                    raise AssertionError("unexpected delete")

                def refresh_lexical_index(self) -> None:
                    raise AssertionError("failed update should not refresh")

            with self.assertLogs("retrieval.index_worker", level="ERROR"):
                IndexWorker({"index": {"max_file_bytes": 10000, "chunk_chars": 1200,
                                        "chunk_overlap": 150}}, db, FailingEngine()).drain()
            self.assertEqual(db.get_file(file_path)["content_hash"], "old-hash")
            self.assertEqual(db.dirty_record(file_path)["attempts"], 1)
            self.assertEqual(db.pending_dirty(), [])

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
                def upsert(self, path, chunks, *, content_hash, refresh_lexical=True) -> None:
                    completed.set()

                def delete_obsolete(self, path, keep_ids) -> None:
                    pass

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
