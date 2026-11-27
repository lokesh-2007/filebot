"""Qt lifecycle tests for work accounting and serialized approved commands."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QTableWidget, QWidget

from daemon.lifecycle_manager import LifecycleManager
from daemon.instance_guard import InstanceGuard
from daemon.metrics import process_working_set_mb
from ui.avatar_bubble import AvatarBubble
from ui.popover_card import PopoverCard
from storage.db import Database


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_working_set_metrics_degrade_cleanly_off_windows(self) -> None:
        with patch("daemon.metrics.os.name", "posix"):
            self.assertIsNone(process_working_set_mb())

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

    def test_wake_shows_loading_popover_before_runtime_initialization(self) -> None:
        class DelayedWakeWorker(QObject):
            ready = pyqtSignal(object, object, str)
            finished = pyqtSignal()

            def start(self) -> None:
                pass

            def isRunning(self) -> bool:
                return False

        class FakeDatabase:
            def pending_commands(self):
                return []

        manager = LifecycleManager({"runtime": {"idle_unload_seconds": 60}}, FakeDatabase(), Path.cwd())
        with patch("daemon.lifecycle_manager._WakeWorker", return_value=DelayedWakeWorker()):
            manager.wake()

        self.assertIsNotNone(manager.popover)
        self.assertIn("Loading the local search index", manager.popover.answer.toPlainText())
        self.assertFalse(manager.popover.input.isEnabled())
        self.assertFalse(manager.popover.search_button.isEnabled())
        manager.shutdown()

    def test_instance_guard_allows_only_one_process_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests") as directory:
            lock_path = Path(directory) / "filebot.lock"
            first, second = InstanceGuard(lock_path), InstanceGuard(lock_path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_avatar_exposes_a_named_status_window(self) -> None:
        avatar = AvatarBubble()
        self.assertEqual(avatar.windowTitle(), "FileBot")
        self.assertEqual(avatar.accessibleName(), "FileBot status avatar")
        self.assertEqual(avatar.label.accessibleName(), "FileBot status")
        avatar.close()

    def test_popover_stays_inside_active_screen_work_area(self) -> None:
        avatar = AvatarBubble()
        screen = avatar.screen()
        self.assertIsNotNone(screen)
        area = screen.availableGeometry()
        avatar.move(area.topLeft())
        avatar.show()
        popover = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        popover.setFixedSize(390, 300)
        avatar.show_popover(popover)
        self.app.processEvents()

        self.assertTrue(area.contains(popover.frameGeometry()))
        popover.close()
        avatar.close()

    def test_popover_shows_local_model_feedback_while_chat_runs(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        class SlowRuntime:
            def __init__(self) -> None:
                self.release = threading.Event()
                self.reset_called = False

            def reset_cancellation(self) -> None:
                self.reset_called = True

            def ask(self, prompt: str) -> str:
                self.release.wait(timeout=2)
                return "Answer received"

        runtime = SlowRuntime()
        card = PopoverCard(engine=None, runtime=runtime, db=FakeDatabase())
        card.input.setText("hello")
        card.submit()
        self.assertTrue(runtime.reset_called)
        self.assertIn("Starting the local model request", card.answer.toPlainText())
        self.assertFalse(card.input.isEnabled())
        runtime.release.set()
        deadline = time.monotonic() + 2
        while card._task is not None and card._task.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertEqual(card.answer.toPlainText(), "Answer received")
        self.assertTrue(card.input.isEnabled())
        card.close()

    def test_typing_resets_the_popover_idle_activity_timer(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        card = PopoverCard(engine=None, runtime=None, db=FakeDatabase())
        activity: list[bool] = []
        card.activity.connect(lambda: activity.append(True))
        card.show()
        card.input.setFocus()
        QTest.keyClicks(card.input, "search")
        self.app.processEvents()

        self.assertGreaterEqual(len(activity), len("search"))
        card.close()

    def test_search_results_show_full_path_and_snippet(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        card = PopoverCard(engine=None, runtime=None, db=FakeDatabase())
        path = r"C:\Users\lokes\Documents\notes.md"
        card._received([{"path": path, "snippet": "The exact matching sentence."}], "")

        item = card.results.item(0)
        self.assertIn(path, item.text())
        self.assertIn("The exact matching sentence.", item.text())
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole), path)
        self.assertIn(path, item.toolTip())
        self.assertIn("click Ask", card.answer.toPlainText())
        card.close()

    def test_chat_wait_feedback_reports_elapsed_time(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        card = PopoverCard(engine=None, runtime=None, db=FakeDatabase())
        card._show_wait_feedback()

        self.assertIn("10 seconds", card.answer.toPlainText())
        self.assertIn("Check that Ollama is running", card.answer.toPlainText())
        card.close()

    def test_chat_can_be_cancelled_without_blocking_the_popover(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        class CancellableRuntime:
            def __init__(self) -> None:
                self.release = threading.Event()
                self.cancelled = threading.Event()

            def ask(self, prompt: str) -> str:
                self.release.wait(timeout=2)
                return "Request cancelled" if self.cancelled.is_set() else "Answer received"

            def cancel_pending(self) -> bool:
                self.cancelled.set()
                self.release.set()
                return True

        runtime = CancellableRuntime()
        card = PopoverCard(engine=None, runtime=runtime, db=FakeDatabase())
        card.input.setText("hello")
        card.submit()
        self.assertTrue(card.stop_button.isEnabled())
        QTest.mouseClick(card.stop_button, Qt.MouseButton.LeftButton)
        deadline = time.monotonic() + 2
        while card._task is not None and card._task.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(runtime.cancelled.is_set())
        self.assertFalse(card.stop_button.isEnabled())
        self.assertEqual(card.answer.toPlainText(), "Request cancelled")
        card.close()

    def test_glossary_editor_saves_changes_through_the_dialog(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests") as directory:
            db = Database(Path(directory) / "state.sqlite3")
            db.initialize()
            db.seed_glossary_entries({"spec": "product specification"})
            card = PopoverCard(engine=None, runtime=None, db=db)

            def edit_and_save() -> None:
                dialog = self.app.activeModalWidget()
                table = dialog.findChild(QTableWidget)
                table.item(0, 1).setText("technical specification")
                table.insertRow(1)
                from PyQt6.QtWidgets import QTableWidgetItem
                table.setItem(1, 0, QTableWidgetItem("api"))
                table.setItem(1, 1, QTableWidgetItem("application programming interface"))
                buttons = dialog.findChild(QDialogButtonBox)
                buttons.button(QDialogButtonBox.StandardButton.Save).click()

            QTimer.singleShot(0, edit_and_save)
            card.edit_glossary()

            self.assertEqual(
                db.glossary_entries(),
                [("api", "application programming interface"), ("spec", "technical specification")],
            )
            self.assertEqual(db.expand_glossary("SPEC API"),
                             "technical specification application programming interface")
            card.close()

    def test_saved_feedback_refreshes_retrieval_signals(self) -> None:
        class FakeEngine:
            refreshed = False

            def refresh_file_signal(self, _path: str) -> None:
                self.refreshed = True

        class FakeDatabase:
            def record_file_feedback(self, _path: str, _vote: str) -> bool:
                return True

        engine = FakeEngine()
        card = PopoverCard(engine=engine, runtime=None, db=FakeDatabase())
        item = QListWidgetItem("selected.md")
        item.setData(Qt.ItemDataRole.UserRole, "C:/docs/selected.md")
        card.results.addItem(item)
        card.results.setCurrentItem(item)

        card._record_feedback("up")

        self.assertTrue(engine.refreshed)
        self.assertIn("Feedback saved", card.answer.toPlainText())
        card.close()

    def test_popover_displays_chat_text_before_the_turn_finishes(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        class SlowStreamingRuntime:
            def __init__(self) -> None:
                self.release = threading.Event()

            def ask_streaming(self, prompt, on_delta) -> str:
                on_delta("Partial answer")
                self.release.wait(timeout=2)
                return "Partial answer complete"

        runtime = SlowStreamingRuntime()
        card = PopoverCard(engine=None, runtime=runtime, db=FakeDatabase())
        card.input.setText("hello")
        card.submit()
        deadline = time.monotonic() + 1
        while "Partial answer" not in card.answer.toPlainText() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

        self.assertEqual(card.answer.toPlainText(), "Partial answer")
        self.assertFalse(card._request_timer.isActive())
        runtime.release.set()
        deadline = time.monotonic() + 2
        while card._task is not None and card._task.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertEqual(card.answer.toPlainText(), "Partial answer complete")
        card.close()

    def test_empty_search_results_explain_that_the_index_has_no_match(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return []

        card = PopoverCard(engine=None, runtime=None, db=FakeDatabase())
        card._received([], "")

        self.assertEqual(card.results.count(), 0)
        self.assertIn("No matching files", card.answer.toPlainText())
        card.close()

    def test_command_approval_shows_reason_and_directory_as_plain_text(self) -> None:
        class FakeDatabase:
            def pending_commands(self):
                return [{"proposal_id": "pending-1", "risk_level": "gated",
                         "command": "mkdir <new-folder>", "reason": "Create the requested folder",
                         "cwd": r"C:\Users\lokes\Documents"}]

        card = PopoverCard(engine=None, runtime=None, db=FakeDatabase())
        frame = card.pending.itemAt(0).widget()
        details = frame.findChild(QLabel)

        self.assertEqual(details.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("Create the requested folder", details.text())
        self.assertIn(r"C:\Users\lokes\Documents", details.text())
        self.assertIn("mkdir <new-folder>", details.text())
        card.close()

    def test_approved_commands_run_in_order_one_at_a_time(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.rows = [
                    {"proposal_id": "first", "risk_level": "gated", "command": "mkdir first",
                     "reason": "create first", "cwd": "C:/"},
                    {"proposal_id": "second", "risk_level": "gated", "command": "mkdir second",
                     "reason": "create second", "cwd": "C:/"},
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
