"""Bridge pynput's listener thread into Qt's main thread."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class HotkeyBridge(QObject):
    activated = pyqtSignal()

    def __init__(self, hotkey: str) -> None:
        super().__init__()
        self.hotkey = hotkey
        self.listener = None

    def start(self) -> None:
        from pynput import keyboard
        self.listener = keyboard.GlobalHotKeys({self.hotkey: self.activated.emit})
        self.listener.start()

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener.join(timeout=2)
