"""Editing one configuration row, with widgets built from the field metadata.

Nothing here knows what a relay is. It walks the ``Section.fields`` declaration
and picks a widget per kind, which is why adding a field to ``config.py`` and
``config_schema.py`` is enough to make it editable.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import RoiCfg, Settings
from ..config_schema import (
    BOOL,
    CHOICE,
    FLOAT,
    INT,
    MULTICHOICE,
    ROI,
    Field,
    Section,
    candidates,
)

INT_UNBOUNDED = 2_000_000_000
FLOAT_UNBOUNDED = 1e9


def _int_editor(field: Field) -> QSpinBox:
    box = QSpinBox()
    box.setRange(int(field.minimum), int(field.maximum))
    if field.suffix:
        box.setSuffix(field.suffix)
    box.setMinimumWidth(160)
    return box


def _float_editor(field: Field) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(float(field.minimum), float(field.maximum))
    box.setDecimals(3)
    box.setSingleStep(0.1)
    if field.suffix:
        box.setSuffix(field.suffix)
    box.setMinimumWidth(160)
    return box


class RoiEditor(QWidget):
    """Four fractions. The same region is draggable on the 监控画面 page."""

    def __init__(self, roi: RoiCfg, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.boxes: dict[str, QDoubleSpinBox] = {}
        for name, label in (("x", "x"), ("y", "y"), ("w", "宽"), ("h", "高")):
            box = QDoubleSpinBox()
            box.setRange(0.0, 1.0)
            box.setDecimals(3)
            box.setSingleStep(0.05)
            box.setValue(getattr(roi, name))
            layout.addWidget(QLabel(label))
            layout.addWidget(box)
            self.boxes[name] = box

    def value(self) -> RoiCfg:
        return RoiCfg(
            *(
                round(self.boxes[name].value(), 4)
                for name in ("x", "y", "w", "h")
            )
        )


class MultiChoiceEditor(QWidget):
    """A checkbox list of the names available in another section."""

    def __init__(
        self, field: Field, settings: Settings, selected: list[str], parent=None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        # Capped as well as floored: left to stretch, three membership lists
        # make a dialog taller than most laptop screens.
        self.list.setMaximumHeight(160)
        chosen = set(selected)
        options = candidates(settings, field.source)
        for name in options:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in chosen else Qt.Unchecked)
            self.list.addItem(item)
        # Anything already referenced stays listed even if it no longer exists,
        # so validation can complain about it instead of this dialog silently
        # dropping the reference.
        for name in sorted(chosen - set(options)):
            item = QListWidgetItem(f"{name}（已不存在）")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
        layout.addWidget(self.list)

    def value(self) -> list[str]:
        out: list[str] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.checkState() != Qt.Checked:
                continue
            out.append(item.data(Qt.UserRole) or item.text())
        return out


class FieldDialog(QDialog):
    """Add or edit one row of one section."""

    def __init__(
        self,
        section: Section,
        item: Any,
        settings: Settings,
        title: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.section = section
        self.settings = settings
        self.editors: dict[str, QWidget] = {}

        self.setWindowTitle(title or f"{section.title} · {getattr(item, 'name', '')}")
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        if section.note:
            note = QLabel(section.note)
            note.setStyleSheet("color:#808080")
            note.setWordWrap(True)
            outer.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        for field in section.fields:
            editor = self._make_editor(field, item)
            self.editors[field.name] = editor
            if field.hint:
                editor.setToolTip(field.hint)
            form.addRow(field.label, editor)
        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _make_editor(self, field: Field, item: Any) -> QWidget:
        current = getattr(item, field.name)
        if field.kind == ROI:
            return RoiEditor(current)
        if field.kind == MULTICHOICE:
            return MultiChoiceEditor(field, self.settings, list(current or []))
        if field.kind == BOOL:
            box = QCheckBox()
            box.setChecked(bool(current))
            return box
        if field.kind == CHOICE:
            combo = QComboBox()
            combo.setMinimumWidth(200)
            options = candidates(self.settings, field.source)
            for name in options:
                combo.addItem(name)
            if current and current not in options:
                # Keep an existing but now-missing reference visible; validation
                # reports it rather than the editor quietly rewriting it.
                combo.addItem(f"{current}（已不存在）")
            if current:
                combo.setCurrentText(str(current))
            return combo
        if field.kind == INT:
            box = _int_editor(field)
            box.setValue(int(current))
            return box
        if field.kind == FLOAT:
            box = _float_editor(field)
            box.setValue(float(current))
            return box
        line = QLineEdit(str(current))
        line.setMinimumWidth(220)
        return line

    def apply_to(self, item: Any) -> Any:
        """Write the edited values back onto the config object."""
        for field in self.section.fields:
            editor = self.editors[field.name]
            if isinstance(editor, RoiEditor):
                value: Any = editor.value()
            elif isinstance(editor, MultiChoiceEditor):
                value = editor.value()
            elif field.kind == BOOL:
                value = editor.isChecked()
            elif field.kind == CHOICE:
                text = editor.currentText()
                value = text.replace("（已不存在）", "").strip()
            elif field.kind in (INT, FLOAT):
                value = editor.value()
            else:
                value = editor.text().strip()
            setattr(item, field.name, value)
        return item
