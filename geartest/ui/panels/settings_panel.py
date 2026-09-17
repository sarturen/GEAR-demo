"""Configuration page: edit the whole bench definition and write the YAML.

Edits happen on a working copy. Nothing touches the running bench until 保存并应用
validates the copy and swaps it in, so a half-finished edit cannot leave the
工具 driving a bench that does not match what is on screen.
"""

from __future__ import annotations

import copy
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...config import SETTINGS_PATH, Settings, save_settings
from ...config_schema import SECTIONS, Section, display_value, new_item, section_for, validate
from ..common import LogView, Panel
from ..config_form import FieldDialog


class SectionTab(QWidget):
    """One section: a table of its rows, its own row buttons, and its note."""

    def __init__(self, section: Section, panel: "SettingsPanel") -> None:
        super().__init__()
        self.section = section
        self.panel = panel
        self.columns = (section.key_field, *section.summary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)

        if section.note:
            note = QLabel(section.note)
            note.setStyleSheet("color:#808080")
            note.setWordWrap(True)
            layout.addWidget(note)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(
            [self.panel.field_label(section, name) for name in self.columns]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for index in range(1, len(self.columns)):
            head.setSectionResizeMode(index, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _: self.edit())
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("新增")
        self.add_button.clicked.connect(self.add)
        self.edit_button = QPushButton("编辑")
        self.edit_button.clicked.connect(self.edit)
        self.remove_button = QPushButton("删除")
        self.remove_button.clicked.connect(self.remove)
        self.up_button = QPushButton("上移")
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton("下移")
        self.down_button.clicked.connect(lambda: self.move(1))
        row.addWidget(self.add_button)
        row.addWidget(self.edit_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.up_button)
        row.addWidget(self.down_button)
        row.addStretch(1)
        self.count = QLabel("")
        self.count.setStyleSheet("color:#808080")
        row.addWidget(self.count)
        layout.addLayout(row)

    # -- table --------------------------------------------------------------

    def refresh(self) -> None:
        items = self.panel.items(self.section)
        self.table.setRowCount(0)
        for row, item in enumerate(items):
            self.table.insertRow(row)
            for column, name in enumerate(self.columns):
                field = self.panel.fields(self.section)[name]
                cell = QTableWidgetItem(display_value(item, field))
                self.table.setItem(row, column, cell)
        self.count.setText(f"{len(items)} 项")
        self._sync_buttons()

    def _selected_index(self) -> int:
        row = self.table.currentRow()
        if 0 <= row < len(self.panel.items(self.section)):
            return row
        return -1

    def _sync_buttons(self) -> None:
        has_rows = bool(self.panel.items(self.section))
        self.edit_button.setEnabled(has_rows)
        self.remove_button.setEnabled(has_rows)
        self.up_button.setEnabled(has_rows)
        self.down_button.setEnabled(has_rows)

    # -- actions ------------------------------------------------------------

    def add(self) -> None:
        item = new_item(self.panel.working, self.section)
        dialog = FieldDialog(
            self.section, item, self.panel.working, f"新增{self.section.title}", self
        )
        if dialog.exec() != FieldDialog.Accepted:
            return
        self.panel.items(self.section).append(dialog.apply_to(item))
        self.panel.mark_dirty()
        self.refresh()
        self.table.selectRow(len(self.panel.items(self.section)) - 1)

    def edit(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        item = self.panel.items(self.section)[index]
        dialog = FieldDialog(self.section, item, self.panel.working, parent=self)
        if dialog.exec() != FieldDialog.Accepted:
            return
        dialog.apply_to(item)
        self.panel.mark_dirty()
        self.refresh()
        self.table.selectRow(index)

    def remove(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        item = self.panel.items(self.section)[index]
        # Deleting a resource a device still lists is the mistake that only
        # shows up when a flow runs, so say so now rather than block it.
        warnings = self.panel.deletion_warnings(self.section, item)
        if warnings:
            answer = QMessageBox.warning(
                self,
                "删除会影响其它配置",
                "\n".join(warnings) + "\n\n仍然删除？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        del self.panel.items(self.section)[index]
        self.panel.mark_dirty()
        self.refresh()
        if self.panel.items(self.section):
            self.table.selectRow(min(index, len(self.panel.items(self.section)) - 1))

    def move(self, delta: int) -> None:
        index = self._selected_index()
        target = index + delta
        items = self.panel.items(self.section)
        if index < 0 or not (0 <= target < len(items)):
            return
        items[index], items[target] = items[target], items[index]
        self.panel.mark_dirty()
        self.refresh()
        self.table.selectRow(target)


class SettingsPanel(Panel):
    def __init__(
        self,
        settings: Settings,
        on_apply: Callable[[Settings], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("配置", parent)
        self.working = copy.deepcopy(settings)
        self.on_apply = on_apply
        self.dirty = False
        self._fields = {section.attr: {f.name: f for f in section.fields} for section in SECTIONS}
        self.tabs: dict[str, SectionTab] = {}
        self._build()
        self.refresh()

    # -- accessors ----------------------------------------------------------

    def items(self, section: Section) -> list:
        return getattr(self.working, section.attr)

    def fields(self, section: Section):
        return self._fields[section.attr]

    def field_label(self, section: Section, name: str) -> str:
        field = self.fields(section).get(name)
        return field.label if field else name

    def mark_dirty(self) -> None:
        self.dirty = True
        self.set_status(f"有未保存的改动（{SETTINGS_PATH.name}）")

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        toolbar = QHBoxLayout()
        self.validate_button = QPushButton("校验")
        self.validate_button.clicked.connect(lambda: self.check(verbose=True))
        self.reload_button = QPushButton("重新载入")
        self.reload_button.clicked.connect(self.reload)
        self.save_button = QPushButton("保存并应用")
        self.save_button.clicked.connect(self.save)
        self.save_button.setToolTip(
            "写回 settings.yaml，并重建各面板——不需要重启程序"
        )
        toolbar.addWidget(self.validate_button)
        toolbar.addWidget(self.reload_button)
        toolbar.addWidget(self.save_button)
        toolbar.addStretch(1)
        self.dirty_label = QLabel("")
        self.dirty_label.setStyleSheet("color:#dcdcaa")
        toolbar.addWidget(self.dirty_label)
        self.body().addLayout(toolbar)

        self.tab_widget = QTabWidget()
        for section in SECTIONS:
            tab = SectionTab(section, self)
            self.tabs[section.attr] = tab
            self.tab_widget.addTab(tab, section.title)
        self.body().addWidget(self.tab_widget, 1)

        self.problems = LogView(max_lines=500)
        self.problems.setMaximumHeight(150)
        self.body().addWidget(QLabel("校验结果"))
        self.body().addWidget(self.problems)

    # -- state --------------------------------------------------------------

    def refresh(self) -> None:
        for tab in self.tabs.values():
            tab.refresh()
        self.dirty_label.setText("● 未保存" if self.dirty else "")
        self.check(verbose=False)

    def reload(self) -> None:
        from ...config import load_settings

        self.working = copy.deepcopy(load_settings())
        self.dirty = False
        self.refresh()
        self.set_status(f"已从 {SETTINGS_PATH.name} 重新载入")

    def check(self, verbose: bool = True) -> list[str]:
        problems = validate(self.working)
        if verbose or problems:
            self.problems.clear()
            if problems:
                for problem in problems:
                    self.problems.append(f"· {problem}", "error")
            else:
                self.problems.append("没有发现问题", "ok")
        if problems and verbose:
            self.set_status(f"发现 {len(problems)} 个问题", ok=False)
        elif verbose:
            self.set_status("校验通过")
        return problems

    def deletion_warnings(self, section: Section, item) -> list[str]:
        name = getattr(item, "name", "")
        warnings: list[str] = []
        for device in self.working.devices:
            for attr, title in (
                ("power", "电源"),
                ("consoles", "串口"),
                ("screens", "画面"),
            ):
                if name in getattr(device, attr):
                    warnings.append(f"被测设备「{device.name}」的{title}资源引用了它")
        if section.attr == "relays":
            for channel in self.working.relay_channels:
                if channel.relay == name:
                    warnings.append(f"继电器资源「{channel.name}」挂在这块板上")
        if section.attr == "cameras":
            for screen in self.working.screens:
                if screen.camera == name:
                    warnings.append(f"监控屏幕「{screen.name}」使用这个摄像头")
        return warnings

    # -- saving -------------------------------------------------------------

    def save(self) -> None:
        problems = self.check(verbose=False)
        if problems:
            self.check(verbose=True)
            self.set_status(
                f"有 {len(problems)} 个问题，未保存。先修好再保存", ok=False
            )
            return
        try:
            save_settings(self.working)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"写入失败：{type(exc).__name__}: {exc}", ok=False)
            return

        self.dirty = False
        self.dirty_label.setText("")
        self.problems.clear()
        self.problems.append(f"已写入 {SETTINGS_PATH}", "ok")
        self.on_apply(copy.deepcopy(self.working))
        self.set_status("已保存并应用")

    def shutdown(self) -> None:
        pass
