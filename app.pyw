"""FileBot Windows entry point. Heavy retrieval and agent modules stay unloaded at startup."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QStyle, QSystemTrayIcon

from daemon.lifecycle_manager import LifecycleManager
from daemon.instance_guard import InstanceGuard
from daemon.metrics import process_working_set_mb
from daemon.watcher import FileWatcher
from filebot_config import expand_config_path, load_settings
from filebot_version import __version__
from storage.db import Database
from ui.avatar_bubble import AvatarBubble
from ui.hotkey_bridge import HotkeyBridge


def main() -> int:
    """Start the lightweight GUI, filesystem watcher, and hotkey listener."""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    settings = load_settings(root / "config" / "settings.yaml")
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
    app.setQuitOnLastWindowClosed(False)
    guard = InstanceGuard(db_path.parent / "filebot.lock")
    if not guard.acquire():
        QMessageBox.information(None, "FileBot is already running",
                                "The existing FileBot chat window is available in the desktop session or system tray.")
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
        expired_deletions, interrupted_deletions = db.recover_file_deletes()
        if expired_deletions or interrupted_deletions:
            logging.warning(
                "Recovered file delete audit: %d expired confirmations, %d interrupted operations",
                expired_deletions, interrupted_deletions,
            )
        lifecycle = LifecycleManager(settings, db, root)
        watcher = FileWatcher(settings, db)
        bridge = HotkeyBridge(settings["runtime"].get("hotkey", "<ctrl>+<alt>+<space>"))
        avatar = AvatarBubble()
        tray = QSystemTrayIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), app)
        tray.setToolTip("FileBot — local file assistant")
        tray_menu = QMenu()
        open_action = tray_menu.addAction("Open FileBot")
        open_action.triggered.connect(lifecycle.wake)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Quit FileBot")
        quit_action.triggered.connect(app.quit)
        tray.setContextMenu(tray_menu)

        def show_chat_window(window: object) -> None:
            avatar.show()
            avatar.show_popover(window)
            window.activateWindow()

        def activate_tray(reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason in {
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
            }:
                lifecycle.toggle()

        lifecycle.popover_ready.connect(show_chat_window)
        lifecycle.status_changed.connect(avatar.set_status)
        avatar.toggled.connect(lifecycle.toggle)
        avatar.quit_requested.connect(app.quit)
        lifecycle.error_occurred.connect(
            lambda message: QMessageBox.warning(None, "FileBot could not wake", message)
        )
        bridge.activated.connect(lifecycle.toggle)
        tray.activated.connect(activate_tray)
        try:
            bridge.start()
            watcher.start()
            tray.show()
            avatar.show()
            lifecycle.wake()
            working_set = process_working_set_mb()
            if working_set is not None:
                logging.info("FileBot chat startup working set: %.1f MiB", working_set)
        except Exception:
            watcher.stop()
            bridge.stop()
            lifecycle.shutdown()
            raise
        app.aboutToQuit.connect(watcher.stop)
        app.aboutToQuit.connect(bridge.stop)
        app.aboutToQuit.connect(lifecycle.shutdown)
        app.aboutToQuit.connect(avatar.close)
        app.aboutToQuit.connect(guard.release)
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
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
