"""Qt lifecycle tests for work accounting and serialized approved commands."""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from daemon.lifecycle_manager import LifecycleManager
from ui.avatar_bubble import AvatarBubble
from ui.popover_card import PopoverCard


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_idle_unload_waits_for_all_active_work(self) -> None:
        manager = LifecycleManager({"runtime": {"idle_unload_seconds": 60}}, None, Path.cwd())
        manager._active = True
        manager.begin_work()
        manager.begin_work()
        self.assertEqual(manager._busy_count, 2)
        manager.end_work()
        self.assertTrue(manager._busy)
        self.assertFalse(manager.idle.isActive())
        manager.end_work()
        self.assertFalse(manager._busy)
        self.assertTrue(manager.idle.isActive())
        manager.idle.stop()

    def test_avatar_exposes_a_named_status_window(self) -> None:
        avatar = AvatarBubble()
        self.assertEqual(avatar.windowTitle(), "FileBot")
        self.assertEqual(avatar.accessibleName(), "FileBot status avatar")
        self.assertEqual(avatar.label.accessibleName(), "FileBot status")
        avatar.close()

    def test_approved_commands_run_in_order_one_at_a_time(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.rows = [
                    {"proposal_id": "first", "risk_level": "gated", "command": "mkdir first"},
                    {"proposal_id": "second", "risk_level": "gated", "command": "mkdir second"},
                ]

            def pending_commands(self):
                return [row for row in self.rows if row.get("status", "pending") == "pending"]

            def decide_command(self, proposal_id: str, approved: bool) -> bool:
                row = next(item for item in self.rows if item["proposal_id"] == proposal_id)
                row["status"] = "approved" if approved else "rejected"
                return True

        class FakeRunner:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.completed: list[str] = []
                self.done = threading.Event()
                self.lock = threading.Lock()

            def execute(self, proposal_id: str):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.03)
                with self.lock:
                    self.completed.append(proposal_id)
                    self.active -= 1
                    if len(self.completed) == 2:
                        self.done.set()
                return 0, proposal_id, ""

        db, runner = FakeDatabase(), FakeRunner()
        with patch("ui.popover_card.CommandRunner", return_value=runner):
            card = PopoverCard(engine=None, runtime=None, db=db)
        started: list[bool] = []
        finished: list[bool] = []
        card.work_started.connect(lambda: started.append(True))
        card.work_finished.connect(lambda: finished.append(True))
        card._decide("first", True)
        self.assertFalse(card.input.isEnabled())
        card._decide("second", True)
        deadline = time.monotonic() + 2
        while not runner.done.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(runner.done.is_set(),
                        f"task={card._command_task!r}, queue={card._command_queue}, started={started}, answer={card.answer.toPlainText()}")
        deadline = time.monotonic() + 2
        while card._command_task is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertEqual(runner.completed, ["first", "second"])
        self.assertEqual(runner.max_active, 1)
        self.assertEqual(started, [True])
        self.assertEqual(finished, [True])
        self.assertTrue(card.input.isEnabled())
        card.close()


if __name__ == "__main__":
    unittest.main()

