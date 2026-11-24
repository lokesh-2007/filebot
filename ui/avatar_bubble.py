"""Frameless always-on-top avatar that snaps to a screen corner."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLabel, QWidget


class AvatarBubble(QWidget):
    toggled = pyqtSignal()
    status_changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(58, 58)
        self.label = QLabel("FB", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setGeometry(2, 2, 54, 54)
        self.label.setStyleSheet("background:#4936a5;color:white;border:2px solid #b9acff;border-radius:27px;font-weight:700;font-size:16px")
        self.set_status("sleep")
        self._offset = None
        self._dragged = False
        self.move_to_corner()

    def set_status(self, value: str) -> None:
        colors = {"sleep": "#4936a5", "active": "#15803d", "cmd_pending": "#b45309"}
        color = colors.get(value, colors["sleep"])
        self.label.setStyleSheet(f"background:{color};color:white;border:2px solid #d8d2ff;border-radius:27px;font-weight:700;font-size:16px")
        self.setToolTip(f"FileBot: {value.replace('_', ' ')}")

    def show_popover(self, popover: QWidget) -> None:
        point = self.frameGeometry().topLeft()
        popover.adjustSize()
        popover.move(max(0, point.x() - popover.width() + self.width()), max(0, point.y() - popover.height() - 8))
        popover.show()
        popover.raise_()

    def move_to_corner(self) -> None:
        screen = self.screen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 12, area.bottom() - self.height() - 12)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._offset
            self.move(target)
            self._dragged = True

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragged:
                screen = self.screen()
                if screen:
                    area = screen.availableGeometry()
                    left = abs(self.x() - area.left()) < abs(self.x() + self.width() - area.right())
                    top = abs(self.y() - area.top()) < abs(self.y() + self.height() - area.bottom())
                    self.move(area.left() + 10 if left else area.right() - self.width() - 10,
                              area.top() + 10 if top else area.bottom() - self.height() - 10)
            else:
                self.toggled.emit()
            self._offset = None
