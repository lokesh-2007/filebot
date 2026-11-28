"""Compact prompt/search card with clickable results and explicit CMD approval."""
from __future__ import annotations

import logging
import re
from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout, QWidget)

from ui.result_delegate import ROW_HEIGHT, ResultDelegate
from ui.theme import PALETTE, STATUS_PILLS, stylesheet

from executor.cmd_runner import CommandRunner
from agent.tools.file_ops_tool import FileOpsTool
from storage.db import Database

log = logging.getLogger(__name__)


class _Task(QThread):
    done = pyqtSignal(object, str)
    delta = pyqtSignal(str)
    def __init__(self, target: Any, prompt: str, search: bool) -> None:
        super().__init__()
        self.target, self.prompt, self.search = target, prompt, search
    def run(self) -> None:
        try:
            if self.search:
                result = self.target.search(self.prompt)
            else:
                stream = getattr(self.target, "ask_streaming", None)
                result = stream(self.prompt, self.delta.emit) if callable(stream) else self.target.ask(self.prompt)
            self.done.emit(result, "")
        except Exception as exc:
            log.exception("FileBot request failed")
            self.done.emit(None, str(exc))


class _CommandTask(QThread):
    done = pyqtSignal(int, str, str, str)

    def __init__(self, runner: CommandRunner, proposal_id: str) -> None:
        super().__init__()
        self.runner, self.proposal_id = runner, proposal_id

    def run(self) -> None:
        try:
            code, stdout, stderr = self.runner.execute(self.proposal_id)
            self.done.emit(code, stdout, stderr, "")
        except Exception as exc:
            log.exception("Approved command execution failed")
            self.done.emit(-1, "", "", str(exc))


