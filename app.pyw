"""FileBot Windows entry point. Heavy retrieval and agent modules stay unloaded at startup."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from daemon.lifecycle_manager import LifecycleManager
from daemon.watcher import FileWatcher
from storage.db import Database
from ui.avatar_bubble import AvatarBubble
from ui.hotkey_bridge import HotkeyBridge


def main() -> int:
    """Start the lightweight GUI, filesystem watcher, and hotkey listener."""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    try:
        import yaml
        settings = yaml.safe_load((root / "config" / "settings.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot load FileBot settings: {exc}") from exc
    logging.basicConfig(level=getattr(logging, settings["app"].get("log_level", "INFO"), logging.INFO))
    db = Database(Path(settings["app"]["database_path"]).expanduser())
    db.initialize()
    app = QApplication(sys.argv)
    lifecycle = LifecycleManager(settings, db, root)
    avatar = AvatarBubble()
    watcher = FileWatcher(settings, db)
    bridge = HotkeyBridge(settings["runtime"].get("hotkey", "<ctrl>+<alt>+space"))
    avatar.toggled.connect(lifecycle.toggle)
    avatar.status_changed.connect(lifecycle.set_status)
    lifecycle.status_changed.connect(avatar.set_status)
    lifecycle.popover_ready.connect(avatar.show_popover)
    bridge.activated.connect(lifecycle.toggle)
    bridge.start()
    watcher.start()
    avatar.show()
    app.aboutToQuit.connect(watcher.stop)
    app.aboutToQuit.connect(bridge.stop)
    app.aboutToQuit.connect(lifecycle.shutdown)
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("FileBot could not start")
        raise
