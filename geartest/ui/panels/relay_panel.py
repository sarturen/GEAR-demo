"""Relay panel: every configured board at once, plus the named rail resources.

Boards are shown side by side in a vertical stack rather than one at a time,
because the state that matters is spread across them -- KL15 may be on board 1
and KL30 on board 2, and an operator wants to see both without switching tabs.
"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench, BenchError
from ...devices.relay_modbus import RelayBoard
from ...devices.serial_ports import list_com_ports
from ..common import LogView, Panel, make_form
from ..worker import run_task

BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
REFRESH_MS = 300

#: Roughly the height of one board's group box: a header row plus a row of
#: channel buttons plus the frame. Used to cap the scroll area's height.
BOARD_ROW_HEIGHT = 132


class BoardRow(QGroupBox):
    """One board: its port binding, and a row of its sixteen channels."""

    def __init__(self, bench: Bench, cfg, on_traffic, on_status, on_record, parent=None):
        super().__init__(cfg.name, parent)
        self.bench = bench
        self.cfg = cfg
        self.on_traffic = on_traffic
        self.on_status = on_status
        self.on_record = on_record
        self.buttons: list[QPushButton] = []

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(240)
        self.port_combo.addItem("")
        for port in list_com_ports():
            marker = "★ " if port.is_relay_candidate else ""
            self.port_combo.addItem(f"{marker}{port.label}", port.device)
        self.port_combo.setCurrentText(cfg.port)
        header.addWidget(self.port_combo)

        header.addWidget(QLabel("波特率"))
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        for rate in BAUD_RATES:
            self.baud_combo.addItem(str(rate))
        self.baud_combo.setCurrentText(str(cfg.baudrate))
        header.addWidget(self.baud_combo)

        header.addWidget(QLabel("从站"))
        self.slave_combo = QComboBox()
        self.slave_combo.setEditable(True)
        for slave in range(1, 17):
            self.slave_combo.addItem(str(slave))
        self.slave_combo.setCurrentText(str(cfg.slave_id))
        header.addWidget(self.slave_combo)

        self.coil_combo = QComboBox()
        self.coil_combo.setEditable(True)
        for base in (0, 1):
            self.coil_combo.addItem(str(base))
        self.coil_combo.setCurrentText(str(cfg.coil_base))
        self.coil_combo.setToolTip("第 1 路对应的 Modbus 线圈地址，有的板子从 1 开始")
        header.addWidget(QLabel("线圈基址"))
        header.addWidget(self.coil_combo)

        self.toggle = QPushButton("打开")
        self.toggle.setCheckable(True)
        self.toggle.clicked.connect(self._on_toggle)
        header.addWidget(self.toggle)
        self.status = QLabel("未打开")
        self.status.setStyleSheet("color:#808080")
        header.addWidget(self.status)
        header.addStretch(1)
        layout.addLayout(header)

        channels = QHBoxLayout()
        channels.setSpacing(4)
        for index in range(RelayBoard.CHANNELS):
            button = QPushButton(str(index + 1))
            button.setCheckable(True)
            button.setFixedSize(52, 34)
            button.clicked.connect(partial(self._on_channel, index + 1))
            button.setEnabled(False)
            channels.addWidget(button)
            self.buttons.append(button)
        channels.addStretch(1)
        layout.addLayout(channels)

        self.setLayout(layout)

    # -- state --------------------------------------------------------------

    @property
    def board(self) -> RelayBoard | None:
        return self.bench.relays.get(self.cfg.name)

    def store_config(self) -> None:
        data = self.port_combo.currentData()
        self.cfg.port = data or self.port_combo.currentText().strip()
        try:
            self.cfg.baudrate = int(self.baud_combo.currentText())
            self.cfg.slave_id = int(self.slave_combo.currentText())
            self.cfg.coil_base = int(self.coil_combo.currentText())
        except ValueError:
            pass

    def refresh(self) -> None:
        board = self.board
        opened = board is not None and board.is_open
        self.toggle.blockSignals(True)
        self.toggle.setChecked(opened)
        self.toggle.setText("关闭" if opened else "打开")
        self.toggle.blockSignals(False)
        for widget in (self.port_combo, self.baud_combo, self.slave_combo, self.coil_combo):
            widget.setEnabled(not opened)
        self.status.setText(
            f"已打开 {self.cfg.port} @ {self.cfg.baudrate}"
            if opened
            else (self.cfg.port or "未绑定串口")
        )
        self.status.setStyleSheet("color:#4ec9b0" if opened else "color:#808080")

        for index, button in enumerate(self.buttons):
            button.setEnabled(opened)
            button.blockSignals(True)
            button.setChecked(bool(board and board.state[index]))
            button.blockSignals(False)

    # -- actions ------------------------------------------------------------

    def _on_toggle(self, checked: bool) -> None:
        if not checked:
            self.bench.close_relay(self.cfg.name)
            self.refresh()
            return
        self.store_config()
        if not self.cfg.port:
            self.toggle.setChecked(False)
            self.on_status(f"{self.cfg.name}：请先选择串口", False)
            return

        def work():
            return self.bench.relay(self.cfg.name, on_traffic=self.on_traffic)

        def done(_):
            self.refresh()
            self.on_status(f"{self.cfg.name} 已打开 {self.cfg.port}")

        def fail(message: str):
            self.toggle.setChecked(False)
            self.refresh()
            self.on_status(f"{self.cfg.name}：{message}", False)

        run_task(work, done, fail)

    def _on_channel(self, channel: int, checked: bool) -> None:
        board = self.board
        if board is None or not board.is_open:
            self.buttons[channel - 1].setChecked(not checked)
            self.on_status(f"{self.cfg.name} 尚未打开", False)
            return

        def done(_):
            self.refresh()
            self.on_status(f"{self.cfg.name} CH{channel} {'闭合' if checked else '断开'}")

        def fail(message: str):
            self.buttons[channel - 1].setChecked(not checked)
            self.on_status(message, False)

        run_task(lambda: board.set_channel(channel, checked), done, fail)


class RelayPanel(Panel):
    traffic = Signal(str, bytes)

    def __init__(self, bench: Bench, recorder=None, parent: QWidget | None = None) -> None:
        super().__init__("继电器（PL2303GT · Modbus RTU）", parent)
        self.bench = bench
        self.recorder = recorder
        self.rows: dict[str, BoardRow] = {}
        self.rail_buttons: dict[str, QPushButton] = {}
        self.traffic.connect(self._on_traffic)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        toolbar = QHBoxLayout()
        open_all = QPushButton("全部打开")
        open_all.clicked.connect(lambda: self._all(True))
        close_all = QPushButton("全部关闭")
        close_all.clicked.connect(lambda: self._all(False))
        refresh_ports = QPushButton("刷新串口")
        refresh_ports.clicked.connect(self.refresh_ports)
        toolbar.addWidget(open_all)
        toolbar.addWidget(close_all)
        toolbar.addWidget(refresh_ports)
        toolbar.addStretch(1)
        self.body().addLayout(toolbar)

        self.board_area = QWidget()
        self.board_layout = QVBoxLayout(self.board_area)
        self.board_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.board_area)
        # Sized to the boards that exist rather than given a stretch factor: a
        # single board would otherwise leave two thirds of the tab empty above
        # the rail list, which is the part an operator actually reads.
        self.scroll.setMaximumHeight(BOARD_ROW_HEIGHT)
        self.body().addWidget(self.scroll)

        self.rails_box = QGroupBox("具名电源资源（流程里引用这些名字）")
        self.rails_grid = QGridLayout(self.rails_box)
        self.body().addWidget(self.rails_box)

        self.log = LogView(max_lines=2000)
        self.body().addWidget(QLabel("Modbus 报文"))
        self.body().addWidget(self.log, 1)

        self.rebuild()

    def rebuild(self) -> None:
        while self.board_layout.count():
            item = self.board_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows.clear()

        for cfg in self.bench.settings.relays:
            row = BoardRow(
                self.bench, cfg, self._traffic, self.set_status, self._record_board
            )
            self.board_layout.addWidget(row)
            self.rows[cfg.name] = row
        self.board_layout.addStretch(1)
        self.scroll.setMaximumHeight(
            max(1, len(self.rows)) * BOARD_ROW_HEIGHT + 8
        )
        self._build_rails()

    def _build_rails(self) -> None:
        while self.rails_grid.count():
            item = self.rails_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rail_buttons.clear()

        if not self.bench.settings.relay_channels:
            self.rails_grid.addWidget(
                QLabel("尚未配置继电器资源（settings.yaml 的 relay_channels）"), 0, 0
            )
            return

        for row, channel in enumerate(self.bench.settings.relay_channels):
            name = QLabel(channel.name)
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            self.rails_grid.addWidget(name, row, 0)
            self.rails_grid.addWidget(QLabel(channel.description), row, 1)

            binding = QLabel(f"{channel.relay} 第 {channel.channel} 路")
            binding.setStyleSheet("color:#808080")
            self.rails_grid.addWidget(binding, row, 2)

            state = QLabel("—")
            state.setStyleSheet("font-family:Consolas")
            self.rails_grid.addWidget(state, row, 3)

            close_button = QPushButton("闭合")
            close_button.clicked.connect(partial(self._set_rail, channel.name, True))
            self.rails_grid.addWidget(close_button, row, 4)

            open_button = QPushButton("断开")
            open_button.clicked.connect(partial(self._set_rail, channel.name, False))
            self.rails_grid.addWidget(open_button, row, 5)

            pulse = QPushButton(f"脉冲 {channel.pulse_ms}ms")
            pulse.clicked.connect(partial(self._pulse_rail, channel.name, channel.pulse_ms))
            self.rails_grid.addWidget(pulse, row, 6)

        self.rails_grid.setColumnStretch(7, 1)

    # -- actions ------------------------------------------------------------

    def refresh_ports(self) -> None:
        ports = list_com_ports()
        for row in self.rows.values():
            current = row.port_combo.currentText()
            row.port_combo.clear()
            row.port_combo.addItem("")
            for port in ports:
                marker = "★ " if port.is_relay_candidate else ""
                row.port_combo.addItem(f"{marker}{port.label}", port.device)
            row.port_combo.setCurrentText(current)

    def refresh(self) -> None:
        for row in self.rows.values():
            row.refresh()
        self._refresh_rails()

    def _refresh_rails(self) -> None:
        for row, channel in enumerate(self.bench.settings.relay_channels):
            label = self.rails_grid.itemAtPosition(row, 3)
            widget = label.widget() if label else None
            if widget is None:
                continue
            board = self.bench.relays.get(channel.relay)
            if board is None or not board.is_open:
                widget.setText("未打开")
                widget.setStyleSheet("color:#808080")
                continue
            index = channel.channel - 1
            closed = 0 <= index < len(board.state) and board.state[index]
            widget.setText("闭合" if closed else "断开")
            widget.setStyleSheet(
                "color:#f48771;font-weight:bold" if closed else "color:#4ec9b0"
            )

    def _all(self, open_them: bool) -> None:
        if not open_them:
            for name in list(self.bench.relays):
                self.bench.close_relay(name)
            self.refresh()
            self.set_status("已关闭全部继电器板")
            return

        def work():
            problems = []
            for cfg in self.bench.settings.relays:
                row = self.rows.get(cfg.name)
                if row is not None:
                    row.store_config()
                if not cfg.port:
                    problems.append(f"{cfg.name}：未绑定串口")
                    continue
                try:
                    self.bench.relay(cfg.name, on_traffic=self._traffic)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{cfg.name}：{exc}")
            return problems

        def done(problems: list[str]):
            self.refresh()
            if problems:
                self.set_status("；".join(problems), ok=False)
            else:
                self.set_status("全部继电器板已打开")

        run_task(work, done, self._fail)

    def _set_rail(self, name: str, closed: bool) -> None:
        try:
            board, channel = self._rail(name)
        except BenchError as exc:
            self._fail(str(exc))
            return

        def done(_):
            self.refresh()
            self.set_status(f"{name} {'闭合' if closed else '断开'}")
            self._record(name, "on" if closed else "off")

        run_task(
            lambda: board.set_channel(channel.channel, closed),
            done,
            self._fail,
        )

    def _pulse_rail(self, name: str, milliseconds: int) -> None:
        try:
            board, channel = self._rail(name)
        except BenchError as exc:
            self._fail(str(exc))
            return
        run_task(
            lambda: board.pulse(channel.channel, milliseconds),
            lambda _: (self.refresh(), self.set_status(f"{name} 脉冲 {milliseconds}ms"),
                       self._record(name, "pulse", milliseconds)),
            self._fail,
        )

    def _rail(self, name: str):
        for channel in self.bench.settings.relay_channels:
            if channel.name == name:
                board = self.bench.relays.get(channel.relay)
                if board is None or not board.is_open:
                    raise BenchError(f"{channel.relay} 尚未打开，先打开它")
                return board, channel
        raise BenchError(f"未配置继电器资源「{name}」")

    def _traffic(self, direction: str, frame: bytes) -> None:
        self.traffic.emit(direction, frame)

    def _on_traffic(self, direction: str, frame: bytes) -> None:
        self.log.append(f"{direction.upper():>2}  {frame.hex(' ').upper()}", direction)

    def _record(self, resource: str, action: str, pulse_ms: int | None = None) -> None:
        if self.recorder is None:
            return
        spec = {"resource": resource, "action": action}
        if pulse_ms is not None:
            spec["pulse_ms"] = pulse_ms
        self.recorder.record("relay", spec)

    def _record_board(self, message: str) -> None:
        self.set_status(message)

    def _fail(self, message: str) -> None:
        self.set_status(message, ok=False)
        self.log.append(message, "error")
