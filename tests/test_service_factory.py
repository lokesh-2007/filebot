import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.demo_llm import DemoServer
from daemon.watcher import FileWatcher
from filebot_config import load_settings
from retrieval.lexical_engine import LexicalEngine
from service_factory import (Capabilities, apply_auto_roots, build_services, embeddings_ready,
                             llm_ready, requested_mode)
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


class AutoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name) / "docs"
        self.docs.mkdir()
        (self.docs / "plan.txt").write_text("The quarterly budget review is on 4 December.", encoding="utf-8")
        self.settings = load_settings(ROOT / "config" / "settings.yaml")
        self.settings["index"]["roots"] = [str(self.docs)]
        self.settings["app"]["database_path"] = str(Path(self.tmp.name) / "fb.db")
        self.settings["models"]["embeddings"]["model_dir"] = str(Path(self.tmp.name) / "no-model")
        self.settings["models"]["llm"]["server_path"] = str(Path(self.tmp.name) / "no-server.exe")
        self.db = Database(Path(self.tmp.name) / "fb.db")
        self.db.initialize()

    def test_default_mode_is_auto_and_validated(self) -> None:
        self.assertEqual(requested_mode(self.settings), "auto")
        self.assertEqual(requested_mode(self.settings, ["app.pyw", "--mode=lite"]), "lite")
        with self.assertRaises(RuntimeError):
            requested_mode(self.settings, ["--mode=turbo"])

    def test_missing_models_fall_back_to_lite_on_real_files(self) -> None:
        self.assertFalse(embeddings_ready(self.settings))
        self.assertFalse(llm_ready(self.settings))
        engine, runtime, caps = build_services(self.settings, self.db, "auto")
        self.addCleanup(runtime.close)
        self.assertIsInstance(engine, LexicalEngine)
        self.assertIsInstance(runtime.server, DemoServer)
        self.assertEqual(caps, Capabilities(semantic=False, llm=False))
        self.assertEqual(caps.label, "Lite mode · keyword search")
        FileWatcher(self.settings, self.db)._scan_once()
        runtime.indexer.drain(limit=50)
        self.assertEqual([os.path.basename(h["path"]) for h in runtime.search("budget")], ["plan.txt"])
        answer = runtime.ask("when is the budget review?")
        self.assertIn("4 December", answer)

    def test_full_mode_without_models_is_an_error_not_a_silent_fallback(self) -> None:
        with self.assertRaises(Exception):
            build_services(self.settings, self.db, "full")

    def test_auto_falls_back_when_semantic_engine_fails_to_start(self) -> None:
        with mock.patch("service_factory.embeddings_ready", return_value=True), \
                mock.patch("retrieval.hybrid_engine.HybridEngine.activate", side_effect=RuntimeError("boom")):
            engine, runtime, caps = build_services(self.settings, self.db, "auto")
        self.addCleanup(runtime.close)
        self.assertIsInstance(engine, LexicalEngine)
        self.assertFalse(caps.semantic)

    def test_roots_that_do_not_exist_are_dropped(self) -> None:
        self.settings["index"]["roots"] = [str(self.docs), str(Path(self.tmp.name) / "gone")]
        dropped = apply_auto_roots(self.settings)
        self.assertEqual(self.settings["index"]["roots"], [str(self.docs)])
        self.assertEqual(len(dropped), 1)

    def test_onedrive_documents_is_added_when_home_documents_is_the_default(self) -> None:
        one_drive = Path(self.tmp.name) / "OneDrive"
        (one_drive / "Documents").mkdir(parents=True)
        home = Path(self.tmp.name) / "home"
        (home / "Documents").mkdir(parents=True)
        self.settings["index"]["roots"] = [str(home / "Documents")]
        with mock.patch.dict(os.environ, {"OneDrive": str(one_drive)}):
            # The default is spelled ~/Documents; emulate it by pointing HOME at our temp home.
            self.settings["index"]["roots"] = ["~/Documents"]
            with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                apply_auto_roots(self.settings)
        self.assertIn(str(one_drive / "Documents"), self.settings["index"]["roots"])
        self.assertIn("~/Documents", self.settings["index"]["roots"])


if __name__ == "__main__":
    unittest.main()
