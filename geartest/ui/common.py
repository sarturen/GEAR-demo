"""Small widgets and constants shared by the panels."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: One palette for every log surface, so "red" always means the same thing.
LEVEL_COLORS = {
    "info": "#d4d4d4",
    "ok": "#4ec9b0",
    "warn": "#dcdcaa",
    "error": "#f48771",
    "tx": "#569cd6",
    "rx": "#ce9178",
    "muted": "#808080",
}


def mono_font(size: int = 9) -> QFont:
    """Monospace for log surfaces, with CJK fallbacks.

    Consolas carries no Chinese glyphs, so device output that mixes Chinese with
    ASCII would otherwise come out as blank boxes. The fallbacks are only used
    for the characters the first family cannot draw.
    """
    font = QFont()
    font.setFamilies(["Consolas", "Microsoft YaHei UI", "Microsoft YaHei", "SimSun"])
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(size)
    return font


def ui_font() -> QFont:
    """The window font. Named explicitly so the choice does not depend on the
    locale the machine happens to be set to."""
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Segoe UI", "Microsoft YaHei", "SimSun"])
    font.setPointSize(9)
    return font


class LogView(QPlainTextEdit):
    """Append-only log with per-line colouring and a bounded history."""

    def __init__(self, max_lines: int = 5000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(mono_font())

    def append(self, text: str, level: str = "info") -> None:
        self.append_colored(text, LEVEL_COLORS.get(level, LEVEL_COLORS["info"]))

    def append_colored(self, text: str, color: str) -> None:
        safe = html.escape(text).replace(" ", "&nbsp;")
        self.appendHtml(f'<span style="color:{color};white-space:pre">{safe}</span>')
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_block(self, rows, scroll: bool = True) -> None:
        """Append many lines in one go.

        Each ``append`` forces the scrollbar to the bottom, which is cheap once
        and ruinous thousands of times: a CH340 at 115200 emits a few thousand
        lines a second, and several of those into one widget will lock the GUI.
        """
        if not rows:
            return
        self.setUpdatesEnabled(False)
        try:
            for text, color in rows:
                safe = html.escape(text).replace(" ", "&nbsp;")
                self.appendHtml(f'<span style="color:{color};white-space:pre">{safe}</span>')
        finally:
            self.setUpdatesEnabled(True)
        if scroll:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    def append_many(self, lines, level: str = "info") -> None:
        color = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])
        self.append_block([(line, color) for line in lines])


class Panel(QWidget):
    """Base for the tab pages: a title, a body, and a status line."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._outer = QVBoxLayout(self)
        heading = QLabel(title)
        font = heading.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        heading.setFont(font)
        self._outer.addWidget(heading)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#808080")
        self._outer.addWidget(self._status)

    def body(self) -> QVBoxLayout:
        return self._outer

    def set_status(self, text: str, ok: bool = True) -> None:
        color = "#4ec9b0" if ok else "#f48771"
        self._status.setStyleSheet(f"color:{color}")
        self._status.setText(text)


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    return row


def make_form() -> QFormLayout:
    """A compact two-column form.

    Fields stay at their natural width rather than growing to fill the row:
    a combo box stretched across a 1500px window is harder to read than a
    compact one, and there is nothing useful to do with the extra space.
    """
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignRight)
    form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
    return form
