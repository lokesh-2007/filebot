"""Compact prompt/search card with clickable results and explicit CMD approval."""
from __future__ import annotations

import logging
import time
from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QTableWidget,
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
    """Single-prompt file chat with visible approvals for sensitive actions."""
    activity = pyqtSignal()
    work_started = pyqtSignal()
    work_finished = pyqtSignal()
    status_changed = pyqtSignal(str)
    def __init__(self, engine: Any, runtime: Any, db: Database, *, loading: bool = False) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.engine, self.runtime, self.db = engine, runtime, db
        self.runner = CommandRunner(db)
        runtime_settings = getattr(runtime, "settings", {})
        roots = runtime_settings.get("index", {}).get("roots") if isinstance(runtime_settings, dict) else None
        self.file_ops = FileOpsTool(db, roots=roots)
        self.setWindowTitle("FileBot")
        self.setMinimumSize(520, 560)
        self.resize(640, 720)
        self.setStyleSheet("QWidget{background:#171923;color:#f5f5f5;font:10pt 'Segoe UI'} QLineEdit,QTextEdit,QListWidget{background:#242736;border:1px solid #40445a;border-radius:7px;padding:8px} QPushButton{background:#5141a4;border:0;border-radius:6px;padding:7px 12px} QPushButton:disabled{background:#35384a;color:#9298aa} QFrame{border:1px solid #45495e;border-radius:7px}")
        layout = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("FileBot")
        title.setStyleSheet("font-weight:700;font-size:12pt")
        title_row.addWidget(title)
        subtitle = QLabel("Local file assistant")
        subtitle.setStyleSheet("color:#aeb4c6")
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        self.glossary_button = QPushButton("Search vocabulary…")
        self.glossary_button.setToolTip("Edit shorthand that FileBot expands in local searches")
        self.glossary_button.clicked.connect(self.edit_glossary)
        self.options_button = QPushButton("⋯")
        self.options_button.setAccessibleName("FileBot options")
        self.options_button.setFixedWidth(36)
        options = QMenu(self)
        options.addAction("Search vocabulary…", self.edit_glossary)
        self.options_button.setMenu(options)
        title_row.addWidget(self.options_button)
        layout.addLayout(title_row)
        self.answer = QTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setPlaceholderText("Your conversation with FileBot will appear here.")
        layout.addWidget(self.answer, 1)
        self.results = QListWidget()
        self.results.setMaximumHeight(160)
        self.results.hide()
        self.results.itemActivated.connect(self.open_result)
        self.results.currentItemChanged.connect(self._update_feedback_controls)
        layout.addWidget(self.results)
        self.feedback_row = QWidget()
        feedback = QHBoxLayout(self.feedback_row)
        feedback.setContentsMargins(0, 0, 0, 0)
        self.helpful_button = QPushButton("Helpful")
        self.unhelpful_button = QPushButton("Not helpful")
        self.helpful_button.setEnabled(False)
        self.unhelpful_button.setEnabled(False)
        self.helpful_button.clicked.connect(lambda: self._record_feedback("up"))
        self.unhelpful_button.clicked.connect(lambda: self._record_feedback("down"))
        feedback.addWidget(self.helpful_button)
        feedback.addWidget(self.unhelpful_button)
        self.feedback_row.hide()
        layout.addWidget(self.feedback_row)
        self.pending = QVBoxLayout()
        layout.addLayout(self.pending)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask FileBot to find, read, create, or use cmd: <command> for approval…")
        self.input.setAccessibleName("Message FileBot")
        self.input.returnPressed.connect(self.submit)
        self.input.textEdited.connect(lambda _text: self.activity.emit())
        layout.addWidget(self.input)
        row = QHBoxLayout()
        self.cmd_button = QPushButton("CMD")
        self.cmd_button.setAccessibleName("Enter a CMD command for approval")
        self.cmd_button.setToolTip("Enter one CMD command in chat; FileBot will ask you to approve it before running")
        self.cmd_button.clicked.connect(self._insert_cmd_prefix)
        self.send_button = QPushButton("Send")
        self.send_button.setDefault(True)
        self.send_button.clicked.connect(self.submit)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Cancel the current local model request")
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self.cancel_request)
        row.addWidget(self.cmd_button)
        row.addWidget(self.send_button, 1)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        # Kept as aliases for lifecycle callers; both refer to the single chat action.
        self.search_button = self.send_button
        self.ask_button = self.send_button
        self._task: _Task | None = None
        self._command_task: _CommandTask | None = None
        self._command_queue: list[str] = []
        self._request_elapsed_seconds = 0
        self._chat_has_deltas = False
        self._chat_session_key = str(getattr(runtime, "_session", "filebot:desktop"))
        self._chat_turns: list[tuple[str, str]] = []
        self._load_chat_turns()
        self._current_chat_prompt: str | None = None
        self._current_stream_text = ""
        self._pending_stream_deltas: list[str] = []
        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setSingleShot(True)
        self._stream_flush_timer.setInterval(30)
        self._stream_flush_timer.timeout.connect(self._flush_stream_deltas)
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(3_000)
        self._request_timer.timeout.connect(self._show_wait_feedback)
        if loading:
            self.set_loading(True)
        elif self._chat_turns:
            self._render_chat_transcript()
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
        self.send_button.setEnabled(not loading)
        self.cmd_button.setEnabled(not loading)
        self.stop_button.setEnabled(False)
        if loading:
            message = "Loading the local search index… You can send any file or chat request as soon as FileBot is ready."
            if self._chat_turns:
                self._render_chat_transcript(message)
            else:
                self.answer.setPlainText(message)

    def set_runtime(self, engine: Any, runtime: Any) -> None:
        """Attach wake-loaded services to the already visible popover."""
        self.engine, self.runtime = engine, runtime
        self._chat_session_key = str(getattr(runtime, "_session", "filebot:desktop"))
        self._load_chat_turns()
        runtime_settings = getattr(runtime, "settings", {})
        roots = runtime_settings.get("index", {}).get("roots") if isinstance(runtime_settings, dict) else None
        self.file_ops = FileOpsTool(self.db, roots=roots)
        self.set_loading(False)
        if self._chat_turns:
            self._render_chat_transcript()
        else:
            self.answer.setPlainText("Ready. Ask FileBot to find, read, summarize, or manage a file.")
        self._refresh_pending()

    def submit(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        prompt = self.input.text().strip()
        if not prompt:
            return
        self.activity.emit()
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self.cmd_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self.status_changed.emit("active")
        # Every request now enters the same conversation flow. FileBot routes
        # find/read/open/create/delete requests from the user's words.
        self._current_chat_prompt = prompt
        self._current_stream_text = ""
        self.results.hide()
        self.feedback_row.hide()
        self._render_chat_transcript("Thinking…")
        self.work_started.emit()
        reset_cancellation = getattr(self.runtime, "reset_cancellation", None)
        if callable(reset_cancellation):
            reset_cancellation()
        self._pending_stream_deltas.clear()
        self._stream_flush_timer.stop()
        self._task = _Task(self.runtime, prompt, False)
        self._task.done.connect(self._received)
        self._task.delta.connect(self._received_delta)
        self._chat_has_deltas = False
        self._request_elapsed_seconds = 0
        self._request_timer.start()
        self._task.start()

    def _received_delta(self, delta: str) -> None:
        """Batch streamed chunks to keep QTextEdit layout off the token path."""
        if not delta:
            return
        if not self._chat_has_deltas:
            self._chat_has_deltas = True
            self._request_timer.stop()
            if self._current_chat_prompt is not None:
                self._render_chat_transcript()
            else:
                self.answer.clear()
        self._pending_stream_deltas.append(delta)
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def _flush_stream_deltas(self) -> None:
        """Append buffered model text in one UI update."""
        if not self._pending_stream_deltas:
            return
        delta = "".join(self._pending_stream_deltas)
        self._pending_stream_deltas.clear()
        first_chat_delta = self._current_chat_prompt is not None and not self._current_stream_text
        self._current_stream_text += delta
        cursor = self.answer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if first_chat_delta:
            cursor.insertText("\n")
        cursor.insertText(delta)
        self.answer.setTextCursor(cursor)
        self.answer.ensureCursorVisible()

    def _show_wait_feedback(self) -> None:
        """Show elapsed time while waiting for the first streamed model token."""
        self._request_elapsed_seconds += self._request_timer.interval() // 1000
        status = (f"Still working on your reply — {self._request_elapsed_seconds} seconds. "
                  "Press Stop to cancel.")
        if self._current_chat_prompt is not None:
            self._render_chat_transcript(status)
        else:
            self.answer.setPlainText(status)

    def _render_chat_transcript(self, pending: str | None = None) -> None:
        """Show prior turns plus the current streamed turn in the answer pane."""
        lines: list[str] = []
        for user_text, assistant_text in self._chat_turns:
            lines.extend((f"You:\n{user_text}", "", f"FileBot:\n{assistant_text}", ""))
        if self._current_chat_prompt is not None:
            lines.extend((f"You:\n{self._current_chat_prompt}", "", "FileBot:"))
            current = self._current_stream_text or pending or ""
            if current:
                lines.append(current)
        self.answer.setPlainText("\n".join(lines).rstrip())
        cursor = self.answer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.answer.setTextCursor(cursor)
        self.answer.ensureCursorVisible()

    def _load_chat_turns(self) -> None:
        """Restore recent completed turns from the local conversation store."""
        load = getattr(self.db, "recent_chat_turns", None)
        if not callable(load):
            return
        try:
            turns = load(self._chat_session_key, limit=50)
            self._chat_turns = [(str(user), str(reply)) for user, reply in turns][-50:]
        except Exception:
            log.exception("Could not load the local chat transcript")

    def _remember_chat_turn(self, user_text: str, assistant_text: str, *, persist: bool = True) -> None:
        """Keep the visible transcript in sync with the bounded local store."""
        save = getattr(self.db, "record_chat_turn", None)
        if persist and callable(save):
            try:
                save(self._chat_session_key, user_text, assistant_text, retain=50)
            except Exception:
                log.exception("Could not save the local chat transcript")
        self._chat_turns.append((user_text, assistant_text))
        self._chat_turns = self._chat_turns[-50:]

    def _received(self, result: object, error: str) -> None:
        self._request_timer.stop()
        self._stream_flush_timer.stop()
        self._flush_stream_deltas()
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(False)
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.cmd_button.setEnabled(True)
        if error:
            if self._current_chat_prompt is not None:
                self._remember_chat_turn(self._current_chat_prompt, error, persist=False)
                self._current_chat_prompt = None
                self._current_stream_text = ""
                self._render_chat_transcript()
            else:
                self.answer.setPlainText(error)
            self._refresh_pending()
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
            self.results.setVisible(bool(result))
            self.feedback_row.setVisible(bool(result))
            if not result:
                response = "No matching files found in the current index."
            else:
                response = f"Found {len(result)} matching files. Select one to open it, or ask a follow-up."
            if self._current_chat_prompt is not None:
                self._remember_chat_turn(self._current_chat_prompt, response)
                self._current_chat_prompt = None
                self._current_stream_text = ""
                self._render_chat_transcript()
            else:
                self.answer.setPlainText(response)
        else:
            final_text = str(result)
            if self._current_chat_prompt is not None:
                self._remember_chat_turn(self._current_chat_prompt, final_text)
                self._current_chat_prompt = None
                self._current_stream_text = ""
                self._render_chat_transcript()
            elif not self._chat_has_deltas or self.answer.toPlainText() != final_text:
                self.answer.setPlainText(final_text)
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
                refresh = getattr(self.engine, "refresh_file_signal", None)
                if callable(refresh):
                    refresh(path)
            except (OSError, ValueError) as exc:
                log.exception("Could not open indexed file %s", path)
                self.answer.setPlainText(f"Could not open {path}: {exc}")

    def _update_feedback_controls(self, current: QListWidgetItem | None,
                                  _previous: QListWidgetItem | None = None) -> None:
        """Enable ranking feedback only when a concrete file result is selected."""
        enabled = current is not None and bool(current.data(Qt.ItemDataRole.UserRole))
        self.helpful_button.setEnabled(enabled)
        self.unhelpful_button.setEnabled(enabled)
        self.feedback_row.setVisible(enabled)

    def _record_feedback(self, vote: str) -> None:
        """Persist a user's relevance signal for the currently selected file."""
        item = self.results.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not path:
            return
        try:
            if self.db.record_file_feedback(str(path), vote):
                refresh = getattr(self.engine, "refresh_file_signal", None)
                if callable(refresh):
                    refresh()
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
            try:
                shell = str(row["shell"] or "cmd")
            except (IndexError, KeyError):
                shell = "cmd"
            shell_label = "PowerShell" if shell == "powershell" else "CMD"
            details = QLabel(
                f"Pending {shell_label} · {row['risk_level']}\n"
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
        list_deletes = getattr(self.db, "pending_file_deletes", None)
        delete_rows = list_deletes() if callable(list_deletes) else []
        for row in delete_rows:
            card = QFrame()
            box = QVBoxLayout(card)
            details = QLabel(
                "Confirm moving this exact file to the Windows Recycle Bin?\n"
                f"File: {row['path']}\nThis confirmation expires automatically."
            )
            details.setTextFormat(Qt.TextFormat.PlainText)
            details.setWordWrap(True)
            box.addWidget(details)
            buttons = QHBoxLayout()
            confirm = QPushButton("Move to Recycle Bin")
            cancel = QPushButton("Cancel delete")
            token = str(row["token"])
            path = str(row["path"])
            confirm.clicked.connect(
                lambda _checked=False, key=token, exact_path=path:
                    self._confirm_pending_delete(key, exact_path)
            )
            cancel.clicked.connect(lambda _checked=False, key=token: self._cancel_pending_delete(key))
            buttons.addWidget(confirm)
            buttons.addWidget(cancel)
            box.addLayout(buttons)
            self.pending.addWidget(card)
        if rows or delete_rows:
            self.status_changed.emit("cmd_pending")
        else:
            self.status_changed.emit("active")

    def _insert_cmd_prefix(self) -> None:
        """Make command access visible while retaining the normal review flow."""
        current = self.input.text().strip()
        if not current.casefold().startswith(("cmd:", "cmd ", "cmd.exe", "command:", "terminal:")):
            self.input.setText(f"cmd: {current}" if current else "cmd: ")
        self.input.setFocus()
        self.input.setCursorPosition(len(self.input.text()))

    def _confirm_pending_delete(self, token: str, path: str) -> None:
        """Require a visible click before moving the displayed exact file to Recycle Bin."""
        if self._task is not None and self._task.isRunning():
            return
        self.activity.emit()
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self.cmd_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status_changed.emit("active")
        self._current_chat_prompt = f"Confirm moving to Recycle Bin: {path}"
        self._current_stream_text = ""
        self._render_chat_transcript("Moving the confirmed file…")
        self.work_started.emit()
        self._task = _Task(self.runtime, f"CONFIRM DELETE {token}", False)
        self._task.done.connect(self._received)
        self._task.delta.connect(self._received_delta)
        self._chat_has_deltas = False
        self._request_timer.stop()
        self._task.start()

    def _cancel_pending_delete(self, token: str) -> None:
        """Cancel a pending delete card without touching its file."""
        if self.db.cancel_file_delete(token):
            self.answer.setPlainText("Canceled. The file was not changed.")
        else:
            self.answer.setPlainText("This delete confirmation expired or was already handled.")
        self._refresh_pending()

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
        self.send_button.setEnabled(enabled)

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
