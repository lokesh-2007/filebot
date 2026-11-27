"""Frameless always-on-top avatar that snaps to a screen corner."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QContextMenuEvent, QMouseEvent
from PyQt6.QtWidgets import QLabel, QMenu, QWidget


class AvatarBubble(QWidget):
    toggled = pyqtSignal()
    status_changed = pyqtSignal(str)
    quit_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("FileBot")
        self.setAccessibleName("FileBot status avatar")
        self.setAccessibleDescription("Click to open FileBot local search and command approval.")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(58, 58)
        self.label = QLabel("FB", self)
        self.label.setAccessibleName("FileBot status")
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
        x = point.x() - popover.width() + self.width()
        y = point.y() - popover.height() - 8
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            if y < area.top():
                y = point.y() + self.height() + 8
            # QRect's right and bottom edges are inclusive. Keep the entire
            # card inside the active monitor's work area, including negative
            # coordinate layouts where a secondary display sits left/up.
            max_x = max(area.left(), area.right() + 1 - popover.width())
            max_y = max(area.top(), area.bottom() + 1 - popover.height())
            x = min(max(area.left(), x), max_x)
            y = min(max(area.top(), y), max_y)
        popover.move(x, y)
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

    def create_context_menu(self) -> QMenu:
        menu = QMenu(self)
        quit_action = menu.addAction("Quit FileBot")
        quit_action.triggered.connect(self.quit_requested)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        menu = self.create_context_menu()
        menu.exec(event.globalPos())
