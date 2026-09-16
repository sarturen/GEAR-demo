"""DUT panel: one device's topology, its adb binding, and a bring-up switch.

The resource lists here are read-only on purpose. Membership is topology, it
belongs in settings.yaml where it can be reviewed next to the test cases that
depend on it; what is bound here is the part that is different on every machine
-- which adb serial this bench is cabled to, and whether the device is up.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench, BenchError
from ..common import LogView, Panel, make_form
from ..worker import run_task

REFRESH_MS = 500


class DeviceView(QWidget):
    """The resource table for one device."""

    def __init__(self, bench: Bench, cfg, parent=None):
        super().__init__(parent)
        self.bench = bench
        self.cfg = cfg
        self.rows: dict[tuple[str, str], QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        binding = QGroupBox("物理绑定")
        form = make_form()
        self.serial_edit = QLineEdit(cfg.adb_serial)
        self.serial_edit.setPlaceholderText("adb 序列号，例如 0123456789")
        self.serial_edit.setMinimumWidth(240)
        form.addRow("adb 序列号", self.serial_edit)
        self.adb_path_edit = QLineEdit(cfg.adb_path)
        self.adb_path_edit.setMinimumWidth(160)
        form.addRow("adb 路径", self.adb_path_edit)
        binding.setLayout(form)
        layout.addWidget(binding)

        resources = QGroupBox(f"资源（{cfg.description or cfg.name}）")
        grid = QVBoxLayout(resources)
        for kind, title in (
            ("power", "电源"),
            ("consoles", "串口"),
            ("screens", "画面"),
        ):
            names = getattr(cfg, kind)
            if not names:
                continue
            header = QLabel(title)
            header.setStyleSheet("color:#808080")
            grid.addWidget(header)
            for name in names:
                row = QHBoxLayout()
                label = QLabel(name)
                font = label.font()
                font.setBold(True)
                label.setFont(font)
                label.setMinimumWidth(100)
                row.addWidget(label)
                where = QLabel(self._binding_text(kind, name))
                where.setStyleSheet("color:#808080")
                row.addWidget(where, 1)
                state = QLabel("—")
                state.setStyleSheet("font-family:Consolas")
                row.addWidget(state)
                grid.addLayout(row)
                self.rows[(kind, name)] = state
        layout.addWidget(resources)
        layout.addStretch(1)

    def store(self) -> None:
        self.cfg.adb_serial = self.serial_edit.text().strip()
        self.cfg.adb_path = self.adb_path_edit.text().strip() or "adb"

    def _binding_text(self, kind: str, name: str) -> str:
        try:
            resource = self.bench.resolve(self.cfg.name, kind, name)
        except BenchError as exc:
            return str(exc)
        if kind == "power":
            return f"→ 板卡 {resource.relay} 第 {resource.channel} 路"
        if kind == "consoles":
            port = resource.port or "（未绑定端口）"
            return f"→ {port} @ {resource.baudrate}"
        camera = self.bench.camera_cfg(resource.camera)
        return f"→ 摄像头 {resource.camera}（源 {camera.source}）"

    def refresh(self) -> None:
        for (kind, name), label in self.rows.items():
            text, color = self._resource_state(kind, name)
            label.setText(text)
            label.setStyleSheet(f"color:{color};font-family:Consolas")

    def _resource_state(self, kind: str, name: str) -> tuple[str, str]:
        if kind == "power":
            channel = next(
                (c for c in self.bench.settings.relay_channels if c.name == name), None
            )
            if channel is None:
                return "未定义", "#f48771"
            board = self.bench.relays.get(channel.relay)
            if board is None or not board.is_open:
                return "板卡未打开", "#808080"
            index = channel.channel - 1
            closed = 0 <= index < len(board.state) and board.state[index]
            return ("闭合" if closed else "断开"), ("#f48771" if closed else "#4ec9b0")

        if kind == "consoles":
            link = self.bench.consoles.get(name)
            if link is None or not link.is_open:
                return "未打开", "#808080"
            return f"已打开 {link.rx_bytes} 字节", "#4ec9b0"

        monitor = self.bench.screens.get(name)
        if monitor is None:
            return "未启动", "#808080"
        state = monitor.snapshot()
        if not state.get("running"):
            return "未启动", "#808080"
        if state.get("black"):
            return "黑屏", "#f48771"
        if state.get("freeze"):
            return "冻屏", "#f48771"
        if not state.get("calibrated"):
            return "校准中", "#dcdcaa"
        return "正常", "#4ec9b0"


class DutPanel(Panel):
    def __init__(self, bench: Bench, parent: QWidget | None = None) -> None:
        super().__init__("被测设备", parent)
        self.bench = bench
        self.views: dict[str, DeviceView] = {}
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def _build(self) -> None:
        toolbar = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(200)
        self.device_combo.currentIndexChanged.connect(self._select)
        open_all = QPushButton("拉起整台设备")
        open_all.clicked.connect(lambda: self._bring_up(True))
        close_all = QPushButton("关闭整台设备")
        close_all.clicked.connect(lambda: self._bring_up(False))
        toolbar.addWidget(QLabel("设备"))
        toolbar.addWidget(self.device_combo)
        toolbar.addWidget(open_all)
        toolbar.addWidget(close_all)
        toolbar.addStretch(1)
        self.body().addLayout(toolbar)

        self.view_area = QWidget()
        self.view_layout = QVBoxLayout(self.view_area)
        self.view_layout.setContentsMargins(0, 0, 0, 0)
        self.body().addWidget(self.view_area)

        self.log = LogView(max_lines=2000)
        self.body().addWidget(QLabel("设备启停日志"))
        self.body().addWidget(self.log, 1)
        self.rebuild()

    def rebuild(self) -> None:
        while self.view_layout.count():
            item = self.view_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.views.clear()

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for cfg in self.bench.settings.devices:
            self.device_combo.addItem(cfg.name)
        self.device_combo.blockSignals(False)

        for cfg in self.bench.settings.devices:
            view = DeviceView(self.bench, cfg)
            self.views[cfg.name] = view
            self.view_layout.addWidget(view)
        if not self.bench.settings.devices:
            self.view_layout.addWidget(
                QLabel("尚未配置被测设备（settings.yaml 的 devices）")
            )
        self._select()

    def _select(self) -> None:
        name = self.device_combo.currentText()
        for index in range(self.view_layout.count()):
            widget = self.view_layout.itemAt(index).widget()
            if isinstance(widget, DeviceView):
                widget.setVisible(widget.cfg.name == name)

    def refresh(self) -> None:
        for view in self.views.values():
            view.refresh()
        self._refresh_hardware_summary()

    def _refresh_hardware_summary(self) -> None:
        if not self.bench.settings.devices:
            return
        cfg = self.bench.device(self.device_combo.currentText())
        self.set_status(
            f"{cfg.name}：{len(cfg.power)} 路电源、{len(cfg.consoles)} 个串口、"
            f"{len(cfg.screens)} 个屏幕"
            + (f"，ADB {cfg.adb_serial}" if cfg.adb_serial else "，ADB 未绑定序列号")
        )

    def _bring_up(self, up: bool) -> None:
        if not self.bench.settings.devices:
            self.set_status("尚未配置被测设备", ok=False)
            return
        name = self.device_combo.currentText()
        view = self.views.get(name)
        if view is not None:
            view.store()

        if not up:
            def close():
                self.bench.close_device(name)

            run_task(
                close,
                lambda _: (self.refresh(), self.log.append(f"{name} 已关闭", "info"),
                           self.set_status(f"{name} 已关闭")),
                self._fail,
            )
            return

        def work():
            return self.bench.open_device(name)

        def done(opened: list[str]):
            self.refresh()
            for item in opened:
                self.log.append(f"  {item}", "ok")
            self.log.append(f"{name} 已拉起（{len(opened)} 项）", "ok")
            self.set_status(f"{name} 已拉起")

        run_task(work, done, self._fail)

    def _fail(self, message: str) -> None:
        for line in message.splitlines():
            self.log.append(line, "error")
        self.set_status(message.splitlines()[0], ok=False)

    def shutdown(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
