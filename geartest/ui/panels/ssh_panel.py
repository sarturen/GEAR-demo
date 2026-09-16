"""SSH panel: drive a build/test container on a remote host."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...bench import Bench
from ...devices.ssh import SshHost
from ..common import LogView, Panel, make_form, mono_font
from ..worker import run_task

FLUSH_INTERVAL_MS = 120


class SshPanel(Panel):
    def __init__(self, bench: Bench, recorder=None, parent: QWidget | None = None) -> None:
        super().__init__("SSH 容器", parent)
        self.bench = bench
        self.recorder = recorder
        self.host: SshHost | None = None
        self._pending: deque[tuple[str, str]] = deque(maxlen=20000)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._streaming = False
        self._build()
        self.refresh_hosts()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        connection = QGroupBox("连接")
        form = make_form()

        self.host_combo = QComboBox()
        self.host_combo.currentIndexChanged.connect(self._load_host)
        form.addRow("配置", self.host_combo)

        address = QHBoxLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("主机名或 IP")
        self.host_edit.setMinimumWidth(220)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        address.addWidget(self.host_edit, 1)
        address.addWidget(QLabel("端口"))
        address.addWidget(self.port_spin)
        form.addRow("主机", address)

        credentials = QHBoxLayout()
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("用户名")
        self.user_edit.setMinimumWidth(120)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("密码（留空则用密钥）")
        self.password_edit.setMinimumWidth(150)
        credentials.addWidget(self.user_edit, 1)
        credentials.addWidget(self.password_edit, 1)
        form.addRow("认证", credentials)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("私钥文件（可选）")
        self.key_edit.setMinimumWidth(240)
        browse_key = QPushButton("选择…")
        browse_key.clicked.connect(self._pick_key)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(browse_key)
        form.addRow("私钥", key_row)

        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText("代码工作路径，例如 /root/gear")
        self.workdir_edit.setMinimumWidth(260)
        form.addRow("工作路径", self.workdir_edit)

        self.docker_edit = QLineEdit()
        self.docker_edit.setPlaceholderText(
            "可选。进容器前缀，例如 docker exec -i mycontainer sh -c"
        )
        self.docker_edit.setMinimumWidth(360)
        self.docker_edit.setToolTip(
            "命令会拼成：cd <工作路径> && <此前缀> '<你的命令>'。\n"
            "要在容器里执行，前缀必须带上 shell，例如 docker exec -i ctr sh -c。"
        )
        form.addRow("容器", self.docker_edit)

        connection.setLayout(form)
        config_row = QHBoxLayout()
        config_row.addWidget(connection)
        config_row.addStretch(1)
        self.body().addLayout(config_row)

        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._toggle_connection)

        files_row = QHBoxLayout()
        upload = QPushButton("上传文件…")
        upload.clicked.connect(self._upload)
        download = QPushButton("下载文件…")
        download.clicked.connect(self._download)
        ls = QPushButton("列目录")
        ls.clicked.connect(lambda: self._run_quick("ls -la"))

        self.body().addLayout(
            _row(self.connect_button, upload, download, ls)
        )

        command_box = QGroupBox("命令")
        command_layout = QVBoxLayout(command_box)
        self.command_input = QPlainTextEdit()
        self.command_input.setFont(mono_font())
        self.command_input.setPlaceholderText("例如 make -j8  ·  Ctrl+Enter 执行")
        self.command_input.setMaximumHeight(70)
        command_layout.addWidget(self.command_input)

        run_row = QHBoxLayout()
        run = QPushButton("执行（等待结束）")
        run.clicked.connect(lambda: self._run(stream=False))
        stream = QPushButton("流式运行")
        stream.clicked.connect(lambda: self._run(stream=True))
        self.stop_button = QPushButton("停止流式")
        self.stop_button.clicked.connect(self._stop_stream)
        self.stop_button.setEnabled(False)
        run_row.addWidget(run)
        run_row.addWidget(stream)
        run_row.addWidget(self.stop_button)
        run_row.addStretch(1)
        command_layout.addLayout(run_row)
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

    # -- config -------------------------------------------------------------

    def refresh_hosts(self) -> None:
        current = self.host_combo.currentText()
        self.host_combo.blockSignals(True)
        self.host_combo.clear()
        for ssh_cfg in self.bench.settings.ssh:
            self.host_combo.addItem(ssh_cfg.name)
        self.host_combo.blockSignals(False)
        if current:
            index = self.host_combo.findText(current)
            if index >= 0:
                self.host_combo.setCurrentIndex(index)
        self._load_host()

    def _cfg(self):
        name = self.host_combo.currentText()
        if not name:
            return None
        try:
            return self.bench.ssh_cfg(name)
        except Exception:
            return None

    def _load_host(self) -> None:
        cfg = self._cfg()
        if cfg is None:
            return
        self.host_edit.setText(cfg.host)
        self.port_spin.setValue(cfg.port)
        self.user_edit.setText(cfg.username)
        self.password_edit.setText(cfg.password)
        self.key_edit.setText(cfg.key_file)
        self.workdir_edit.setText(cfg.workdir)
        self.docker_edit.setText(cfg.docker_exec)

    def _store_host(self) -> None:
        cfg = self._cfg()
        if cfg is None:
            return
        cfg.host = self.host_edit.text().strip()
        cfg.port = self.port_spin.value()
        cfg.username = self.user_edit.text().strip()
        cfg.password = self.password_edit.text()
        cfg.key_file = self.key_edit.text().strip()
        cfg.workdir = self.workdir_edit.text().strip()
        cfg.docker_exec = self.docker_edit.text().strip()

    def _pick_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥")
        if path:
            self.key_edit.setText(path)

    # -- connection ---------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.host is not None:
            self.bench.close_ssh(self.host.cfg.name)
            self.host = None
            self.connect_button.setText("连接")
            self.set_status("已断开")
            return

        self._store_host()
        cfg = self._cfg()
        if cfg is None or not cfg.host:
            self.set_status("请先填写主机地址", ok=False)
            return

        def done(host: SshHost):
            self.host = host
            self.connect_button.setText("断开")
            target = cfg.workdir or "~"
            self.set_status(f"已连接 {cfg.username}@{cfg.host}，工作路径 {target}")

        run_task(lambda: self.bench.ssh(cfg.name), done, self._fail)

    # -- running ------------------------------------------------------------

    def _run_quick(self, command: str) -> None:
        self.command_input.setPlainText(command)
        self._run(stream=False)

    def _run(self, stream: bool) -> None:
        command = self.command_input.toPlainText().strip()
        if not command:
            self.set_status("请输入命令", ok=False)
            return
        if self.host is None:
            self.set_status("请先连接", ok=False)
            return

        self._store_host()
        host = self.host
        cfg = host.cfg
        prefix = f"{cfg.workdir} $ " if cfg.workdir else "$ "
        self.output.append(f"$ {prefix}{command}", "info")

        if stream:
            self._start_stream(host, command)
        else:
            host_name = self.host_combo.currentText()

            def done(text: str):
                self._show_output(text)
                if self.recorder is not None:
                    self.recorder.record("ssh", {"host": host_name, "command": command})

            run_task(lambda: host.run(command, timeout=600.0), done, self._fail)

    def _start_stream(self, host: SshHost, command: str) -> None:
        def work():
            return host.stream(
                command,
                on_line=lambda line: self._pending.append((line, "rx")),
                on_exit=self._stream_exited,
                on_error=lambda exc: self._pending.append((f"流中断：{exc}", "error")),
            )

        def done(_):
            self._streaming = True
            self.stop_button.setEnabled(True)
            self._flush_timer.start()
            self.set_status("流式运行中")

        run_task(work, done, self._fail)

    def _stream_exited(self, code: int) -> None:
        self._pending.append((f"— 命令结束，退出码 {code} —", "muted"))
        self._streaming = False
        self.stop_button.setEnabled(False)
        self._flush_timer.stop()

    def _stop_stream(self) -> None:
        host = self.host
        if host is not None:
            for pump in host._pumps:  # noqa: SLF001 - the host owns its pumps
                pump.stop()
        self._streaming = False
        self.stop_button.setEnabled(False)
        self._flush_timer.stop()
        self.set_status("已请求停止")

    def _show_output(self, text: str) -> None:
        if text.strip():
            for line in text.rstrip().splitlines():
                self.output.append(line, "rx")
        else:
            self.output.append("（无输出）", "muted")
        self._scroll()
        self.set_status("执行完成")

    # -- files --------------------------------------------------------------

    def _upload(self) -> None:
        if self.host is None:
            self.set_status("请先连接", ok=False)
            return
        local, _ = QFileDialog.getOpenFileName(self, "选择要上传的文件")
        if not local:
            return
        cfg = self.host.cfg
        default = host_join(cfg.workdir, local.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        remote, ok = _ask_text(self, "上传到远端路径", default)
        if not ok or not remote:
            return
        run_task(
            lambda: self.host.upload(local, remote),
            lambda _: self.set_status(f"已上传到 {remote}"),
            self._fail,
        )

    def _download(self) -> None:
        if self.host is None:
            self.set_status("请先连接", ok=False)
            return
        remote, ok = _ask_text(self, "要下载的远端路径", host_join(self.host.cfg.workdir, ""))
        if not ok or not remote:
            return
        local, _ = QFileDialog.getSaveFileName(self, "保存到")
        if not local:
            return
        run_task(
            lambda: self.host.download(remote, local),
            lambda _: self.set_status(f"已下载到 {local}"),
            self._fail,
        )

    # -- plumbing -----------------------------------------------------------

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
        if self._flush_timer.isActive():
            self._flush_timer.stop()


def host_join(*parts: str) -> str:
    """Join remote paths with forward slashes."""
    return "/".join(p.strip("/") for p in parts if p)


def _ask_text(parent: QWidget, title: str, default: str) -> tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog

    text, ok = QInputDialog.getText(parent, title, "路径：", text=default)
    return text, ok


def _row(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch(1)
    return row
