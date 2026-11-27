"""FileBot Windows entry point. Heavy retrieval and agent modules stay unloaded at startup."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox

from daemon.lifecycle_manager import LifecycleManager
from daemon.instance_guard import InstanceGuard
from daemon.watcher import FileWatcher
from filebot_config import expand_config_path, load_settings
from filebot_version import __version__
from resources import app_root, user_config_dir, user_settings_path
from storage.db import Database
from ui.avatar_bubble import AvatarBubble
from ui.hotkey_bridge import HotkeyBridge


_USER_SETTINGS_TEMPLATE = """# Your FileBot overrides. Values here are merged over the defaults shipped with the app,
# so upgrades never overwrite them. Example (uncomment to use):
#
# index:
#   roots:
#     - "~/Documents"
#     - "D:/Projects"
#   scan_fixed_drives: false
"""


def _ensure_user_settings_template() -> None:
    """Installed builds: create an editable override file on first run (never overwrites)."""
    if not getattr(sys, "frozen", False):
        return
    path = user_settings_path()
    try:
        if not path.exists():
            user_config_dir().mkdir(parents=True, exist_ok=True)
            path.write_text(_USER_SETTINGS_TEMPLATE, encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning("Could not create %s", path)


def main() -> int:
    """Start the lightweight GUI, filesystem watcher, and hotkey listener."""
    root = app_root()
    _ensure_user_settings_template()
    settings = load_settings(root / "config" / "settings.yaml", user_settings_path())
    db_path = expand_config_path(settings["app"]["database_path"])
    log_dir = db_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_handler = RotatingFileHandler(log_dir / "filebot.log", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
    handlers: list[logging.Handler] = [log_handler]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, str(settings["app"].get("log_level", "INFO")).upper(), logging.INFO),
        handlers=handlers, force=True,
    )
    app = QApplication(sys.argv)
    app.setApplicationName("FileBot")
    app.setApplicationDisplayName("FileBot")
    app.setApplicationVersion(__version__)
    guard = InstanceGuard(db_path.parent / "filebot.lock")
    if not guard.acquire():
        QMessageBox.information(None, "FileBot is already running",
                                "The existing FileBot avatar is available in the desktop session.")
        return 0
    try:
        db = Database(db_path)
        db.initialize()
        requeued, interrupted = db.recover_command_states()
        if requeued or interrupted:
            logging.warning(
                "Recovered command audit after an unclean shutdown: %d require re-approval, %d interrupted",
                requeued, interrupted,
            )
        lifecycle = LifecycleManager(settings, db, root)
        avatar = AvatarBubble()
        watcher = FileWatcher(settings, db)
        bridge = HotkeyBridge(settings["runtime"].get("hotkey", "<ctrl>+<alt>+<space>"))
        avatar.toggled.connect(lifecycle.toggle)
        avatar.status_changed.connect(lifecycle.set_status)
        lifecycle.status_changed.connect(avatar.set_status)
        lifecycle.popover_ready.connect(avatar.show_popover)
        lifecycle.error_occurred.connect(
            lambda message: QMessageBox.warning(avatar, "FileBot could not wake", message)
        )
        bridge.activated.connect(lifecycle.toggle)
        try:
            bridge.start()
            watcher.start()
            avatar.show()
        except Exception:
            watcher.stop()
            bridge.stop()
            lifecycle.shutdown()
            raise
        app.aboutToQuit.connect(watcher.stop)
        app.aboutToQuit.connect(bridge.stop)
        app.aboutToQuit.connect(lifecycle.shutdown)
        app.aboutToQuit.connect(guard.release)
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import selfcheck  # imported lazily: it pulls in the heavy runtime modules on purpose
        raise SystemExit(selfcheck.main(sys.argv[1:]))
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("FileBot could not start")
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "FileBot could not start", str(exc))
        except Exception:
            pass
        raise
