"""Configuration loading and path expansion tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from filebot_config import expand_config_path, load_settings


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_shipped_settings_load_and_expand_environment_path(self) -> None:
        settings = load_settings(ROOT / "config" / "settings.yaml")
        self.assertEqual(settings["models"]["ollama"]["model"], "qwen2.5-coder:3b")
        with patch.dict(os.environ, {"FILEBOT_TEST_ROOT": str(ROOT)}):
            self.assertEqual(expand_config_path("%FILEBOT_TEST_ROOT%/data"), ROOT / "data")

    def test_invalid_settings_report_missing_section(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "settings.yaml"
            path.write_text("app: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "section 'index'"):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()

