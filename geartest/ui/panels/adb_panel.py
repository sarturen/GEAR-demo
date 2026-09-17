"""ADB panel: pick a device, run commands, watch its output stream."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench
from ...devices.adb import Adb, AdbDevice, AdbError
from ..common import LogView, Panel, make_form, mono_font
from ..worker import run_task

QUICK_COMMANDS = [
    ("设备信息", "shell getprop ro.product.model"),
    ("系统版本", "shell getprop ro.build.version.release"),
    ("运行进程", "shell ps -A"),
    ("存储", "shell df -h"),
    ("重启", "reboot"),
]

#: logcat can emit thousands of lines a second. Lines queue here and reach the
#: view on a timer, so a busy stream cannot flood the GUI thread.
FLUSH_INTERVAL_MS = 120
MAX_BUFFER = 20000


class AdbPanel(Panel):
    def __init__(self, bench: Bench, recorder=None, parent: QWidget | None = None) -> None:
        super().__init__("ADB 设备", parent)
        self.bench = bench
        self.recorder = recorder
        self.client: Adb | None = None
        self._pending: deque[tuple[str, str]] = deque(maxlen=MAX_BUFFER)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._logcat_running = False
        self._build()
        self._load_device()
        self.refresh_devices()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        connection = QGroupBox("连接")
        form = make_form()

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        # Populate before wiring the change handler: the first addItem fires it,
        # and the widgets it reaches for do not exist yet.
        for cfg in self.bench.settings.devices:
            self.device_combo.addItem(cfg.name)
        self.device_combo.currentIndexChanged.connect(self._load_device)
        device_row.addWidget(QLabel("被测设备"))
        device_row.addWidget(self.device_combo)
        device_row.addStretch(1)
        form.addRow("", device_row)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit("adb")
        self.path_edit.setMinimumWidth(160)
        self.path_edit.editingFinished.connect(self._store_path)
        path_row.addWidget(self.path_edit, 1)
        form.addRow("adb 路径", path_row)

        serial_row = QHBoxLayout()
        self.serial_input = QLineEdit()
        self.serial_input.setPlaceholderText("绑定的序列号；留空则用 adb 默认设备")
        self.serial_input.setMinimumWidth(240)
        self.serial_input.textChanged.connect(self._on_serial_typed)
        serial_row.addWidget(self.serial_input, 1)
        form.addRow("adb 序列号", serial_row)

        found_row = QHBoxLayout()
        self.found_combo = QComboBox()
        self.found_combo.setMinimumWidth(300)
        refresh = QPushButton("刷新设备")
        refresh.clicked.connect(self.refresh_devices)
        bind = QPushButton("绑定选中")
        bind.clicked.connect(self._bind_selected)
        found_row.addWidget(self.found_combo, 1)
        found_row.addWidget(refresh)
        found_row.addWidget(bind)
        form.addRow("已发现", found_row)

        connection.setLayout(form)
        config_row = QHBoxLayout()
        config_row.addWidget(connection)
        config_row.addStretch(1)
        self.body().addLayout(config_row)

        command_box = QGroupBox("命令")
        command_layout = QVBoxLayout(command_box)
        self.command_input = QPlainTextEdit()
        self.command_input.setFont(mono_font())
        self.command_input.setPlaceholderText("例如 shell dumpsys battery  ·  Ctrl+Enter 执行")
        self.command_input.setMaximumHeight(70)
        command_layout.addWidget(self.command_input)

        row = QHBoxLayout()
        run = QPushButton("执行")
        run.clicked.connect(self._run_command)
        row.addWidget(run)
        for label, command in QUICK_COMMANDS:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, c=command: self._run_quick(c))
            row.addWidget(button)
        row.addStretch(1)
        command_layout.addLayout(row)

        logcat_row = QHBoxLayout()
        self.logcat_filter = QLineEdit()
        self.logcat_filter.setPlaceholderText("logcat 过滤（可留空），例如 ActivityManager:I *:S")
        self.logcat_button = QPushButton("开始 logcat")
        self.logcat_button.clicked.connect(self._toggle_logcat)
        logcat_row.addWidget(QLabel("输出监控"))
        logcat_row.addWidget(self.logcat_filter, 1)
        logcat_row.addWidget(self.logcat_button)
        command_layout.addLayout(logcat_row)

        self.body().addWidget(command_box)

        header = QHBoxLayout()
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        clear = QPushButton("清空输出")
        clear.clicked.connect(lambda: self.output.clear())
        header.addWidget(QLabel("输出"))
        header.addStretch(1)
        header.addWidget(self.autoscroll)
        header.addWidget(clear)
        self.body().addLayout(header)

        self.output = LogView(max_lines=8000)
        self.body().addWidget(self.output, 1)

    # -- devices ------------------------------------------------------------

    def rebuild(self) -> None:
        """Re-read the device list after the configuration changed."""
        self.client = None
        self.found_combo.clear()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for cfg in self.bench.settings.devices:
            self.device_combo.addItem(cfg.name)
        self.device_combo.blockSignals(False)
        self._load_device()

    def _device_cfg(self):
        name = self.device_combo.currentText()
        if not name:
            raise AdbError("尚未配置被测设备")
        return self.bench.device(name)

    def _load_device(self) -> None:
        try:
            cfg = self._device_cfg()
        except AdbError:
            return
        self.path_edit.blockSignals(True)
        self.path_edit.setText(cfg.adb_path)
        self.path_edit.blockSignals(False)
        self.serial_input.blockSignals(True)
        self.serial_input.setText(cfg.adb_serial)
        self.serial_input.blockSignals(False)
        self._rebuild_client()

    def _store_path(self) -> None:
        try:
            self._device_cfg().adb_path = self.path_edit.text().strip() or "adb"
        except AdbError:
            pass
        self._rebuild_client()

    def _on_serial_typed(self, text: str) -> None:
        """Editing the serial rebinds the DUT -- it is a physical fact about
        this bench, not part of the logical topology."""
        try:
            self._device_cfg().adb_serial = text.strip()
        except AdbError:
            pass
        self._rebuild_client()

    def _rebuild_client(self) -> None:
        try:
            cfg = self._device_cfg()
        except AdbError:
            self.client = None
            return
        self.client = self.bench.adb(cfg.name)

    def refresh_devices(self) -> None:
        path = self.path_edit.text().strip() or "adb"

        def done(devices: list[AdbDevice]):
            self.found_combo.clear()
            for device in devices:
                self.found_combo.addItem(device.label, device.serial)
            ready = [d for d in devices if d.is_ready]
            if devices:
                self.set_status(f"发现 {len(devices)} 台设备，{len(ready)} 台就绪")
            else:
                self.set_status(
                    "未发现 adb 设备（用 USB 连接设备并开启调试后点刷新）", ok=False
                )

        run_task(lambda: Adb(path).list_devices(), done, self._fail)

    def _bind_selected(self) -> None:
        serial = self.found_combo.currentData()
        if not serial:
            self.set_status("请先在已发现列表里选择一台设备", ok=False)
            return
        self.serial_input.setText(str(serial))
        self.set_status(f"已把 {serial} 绑定到 {self.device_combo.currentText()}")

    # -- commands -----------------------------------------------------------

    def _run_quick(self, command: str) -> None:
        self.command_input.setPlainText(command)
        self._run_command()

    def _run_command(self) -> None:
        command = self.command_input.toPlainText().strip()
        if not command:
            self.set_status("请输入命令", ok=False)
            return
        if self.client is None:
            self.set_status("尚未配置被测设备，无法定位 adb 目标", ok=False)
            return
        client = self.client
        device = self.device_combo.currentText()
        serial = self.serial_input.text().strip()
        self.output.append(
            f"$ adb {'-s ' + serial + ' ' if serial else ''}{command}   [{device}]", "info"
        )

        def done(text: str):
            self._show_output(text)
            if self.recorder is not None:
                self.recorder.record("adb", {"device": device, "command": command})

        run_task(lambda: client.run(command, timeout=60.0), done, self._fail)

    def _ensure_client(self) -> None:
        if self.client is None:
            self._rebuild_client()

    def _show_output(self, text: str) -> None:
        if text.strip():
            for line in text.rstrip().splitlines():
                self.output.append(line, "rx")
        else:
            self.output.append("（无输出）", "muted")
        self._scroll()
        self.set_status("执行完成")

    # -- logcat -------------------------------------------------------------

    def _toggle_logcat(self) -> None:
        if self._logcat_running:
            client = self.client
            if client is not None:
                client.stop_all()
            self._logcat_running = False
            self._flush_timer.stop()
            self.logcat_button.setText("开始 logcat")
            self.set_status("已停止输出监控")
            return

        self._ensure_client()
        client = self.client
        filter_spec = self.logcat_filter.text().strip()
        command = ["logcat", "-v", "time"] + (filter_spec.split() if filter_spec else [])

        def work():
            return client.stream(
                command,
                on_line=lambda line: self._pending.append((line, "rx")),
                on_exit=self._logcat_exited,
                on_error=lambda exc: self._pending.append((f"输出流中断：{exc}", "error")),
            )

        def done(_):
            self._logcat_running = True
            self._flush_timer.start()
            self.logcat_button.setText("停止 logcat")
            self.set_status("正在监控输出")

        run_task(work, done, self._fail)

    def _logcat_exited(self, code: int) -> None:
        self._pending.append((f"logcat 退出（{code}）", "muted"))
        self._logcat_running = False
        self.logcat_button.setText("开始 logcat")
        self._flush_timer.stop()

    def _flush(self) -> None:
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        for line, level in batch:
            self.output.append(line, level)
        self._scroll()

    def _scroll(self) -> None:
        if self.autoscroll.isChecked():
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _fail(self, message: str) -> None:
        self.set_status(message, ok=False)
        self.output.append(message, "error")

    def shutdown(self) -> None:
        """Stop streaming before the window goes away."""
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        client = self.client
        if client is not None:
            client.stop_all()
