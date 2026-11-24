"""Compact prompt/search card with clickable results and explicit CMD approval."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout, QWidget)

from executor.cmd_runner import CommandRunner
from storage.db import Database

log = logging.getLogger(__name__)


class _Task(QThread):
    done = pyqtSignal(object, str)
    def __init__(self, target: Any, prompt: str, search: bool) -> None:
        super().__init__()
        self.target, self.prompt, self.search = target, prompt, search
    def run(self) -> None:
        try:
            result = self.target.search(self.prompt) if self.search else self.target.ask(self.prompt)
            self.done.emit(result, "")
        except Exception as exc:
            log.exception("FileBot request failed")
            self.done.emit(None, str(exc))


class PopoverCard(QWidget):
    """Search/chat panel that surfaces pending commands with Approve/Reject controls."""
    activity = pyqtSignal()
    status_changed = pyqtSignal(str)
    def __init__(self, engine: Any, runtime: Any, db: Database) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.engine, self.runtime, self.db = engine, runtime, db
        self.runner = CommandRunner(db)
        self.setWindowTitle("FileBot")
        self.setFixedWidth(390)
        self.setStyleSheet("QWidget{background:#171923;color:#f5f5f5;font:10pt 'Segoe UI'} QLineEdit,QTextEdit,QListWidget{background:#242736;border:1px solid #40445a;border-radius:7px;padding:7px} QPushButton{background:#5141a4;border:0;border-radius:6px;padding:6px 10px} QFrame{border:1px solid #45495e;border-radius:7px}")
        layout = QVBoxLayout(self)
        title = QLabel("FileBot  ·  local search")
        title.setStyleSheet("font-weight:700;font-size:12pt")
        layout.addWidget(title)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Search files or ask a question…")
        self.input.returnPressed.connect(self.submit)
        layout.addWidget(self.input)
        row = QHBoxLayout()
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(lambda: self.submit(force_search=True))
        self.ask_button = QPushButton("Ask")
        self.ask_button.clicked.connect(self.submit)
        row.addWidget(self.search_button)
        row.addWidget(self.ask_button)
        layout.addLayout(row)
        self.results = QListWidget()
        self.results.setMaximumHeight(210)
        self.results.itemActivated.connect(self.open_result)
        layout.addWidget(self.results)
        self.answer = QTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setMaximumHeight(110)
        layout.addWidget(self.answer)
        self.pending = QVBoxLayout()
        layout.addLayout(self.pending)
        self._task: _Task | None = None
        self._refresh_pending()
        if self.db.pending_commands():
            self.status_changed.emit("cmd_pending")

    def submit(self, force_search: bool = False) -> None:
        prompt = self.input.text().strip()
        if not prompt:
            return
        self.activity.emit()
        self.search_button.setEnabled(False)
        self.ask_button.setEnabled(False)
        self.status_changed.emit("active")
        self._task = _Task(self.runtime, prompt, force_search)
        self._task.done.connect(self._received)
        self._task.start()

    def _received(self, result: object, error: str) -> None:
        self.search_button.setEnabled(True)
        self.ask_button.setEnabled(True)
        if error:
            self.answer.setPlainText(error)
            return
        if isinstance(result, list):
            self.results.clear()
            for match in result:
                path = str(match.get("path", ""))
                text = str(match.get("snippet", "")).replace("\n", " ")[:180]
                item = QListWidgetItem(f"{Path(path).name}  ·  {text}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.results.addItem(item)
            self.answer.clear()
        else:
            self.answer.setPlainText(str(result))
        self._refresh_pending()

    def open_result(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).is_file():
            os.startfile(path)  # type: ignore[attr-defined]

    def _refresh_pending(self) -> None:
        while self.pending.count():
            child = self.pending.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        rows = self.db.pending_commands()
        for row in rows:
            card = QFrame()
            box = QVBoxLayout(card)
            box.addWidget(QLabel(f"Pending CMD · {row['risk_level']}\n{row['command']}"))
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

    def _decide(self, proposal_id: str, approved: bool) -> None:
        if not self.db.decide_command(proposal_id, approved):
            self.answer.setPlainText("This proposal has already been handled.")
            self._refresh_pending()
            return
        if approved:
            self.status_changed.emit("active")
            try:
                code, stdout, stderr = self.runner.execute(proposal_id)
                self.answer.setPlainText(f"Exit code: {code}\n{stdout}\n{stderr}".strip())
            except Exception as exc:
                log.exception("Approved command failed")
                self.answer.setPlainText(f"Could not run approved command: {exc}")
        self._refresh_pending()
