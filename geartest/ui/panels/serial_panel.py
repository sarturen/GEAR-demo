"""Serial panel: every console open at once, merged log plus one tab per port.

The merged view is the default because the interesting failures span ports --
"the MCU reset right after the SOC printed its first line" is only visible when
both are on screen together.
"""

from __future__ import annotations

import time
from collections import deque
from functools import partial

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench
from ...devices.serial_link import SerialLink
from ...devices.serial_ports import list_com_ports
from ..common import LogView, Panel, make_form, mono_font
from ..worker import run_task

BAUD_RATES = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
LINE_ENDINGS = {"无": "", "CR+LF (\\r\\n)": "\r\n", "LF (\\n)": "\n", "CR (\\r)": "\r"}
PARITIES = {"无 (N)": "N", "偶 (E)": "E", "奇 (O)": "O"}

#: One colour per console, so the merged view stays readable.
CONSOLE_COLORS = ["#4ec9b0", "#ce9178", "#dcdcaa", "#c586c0", "#569cd6", "#9cdcfe"]

FLUSH_MS = 80
MERGED_PREFIX_WIDTH = 5


class ConsoleRow(QGroupBox):
    """The physical binding for one console: which COM port it is on."""

    def __init__(self, bench: Bench, cfg, on_toggle, on_status, parent=None):
        super().__init__(cfg.name, parent)
        self.bench = bench
        self.cfg = cfg
        self.on_toggle = on_toggle
        self.on_status = on_status

        form = make_form()
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(230)
        self.port_combo.addItem("")
        for port in list_console_ports():
            marker = "★ " if port.is_console_candidate else ""
            self.port_combo.addItem(f"{marker}{port.label}", port.device)
        self.port_combo.setCurrentText(cfg.port)
        row.addWidget(self.port_combo)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        for rate in BAUD_RATES:
            self.baud_combo.addItem(str(rate))
        self.baud_combo.setCurrentText(str(cfg.baudrate))
        row.addWidget(QLabel("波特率"))
        row.addWidget(self.baud_combo)

        self.parity_combo = QComboBox()
        for label, value in PARITIES.items():
            self.parity_combo.addItem(label, value)
        index = self.parity_combo.findData(cfg.parity)
        self.parity_combo.setCurrentIndex(max(0, index))
        row.addWidget(QLabel("校验"))
        row.addWidget(self.parity_combo)

        self.toggle = QPushButton("打开")
        self.toggle.setCheckable(True)
        self.toggle.clicked.connect(self._on_toggle)
        row.addWidget(self.toggle)

        self.counter = QLabel("")
        self.counter.setStyleSheet("color:#808080;font-family:Consolas")
        row.addWidget(self.counter)
        row.addStretch(1)

        form.addRow("", row)
        self.setLayout(form)

    def store_config(self) -> None:
        data = self.port_combo.currentData()
        self.cfg.port = data or self.port_combo.currentText().strip()
        try:
            self.cfg.baudrate = int(self.baud_combo.currentText())
        except ValueError:
            pass
        self.cfg.parity = self.parity_combo.currentData() or "N"

    def refresh(self) -> None:
        link = self.bench.consoles.get(self.cfg.name)
        opened = link is not None and link.is_open
        self.toggle.blockSignals(True)
        self.toggle.setChecked(opened)
        self.toggle.setText("关闭" if opened else "打开")
        self.toggle.blockSignals(False)
        for widget in (self.port_combo, self.baud_combo, self.parity_combo):
            widget.setEnabled(not opened)
        self.counter.setText(
            f"{link.tx_bytes}/{link.rx_bytes} 字节" if link is not None else ""
        )

    def _on_toggle(self, checked: bool) -> None:
        self.store_config()
        self.on_toggle(self.cfg.name, checked)


