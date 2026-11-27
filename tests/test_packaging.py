"""Settings override merge, resource lookup, and the --selfcheck report."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import resources
from filebot_config import load_settings
from selfcheck import run_selfcheck

ROOT = Path(__file__).resolve().parents[1]


class UserSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_user_file_is_merged_over_defaults_without_dropping_siblings(self) -> None:
        user = self.dir / "settings.yaml"
        user.write_text("index:\n  roots:\n    - D:/Projects\n  debounce_seconds: 3\n"
                        "models:\n  llm:\n    n_ctx: 4096\n", encoding="utf-8")
        merged = load_settings(ROOT / "config" / "settings.yaml", user)
        self.assertEqual(merged["index"]["roots"], ["D:/Projects"])  # lists replace
        self.assertEqual(merged["index"]["debounce_seconds"], 3)
        self.assertIn(".pdf", merged["index"]["include_extensions"])  # untouched sibling
        self.assertEqual(merged["models"]["llm"]["n_ctx"], 4096)
        self.assertTrue(merged["models"]["llm"]["model_path"].endswith(".gguf"))  # sibling kept

    def test_missing_empty_or_comment_only_user_file_is_harmless(self) -> None:
        base = load_settings(ROOT / "config" / "settings.yaml")
        self.assertEqual(load_settings(ROOT / "config" / "settings.yaml", self.dir / "none.yaml"), base)
        comment_only = self.dir / "c.yaml"
        comment_only.write_text("# nothing here\n", encoding="utf-8")
        self.assertEqual(load_settings(ROOT / "config" / "settings.yaml", comment_only), base)

    def test_invalid_user_file_names_the_file(self) -> None:
        bad = self.dir / "bad.yaml"
        bad.write_text("index: [unclosed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "your FileBot settings"):
            load_settings(ROOT / "config" / "settings.yaml", bad)
        bad.write_text("- just\n- a list\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "must be a YAML mapping"):
            load_settings(ROOT / "config" / "settings.yaml", bad)

    def test_user_values_are_still_validated(self) -> None:
        user = self.dir / "settings.yaml"
        user.write_text("models:\n  llm:\n    n_ctx: 10\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "n_ctx"):
            load_settings(ROOT / "config" / "settings.yaml", user)


class ResourceTests(unittest.TestCase):
    def test_frozen_root_uses_meipass_and_source_root_is_the_repo(self) -> None:
        self.assertEqual(resources.app_root(), ROOT)
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(ROOT / "tests"), create=True):
            self.assertEqual(resources.app_root(), ROOT / "tests")
            self.assertEqual(resources.resolve_resource("models/x.gguf"), ROOT / "tests" / "models" / "x.gguf")

    def test_user_settings_path_follows_appdata_on_windows_and_xdg_elsewhere(self) -> None:
        with patch.object(sys, "platform", "win32"), patch.dict(os.environ, {"APPDATA": "C:/Users/me/AppData/Roaming"}):
            self.assertEqual(resources.user_settings_path(),
                             Path("C:/Users/me/AppData/Roaming") / "FileBot" / "settings.yaml")
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(resources.user_config_dir(), Path("/xdg") / "filebot")


class SelfcheckTests(unittest.TestCase):
    def settings(self, directory: Path) -> dict:
        settings = load_settings(ROOT / "config" / "settings.yaml")
        settings["models"]["llm"]["server_path"] = str(directory / "llama-server.exe")
        settings["models"]["llm"]["model_path"] = str(directory / "m.gguf")
        settings["models"]["embeddings"]["model_dir"] = str(directory / "onnx")
        return settings

    def test_missing_assets_only_fail_when_required(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            settings = self.settings(Path(tmp))
            ok, report = run_selfcheck(settings, modules=("json",))
            self.assertTrue(ok)
            self.assertEqual(sum(1 for r in report if r["check"].startswith("asset") and not r["ok"]), 3)
            ok, _ = run_selfcheck(settings, require_assets=True, modules=("json",))
            self.assertFalse(ok)

    def test_present_assets_pass_and_missing_module_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            base = Path(tmp)
            (base / "llama-server.exe").write_bytes(b"x")
            (base / "m.gguf").write_bytes(b"x")
            (base / "onnx").mkdir()
            (base / "onnx" / "model_optimized.onnx").write_bytes(b"x")
            ok, _ = run_selfcheck(self.settings(base), require_assets=True, modules=("json",))
            self.assertTrue(ok)
            ok, report = run_selfcheck(self.settings(base), modules=("json", "no_such_module_xyz"))
            self.assertFalse(ok)
            self.assertIn("ModuleNotFoundError", [r for r in report if not r["ok"]][0]["detail"])

    def test_empty_embedding_folder_is_not_an_asset(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            (Path(tmp) / "onnx").mkdir()
            _, report = run_selfcheck(self.settings(Path(tmp)), modules=())
            embedding = next(r for r in report if r["check"] == "asset: embedding model")
            self.assertFalse(embedding["ok"])
            self.assertIn("no .onnx", embedding["detail"])

    def test_entry_point_flag_writes_json_report_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            out = Path(tmp) / "report.json"
            code = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, '.'); import selfcheck; "
                 "selfcheck.RUNTIME_MODULES = ('json',); "
                 "sys.exit(selfcheck.main(['--selfcheck', '--selfcheck-out=" + str(out).replace("\\", "/") + "']))"],
                cwd=ROOT, capture_output=True, text=True, timeout=60).returncode
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(code, 0, data)
            self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
