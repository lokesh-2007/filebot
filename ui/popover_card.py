"""Compact prompt/search card with clickable results and explicit CMD approval."""
from __future__ import annotations

import logging
import time
from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

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
        started = time.perf_counter()
        first_delta: float | None = None

        def emit_delta(value: str) -> None:
            nonlocal first_delta
            if value and first_delta is None:
                first_delta = time.perf_counter() - started
            self.delta.emit(value)

        try:
            if self.search:
                result = self.target.search(self.prompt)
            else:
                stream = getattr(self.target, "ask_streaming", None)
                result = stream(self.prompt, emit_delta) if callable(stream) else self.target.ask(self.prompt)
            log.info(
                "FileBot %s completed in %.3fs; time_to_first_token=%s",
                "search" if self.search else "chat",
                time.perf_counter() - started,
                f"{first_delta:.3f}s" if first_delta is not None else "n/a",
            )
            self.done.emit(result, "")
        except Exception as exc:
            log.info(
                "FileBot %s failed after %.3fs; time_to_first_token=%s",
                "search" if self.search else "chat",
                time.perf_counter() - started,
                f"{first_delta:.3f}s" if first_delta is not None else "n/a",
            )
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
    def __init__(self, engine: Any, runtime: Any, db: Database, *, loading: bool = False) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.engine, self.runtime, self.db = engine, runtime, db
        self.runner = CommandRunner(db)
        runtime_settings = getattr(runtime, "settings", {})
        roots = runtime_settings.get("index", {}).get("roots") if isinstance(runtime_settings, dict) else None
        self.file_ops = FileOpsTool(db, roots=roots)
        self.setWindowTitle("FileBot")
        self.setFixedWidth(390)
        self.setStyleSheet("QWidget{background:#171923;color:#f5f5f5;font:10pt 'Segoe UI'} QLineEdit,QTextEdit,QListWidget{background:#242736;border:1px solid #40445a;border-radius:7px;padding:7px} QPushButton{background:#5141a4;border:0;border-radius:6px;padding:6px 10px} QFrame{border:1px solid #45495e;border-radius:7px}")
        layout = QVBoxLayout(self)
        title = QLabel("FileBot  ·  local search")
        title.setStyleSheet("font-weight:700;font-size:12pt")
        layout.addWidget(title)
        self.glossary_button = QPushButton("Search vocabulary…")
        self.glossary_button.setToolTip("Edit shorthand that FileBot expands in local searches")
        self.glossary_button.clicked.connect(self.edit_glossary)
        layout.addWidget(self.glossary_button)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Search files or ask a question…")
        self.input.returnPressed.connect(self.submit)
        self.input.textEdited.connect(lambda _text: self.activity.emit())
        layout.addWidget(self.input)
        row = QHBoxLayout()
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(lambda: self.submit(force_search=True))
        self.ask_button = QPushButton("Ask")
        self.ask_button.clicked.connect(self.submit)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Cancel the current local model request")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel_request)
        row.addWidget(self.search_button)
        row.addWidget(self.ask_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.results = QListWidget()
        self.results.setMaximumHeight(210)
        self.results.itemActivated.connect(self.open_result)
        self.results.currentItemChanged.connect(self._update_feedback_controls)
        layout.addWidget(self.results)
        feedback = QHBoxLayout()
        self.helpful_button = QPushButton("Helpful")
        self.unhelpful_button = QPushButton("Not helpful")
        self.helpful_button.setEnabled(False)
        self.unhelpful_button.setEnabled(False)
        self.helpful_button.clicked.connect(lambda: self._record_feedback("up"))
        self.unhelpful_button.clicked.connect(lambda: self._record_feedback("down"))
        feedback.addWidget(self.helpful_button)
        feedback.addWidget(self.unhelpful_button)
        layout.addLayout(feedback)
        self.answer = QTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setMaximumHeight(110)
        layout.addWidget(self.answer)
        self.pending = QVBoxLayout()
        layout.addLayout(self.pending)
        self._task: _Task | None = None
        self._command_task: _CommandTask | None = None
        self._command_queue: list[str] = []
        self._request_elapsed_seconds = 0
        self._chat_has_deltas = False
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(10_000)
        self._request_timer.timeout.connect(self._show_wait_feedback)
        if loading:
            self.set_loading(True)
        self._refresh_pending()
        if self.db.pending_commands():
            self.status_changed.emit("cmd_pending")

    def edit_glossary(self) -> None:
        """Let the user edit SQLite-backed search aliases without restarting."""
        dialog = QDialog(self)
        dialog.setWindowTitle("FileBot search vocabulary")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Searches expand these aliases before retrieval. Edits take effect immediately."
        ))
        table = QTableWidget(0, 2, dialog)
        table.setHorizontalHeaderLabels(["Alias", "Expand to"])
        table.horizontalHeader().setStretchLastSection(True)
        for alias, expansion in self.db.glossary_entries():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(alias))
            table.setItem(row, 1, QTableWidgetItem(expansion))
        layout.addWidget(table)
        row_buttons = QHBoxLayout()
        add_row = QPushButton("Add alias")
        remove_row = QPushButton("Remove selected")
        row_buttons.addWidget(add_row)
        row_buttons.addWidget(remove_row)
        layout.addLayout(row_buttons)
        add_row.clicked.connect(lambda: self._add_glossary_row(table))
        remove_row.clicked.connect(lambda: self._remove_glossary_row(table))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        entries: dict[str, str] = {}
        for row in range(table.rowCount()):
            alias_item, expansion_item = table.item(row, 0), table.item(row, 1)
            alias = alias_item.text().strip() if alias_item else ""
            expansion = expansion_item.text().strip() if expansion_item else ""
            if not alias and not expansion:
                continue
            if not alias or not expansion:
                self.answer.setPlainText("Each glossary row needs both an alias and an expansion.")
                return
            if alias in entries:
                self.answer.setPlainText(f"Duplicate glossary alias: {alias}")
                return
            entries[alias] = expansion
        try:
            self.db.replace_glossary_entries(entries)
            self.answer.setPlainText(f"Saved {len(entries)} search alias(es). They are active now.")
        except (ValueError, RuntimeError) as exc:
            self.answer.setPlainText(f"Could not save the search vocabulary: {exc}")
            log.exception("Could not save search glossary")

    @staticmethod
    def _add_glossary_row(table: QTableWidget) -> None:
        """Append an editable alias row and focus its first cell."""
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.setCurrentCell(row, 0)
        table.editItem(table.item(row, 0))

    @staticmethod
    def _remove_glossary_row(table: QTableWidget) -> None:
        """Remove the selected glossary row, if one is selected."""
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)

    def set_loading(self, loading: bool) -> None:
        """Show immediate wake feedback and gate requests until retrieval is ready."""
        self.input.setEnabled(not loading)
        self.search_button.setEnabled(not loading)
        self.ask_button.setEnabled(not loading)
        self.stop_button.setEnabled(False)
        if loading:
            self.answer.setPlainText("Loading the local search index… You can search or ask as soon as FileBot is ready.")

    def set_runtime(self, engine: Any, runtime: Any) -> None:
        """Attach wake-loaded services to the already visible popover."""
        self.engine, self.runtime = engine, runtime
        runtime_settings = getattr(runtime, "settings", {})
        roots = runtime_settings.get("index", {}).get("roots") if isinstance(runtime_settings, dict) else None
        self.file_ops = FileOpsTool(self.db, roots=roots)
        self.set_loading(False)
        self.answer.setPlainText("Ready. Search local files or ask a question.")
        self._refresh_pending()

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
        self.stop_button.setEnabled(not force_search)
        self.status_changed.emit("active")
        self.answer.setPlainText(
            "Searching indexed files…" if force_search
            else "Starting the local model request… It can take a little while on first use. Press Stop to cancel."
        )
        self.work_started.emit()
        reset_cancellation = getattr(self.runtime, "reset_cancellation", None)
        if callable(reset_cancellation):
            reset_cancellation()
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
            f"Waiting for the local model ({self._request_elapsed_seconds} seconds)… "
            "Check that Ollama is running. Press Stop to cancel this request."
        )

    def _received(self, result: object, error: str) -> None:
        self._request_timer.stop()
        self.stop_button.setEnabled(False)
        self.input.setEnabled(True)
        self.search_button.setEnabled(True)
        self.ask_button.setEnabled(True)
        if error:
            self.answer.setPlainText(error)
            self.work_finished.emit()
            return
        if isinstance(result, list):
            self.results.clear()
            for match in result:
                path = str(match.get("path", ""))
                text = str(match.get("snippet", "")).replace("\n", " ")[:180]
                source = " · ".join(str(value) for value in (
                    match.get("page_or_line"), match.get("heading"),
                ) if value)
                detail = f"{text} · {source}" if text and source else text or source
                item = QListWidgetItem(f"{path}\n{detail}")
                item.setSizeHint(QSize(0, 54))
                item.setToolTip(f"{path}\n\n{detail}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.results.addItem(item)
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

    def cancel_request(self) -> None:
        """Cancel an in-flight model request without blocking the UI thread."""
        cancel = getattr(self.runtime, "cancel_pending", None)
        if callable(cancel) and cancel():
            self.stop_button.setEnabled(False)
            self.answer.setPlainText("Stopping the local model request…")
        else:
            self.answer.setPlainText("There is no cancellable model request running.")

    def open_result(self, item: QListWidgetItem) -> None:
        self.activity.emit()
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            try:
                self.file_ops.open_file(path)
            except (OSError, ValueError) as exc:
                log.exception("Could not open indexed file %s", path)
                self.answer.setPlainText(f"Could not open {path}: {exc}")

    def _update_feedback_controls(self, current: QListWidgetItem | None,
                                  _previous: QListWidgetItem | None = None) -> None:
        """Enable ranking feedback only when a concrete file result is selected."""
        enabled = current is not None and bool(current.data(Qt.ItemDataRole.UserRole))
        self.helpful_button.setEnabled(enabled)
        self.unhelpful_button.setEnabled(enabled)

    def _record_feedback(self, vote: str) -> None:
        """Persist a user's relevance signal for the currently selected file."""
        item = self.results.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not path:
            return
        try:
            if self.db.record_file_feedback(str(path), vote):
                label = "Helpful" if vote == "up" else "Not helpful"
                self.answer.setPlainText(f"Feedback saved as “{label}”; it will influence later searches.")
            else:
                self.answer.setPlainText("This file is no longer in FileBot's index.")
        except Exception as exc:
            log.exception("Could not save search feedback for %s", path)
            self.answer.setPlainText(f"Could not save feedback: {exc}")

    def _refresh_pending(self) -> None:
        while self.pending.count():
            child = self.pending.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        rows = self.db.pending_commands()
        for row in rows:
            card = QFrame()
            box = QVBoxLayout(card)
            details = QLabel(
                f"Pending CMD · {row['risk_level']}\n"
                f"Command: {row['command']}\n"
                f"Reason: {row['reason']}\n"
                f"Working directory: {row['cwd']}"
            )
            details.setTextFormat(Qt.TextFormat.PlainText)
            details.setWordWrap(True)
            box.addWidget(details)
            buttons = QHBoxLayout()
            approve, reject = QPushButton("Approve"), QPushButton("Reject")
            pid = str(row["proposal_id"])
            approve.clicked.connect(lambda _checked=False, key=pid: self._decide(key, True))
            reject.clicked.connect(lambda _checked=False, key=pid: self._decide(key, False))
            buttons.addWidget(approve)
            buttons.addWidget(reject)
            box.addLayout(buttons)
            self.pending.addWidget(card)
        if rows:
            self.status_changed.emit("cmd_pending")
        else:
            self.status_changed.emit("active")

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
