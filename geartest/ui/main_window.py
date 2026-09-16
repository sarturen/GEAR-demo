"""Main window: one tab per device family, plus the flow runner."""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from ..bench import Bench
from ..config import EVIDENCE_DIR, FLOW_DIR, SETTINGS_PATH, Settings, save_settings
from ..devices.serial_ports import list_com_ports
from ..flow import Recorder
from .panels.adb_panel import AdbPanel
from .panels.camera_panel import CameraPanel
from .panels.dut_panel import DutPanel
from .panels.flow_panel import FlowPanel
from .panels.relay_panel import RelayPanel
from .panels.serial_panel import SerialPanel
from .panels.ssh_panel import SshPanel
from .worker import drain

HARDWARE_HINT = (
    "未检测到串口设备。把 PL2303GT 与 CH340 插上后，点「文件 → 刷新设备」即可。"
)


class MainWindow(QMainWindow):
    error_reported = Signal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("GEAR 测试台")
        self.resize(1520, 960)

        self.recorder = Recorder()
        self.bench = Bench(settings)
        self.error_reported.connect(self._show_error)

        self.tabs = QTabWidget()
        self.relay_panel = RelayPanel(self.bench, self.recorder)
        self.serial_panel = SerialPanel(self.bench, self.recorder)
        self.adb_panel = AdbPanel(self.bench, self.recorder)
        self.ssh_panel = SshPanel(self.bench, self.recorder)
        self.camera_panel = CameraPanel(self.bench)
        self.dut_panel = DutPanel(self.bench)
        self.flow_panel = FlowPanel(self.bench, self.recorder)

        for panel, title in (
            (self.dut_panel, "被测设备"),
            (self.relay_panel, "继电器"),
            (self.serial_panel, "串口"),
            (self.adb_panel, "ADB"),
            (self.ssh_panel, "SSH 容器"),
            (self.camera_panel, "监控画面"),
            (self.flow_panel, "测试流程"),
        ):
            self.tabs.addTab(panel, title)
        self.setCentralWidget(self.tabs)

        self._build_menu()
        self.statusBar().showMessage(self._hardware_summary())

    # -- menu ---------------------------------------------------------------

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")

        save = QAction("保存配置(&S)", self)
        save.setShortcut("Ctrl+S")
        save.triggered.connect(self.save_settings)
        file_menu.addAction(save)

        refresh = QAction("刷新设备(&R)", self)
        refresh.setShortcut("F5")
        refresh.triggered.connect(self.refresh_devices)
        file_menu.addAction(refresh)

        close_all = QAction("关闭所有连接(&C)", self)
        close_all.triggered.connect(self._close_all)
        file_menu.addAction(close_all)

        file_menu.addSeparator()
        for label, directory in (
            ("打开配置目录", SETTINGS_PATH.parent),
            ("打开流程目录", FLOW_DIR),
            ("打开证据目录", EVIDENCE_DIR),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, d=directory: self._open_directory(d))
            file_menu.addAction(action)

        file_menu.addSeparator()
        quit_action = QAction("退出(&Q)", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        about = QAction("关于", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    # -- actions ------------------------------------------------------------

    def save_settings(self) -> None:
        try:
            save_settings(self.settings)
        except Exception as exc:
            self._show_error(f"保存配置失败：{exc}")
            return
        self.statusBar().showMessage(f"配置已保存到 {SETTINGS_PATH}", 5000)

    def refresh_devices(self) -> None:
        self.relay_panel.refresh_ports()
        self.serial_panel.refresh_ports()
        self.adb_panel.refresh_devices()
        self.ssh_panel.refresh_hosts()
        self.dut_panel.refresh()
        self.flow_panel.refresh_flows()
        self.statusBar().showMessage(self._hardware_summary(), 8000)

    def _close_all(self) -> None:
        self.bench.close_all()
        self.statusBar().showMessage("已关闭所有连接", 5000)

    def _open_directory(self, directory) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(directory))  # noqa: S606 - Windows shell open
        except AttributeError:
            subprocess.Popen(["xdg-open", str(directory)])
        except Exception as exc:
            self._show_error(f"无法打开目录：{exc}")

    def _about(self) -> None:
        QMessageBox.information(
            self,
            "关于 GEAR 测试台",
            "GEAR 测试台\n\n"
            "以被测设备为中心：用例声明一台设备，步骤引用它的资源名，\n"
            "不出现 COM 口、板卡和摄像头编号。\n\n"
            "继电器：多块 PL2303GT 16 路 · Modbus RTU\n"
            "串口：多个 CH340 控制台，合并视图 + 每口标签页\n"
            "设备：ADB 与 SSH 容器\n"
            "画面：多个监控屏幕的黑屏 / 闪黑 / 冻屏检测\n\n"
            "画面判定全部基于画面自身推导的相对量，不做绝对亮度比较，\n"
            "因此背光或环境光变化不会影响判定结果。",
        )

    # -- status -------------------------------------------------------------

    def _hardware_summary(self) -> str:
        ports = list_com_ports()
        if not ports:
            return HARDWARE_HINT
        summary = "  ".join(f"{p.device}({p.chip})" for p in ports)
        return f"检测到 {len(ports)} 个串口：{summary}"

    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)

    # -- teardown -----------------------------------------------------------

    def shutdown_panels(self) -> None:
        """Stop the panels' timers and streams, leaving the bench open.

        Separate from closing the window so a test can tear a window down
        without touching the saved settings.
        """
        for panel in (
            self.flow_panel,
            self.camera_panel,
            self.serial_panel,
            self.dut_panel,
            self.adb_panel,
            self.ssh_panel,
        ):
            shutdown = getattr(panel, "shutdown", None)
            if shutdown is not None:
                shutdown()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        try:
            self.shutdown_panels()
            # A device call started moments ago -- an "adb devices" refresh, a
            # camera probe -- may still be in flight. Let it finish before the
            # connections it is using are closed underneath it.
            drain(3000)
            self.bench.close_all()
            save_settings(self.settings)
        except Exception:
            # Never block the window from closing on a teardown error.
            pass
        event.accept()