class PopoverCard(QWidget):
    """Search/chat panel that surfaces pending commands with Approve/Reject controls."""
    activity = pyqtSignal()
    work_started = pyqtSignal()
    work_finished = pyqtSignal()
    status_changed = pyqtSignal(str)
    def __init__(self, engine: Any, runtime: Any, db: Database) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.engine, self.runtime, self.db = engine, runtime, db
        self.runner = CommandRunner(db)
        runtime_settings = getattr(runtime, "settings", {})
        roots = runtime_settings.get("index", {}).get("roots") if isinstance(runtime_settings, dict) else None
        self.file_ops = FileOpsTool(db, roots=roots)
        self.setWindowTitle("FileBot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(440)
        self.setStyleSheet(stylesheet())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        panel = QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(0)
        title = QLabel("FileBot")
        title.setObjectName("title")
        subtitle = QLabel("Local file search · runs offline")
        subtitle.setObjectName("subtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch(1)
        self.status_pill = QLabel()
        self.status_pill.setObjectName("pill")
        header.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self._set_pill("active")

        self.input = QLineEdit()
        self.input.setPlaceholderText("Search files or ask a question…")
        self.input.setClearButtonEnabled(True)
        self.input.returnPressed.connect(self.submit)
        self.input.textEdited.connect(lambda _text: self.activity.emit())
        layout.addWidget(self.input)

        row = QHBoxLayout()
        self.search_button = QPushButton("Search files")
        self.search_button.clicked.connect(lambda: self.submit(force_search=True))
        self.ask_button = QPushButton("Ask FileBot")
        self.ask_button.setObjectName("primary")
        self.ask_button.clicked.connect(self.submit)
        row.addWidget(self.search_button)
        row.addWidget(self.ask_button)
        layout.addLayout(row)

        self.results_label = QLabel("RESULTS")
        self.results_label.setObjectName("section")
        self.results_label.hide()
        layout.addWidget(self.results_label)
        self.results = QListWidget()
        self.results.setMouseTracking(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results_delegate = ResultDelegate(self.results)
        self.results.setItemDelegate(self.results_delegate)
        self.results.itemActivated.connect(self.open_result)
        self.results.itemClicked.connect(self.open_result)
        self.results.hide()
        layout.addWidget(self.results)

        self.answer = QTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setMinimumHeight(64)
        self.answer.setMaximumHeight(104)
        self.answer.setPlaceholderText("Answers and status appear here.")
        layout.addWidget(self.answer)
        self.pending = QVBoxLayout()
        layout.addLayout(self.pending)

        footer = QHBoxLayout()
        for text in ("100% local", "No cloud", "Approve before any command"):
            chip = QLabel(text)
            chip.setObjectName("chip")
            footer.addWidget(chip)
        footer.addStretch(1)
        layout.addLayout(footer)
        self._task: _Task | None = None
        self._command_task: _CommandTask | None = None
        self._command_queue: list[str] = []
        self._request_elapsed_seconds = 0
        self._chat_has_deltas = False
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(10_000)
        self._request_timer.timeout.connect(self._show_wait_feedback)
        self._refresh_pending()
        if self.db.pending_commands():
            self.status_changed.emit("cmd_pending")

    def _set_pill(self, state: str) -> None:
        label, color = STATUS_PILLS.get(state, STATUS_PILLS["active"])
        self.status_pill.setText(f"●  {label}")
        self.status_pill.setStyleSheet(f"color:{color}")

    def submit(self, force_search: bool = False) -> None:
        if self._task is not None and self._task.isRunning():
            return
        prompt = self.input.text().strip()
        if not prompt:
            return
        self.activity.emit()
        self.input.setEnabled(False)
        self.search_button.setEnabled(False)
        self.ask_button.setEnabled(False)
        self.status_changed.emit("active")
        self._set_pill("working")
        self.answer.setPlainText(
            "Searching indexed files…" if force_search
            else "Waiting for the local model… The first response can take up to a minute while it loads."
        )
        self.work_started.emit()
        self._task = _Task(self.runtime, prompt, force_search)
        self._task.done.connect(self._received)
        self._task.delta.connect(self._received_delta)
        self._chat_has_deltas = False
        if not force_search:
            self._request_elapsed_seconds = 0
            self._request_timer.start()
        self._task.start()

    def _received_delta(self, delta: str) -> None:
        """Append a streamed model token on the UI thread."""
        if not delta:
            return
        if not self._chat_has_deltas:
            self._chat_has_deltas = True
            self._request_timer.stop()
            self.answer.clear()
        cursor = self.answer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)
        self.answer.setTextCursor(cursor)
        self.answer.ensureCursorVisible()

    def _show_wait_feedback(self) -> None:
        """Show elapsed time while waiting for the first streamed model token."""
        self._request_elapsed_seconds += self._request_timer.interval() // 1000
        self.answer.setPlainText(
            f"Still waiting for the local model ({self._request_elapsed_seconds} seconds)… "
            "FileBot is working; the first answer can be slow while the local model loads."
        )

    def _received(self, result: object, error: str) -> None:
        self._request_timer.stop()
        self.input.setEnabled(True)
        self.search_button.setEnabled(True)
        self.ask_button.setEnabled(True)
        self._set_pill("active")
        if error:
            self.answer.setPlainText(error)
            self.work_finished.emit()
            return
        if isinstance(result, list):
            self.results.clear()
            self.results_delegate.terms = [t for t in re.findall(r"\w+", self.input.text()) if t]
            for match in result:
                path = str(match.get("path", ""))
                text = str(match.get("snippet", "")).replace("\n", " ")[:180]
                item = QListWidgetItem(f"{path}\n{text}")
                item.setSizeHint(QSize(0, ROW_HEIGHT))
                item.setToolTip(f"{path}\n\n{text}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.results.addItem(item)
            self.results.setFixedHeight(min(len(result), 3) * ROW_HEIGHT + 4)
            self.results.setVisible(bool(result))
            self.results_label.setText(f"RESULTS  ·  {len(result)}")
            self.results_label.setVisible(bool(result))
            if not result:
                self.answer.setPlainText("No matching files found in the current index.")
            else:
                self.answer.setPlainText(
                    f"Found {len(result)} files. Select a result to open it, or click Ask "
                    "for a local model answer."
                )
        else:
            self.answer.setPlainText(str(result))
        self._refresh_pending()
        self.work_finished.emit()

    def open_result(self, item: QListWidgetItem) -> None:
        self.activity.emit()
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            try:
                self.file_ops.open_file(path)
            except (OSError, ValueError) as exc:
                log.exception("Could not open indexed file %s", path)
                self.answer.setPlainText(f"Could not open {path}: {exc}")

    def _refresh_pending(self) -> None:
        while self.pending.count():
            child = self.pending.takeAt(0)
            if child.widget():
                child.widget().hide()
                child.widget().deleteLater()
        rows = self.db.pending_commands()
        for row in rows:
            card = QFrame()
            card.setObjectName("cmdcard")
            box = QVBoxLayout(card)
            box.setContentsMargins(12, 10, 12, 10)
            details = QLabel(
                f"Pending CMD · {row['risk_level']}\n"
                f"Command: {row['command']}\n"
                f"Reason: {row['reason']}\n"
                f"Working directory: {row['cwd']}"
            )
            details.setTextFormat(Qt.TextFormat.PlainText)
            details.setWordWrap(True)
            badge = QLabel("⚠  Approval required — nothing runs until you click Approve")
            badge.setStyleSheet(f"color:{PALETTE['warn']};font-weight:600;font-size:8.5pt")
            badge.setTextFormat(Qt.TextFormat.PlainText)
            badge.setWordWrap(True)
            box.addWidget(details)
            box.addWidget(badge)
            buttons = QHBoxLayout()
            approve, reject = QPushButton("Approve"), QPushButton("Reject")
            approve.setObjectName("approve")
            reject.setObjectName("reject")
            pid = str(row["proposal_id"])
            approve.clicked.connect(lambda _checked=False, key=pid: self._decide(key, True))
            reject.clicked.connect(lambda _checked=False, key=pid: self._decide(key, False))
            buttons.addWidget(approve)
            buttons.addWidget(reject)
            box.addLayout(buttons)
            self.pending.addWidget(card)
        if rows:
            self.status_changed.emit("cmd_pending")
            self._set_pill("cmd_pending")
        else:
            self.status_changed.emit("active")
            self._set_pill("active")

    def _decide(self, proposal_id: str, approved: bool) -> None:
        self.activity.emit()
        if not self.db.decide_command(proposal_id, approved):
            self.answer.setPlainText("This proposal has already been handled.")
            self._refresh_pending()
            return
        if approved:
            self._command_queue.append(proposal_id)
            if self._command_task is None:
                self.status_changed.emit("active")
                self._set_request_controls_enabled(False)
                self.work_started.emit()
                self._start_next_command()
        self._refresh_pending()

    def _start_next_command(self) -> None:
        if not self._command_queue:
            self._command_task = None
            self._set_request_controls_enabled(True)
            self.work_finished.emit()
            self._refresh_pending()
            return
        proposal_id = self._command_queue.pop(0)
        task = _CommandTask(self.runner, proposal_id)
        self._command_task = task
        task.done.connect(self._command_received)
        task.start()

    def _command_received(self, code: int, stdout: str, stderr: str, error: str) -> None:
        if error:
            self.answer.setPlainText(f"Could not run approved command: {error}")
        else:
            self.answer.setPlainText(f"Exit code: {code}\n{stdout}\n{stderr}".strip())
        task = self.sender()
        if task is self._command_task:
            self._command_task = None
        if task is not None:
            task.deleteLater()
        if self._command_queue:
            self._start_next_command()
        else:
            self._refresh_pending()
            self._set_request_controls_enabled(True)
            self.work_finished.emit()

    def _set_request_controls_enabled(self, enabled: bool) -> None:
        """Prevent a second agent request from racing an approved CMD action."""
        self.input.setEnabled(enabled)
        self.search_button.setEnabled(enabled)
        self.ask_button.setEnabled(enabled)

    def stop_tasks(self) -> None:
        """Cancel active model work and wait for approved commands to be audited."""
        if self._task is not None and self._task.isRunning():
            while self._task.isRunning():
                cancel = getattr(self.runtime, "cancel_pending", None)
                if callable(cancel):
                    cancel()
                self._task.wait(100)
        if self._command_task is not None and self._command_task.isRunning():
            # CommandRunner has a bounded subprocess timeout; wait so its audit
            # record is finalized before the runtime and database are released.
            self._command_task.wait()
            self._command_task = None
        # These rows are already approved by the user's click. Finish them before
        # shutdown so the audit log cannot leave an approved-but-unrun command.
        while self._command_queue:
            proposal_id = self._command_queue.pop(0)
            try:
                self.runner.execute(proposal_id)
            except Exception:
                log.exception("Could not finish an approved command during shutdown")
