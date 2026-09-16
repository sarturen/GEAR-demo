"""Screen panel: every monitored region visible at once.

A grid rather than one preview at a time, because the failures this exists to
catch are comparisons -- 屏幕2 stayed dark while 屏幕1 and 屏幕3 came up -- and
seeing all three at the same moment is the whole point. One shared preview timer
drives every tile: detection already runs at full rate on the capture threads,
so the previews only have to be fast enough to aim a camera and drag a region.
"""

from __future__ import annotations

import time
from functools import partial

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench
from ...config import RoiCfg
from ...devices.camera import probe_sources
from ...vision.detector import Event, EventType
from ..common import Panel, make_form
from ..worker import run_task
from ..video_view import VideoView

#: One timer for every tile. Per-tile timers would mean N BGR->QImage
#: conversions and N repaints per tick on the GUI thread.
PREVIEW_MS = 120
TILE_COLUMNS = 2

EVENT_COLORS = {
    EventType.BLACK_SCREEN: "#f48771",
    EventType.TRANSIENT_BLACKOUT: "#dcdcaa",
    EventType.FREEZE: "#f48771",
    EventType.CAPTURE_STALL: "#c586c0",
    EventType.RECOVERED: "#4ec9b0",
}

EVENT_COLUMNS = ("时间", "屏幕", "类型", "说明", "时长")


class ScreenTile(QGroupBox):
    """One screen: its preview, its status, and its draggable region."""

    roi_changed = Signal(str, object)
    clicked = Signal(str)

    def __init__(self, monitor, parent: QWidget | None = None) -> None:
        super().__init__(monitor.cfg.name, parent)
        self.monitor = monitor
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.badge = QLabel("未监控")
        self.badge.setStyleSheet("font-weight:bold;color:#808080")
        header.addWidget(self.badge)
        header.addStretch(1)
        self.detail = QLabel("")
        self.detail.setStyleSheet("color:#808080;font-family:Consolas")
        header.addWidget(self.detail)
        layout.addLayout(header)

        self.view = VideoView()
        self.view.setMinimumSize(300, 190)
        self.view.placeholder = f"{monitor.cfg.name}：未开始监控"
        self.view.roi_changed.connect(self._on_roi)
        layout.addWidget(self.view)

    def _on_roi(self, roi: RoiCfg) -> None:
        self.roi_changed.emit(self.monitor.cfg.name, roi)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.clicked.emit(self.monitor.cfg.name)
        super().mousePressEvent(event)

    def update_state(self, frame, state: dict) -> None:
        if frame is not None:
            self.view.set_frame(frame)
        if state.get("stall"):
            text, color = "采集停滞", "#c586c0"
        elif state.get("black"):
            text, color = "黑屏", "#f48771"
        elif state.get("freeze"):
            text, color = "冻屏", "#f48771"
        elif not state.get("running"):
            text, color = "未监控", "#808080"
        elif not state.get("calibrated"):
            text, color = "校准中", "#dcdcaa"
        else:
            text, color = "正常", "#4ec9b0"
        self.badge.setText(text)
        self.badge.setStyleSheet(f"font-weight:bold;color:{color}")
        flat = state.get("flatness")
        bright = state.get("brightness")
        self.detail.setText(
            f"{state.get('fps', 0):.1f}fps  平坦 {flat if flat is not None else '—'}"
            f"  亮度 {bright if bright is not None else '—'}"
        )
        error = state.get("error")
        if error:
            self.detail.setText(error[:60])


