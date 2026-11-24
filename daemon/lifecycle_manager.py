"""Wake/sleep coordinator; imports vector and agent stacks only on demand."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from storage.db import Database

log = logging.getLogger(__name__)


class LifecycleManager(QObject):
    popover_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str)

    def __init__(self, settings: dict[str, Any], db: Database, project_root: Path) -> None:
        super().__init__()
        self.settings, self.db, self.project_root = settings, db, project_root
        self.engine = self.runtime = self.popover = None
        self._active = False
        self.idle = QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.timeout.connect(self.sleep)

    def toggle(self) -> None:
        if self._active:
            self.sleep()
        else:
            self.wake()

    def wake(self) -> None:
        if not self._active:
            try:
                from retrieval.hybrid_engine import HybridEngine
                from retrieval.index_worker import IndexWorker
                from agent.bot_runtime import BotRuntime
                self.engine = HybridEngine(self.settings, self.db)
                self.engine.activate()
                IndexWorker(self.settings, self.db, self.engine).drain()
                self.runtime = BotRuntime(self.settings, self.db, self.engine)
                self._active = True
            except Exception:
                log.exception("Could not wake FileBot retrieval/agent services")
                self.status_changed.emit("sleep")
                return
        self.status_changed.emit("active")
        if self.popover is None:
            from ui.popover_card import PopoverCard
            self.popover = PopoverCard(self.engine, self.runtime, self.db)
            self.popover.activity.connect(self.touch)
            self.popover.status_changed.connect(self.set_status)
        self.popover_ready.emit(self.popover)
        self.popover.show()
        self.touch()

    def touch(self) -> None:
        self.idle.start(max(1, int(self.settings["runtime"].get("idle_unload_seconds", 120))) * 1000)

    def set_status(self, status: str) -> None:
        self.status_changed.emit(status)

    def sleep(self) -> None:
        self.idle.stop()
        if self.popover is not None:
            self.popover.hide()
        for item in (self.runtime, self.engine):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    log.exception("Error while unloading FileBot component")
        self.runtime = self.engine = self.popover = None
        self._active = False
        self.status_changed.emit("sleep")

    def shutdown(self) -> None:
        self.sleep()
