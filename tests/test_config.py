"""Configuration loading and path expansion tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from filebot_config import expand_config_path, load_settings


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_shipped_settings_load_and_expand_environment_path(self) -> None:
        settings = load_settings(ROOT / "config" / "settings.yaml")
        self.assertEqual(settings["models"]["ollama"]["model"], "qwen2.5-coder:3b")
        self.assertTrue(settings["models"]["ollama"]["auto_start"])
        self.assertEqual(settings["models"]["ollama"]["startup_timeout_seconds"], 30)
        self.assertEqual(settings["retrieval"]["glossary"], {})
        with patch.dict(os.environ, {"FILEBOT_TEST_ROOT": str(ROOT)}):
            self.assertEqual(expand_config_path("%FILEBOT_TEST_ROOT%/data"), ROOT / "data")

    def test_invalid_ollama_autostart_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            settings = load_settings(ROOT / "config" / "settings.yaml")
            settings["models"]["ollama"]["auto_start"] = "yes"
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "auto_start.*true or false"):
                load_settings(path)

            settings["models"]["ollama"]["auto_start"] = True
            settings["models"]["ollama"]["startup_timeout_seconds"] = 0
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "startup_timeout_seconds.*integer"):
                load_settings(path)

    def test_invalid_search_glossary_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            settings = load_settings(ROOT / "config" / "settings.yaml")
            settings["retrieval"]["glossary"] = {"spec": ""}
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "retrieval.glossary"):
                load_settings(path)

    def test_invalid_settings_report_missing_section(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            path.write_text("app: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "section 'index'"):
                load_settings(path)

    def test_invalid_index_bounds_fail_during_configuration_load(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            settings = load_settings(ROOT / "config" / "settings.yaml")
            settings["index"]["chunk_overlap"] = settings["index"]["chunk_chars"]
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "chunk_overlap.*smaller"):
                load_settings(path)

            settings["index"]["chunk_overlap"] = 100
            settings["models"]["embeddings"]["batch_size"] = "many"
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "models.embeddings.batch_size.*integer"):
                load_settings(path)

            settings["models"]["embeddings"]["batch_size"] = 64
            settings["index"]["max_docx_uncompressed_bytes"] = 0
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "max_docx_uncompressed_bytes.*integer"):
                load_settings(path)

    def test_request_timeout_must_be_bounded(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            settings = load_settings(ROOT / "config" / "settings.yaml")
            settings["runtime"]["request_timeout_seconds"] = 601
            path.write_text(yaml.safe_dump(settings), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "request_timeout_seconds.*600"):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
