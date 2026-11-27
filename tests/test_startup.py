"""Verify the dormant Windows entry point keeps model libraries unloaded."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StartupImportTests(unittest.TestCase):
    def test_importing_entry_point_does_not_load_heavy_model_stack(self) -> None:
        script = """
import runpy, sys
runpy.run_path('app.pyw', run_name='filebot_startup_import_test')
heavy = {'fastembed', 'chromadb'} & sys.modules.keys()
if heavy:
    raise SystemExit(f'heavy runtime modules loaded during startup: {sorted(heavy)}')
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, text=True,
            capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
