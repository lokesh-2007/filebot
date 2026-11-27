"""Conversation turns receive relevant, bounded local file context."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.bot_runtime import BotRuntime
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class ChatContextTests(unittest.TestCase):
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
