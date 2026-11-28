"""Conversation turns receive relevant, bounded local file context."""
from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.bot_runtime import BotRuntime, _warm_prompt_tokenizer
from agent.fast_router import FastRouter
from agent.tools.search_tool import SearchFilesTool
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class ChatContextTests(unittest.TestCase):
    def test_prompt_tokenizer_warmup_waits_for_the_background_load(self) -> None:
        calls = {"encoding": 0, "warmup": 0}
        encoding = object()

        def get_encoding():
            calls["encoding"] += 1
            return encoding if calls["encoding"] >= 2 else None

        module = SimpleNamespace(
            get_token_encoding=get_encoding,
            warmup_token_encoding=lambda: calls.__setitem__("warmup", calls["warmup"] + 1),
        )
        with patch.dict("sys.modules", {"nanobot.utils.token_encoding": module}):
            self.assertTrue(_warm_prompt_tokenizer(timeout_seconds=0.5))

        self.assertEqual(calls["warmup"], 1)
        self.assertGreaterEqual(calls["encoding"], 2)

    def test_prompt_tokenizer_warmup_fails_fast_if_encoding_is_unavailable(self) -> None:
        module = SimpleNamespace(
            get_token_encoding=lambda: None,
            warmup_token_encoding=lambda: None,
        )
        with patch.dict("sys.modules", {"nanobot.utils.token_encoding": module}):
            self.assertFalse(_warm_prompt_tokenizer(timeout_seconds=0))

    def test_command_routing_does_not_capture_normal_conversation(self) -> None:
        router = FastRouter()

        self.assertEqual(router.route("Run me through the plan"), "chat")
        self.assertEqual(router.route("I run the support team"), "chat")
        self.assertEqual(router.route("Run `whoami`"), "cmd")
        self.assertEqual(router.route("Run the tests"), "cmd")
        self.assertEqual(router.route("Find and run a command"), "cmd")
        self.assertEqual(router.route("Use PowerShell to show running processes"), "powershell")
        self.assertEqual(router.route("Get-ChildItem Downloads"), "powershell")
        self.assertEqual(router.route("What is PowerShell?"), "chat")

    def test_open_folder_request_routes_directly_without_chat_retrieval(self) -> None:
        router = FastRouter()

        self.assertEqual(router.route("open downloads folder"), "open_folder")
        self.assertEqual(router.folder_target("open downloads folder"), "downloads")
        self.assertEqual(router.route("open downloads"), "open_folder")
        self.assertEqual(router.folder_target("open downloads"), "downloads")
        self.assertEqual(router.route("open chat gpt folder"), "open_folder")
        self.assertEqual(router.folder_target("open chat gpt folder"), "chat gpt")
        self.assertEqual(
            router.route("open the chatgpt folder inside onedrive"), "open_folder",
        )
        self.assertEqual(
            router.folder_target("open the chatgpt folder inside onedrive"), "chatgpt",
        )
        self.assertEqual(router.route("How do I open the Downloads folder?"), "chat")
        self.assertEqual(router.route("What's in the Downloads folder?"), "list")
        self.assertEqual(router.list_target("What's in the Downloads folder?"), "Downloads")

    def test_create_request_extracts_named_target_and_explicit_content(self) -> None:
        router = FastRouter()

        self.assertEqual(router.route("create a text file named h2.txt"), "create")
        self.assertEqual(router.create_target("create a text file named h2.txt"), "h2.txt")
        self.assertIsNone(router.create_content("create a text file named h2.txt"))
        self.assertFalse(router.create_needs_generated_content("create a text file named h2.txt"))
        self.assertEqual(
            router.create_content('create a file named notes.txt containing: "first line"'),
            "first line",
        )
        self.assertTrue(
            router.create_needs_generated_content(
                "create a file named plan.txt with a brief project plan about backups"
            )
        )

    def test_context_overflow_retries_once_in_a_fresh_session(self) -> None:
        runtime = BotRuntime({}, object(), object())
        calls: list[str] = []

        runtime.bot = SimpleNamespace(_loop=object())

        def run_fake_agent(_intent, coroutine):
            coroutine.close()
            if len(calls) == 1:
                return SimpleNamespace(
                    content="", error="Model input exceeds the local context budget: 9000/2800",
                )
            return SimpleNamespace(content="Opened the folder.", error=None)

        def make_request():
            calls.append(runtime._session)
            async def request():
                return None
            return request()

        with patch.object(runtime, "_run_agent", side_effect=run_fake_agent):
            answer = runtime._run_agent_with_context_recovery(
                "chat", make_request,
            )

        self.assertEqual(answer, "Opened the folder.")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_retrieved_prompt_context_is_bounded_and_keeps_source_paths(self) -> None:
        hits = [
            {"path": rf"C:\Docs\source-{index}.md", "snippet": "evidence " * 1000}
            for index in range(8)
        ]

        context = BotRuntime._format_local_file_context(hits)
        serialized = context.split("paths):\n", 1)[1]
        entries = json.loads(serialized)

        self.assertLessEqual(len(serialized), 6000)
        self.assertEqual(entries[0]["path"], r"C:\Docs\source-0.md")
        self.assertLessEqual(len(entries), 8)

    def test_model_search_tool_caps_result_count_and_excerpt_size(self) -> None:
        class Engine:
            def __init__(self) -> None:
                self.limit = None

            def search(self, _query, limit):
                self.limit = limit
                return [
                    {"path": f"doc-{index}.txt", "snippet": "x" * 2000}
                    for index in range(15)
                ]

        engine = Engine()
        results = SearchFilesTool(engine).execute("documents", limit=50)

        self.assertEqual(engine.limit, 10)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(len(item["snippet"]) == 1200 for item in results))

    def test_chat_history_is_session_scoped_bounded_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            database_path = Path(directory) / "state.sqlite3"
            database = Database(database_path)
            database.initialize()
            database.record_chat_turn("desktop", "first", "one", retain=2)
            database.record_chat_turn("desktop", "second", "two", retain=2)
            database.record_chat_turn("desktop", "third", "three", retain=2)
            database.record_chat_turn("other", "separate", "session", retain=2)

            reopened = Database(database_path)
            self.assertEqual(
                reopened.recent_chat_turns("desktop", limit=50),
                [("second", "two"), ("third", "three")],
            )
            self.assertEqual(
                reopened.recent_chat_turns("other", limit=50),
                [("separate", "session")],
            )

    def test_ordinary_chat_turn_includes_local_excerpts_for_grounded_answers(self) -> None:
        hits = [{
            "path": r"C:\Docs\Warranty.txt",
            "snippet": "The warranty lasts two years from the purchase date.",
        }]

        class Engine:
            def __init__(self) -> None:
                self.calls = []

            def search(self, query: str, limit: int = 10):
                self.calls.append((query, limit))
                return hits

        engine = Engine()
        runtime = BotRuntime({"index": {}}, object(), engine)
        runtime.bot = object()  # _prepare_prompt must not construct Nanobot in this unit test.
        with patch.object(runtime, "_check_ollama_ready") as preflight:
            intent, prompt = runtime._prepare_prompt("How long is the warranty?")

        self.assertEqual(intent, "chat")
        self.assertEqual(engine.calls, [("How long is the warranty?", 8)])
        self.assertIn("Relevant local file excerpts", prompt)
        self.assertIn("Warranty.txt", prompt)
        self.assertIn("two years", prompt)
        self.assertIn("cite their full paths", prompt)
        self.assertIn("Answer the main point first", prompt)
        self.assertGreaterEqual(runtime._last_request_timings["retrieval_seconds"], 0.0)
        preflight.assert_not_called()

    def test_chat_stays_available_when_local_retrieval_fails(self) -> None:
        class Engine:
            def search(self, *_args, **_kwargs):
                raise OSError("index unavailable")

        runtime = BotRuntime({"index": {}}, object(), Engine())
        runtime.bot = object()
        with patch.object(runtime, "_check_ollama_ready"), self.assertLogs(
            "agent.bot_runtime", level="ERROR"
        ):
            intent, prompt = runtime._prepare_prompt("Explain photosynthesis")

        self.assertEqual(intent, "chat")
        self.assertIn("could not retrieve local file context", prompt)
        self.assertIn("Answer general questions normally", prompt)


if __name__ == "__main__":
    unittest.main()
