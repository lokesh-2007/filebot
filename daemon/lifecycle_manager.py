"""Wake/sleep coordinator with model startup and user work off the GUI thread."""
from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from storage.db import Database
from daemon.metrics import process_working_set_mb

log = logging.getLogger(__name__)


class _WakeWorker(QThread):
    """Load retrieval resources away from Qt's event loop."""
    ready = pyqtSignal(object, object, str)

    def __init__(self, settings: dict[str, Any], db: Database) -> None:
        super().__init__()
        self.settings, self.db = settings, db
        self.result: tuple[Any, Any] | None = None

    def run(self) -> None:
        started = time.perf_counter()
        engine = runtime = None
        try:
            from retrieval.hybrid_engine import HybridEngine
            from agent.bot_runtime import BotRuntime
            engine = HybridEngine(self.settings, self.db)
            engine.activate()
            runtime = BotRuntime(self.settings, self.db, engine)
            self.result = (engine, runtime)
            working_set = process_working_set_mb()
            log.info(
                "FileBot wake initialized in %.3fs; working_set=%s",
                time.perf_counter() - started,
                f"{working_set:.1f} MiB" if working_set is not None else "unavailable",
            )
            self.ready.emit(engine, runtime, "")
        except Exception as exc:
            log.exception("Could not wake FileBot retrieval/agent services")
            for item in (runtime, engine):
                if item is not None:
                    try:
                        item.close()
                    except Exception:
                        log.exception("Could not release partially initialized wake resources")
            self.ready.emit(None, None, str(exc))


class LifecycleManager(QObject):
    """Own active resources and keep the avatar responsive during model work."""
    popover_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, settings: dict[str, Any], db: Database, project_root: Path) -> None:
        super().__init__()
        self.settings, self.db, self.project_root = settings, db, project_root
        self.engine = self.runtime = self.popover = None
        self._index_service = None
        self._active = False
        self._waking = False
        self._cancel_wake = False
        self._busy = False
        self._busy_count = 0
        self._sleep_requested = False
        self._shutting_down = False
        self._wake_worker: _WakeWorker | None = None
        self.idle = QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.timeout.connect(self.sleep)

    def toggle(self) -> None:
        if self._waking:
            self._cancel_wake = True
            self.status_changed.emit("sleep")
            if self.popover is not None:
                self.popover.hide()
        elif self._active:
            self.sleep()
        else:
            self.wake()

    def wake(self) -> None:
        if self._active:
            if self.popover is not None:
                self.popover_ready.emit(self.popover)
                self.popover.show()
            self.touch()
            return
        if self._waking or self._shutting_down:
            return
        self._waking = True
        self._cancel_wake = False
        self.status_changed.emit("active")
        from ui.popover_card import PopoverCard
        self.popover = PopoverCard(None, None, self.db, loading=True)
        self._connect_popover(self.popover)
        self.popover_ready.emit(self.popover)
        self._wake_worker = _WakeWorker(self.settings, self.db)
        self._wake_worker.ready.connect(self._on_wake_ready)
        self._wake_worker.finished.connect(self._on_wake_finished)
        self._wake_worker.start()

    def _on_wake_ready(self, engine: Any, runtime: Any, error: str) -> None:
        self._waking = False
        if error:
            if self.popover is not None:
                self.popover.close()
                self.popover = None
            self.status_changed.emit("sleep")
            self.error_occurred.emit(error)
            return
        if self._cancel_wake or self._shutting_down:
            for item in (runtime, engine):
                if item is not None:
                    try:
                        item.close()
                    except Exception:
                        log.exception("Could not release canceled wake resources")
            if self.popover is not None:
                self.popover.close()
                self.popover = None
            self.status_changed.emit("sleep")
            return
        self.engine, self.runtime = engine, runtime
        self._active = True
        self.status_changed.emit("active")
        if self.popover is None:
            from ui.popover_card import PopoverCard
            self.popover = PopoverCard(self.engine, self.runtime, self.db)
            self._connect_popover(self.popover)
        else:
            self.popover.set_runtime(self.engine, self.runtime)
        self.popover_ready.emit(self.popover)
        self.popover.show()
        from retrieval.index_worker import IndexQueueService
        index_config = self.settings["index"]
        self._index_service = IndexQueueService(
            runtime.indexer,
            batch_size=int(index_config.get("background_batch_size", 10)),
            poll_seconds=float(index_config.get("background_poll_seconds", 1.0)),
        )
        self._index_service.start()
        self.touch()

    def _connect_popover(self, popover: Any) -> None:
        """Connect user activity, busy accounting, and avatar state once."""
        popover.activity.connect(self.touch)
        popover.work_started.connect(self.begin_work)
        popover.work_finished.connect(self.end_work)
        popover.status_changed.connect(self.set_status)

    def _on_wake_finished(self) -> None:
        self._wake_worker = None

    def begin_work(self) -> None:
        """Suspend idle unload while an assistant request or approved command runs."""
        self._busy_count += 1
        self._busy = True
        self.idle.stop()

    def end_work(self) -> None:
        """Resume the idle clock when active work finishes."""
        self._busy_count = max(0, self._busy_count - 1)
        self._busy = self._busy_count > 0
        if self._busy:
            return
        if self._sleep_requested:
            self._finish_sleep()
        else:
            self.touch()

    def touch(self) -> None:
        if self._active and not self._busy:
            self.idle.start(max(1, int(self.settings["runtime"].get("idle_unload_seconds", 120))) * 1000)

    def set_status(self, status: str) -> None:
        self.status_changed.emit(status)

    def sleep(self) -> None:
        self.idle.stop()
        if self.popover is not None:
            self.popover.hide()
        if self._busy:
            self._sleep_requested = True
            return
        self._finish_sleep()

    def _finish_sleep(self) -> None:
        started = time.perf_counter()
        if self._index_service is not None:
            try:
                self._index_service.stop()
            except Exception:
                log.exception("Error while stopping the background index service")
            self._index_service = None
        for item in (self.runtime, self.engine):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    log.exception("Error while unloading FileBot component")
        if self.popover is not None:
            self.popover.close()
        self.runtime = self.engine = self.popover = None
        self._active = False
        self._busy = False
        self._busy_count = 0
        self._sleep_requested = False
        self.status_changed.emit("sleep")
        working_set = process_working_set_mb()
        log.info(
            "FileBot idle unload completed in %.3fs; working_set=%s",
            time.perf_counter() - started,
            f"{working_set:.1f} MiB" if working_set is not None else "unavailable",
        )

    def shutdown(self) -> None:
        self._shutting_down = True
        self.idle.stop()
        if self.popover is not None:
            self.popover.stop_tasks()
        if self._wake_worker is not None and self._wake_worker.isRunning():
            self._cancel_wake = True
            self._wake_worker.wait()
            if self._wake_worker.result is not None:
                for item in reversed(self._wake_worker.result):
                    try:
                        item.close()
                    except Exception:
                        log.exception("Could not release wake resources during shutdown")
            self._wake_worker = None
        self._busy = False
        self._finish_sleep()
