"""BotRuntime and LlamaServer tests using a fake llama-server (no real model needed)."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.bot_runtime import BotRuntime, _is_unexecuted_tool_text
from agent.llm_server import LlamaServer
from storage.db import Database

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_llama_server.py"


class _Engine:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return [{"path": "C:/docs/budget.md", "snippet": "Q3 budget " * 200}]


class _Base(unittest.TestCase):
    mode = "ok"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self._cleanup_tmp)
        self.dir = Path(self._tmp.name)
        self.db = Database(self.dir / "fb.sqlite3")
        self.db.initialize()
        self.model = self.dir / "model.gguf"
        self.model.write_text(self.mode, encoding="utf-8")

    def _cleanup_tmp(self) -> None:
        for attempt in range(5):
            try:
                self._tmp.cleanup()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)

    def make_server(self, **kwargs) -> LlamaServer:
        server = LlamaServer(FAKE, self.model, startup_timeout=20,
                             log_path=self.dir / "llama.log", launcher=[sys.executable], **kwargs)
        self.addCleanup(server.stop)
        return server

    def make_runtime(self, engine=None) -> BotRuntime:
        runtime = BotRuntime({"index": {"roots": [str(self.dir)]}, "models": {}}, self.db,
                             engine or _Engine(), server=self.make_server())
        self.addCleanup(runtime.close)
        return runtime


class LlamaServerTests(_Base):
    def test_starts_streams_and_stops_on_loopback(self) -> None:
        server = self.make_server()
        server.start()
        self.assertTrue(server.running())
        self.assertEqual(server.base_url, f"http://127.0.0.1:{server.port}")
        text = "".join(c["choices"][0]["delta"].get("content", "")
                       for c in server.chat_stream([{"role": "user", "content": "hi"}], None))
        self.assertEqual(text, "Hello from FileBot")
        server.stop()
        self.assertFalse(server.running())

    def test_missing_assets_fail_with_clear_message(self) -> None:
        server = LlamaServer(self.dir / "nope.exe", self.model)
        with self.assertRaisesRegex(RuntimeError, "engine is missing"):
            server.start()
        server = LlamaServer(FAKE, self.dir / "nope.gguf")
        with self.assertRaisesRegex(RuntimeError, "language model is missing"):
            server.start()


class CrashTests(_Base):
    mode = "crash"

    def test_startup_failure_reports_exit_code_and_log_tail(self) -> None:
        server = self.make_server()
        with self.assertRaises(RuntimeError) as ctx:
            server.start()
        self.assertIn("code 3", str(ctx.exception))
        self.assertIn("model failed to load", str(ctx.exception))
        self.assertFalse(server.running())


class RuntimeOkTests(_Base):
    def test_chat_streams_deltas_offers_no_tools_and_keeps_history(self) -> None:
        runtime = self.make_runtime()
        chunks: list[str] = []
        self.assertEqual(runtime.ask_streaming("Hello, how are you?", chunks.append), "Hello from FileBot")
        self.assertEqual("".join(chunks), "Hello from FileBot")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([m["role"] for m in runtime._history], ["user", "assistant"])

    def test_plain_chat_does_not_drain_the_index_queue(self) -> None:
        runtime = self.make_runtime()
        runtime.indexer.drain = lambda **kw: self.fail("chat must not wait for indexing")
        self.assertEqual(runtime.ask("Hello there"), "Hello from FileBot")

    def test_history_is_bounded(self) -> None:
        runtime = self.make_runtime()
        for i in range(10):
            runtime.ask(f"Say hello {i}")
        self.assertEqual(len(runtime._history), 8)

    def test_unexecuted_tool_json_is_replaced_with_capability_message(self) -> None:
        self.assertTrue(_is_unexecuted_tool_text(
            '```json\n{"name":"open_file","arguments":{"path":"new.html"}}\n```'))
        answer = BotRuntime._answer_text('{"name":"open_file","arguments":{"path":"x"}}')
        self.assertIn("did not run that action", answer)
        self.assertNotIn('"name"', answer)

    def test_unavailable_tool_is_refused_for_the_intent(self) -> None:
        runtime = self.make_runtime()
        result, ok = runtime._execute_tool("propose_cmd", "{}", {"search_files"})
        self.assertFalse(ok)
        self.assertIn("not available", result)


class RuntimeToolTests(_Base):
    mode = "tool"

    def test_search_intent_runs_tool_loop_and_answers(self) -> None:
        engine = _Engine()
        runtime = self.make_runtime(engine)
        runtime.indexer.drain = lambda **kw: 0
        chunks: list[str] = []
        answer = runtime.ask_streaming("Find the budget notes", chunks.append)
        self.assertTrue(answer.startswith("Found: "), answer)
        self.assertIn("budget.md", answer)
        # one prefetch search for prompt context + one from the model's tool call
        self.assertEqual(engine.queries[-1], "budget")
        self.assertEqual(len(engine.queries), 2)

    def test_search_context_snippets_are_trimmed(self) -> None:
        runtime = self.make_runtime()
        runtime.indexer.drain = lambda **kw: 0
        _, prompt = runtime._prepare_prompt("Find the budget notes")
        self.assertLess(len(prompt), 1500)

    def test_cmd_intent_stages_nothing_without_a_matching_tool_call(self) -> None:
        runtime = self.make_runtime()
        # The fake model asks for search_files, which is not allowed for cmd turns.
        answer = runtime.ask("Run command whoami")
        self.assertTrue(answer.startswith("Found: Error: tool 'search_files'"), answer)
        self.assertEqual(list(self.db.pending_commands()), [])


class RuntimeSketchTests(_Base):
    mode = "sketch"

    def test_text_json_sketch_is_converted_and_not_shown_to_the_user(self) -> None:
        runtime = self.make_runtime()
        runtime.indexer.drain = lambda **kw: 0
        chunks: list[str] = []
        answer = runtime.ask_streaming("Find the budget notes", chunks.append)
        self.assertEqual(answer, "Sketch handled")
        self.assertNotIn('"name"', "".join(chunks))


class RuntimeCancelTests(_Base):
    mode = "slow"

    def test_cancel_pending_unblocks_a_running_turn(self) -> None:
        runtime = self.make_runtime()
        errors: list[Exception] = []

        def ask() -> None:
            try:
                runtime.ask("Explain local indexing")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=ask)
        worker.start()
        deadline = time.monotonic() + 10
        while not runtime._turn_active and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        self.assertTrue(runtime.cancel_pending())
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertIn("cancelled", str(errors[0]))
        self.assertFalse(runtime.cancel_pending())


if __name__ == "__main__":
    unittest.main()
