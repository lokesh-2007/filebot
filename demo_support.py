"""Demo mode: run the real app on bundled sample files with no model downloads.

``python app.pyw --demo`` (or ``FILEBOT_DEMO=1``) points FileBot at ``demo/sample_files``, stores its
database under ``~/.filebot-demo`` and swaps the heavy services for model-free stand-ins. Real files
are never indexed in this mode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEMO_HOME = "~/.filebot-demo"


def demo_requested(argv: list[str] | None = None) -> bool:
    return "--demo" in (argv if argv is not None else sys.argv) or os.environ.get("FILEBOT_DEMO") == "1"


def demo_corpus(root: Path) -> Path:
    return (root / "demo" / "sample_files").resolve()


def apply_demo(settings: dict[str, Any], root: Path) -> dict[str, Any]:
    """Rewrite settings in place for demo mode and return them."""
    home = Path(DEMO_HOME).expanduser()
    settings["app"]["database_path"] = str(home / "filebot.db")
    settings["models"]["vector_store"]["path"] = str(home / "vectors")
    settings["models"]["embeddings"]["cache_dir"] = str(home / "models")
    index = settings["index"]
    index["roots"] = [str(demo_corpus(root))]
    index["scan_fixed_drives"] = False
    index["exclude_paths"] = []
    settings["runtime"]["demo_mode"] = True
    return settings
