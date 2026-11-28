import os
import tempfile
import unittest
from pathlib import Path

from agent.bot_runtime import BotRuntime
from agent.demo_llm import DemoServer, compose_answer
from agent.fast_router import FastRouter
from daemon.watcher import FileWatcher
from demo_support import apply_demo, demo_corpus, demo_requested
from filebot_config import load_settings
from retrieval.lexical_engine import LexicalEngine
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class _Router(FastRouter):
    def route(self, prompt: str) -> str:
        intent = super().route(prompt)
        return "search" if intent == "chat" else intent


class DemoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = apply_demo(load_settings(ROOT / "config" / "settings.yaml"), ROOT)
        self.settings["app"]["database_path"] = str(Path(self.tmp.name) / "demo.db")
        self.db = Database(Path(self.tmp.name) / "demo.db")
        self.db.initialize()
        FileWatcher(self.settings, self.db)._scan_once()
        self.engine = LexicalEngine(self.settings, self.db)
        self.runtime = BotRuntime(self.settings, self.db, self.engine, server=DemoServer(0))
        self.runtime.router = _Router()
        self.runtime.indexer.drain(limit=500)

    def test_apply_demo_points_at_sample_files_only(self) -> None:
        self.assertEqual(self.settings["index"]["roots"], [str(demo_corpus(ROOT))])
        self.assertTrue(self.settings["runtime"]["demo_mode"])
        self.assertIn(".filebot-demo", self.settings["models"]["vector_store"]["path"])
        self.assertTrue(demo_requested(["app.pyw", "--demo"]))

    def test_sample_corpus_is_indexed_and_searchable(self) -> None:
        names = [os.path.basename(hit["path"]) for hit in self.runtime.search("invoice")]
        self.assertIn("invoice_2026_0142.txt", names)
        self.assertEqual(len(names), len(set(names)))

    def test_question_gets_extractive_answer_from_local_files(self) -> None:
        pieces: list[str] = []
        answer = self.runtime.ask_streaming("when is the faculty demo?", pieces.append)
        self.assertIn("meeting_notes_nov.md", answer)
        self.assertIn("28 November", answer)
        self.assertEqual("".join(pieces).strip(), answer.strip())

    def test_command_request_is_only_staged_for_approval(self) -> None:
        answer = self.runtime.ask("run a command to list files")
        self.assertIn("Approve", answer)
        rows = self.db.pending_commands()
        self.assertEqual([row["command"] for row in rows], ["dir"])
        self.assertEqual(rows[0]["status"], "pending")

    def test_no_hits_message(self) -> None:
        self.assertIn("couldn't find", compose_answer("zzz", []))


if __name__ == "__main__":
    unittest.main()
