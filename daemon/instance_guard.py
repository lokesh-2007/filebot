"""Prevent duplicate FileBot tray, watcher, and hotkey processes."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QLockFile


class InstanceGuard:
    """Own a process lock file for as long as one FileBot instance is active."""

    def __init__(self, path: str | Path) -> None:
        self._lock = QLockFile(str(Path(path).expanduser().resolve()))
        # The app keeps this lock for its whole lifetime; rely on process liveness,
        # not the lock file's timestamp, to identify stale owners after a crash.
        self._lock.setStaleLockTime(0)
        self._acquired = False

    def acquire(self) -> bool:
        """Acquire without waiting; return false when another instance owns it."""
        if self._acquired:
            return True
        self._acquired = self._lock.tryLock(0)
        return self._acquired

    def release(self) -> None:
        """Release the lock after orderly application shutdown."""
        if self._acquired:
            self._lock.unlock()
            self._acquired = False