class CameraPanel(Panel):
    screen_event = Signal(str, object)

    def __init__(self, bench: Bench, parent: QWidget | None = None) -> None:
        super().__init__("监控画面（屏幕异常）", parent)
        self.bench = bench
        self.tiles: dict[str, ScreenTile] = {}
        self.selected: str = ""
        self.screen_event.connect(self._on_event)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(PREVIEW_MS)
        self.preview_timer.timeout.connect(self._tick)
        self._build()
        self.rebuild()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        toolbar = QHBoxLayout()
        start_all = QPushButton("全部开始监控")
        start_all.clicked.connect(lambda: self._all(True))
        stop_all = QPushButton("全部停止")
        stop_all.clicked.connect(lambda: self._all(False))
        probe = QPushButton("探测摄像头")
        probe.setToolTip("逐个尝试打开 0-5 号采集设备，较慢")
        probe.clicked.connect(self._probe)
        toolbar.addWidget(start_all)
        toolbar.addWidget(stop_all)
        toolbar.addWidget(probe)
        toolbar.addStretch(1)
        self.body().addLayout(toolbar)

        columns = QHBoxLayout()

        left = QVBoxLayout()
        left.addWidget(self._build_bindings())
        left.addWidget(self._build_params())
        left.addStretch(1)
        left_host = QWidget()
        left_host.setLayout(left)
        left_host.setMaximumWidth(400)
        columns.addWidget(left_host)

        self.tile_area = QWidget()
        self.tile_grid = QGridLayout(self.tile_area)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tile_area)
        columns.addWidget(scroll, 1)
        self.body().addLayout(columns, 1)

        self.body().addWidget(self._build_events())

    def _build_bindings(self) -> QGroupBox:
        box = QGroupBox("摄像头绑定")
        form = make_form()
        self.camera_rows: dict[str, tuple] = {}
        for cfg in self.bench.settings.cameras:
            row = QHBoxLayout()
            source = QLineEdit(cfg.source)
            source.setPlaceholderText("索引 0/1/… 或 rtsp://…")
            source.setMinimumWidth(120)
            width = QSpinBox()
            width.setRange(160, 7680)
            width.setSingleStep(160)
            width.setValue(cfg.width)
            height = QSpinBox()
            height.setRange(120, 4320)
            height.setSingleStep(120)
            height.setValue(cfg.height)
            row.addWidget(source, 1)
            row.addWidget(width)
            row.addWidget(QLabel("×"))
            row.addWidget(height)
            form.addRow(cfg.name, row)
            self.camera_rows[cfg.name] = (source, width, height)
        box.setLayout(form)
        return box

    def _build_params(self) -> QGroupBox:
        box = QGroupBox("监控屏幕参数")
        form = make_form()

        self.screen_combo = QComboBox()
        self.screen_combo.currentIndexChanged.connect(self._load_screen)
        form.addRow("屏幕", self.screen_combo)

        binding = QLabel("—")
        binding.setStyleSheet("color:#808080")
        form.addRow("绑定", binding)
        self.binding_label = binding

        self.sensitivity_spin = QSpinBox()
        self.sensitivity_spin.setRange(0, 100)
        self.sensitivity_spin.setToolTip(
            "唯一的强度旋钮，同时影响平坦度、暗度与画面变化的判定门槛。\n"
            "所有实际阈值都由画面自身推导，与绝对亮度无关。"
        )
        self.sensitivity_spin.valueChanged.connect(self._apply_params)
        form.addRow("灵敏度", self.sensitivity_spin)

        self.freeze_spin = QDoubleSpinBox()
        self.freeze_spin.setRange(1.0, 600.0)
        self.freeze_spin.setSuffix(" s")
        self.freeze_spin.valueChanged.connect(self._apply_params)
        form.addRow("冻屏超时", self.freeze_spin)

        self.black_spin = QDoubleSpinBox()
        self.black_spin.setRange(0.2, 60.0)
        self.black_spin.setSuffix(" s")
        self.black_spin.valueChanged.connect(self._apply_params)
        form.addRow("黑屏确认", self.black_spin)

        self.transient_spin = QDoubleSpinBox()
        self.transient_spin.setRange(0.0, 1000.0)
        self.transient_spin.setSuffix(" ms")
        self.transient_spin.setToolTip("短于此长度的黑屏视为采集毛刺，不报事件")
        self.transient_spin.valueChanged.connect(self._apply_params)
        form.addRow("最小闪黑", self.transient_spin)

        reset = QPushButton("监控范围=整幅")
        reset.clicked.connect(self._reset_roi)
        form.addRow("", reset)

        box.setLayout(form)

        self.screen_combo.blockSignals(True)
        for cfg in self.bench.settings.screens:
            self.screen_combo.addItem(cfg.name)
        self.screen_combo.blockSignals(False)
        if self.bench.settings.screens:
            self.selected = self.bench.settings.screens[0].name
        return box

    def _build_events(self) -> QGroupBox:
        box = QGroupBox("异常事件")
        layout = QVBoxLayout(box)
        header = QHBoxLayout()
        header.addWidget(QLabel("黑屏 / 短暂闪黑 / 冻屏 / 采集停滞"))
        header.addStretch(1)
        clear = QPushButton("清空事件")
        clear.clicked.connect(lambda: self.events_table.setRowCount(0))
        header.addWidget(clear)
        layout.addLayout(header)

        self.events_table = QTableWidget(0, len(EVENT_COLUMNS))
        self.events_table.setHorizontalHeaderLabels(EVENT_COLUMNS)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.events_table.setWordWrap(False)
        head = self.events_table.horizontalHeader()
        for column in (0, 1, 2, 4):
            head.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.Stretch)
        self.events_table.setMaximumHeight(180)
        layout.addWidget(self.events_table)
        return box

    # -- structure ----------------------------------------------------------

    def rebuild(self) -> None:
        while self.tile_grid.count():
            item = self.tile_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.tiles.clear()

        for index, cfg in enumerate(self.bench.settings.screens):
            monitor = self.bench.screen(
                cfg.name, on_event=partial(self.screen_event.emit, cfg.name)
            )
            tile = ScreenTile(monitor)
            tile.view.set_roi(cfg.roi)
            tile.roi_changed.connect(self._on_roi_changed)
            tile.clicked.connect(self._select)
            self.tile_grid.addWidget(
                tile, index // TILE_COLUMNS, index % TILE_COLUMNS
            )
            self.tiles[cfg.name] = tile

        if not self.bench.settings.screens:
            self.tile_grid.addWidget(
                QLabel("尚未配置监控屏幕（settings.yaml 的 screens）"), 0, 0
            )
        self._load_screen()

    def refresh(self) -> None:
        """Re-read the camera bindings from settings (e.g. after an edit)."""
        for name, (source, width, height) in self.camera_rows.items():
            for cfg in self.bench.settings.cameras:
                if cfg.name == name:
                    cfg.source = source.text().strip()
                    cfg.width = width.value()
                    cfg.height = height.value()

    # -- screen parameters --------------------------------------------------

    def _screen_cfg(self, name: str):
        for cfg in self.bench.settings.screens:
            if cfg.name == name:
                return cfg
        return None

    def _load_screen(self) -> None:
        name = self.screen_combo.currentText()
        if not name:
            return
        self.selected = name
        cfg = self._screen_cfg(name)
        if cfg is None:
            return
        camera = self._camera_cfg(cfg.camera)
        self.binding_label.setText(
            f"摄像头 {cfg.camera}（源 {camera.source if camera else '?'}）"
        )
        for spin, value in (
            (self.sensitivity_spin, cfg.sensitivity),
            (self.freeze_spin, cfg.freeze_timeout_s),
            (self.black_spin, cfg.black_confirm_s),
            (self.transient_spin, cfg.min_transient_ms),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _camera_cfg(self, name: str):
        for cfg in self.bench.settings.cameras:
            if cfg.name == name:
                return cfg
        return None

    def _apply_params(self) -> None:
        cfg = self._screen_cfg(self.selected)
        if cfg is None:
            return
        cfg.sensitivity = self.sensitivity_spin.value()
        cfg.freeze_timeout_s = self.freeze_spin.value()
        cfg.black_confirm_s = self.black_spin.value()
        cfg.min_transient_ms = self.transient_spin.value()
        monitor = self.bench.screens.get(self.selected)
        if monitor is not None:
            monitor.reset_detector()

    def _on_roi_changed(self, name: str, roi: RoiCfg) -> None:
        cfg = self._screen_cfg(name)
        if cfg is None:
            return
        cfg.roi = roi
        monitor = self.bench.screens.get(name)
        if monitor is not None:
            monitor.set_roi(roi)
        self.set_status(
            f"{name} 监控范围已更新：{roi.w * 100:.0f}% × {roi.h * 100:.0f}%"
        )

    def _reset_roi(self) -> None:
        tile = self.tiles.get(self.selected)
        if tile is not None:
            tile.view.reset_roi()

    def _select(self, name: str) -> None:
        index = self.screen_combo.findText(name)
        if index >= 0:
            self.screen_combo.setCurrentIndex(index)

    # -- lifecycle ----------------------------------------------------------

    def _probe(self) -> None:
        self.set_status("正在探测采集设备…")

        def done(sources: list[str]):
            if not sources:
                self.set_status("没有探测到可用的采集设备", ok=False)
                return
            self.set_status(f"可用视频源：{', '.join(sources)}")

        run_task(probe_sources, done, self._fail)

    def _all(self, start: bool) -> None:
        if not start:
            for name in list(self.bench.screens):
                self.bench.stop_screen(name)
            self.preview_timer.stop()
            for tile in self.tiles.values():
                tile.update_state(None, {"running": False})
            self.set_status("已停止全部监控")
            return

        self.refresh()
        for cfg in self.bench.settings.screens:
            monitor = self.bench.screens.get(cfg.name)
            if monitor is not None:
                monitor.reset_detector()

        def work():
            problems = []
            for cfg in self.bench.settings.screens:
                try:
                    self.bench.start_screen(cfg.name, on_event=self.screen_event.emit)
                except Exception as exc:  # noqa: BLE001 - collected
                    problems.append(f"{cfg.name}：{exc}")
            return problems

        def done(problems: list[str]):
            self.preview_timer.start()
            if problems:
                self.set_status("；".join(problems), ok=False)
            else:
                self.set_status(f"正在监控 {len(self.bench.settings.screens)} 个屏幕")

        run_task(work, done, self._fail)

    # -- live updates -------------------------------------------------------

    def _tick(self) -> None:
        for name, tile in self.tiles.items():
            monitor = self.bench.screens.get(name)
            if monitor is None:
                continue
            tile.update_state(monitor.latest_frame(), monitor.snapshot())

    # -- events -------------------------------------------------------------

    def _on_event(self, screen: str, event: Event) -> None:
        """Runs on the GUI thread: the signal crosses from a capture thread."""
        row = self.events_table.rowCount()
        self.events_table.insertRow(row)
        duration = event.duration
        cells = (
            time.strftime("%H:%M:%S"),
            screen,
            event.label,
            event.detail,
            "—" if duration is None else f"{duration * 1000:.0f} ms",
        )
        color = QColor(EVENT_COLORS.get(event.type, "#d4d4d4"))
        for column, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            item.setForeground(color)
            self.events_table.setItem(row, column, item)
        self.events_table.scrollToBottom()

        if event.type is EventType.RECOVERED:
            self.set_status(f"{screen}：{event.detail}")
        else:
            self.set_status(f"{screen}：{event.detail}", ok=False)

    def _fail(self, message: str) -> None:
        self.set_status(message, ok=False)

    def shutdown(self) -> None:
        if self.preview_timer.isActive():
            self.preview_timer.stop()
        for name in list(self.bench.screens):
            self.bench.stop_screen(name)
