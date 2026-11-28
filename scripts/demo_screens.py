"""Render FileBot's UI states to PNGs without a display (QT_QPA_PLATFORM=offscreen).

Usage: python scripts/demo_screens.py [output_dir]
Indexes demo/sample_files into a temporary database and drives the real popover with real search.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "2")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from agent.bot_runtime import BotRuntime  # noqa: E402
from agent.demo_llm import DemoServer  # noqa: E402
from agent.fast_router import FastRouter  # noqa: E402
from daemon.watcher import FileWatcher  # noqa: E402
from demo_support import apply_demo  # noqa: E402
from filebot_config import load_settings  # noqa: E402
from retrieval.lexical_engine import LexicalEngine  # noqa: E402
from storage.db import Database  # noqa: E402
from ui.avatar_bubble import AvatarBubble  # noqa: E402
from ui.popover_card import PopoverCard  # noqa: E402


class _Router(FastRouter):
    def route(self, prompt: str) -> str:
        intent = super().route(prompt)
        return "search" if intent == "chat" else intent


def main(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="filebot-screens-"))
    os.environ["HOME"] = str(home)
    settings = apply_demo(load_settings(ROOT / "config" / "settings.yaml"), ROOT)
    settings["app"]["database_path"] = str(home / "filebot.db")
    db = Database(Path(settings["app"]["database_path"]))
    db.initialize()
    FileWatcher(settings, db)._scan_once()
    engine = LexicalEngine(settings, db)
    runtime = BotRuntime(settings, db, engine, server=DemoServer(0))
    runtime.router = _Router()
    runtime.indexer.drain(limit=500)

    app = QApplication.instance() or QApplication(sys.argv)
    saved: list[Path] = []

    def shot(widget, name: str) -> None:
        app.processEvents()
        if widget.layout() is not None:
            widget.layout().activate()
            widget.resize(widget.width(), widget.sizeHint().height())
        else:
            widget.adjustSize()
        app.processEvents()
        path = out_dir / f"{name}.png"
        widget.grab().save(str(path))
        saved.append(path)

    avatar = AvatarBubble()
    for state in ("sleep", "active", "cmd_pending"):
        avatar.set_status(state)
        avatar.show()
        shot(avatar, f"avatar_{state}")

    card = PopoverCard(engine, runtime, db)
    card.show()
    shot(card, "01_popover_empty")

    card.input.setText("invoice")
    card._received(runtime.search("invoice"), "")
    shot(card, "02_search_results_single")

    card.input.setText("project demo")
    card._received(runtime.search("project demo"), "")
    shot(card, "03_search_results_multiple")

    card.input.setText("when is my project demo?")
    card._received(runtime.ask("when is my project demo?"), "")
    card.results.hide()
    card.results_label.hide()
    shot(card, "04_ask_answer")

    card.input.setText("zebra spaceship")
    card._received(runtime.search("zebra spaceship"), "")
    shot(card, "05_no_results")

    card.input.setText("run a command to list files")
    print("staged:", runtime.ask("run a command to list files"), len(db.pending_commands()))
    card._refresh_pending()
    card._received("I staged a command proposal. Review it and click Approve to run it.", "")
    shot(card, "06_command_approval")

    card.input.setText("hello")
    card._received(None, "The local model could not start. Check bin/llama-server.exe and the GGUF model.")
    shot(card, "07_error_state")
    card.close()
    return saved


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "demo" / "screenshots"
    for item in main(target):
        print(item)