class ConsoleTab(QWidget):
    """One console's own view, with its own send box."""

    def __init__(self, bench: Bench, cfg, recorder, parent=None):
        super().__init__(parent)
        self.bench = bench
        self.cfg = cfg
        self.recorder = recorder

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)

        header = QHBoxLayout()
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        self.timestamps = QCheckBox("时间戳")
        self.hex_view = QCheckBox("十六进制")
        clear = QPushButton("清空")
        clear.clicked.connect(lambda: self.receive.clear())
        header.addStretch(1)
        for widget in (self.autoscroll, self.timestamps, self.hex_view, clear):
            header.addWidget(widget)
        layout.addLayout(header)

        self.receive = LogView(max_lines=8000)
        layout.addWidget(self.receive, 1)

        send_row = QHBoxLayout()
        self.send_box = QPlainTextEdit()
        self.send_box.setFont(mono_font())
        self.send_box.setPlaceholderText("要发送的内容，Ctrl+Enter 发送")
        self.send_box.setMaximumHeight(70)
        QShortcut(QKeySequence("Ctrl+Return"), self.send_box, self.send)
        self.send_box.setEnabled(False)
        send_row.addWidget(self.send_box, 1)

        controls = QVBoxLayout()
        self.ending_combo = QComboBox()
        for label in LINE_ENDINGS:
            self.ending_combo.addItem(label)
        self.ending_combo.setCurrentIndex(1)
        self.send_hex = QCheckBox("十六进制发送")
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send)
        self.send_button.setEnabled(False)
        controls.addWidget(self.ending_combo)
        controls.addWidget(self.send_hex)
        controls.addWidget(self.send_button)
        controls.addStretch(1)
        send_row.addLayout(controls)
        layout.addLayout(send_row)

    def set_open(self, opened: bool) -> None:
        self.send_box.setEnabled(opened)
        self.send_button.setEnabled(opened)

    def append_rows(self, rows, scroll: bool) -> None:
        self.receive.append_block(rows, scroll=scroll and self.autoscroll.isChecked())

    def send(self) -> None:
        link = self.bench.consoles.get(self.cfg.name)
        if link is None or not link.is_open:
            return
        content = self.send_box.toPlainText()
        if not content:
            return
        try:
            if self.send_hex.isChecked():
                payload = bytes.fromhex(content.replace(" ", "").replace("\n", ""))
                shown = payload.hex(" ").upper()
                step = {"console": self.cfg.name, "hex": payload.hex(" ")}
            else:
                payload = (
                    content + LINE_ENDINGS[self.ending_combo.currentText()]
                ).encode(link.cfg.encoding, errors="replace")
                shown = content.replace("\r", "\\r").replace("\n", "\\n")
                step = {"console": self.cfg.name, "text": content}
            link.write_bytes(payload)
        except ValueError as exc:
            self.append_rows([(f"十六进制格式错误：{exc}", "#f48771")], True)
            return
        except Exception as exc:  # noqa: BLE001
            self.append_rows([(f"{type(exc).__name__}: {exc}", "#f48771")], True)
            return

        if self.recorder is not None:
            self.recorder.record("serial_write", step)
        prefix = time.strftime("[%H:%M:%S] ") if self.timestamps.isChecked() else ""
        self.append_rows([(f"{prefix}→ {shown}", "#569cd6")], True)
        if self.send_hex.isChecked():
            self.send_box.clear()


def list_console_ports():
    return list_com_ports()


