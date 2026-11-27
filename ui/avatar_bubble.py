"""Frameless always-on-top avatar that snaps to a screen corner."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QColor, QContextMenuEvent, QLinearGradient, QMouseEvent, QPainter,
    QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QLabel, QMenu, QWidget


class AvatarBubble(QWidget):
    toggled = pyqtSignal()
    status_changed = pyqtSignal(str)
    quit_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("FileBot")
        self.setAccessibleName("FileBot status avatar")
        self.setAccessibleDescription(
            "Click to chat with FileBot, search local files, and review command proposals."
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(82, 82)
        # Keep a named accessibility child for screen readers; the character is
        # painted by this widget so the avatar remains crisp at Windows scaling.
        self.label = QLabel("", self)
        self.label.setAccessibleName("FileBot status")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setGeometry(0, 0, 1, 1)
        self.set_status("sleep")
        self._offset = None
        self._dragged = False
        self.move_to_corner()

    def set_status(self, value: str) -> None:
        if value not in {"sleep", "active", "cmd_pending"}:
            value = "sleep"
        self._status = value
        self.label.setToolTip(f"FileBot: {value.replace('_', ' ')}")
        self.setToolTip(f"FileBot: {value.replace('_', ' ')}")
        self.update()

    @staticmethod
    def _gradient(rect: QRectF, top: str, bottom: str) -> QLinearGradient:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor(top))
        gradient.setColorAt(1, QColor(bottom))
        return gradient

    def paintEvent(self, _event) -> None:
        """Paint a compact shaded robot bust in the supplied FileBot colors."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self.width() / 82.0, self.height() / 82.0)
        painter.setPen(Qt.PenStyle.NoPen)

        # Soft ground shadow and antenna fins.
        shadow = QRadialGradient(QRectF(12, 67, 58, 13).center(), 30)
        shadow.setColorAt(0, QColor(7, 26, 73, 75))
        shadow.setColorAt(1, QColor(7, 26, 73, 0))
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(12, 67, 58, 13))
        painter.setBrush(self._gradient(QRectF(14, 16, 13, 30), "#38e7ec", "#008caa"))
        painter.drawRoundedRect(QRectF(15, 15, 11, 31), 5, 5)
        painter.drawRoundedRect(QRectF(56, 15, 11, 31), 5, 5)

        # Small shoulder/body shell behind the larger helmet.
        body = QPainterPath()
        body.moveTo(27, 48)
        body.cubicTo(20, 51, 18, 60, 19, 69)
        body.cubicTo(21, 77, 32, 79, 41, 79)
        body.cubicTo(52, 79, 62, 75, 64, 67)
        body.cubicTo(65, 58, 60, 50, 54, 48)
        body.closeSubpath()
        body_rect = QRectF(19, 48, 45, 31)
        painter.setBrush(self._gradient(body_rect, "#ffffff", "#9aa9ca"))
        painter.drawPath(body)
        painter.setBrush(self._gradient(QRectF(29, 57, 25, 17), "#34e5ea", "#078cae"))
        painter.drawRoundedRect(QRectF(29, 57, 25, 17), 8, 8)
        painter.setPen(QPen(QColor("#344980"), 1.6))
        painter.drawLine(23, 58, 29, 60)
        painter.drawLine(54, 60, 60, 58)
        painter.setPen(Qt.PenStyle.NoPen)

        # Ear pods and pearl-white helmet.
        painter.setBrush(self._gradient(QRectF(5, 29, 18, 24), "#ffffff", "#aab7d3"))
        painter.drawRoundedRect(QRectF(5, 29, 18, 24), 9, 9)
        painter.setBrush(self._gradient(QRectF(59, 29, 18, 24), "#ffffff", "#aab7d3"))
        painter.drawRoundedRect(QRectF(59, 29, 18, 24), 9, 9)
        painter.setBrush(QColor("#172963"))
        painter.drawEllipse(QRectF(8, 36, 4, 10))
        painter.drawEllipse(QRectF(70, 36, 4, 10))

        helmet = QPainterPath()
        helmet.moveTo(20, 23)
        helmet.cubicTo(21, 11, 31, 6, 41, 6)
        helmet.cubicTo(55, 6, 64, 14, 65, 27)
        helmet.lineTo(65, 45)
        helmet.cubicTo(63, 54, 55, 57, 42, 57)
        helmet.cubicTo(28, 57, 19, 52, 18, 43)
        helmet.closeSubpath()
        painter.setBrush(self._gradient(QRectF(18, 6, 47, 52), "#ffffff", "#b4c0dc"))
        painter.drawPath(helmet)

        # Deep blue glass, cyan edge and an upper glass reflection.
        visor = QRectF(22, 23, 39, 27)
        painter.setBrush(self._gradient(visor, "#07d9f4", "#0533ad"))
        painter.setPen(QPen(QColor("#36efff"), 1.5))
        painter.drawRoundedRect(visor, 14, 14)
        painter.setPen(Qt.PenStyle.NoPen)
        shine = QPainterPath()
        shine.moveTo(39, 26)
        shine.cubicTo(44, 23, 53, 25, 56, 30)
        shine.cubicTo(54, 34, 48, 33, 44, 31)
        shine.closeSubpath()
        painter.setBrush(QColor(151, 229, 255, 90))
        painter.drawPath(shine)

        # Sleep uses curved closed eyes; active and approval states use bright
        # open eye pixels. The amber badge makes the approval gate visible.
        if self._status == "sleep":
            painter.setPen(QPen(QColor("#e8ffff"), 3.2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for x in (31, 48):
                eye = QPainterPath()
                eye.moveTo(x - 4, 37)
                eye.quadTo(x, 31, x + 4, 37)
                painter.drawPath(eye)
        else:
            eye_color = QColor("#ffecad" if self._status == "cmd_pending" else "#d8ffff")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(31, 239, 255, 110))
            painter.drawEllipse(QRectF(27, 31, 10, 12))
            painter.drawEllipse(QRectF(44, 31, 10, 12))
            painter.setBrush(eye_color)
            painter.drawEllipse(QRectF(30, 33, 4, 8))
            painter.drawEllipse(QRectF(47, 33, 4, 8))

        status_color = {
            "sleep": "#7c8cae",
            "active": "#21d8aa",
            "cmd_pending": "#ffb547",
        }[self._status]
        painter.setBrush(QColor("#f8fbff"))
        painter.drawEllipse(QRectF(62, 62, 17, 17))
        painter.setBrush(QColor(status_color))
        painter.drawEllipse(QRectF(65, 65, 11, 11))

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
