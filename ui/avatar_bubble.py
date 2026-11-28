"""Frameless always-on-top avatar that snaps to a screen corner."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QLabel, QMenu, QWidget

STATE_COLORS = {"sleep": "#6a5de0", "active": "#34d399", "cmd_pending": "#f5a524"}


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
        self.setFixedSize(64, 64)
        self.label = QLabel("FB", self)
        self.label.setAccessibleName("FileBot status")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setGeometry(0, 0, 64, 64)
        self.label.setStyleSheet("background:transparent;color:white;border:0;font-weight:800;font-size:17px")
        self._status = "sleep"
        self._phase = 0.0
        self._pulse = QTimer(self)
        self._pulse.setInterval(60)
        self._pulse.timeout.connect(self._tick)
        self.set_status("sleep")
        self._offset = None
        self._dragged = False
        self.move_to_corner()

    def set_status(self, value: str) -> None:
        self._status = value if value in STATE_COLORS else "sleep"
        # Awake and waiting-for-approval states breathe; sleeping stays still.
        if self._status == "sleep":
            self._pulse.stop()
            self._phase = 0.0
        elif not self._pulse.isActive():
            self._pulse.start()
        self.setToolTip(f"FileBot: {value.replace('_', ' ')}")
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.07) % 1.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QColor(STATE_COLORS[self._status])
        centre = QPointF(self.width() / 2, self.height() / 2)
        if self._status != "sleep":
            glow = ring
            glow.setAlpha(int(70 * (1 - self._phase)))
            radius = 23 + 9 * self._phase
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(centre, radius, radius)
        gradient = QRadialGradient(QPointF(centre.x() - 8, centre.y() - 10), 40)
        gradient.setColorAt(0, QColor("#8f82ff"))
        gradient.setColorAt(1, QColor("#3d2fa0"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(STATE_COLORS[self._status]), 3))
        painter.drawEllipse(centre, 24, 24)

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

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 (Qt API)
        """Right-click: open/close the panel or end the whole FileBot process."""
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#1b1930;color:#ecebf7;border:1px solid #2f2c4d;padding:4px}"
                           "QMenu::item{padding:6px 18px;border-radius:5px}"
                           "QMenu::item:selected{background:#6a5de0}")
        toggle = menu.addAction("Open / close FileBot")
        menu.addSeparator()
        quit_action = menu.addAction("Quit FileBot (end task)")
        chosen = menu.exec(event.globalPos())
        if chosen is toggle:
            self.toggled.emit()
        elif chosen is quit_action:
            self.quit_requested.emit()

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
