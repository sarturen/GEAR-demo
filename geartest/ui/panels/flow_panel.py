"""Flow panel: load a YAML flow, run it, watch the steps and the log."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench
from ...config import FLOW_DIR
from ...flow import FlowEngine, FlowResult, Recorder, StepResult, load_flow
from ..common import LogView, Panel
from ..worker import run_task

STEP_COLUMNS = ("#", "步骤", "状态", "说明", "耗时")
STATUS_COLORS = {
    "待运行": "#808080",
    "运行中": "#dcdcaa",
    "通过": "#4ec9b0",
    "失败": "#f48771",
}


class FlowPanel(Panel):
    log_line = Signal(str, str)
    step_finished = Signal(object)

    def __init__(
        self,
        bench: Bench,
        recorder: Recorder,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("测试流程（手动 + 脚本）", parent)
        self.bench = bench
        self.recorder = recorder
        self.engine = FlowEngine(bench, on_log=self.log_line.emit)
        self.cancel_event: threading.Event | None = None
        self.running = False
        self.log_line.connect(self._append_log)
        self.step_finished.connect(self._on_step)
        self._build()
        self.refresh_flows()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        row = QHBoxLayout()
        self.flow_combo = QComboBox()
        self.flow_combo.setMinimumWidth(280)
        self.flow_combo.currentIndexChanged.connect(self._preview)
        reload_button = QPushButton("刷新")
        reload_button.clicked.connect(self.refresh_flows)
        self.run_button = QPushButton("运行")
        self.run_button.clicked.connect(self._run)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self._stop)
        self.stop_button.setEnabled(False)
        row.addWidget(QLabel("流程"))
        row.addWidget(self.flow_combo, 1)
        row.addWidget(reload_button)
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        self.body().addLayout(row)

        record_row = QHBoxLayout()
        self.record_check = QCheckBox("录制手动操作")
        self.record_check.toggled.connect(self._toggle_recording)
        self.record_count = QLabel("已记录 0 步")
        export = QPushButton("导出为流程…")
        export.clicked.connect(self._export)
        clear = QPushButton("清空记录")
        clear.clicked.connect(self._clear_recording)
        record_row.addWidget(self.record_check)
        record_row.addWidget(self.record_count)
        record_row.addStretch(1)
        record_row.addWidget(export)
        record_row.addWidget(clear)
        self.body().addLayout(record_row)

        self.table = QTableWidget(0, len(STEP_COLUMNS))
        self.table.setHorizontalHeaderLabels(STEP_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        # Elide rather than wrap: a wrapped step label turns one row into three
        # and makes the run hard to scan while it is moving.
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Interactive)
        head.resizeSection(1, 320)
        head.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.body().addWidget(self.table, 1)

        header = QHBoxLayout()
        header.addWidget(QLabel("运行日志"))
        header.addStretch(1)
        clear_log = QPushButton("清空日志")
        clear_log.clicked.connect(lambda: self.log.clear())
        header.addWidget(clear_log)
        self.body().addLayout(header)

        self.log = LogView(max_lines=5000)
        self.body().addWidget(self.log, 2)

    # -- flows --------------------------------------------------------------

    def refresh_flows(self) -> None:
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        current = self.flow_combo.currentText()
        self.flow_combo.blockSignals(True)
        self.flow_combo.clear()
        for path in sorted(FLOW_DIR.glob("*.y*ml")):
            self.flow_combo.addItem(path.name, str(path))
        self.flow_combo.blockSignals(False)
        if current:
            index = self.flow_combo.findText(current)
            if index >= 0:
                self.flow_combo.setCurrentIndex(index)
        self._preview()

    def _selected_path(self) -> str | None:
        return self.flow_combo.currentData()

    def _preview(self) -> None:
        path = self._selected_path()
        self.table.setRowCount(0)
        if not path:
            return
        try:
            flow = load_flow(path)
        except Exception as exc:
            self.set_status(f"{type(exc).__name__}: {exc}", ok=False)
            return
        device = flow.get("device", "（未声明设备）")
        self.set_status(
            f"{flow.get('name', path)}：设备 {device}，{len(flow.get('steps') or [])} 步"
        )
        for index, label in enumerate(self.engine.describe(flow)):
            self._add_row(index + 1, label, "待运行", "", "")

    def _selected_flow_name(self) -> str:
        path = self._selected_path()
        if not path:
            return "(未命名流程)"
        try:
            return str(load_flow(path).get("name") or Path(path).stem)
        except Exception:
            return Path(path).stem

    # -- running ------------------------------------------------------------

    def _run(self) -> None:
        if self.running:
            return
        path = self._selected_path()
        if not path:
            self.set_status("请先选择一个流程文件", ok=False)
            return
        try:
            flow = load_flow(path)
        except Exception as exc:
            self.set_status(f"{type(exc).__name__}: {exc}", ok=False)
            return

        self._preview()
        self.cancel_event = threading.Event()
        self.engine = FlowEngine(
            self.bench, on_log=self.log_line.emit, on_step=self.step_finished.emit
        )
        self.running = True
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.log.append(f"===== 开始运行 {path} =====", "ok")

        run_task(
            lambda: self.engine.run(flow, self.cancel_event),
            self._finished,
            self._failed,
        )

    def _stop(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.engine.cancel()
        self.set_status("已请求停止", ok=False)

    def _finished(self, result: FlowResult) -> None:
        self.running = False
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.log.append(f"===== {result.summary} =====", "ok" if result.ok else "error")
        if result.incident_dir is not None:
            self.log.append(f"证据目录：{result.incident_dir}", "warn")
        self.set_status(result.summary, ok=result.ok)

    def _failed(self, message: str) -> None:
        self.running = False
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._append_log("error", message)
        self.set_status(message, ok=False)

    def _append_log(self, level: str, text: str) -> None:
        self.log.append(text, level)

    def _on_step(self, outcome: StepResult) -> None:
        row = self._find_row(outcome.index)
        if row < 0:
            row = self._add_row(outcome.index + 1, outcome.label, "", "", "")
        self._set_cell(row, 2, "通过" if outcome.ok else "失败")
        self._set_cell(row, 3, outcome.message)
        self._set_cell(row, 4, f"{outcome.seconds:.2f}s")

    # -- recording ----------------------------------------------------------

    def _toggle_recording(self, enabled: bool) -> None:
        self.recorder.set_enabled(enabled)
        if enabled:
            self.set_status("已开始录制：在其它面板上的操作会转成流程步骤")
        else:
            self.set_status(f"已停止录制，共 {len(self.recorder)} 步")
        self._update_record_count()

    def _clear_recording(self) -> None:
        self.recorder.clear()
        self._update_record_count()

    def _update_record_count(self) -> None:
        self.record_count.setText(f"已记录 {len(self.recorder)} 步")

    def _export(self) -> None:
        if not len(self.recorder):
            self.set_status("还没有记录任何步骤", ok=False)
            return
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        default = FLOW_DIR / time.strftime("录制的流程_%Y%m%d-%H%M%S.yaml")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出流程", str(default), "YAML (*.yaml *.yml)"
        )
        if not path:
            return
        name = Path(path).stem
        Path(path).write_text(self.recorder.to_yaml(name), encoding="utf-8")
        self.refresh_flows()
        index = self.flow_combo.findText(Path(path).name)
        if index >= 0:
            self.flow_combo.setCurrentIndex(index)
        self.set_status(f"已导出到 {path}")

    # -- table helpers ------------------------------------------------------

    def _add_row(self, index: int, label: str, status: str, message: str, seconds: str) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, text in enumerate((str(index), label, status, message, seconds)):
            self._set_cell(row, column, str(text))
        return row

    def _set_cell(self, row: int, column: int, text: str) -> None:
        color = QColor(
            STATUS_COLORS.get(text, "#d4d4d4") if column == 2 else "#d4d4d4"
        )
        item = QTableWidgetItem(text)
        item.setForeground(color)
        self.table.setItem(row, column, item)

    def _find_row(self, index: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == str(index + 1):
                return row
        return -1

    def shutdown(self) -> None:
        if self.running and self.cancel_event is not None:
            self.cancel_event.set()
        self.engine.cancel()
        self.recorder.set_enabled(False)