class SerialPanel(Panel):
    def __init__(self, bench: Bench, recorder=None, parent: QWidget | None = None) -> None:
        super().__init__("串口（CH340 设备控制台）", parent)
        self.bench = bench
        self.recorder = recorder
        self.rows: dict[str, ConsoleRow] = {}
        self.tabs: dict[str, ConsoleTab] = {}
        self.colors: dict[str, str] = {}
        # Fan-in buffers. Every open console writes into its own and into the
        # merged one; a timer drains them in blocks.
        self._pending: dict[str, deque] = {}
        self._merged: deque = deque(maxlen=40000)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def _build(self) -> None:
        toolbar = QHBoxLayout()
        open_all = QPushButton("全部打开")
        open_all.clicked.connect(lambda: self._all(True))
        close_all = QPushButton("全部关闭")
        close_all.clicked.connect(lambda: self._all(False))
        refresh = QPushButton("刷新串口")
        refresh.clicked.connect(self.refresh_ports)
        toolbar.addWidget(open_all)
        toolbar.addWidget(close_all)
        toolbar.addWidget(refresh)
        toolbar.addStretch(1)
        self.body().addLayout(toolbar)

        self.bind_area = QWidget()
        self.bind_layout = QVBoxLayout(self.bind_area)
        self.bind_layout.setContentsMargins(0, 0, 0, 0)
        self.body().addWidget(self.bind_area)

        self.tab_widget = QTabWidget()
        self.merged_tab = QWidget()
        merged_layout = QVBoxLayout(self.merged_tab)
        merged_layout.setContentsMargins(0, 6, 0, 0)
        merged_header = QHBoxLayout()
        self.merged_autoscroll = QCheckBox("自动滚动")
        self.merged_autoscroll.setChecked(True)
        clear_merged = QPushButton("清空合并视图")
        clear_merged.clicked.connect(lambda: self.merged.clear())
        merged_header.addWidget(QLabel("所有已打开串口的输出，按端口分色"))
        merged_header.addStretch(1)
        merged_header.addWidget(self.merged_autoscroll)
        merged_header.addWidget(clear_merged)
        merged_layout.addLayout(merged_header)
        self.merged = LogView(max_lines=20000)
        merged_layout.addWidget(self.merged, 1)
        self.tab_widget.addTab(self.merged_tab, "合并")

        self.body().addWidget(self.tab_widget, 1)
        self.rebuild()

    def rebuild(self) -> None:
        while self.bind_layout.count():
            item = self.bind_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows.clear()

        while self.tab_widget.count() > 1:
            self.tab_widget.removeTab(1)
        self.tabs.clear()
        self.colors.clear()

        for index, cfg in enumerate(self.bench.settings.consoles):
            self.colors[cfg.name] = CONSOLE_COLORS[index % len(CONSOLE_COLORS)]
            row = ConsoleRow(self.bench, cfg, self._toggle, self.set_status)
            self.bind_layout.addWidget(row)
            self.rows[cfg.name] = row

            tab = ConsoleTab(self.bench, cfg, self.recorder)
            self.tabs[cfg.name] = tab
            self.tab_widget.addTab(tab, cfg.name)
            self._pending[cfg.name] = deque(maxlen=20000)

        if not self.bench.settings.consoles:
            self.bind_layout.addWidget(
                QLabel("尚未配置串口（settings.yaml 的 consoles）")
            )

    # -- open / close -------------------------------------------------------

    def refresh_ports(self) -> None:
        ports = list_com_ports()
        for row in self.rows.values():
            current = row.port_combo.currentText()
            row.port_combo.clear()
            row.port_combo.addItem("")
            for port in ports:
                marker = "★ " if port.is_console_candidate else ""
                row.port_combo.addItem(f"{marker}{port.label}", port.device)
            row.port_combo.setCurrentText(current)

    def refresh(self) -> None:
        for name, row in self.rows.items():
            row.refresh()
            tab = self.tabs.get(name)
            link = self.bench.consoles.get(name)
            if tab is not None:
                tab.set_open(bool(link is not None and link.is_open))

    def _toggle(self, name: str, opened: bool) -> None:
        if not opened:
            self.bench.close_console(name)
            self.refresh()
            self.set_status(f"{name} 已关闭")
            return

        cfg = self._cfg(name)
        if cfg is None or not cfg.port:
            self.rows[name].toggle.setChecked(False)
            self.rows[name].refresh()
            self.set_status(f"{name}：请先选择串口", ok=False)
            return

        def work():
            return self.bench.console(name, on_line=partial(self._on_line, name))

        def done(_):
            self.refresh()
            self.set_status(f"{name} 已打开 {cfg.port} @ {cfg.baudrate}")

        def fail(message: str):
            self.refresh()
            self.set_status(f"{name}：{message}", ok=False)

        run_task(work, done, fail)

    def _cfg(self, name: str):
        for cfg in self.bench.settings.consoles:
            if cfg.name == name:
                return cfg
        return None

    def _all(self, opened: bool) -> None:
        if not opened:
            for name in list(self.bench.consoles):
                self.bench.close_console(name)
            self.refresh()
            self.set_status("已关闭全部串口")
            return

        for row in self.rows.values():
            row.store_config()
        missing = [c.name for c in self.bench.settings.consoles if not c.port]
        if missing:
            self.set_status(f"未绑定串口：{', '.join(missing)}", ok=False)
        for name in self.rows:
            self._toggle(name, True)

    # -- receiving ----------------------------------------------------------

    def _on_line(self, name: str, text: str) -> None:
        """Called on the console's reader thread: buffer only, never touch widgets."""
        tab = self.tabs.get(name)
        stamp = time.strftime("%H:%M:%S")
        if tab is not None and tab.timestamps.isChecked():
            line = f"[{stamp}] {text}"
        else:
            line = text
        color = self.colors.get(name, "#d4d4d4")
        self._pending[name].append((line, color))
        self._merged.append((f"{name:>{MERGED_PREFIX_WIDTH}} │ {line}", color))

    def _flush(self) -> None:
        for name, pending in self._pending.items():
            if not pending:
                continue
            batch = list(pending)
            pending.clear()
            tab = self.tabs.get(name)
            if tab is not None:
                tab.append_rows(batch, scroll=True)
        if self._merged:
            batch = list(self._merged)
            self._merged.clear()
            self.merged.append_block(
                batch, scroll=self.merged_autoscroll.isChecked()
            )
        self.refresh()

    def shutdown(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        for name in list(self.bench.consoles):
            self.bench.close_console(name)
