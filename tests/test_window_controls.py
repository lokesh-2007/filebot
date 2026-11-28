"""Minimise / Close / End task controls on the popover and the avatar menu."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from daemon.lifecycle_manager import LifecycleManager
from storage.db import Database
from ui.avatar_bubble import AvatarBubble
from ui.popover_card import PopoverCard


class _Engine:
    def search(self, *_a, **_k):
        return []


class _Runtime:
    settings = {"index": {"roots": []}}
    def cancel_pending(self):
        return False


class WindowControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "w.db")
        self.db.initialize()
        self.manager = LifecycleManager({"runtime": {"idle_unload_seconds": 60}}, self.db, Path.cwd())
        self.popover = PopoverCard(_Engine(), _Runtime(), self.db)
        self.addCleanup(self.popover.close)
        self.manager.popover = self.popover
        self.manager._active = True
        for signal, slot in ((self.popover.minimize_requested, self.manager.minimize),
                             (self.popover.close_requested, self.manager.sleep),
                             (self.popover.quit_requested, self.manager.request_quit)):
            signal.connect(slot)
        self.addCleanup(self.manager.idle.stop)

    def test_three_labelled_buttons_exist_with_tooltips(self) -> None:
        for button, name in ((self.popover.minimize_button, "Minimise"),
                             (self.popover.close_button, "Close"),
                             (self.popover.quit_button, "End task")):
            self.assertEqual(button.accessibleName(), name)
            self.assertTrue(button.toolTip())

    def test_minimise_hides_panel_but_stays_awake_and_avatar_click_restores_it(self) -> None:
        self.popover.show()
        QTest.mouseClick(self.popover.minimize_button, Qt.MouseButton.LeftButton)
        self.assertFalse(self.popover.isVisible())
        self.assertTrue(self.manager._active)
        self.assertIsNotNone(self.manager.popover)
        shown: list[object] = []
        self.manager.popover_ready.connect(shown.append)
        self.manager.toggle()  # avatar click while minimised
        self.assertTrue(self.popover.isVisible())
        self.assertEqual(shown, [self.popover])
        self.assertTrue(self.manager._active)

    def test_avatar_click_while_panel_is_open_still_closes_it(self) -> None:
        self.popover.show()
        states: list[str] = []
        self.manager.status_changed.connect(states.append)
        self.manager.toggle()
        self.assertFalse(self.manager._active)
        self.assertEqual(states[-1], "sleep")

    def test_close_unloads_and_hides(self) -> None:
        self.popover.show()
        QTest.mouseClick(self.popover.close_button, Qt.MouseButton.LeftButton)
        self.assertFalse(self.popover.isVisible())
        self.assertFalse(self.manager._active)
        self.assertIsNone(self.manager.popover)

    def test_end_task_requests_application_quit(self) -> None:
        asked: list[bool] = []
        self.manager.quit_requested.connect(lambda: asked.append(True))
        self.popover.show()
        QTest.mouseClick(self.popover.quit_button, Qt.MouseButton.LeftButton)
        self.assertEqual(asked, [True])

    def test_escape_minimises(self) -> None:
        self.popover.show()
        QTest.keyClick(self.popover, Qt.Key.Key_Escape)
        self.assertFalse(self.popover.isVisible())

    def test_avatar_has_a_quit_signal(self) -> None:
        avatar = AvatarBubble()
        self.addCleanup(avatar.close)
        got: list[bool] = []
        avatar.quit_requested.connect(lambda: got.append(True))
        avatar.quit_requested.emit()
        self.assertEqual(got, [True])


if __name__ == "__main__":
    unittest.main()
