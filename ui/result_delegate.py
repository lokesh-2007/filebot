"""Paints search results as cards: type badge, file name, path and highlighted snippet."""
from __future__ import annotations

import html
import os
import re

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QTextDocument
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from ui.theme import DEFAULT_EXT_COLOR, EXT_COLORS, PALETTE

ROW_HEIGHT = 92


def split_item_text(text: str) -> tuple[str, str]:
    """Items are stored as 'path\\nsnippet' (keeps tests, tooltips and copy simple)."""
    path, _, snippet = text.partition("\n")
    return path, snippet


def highlight_html(snippet: str, terms: list[str]) -> str:
    escaped = html.escape(snippet)
    for term in sorted({t for t in terms if len(t) > 1}, key=len, reverse=True):
        pattern = re.compile(re.escape(html.escape(term)), re.IGNORECASE)
        escaped = pattern.sub(
            lambda m: f"<span style='color:{PALETTE['accent_hi']};font-weight:600'>{m.group(0)}</span>",
            escaped)
    return escaped


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.terms: list[str] = []

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 (Qt API)
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        path, snippet = split_item_text(index.data(Qt.ItemDataRole.DisplayRole) or "")
        name = os.path.basename(path.replace("\\", "/")) or path
        ext = os.path.splitext(name)[1].lower()
        color = QColor(EXT_COLORS.get(ext, DEFAULT_EXT_COLOR))
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(option.rect).adjusted(0, 3, 0, -3)
        painter.setBrush(QColor(PALETTE["surface_hi"] if hovered or selected else PALETTE["surface"]))
        painter.setPen(QPen(QColor(PALETTE["accent"] if selected else PALETTE["border"]), 1))
        painter.drawRoundedRect(card, 12, 12)

        badge = QRectF(card.left() + 10, card.top() + 10, 34, 34)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(color)
        fill.setAlpha(46)
        painter.setBrush(fill)
        painter.drawRoundedRect(badge, 9, 9)
        painter.setPen(color)
        badge_font = QFont(option.font)
        badge_font.setPointSizeF(7.5)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, (ext.lstrip(".") or "file")[:4].upper())

        left = badge.right() + 10
        width = card.right() - left - 10
        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(QColor(PALETTE["text"]))
        metrics = QFontMetrics(name_font)
        painter.drawText(QRectF(left, card.top() + 8, width, 18), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(width)))

        path_font = QFont(option.font)
        path_font.setPointSizeF(7.5)
        painter.setFont(path_font)
        painter.setPen(QColor(PALETTE["muted"]))
        pm = QFontMetrics(path_font)
        painter.drawText(QRectF(left, card.top() + 26, width, 14), Qt.AlignmentFlag.AlignVCenter,
                         pm.elidedText(path, Qt.TextElideMode.ElideMiddle, int(width)))

        doc = QTextDocument()
        snippet_font = QFont(option.font)
        snippet_font.setPointSizeF(8.5)
        doc.setDefaultFont(snippet_font)
        doc.setDocumentMargin(0)
        doc.setTextWidth(card.width() - 20)
        doc.setHtml(f"<div style='color:{PALETTE['muted']}'>"
                    f"{highlight_html(snippet[:200], self.terms)}</div>")
        painter.translate(card.left() + 10, card.top() + 50)
        painter.setClipRect(QRectF(0, 0, card.width() - 20, 30))
        doc.drawContents(painter)
        painter.restore()
