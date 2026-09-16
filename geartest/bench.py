"""Live device registry and resource resolution.

Panels and the flow engine both go through this, so a rail the operator flipped
by hand is the same rail a test step drives, and a port is opened once rather
than twice.

Names are resolved in two steps, and the two failures are kept distinct because
they need different fixes: a resource may not be defined at all, or it may exist
but not belong to the device being driven.
"""

from __future__ import annotations

from typing import Callable

from .config import (
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    RelayCfg,
    RelayChannelCfg,
    ScreenCfg,
    Settings,
    SshCfg,
)
from .devices.adb import Adb
from .devices.relay_modbus import RelayBoard
from .devices.serial_link import SerialLink
from .devices.ssh import SshHost
from .vision.monitor import CameraHub, ScreenMonitor

#: Settings section, resource pool, and the Chinese noun for each kind.
RESOURCE_KINDS = {
    "power": ("relay_channels", "继电器资源"),
    "consoles": ("consoles", "串口资源"),
    "screens": ("screens", "画面资源"),
}


class BenchError(RuntimeError):
    pass


class Bench:
    """Lazily opens devices by name and keeps them alive until closed."""

    def __init__(
        self,
        settings: Settings,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.settings = settings
        self._on_error = on_error
        self.relays: dict[str, RelayBoard] = {}
        self.consoles: dict[str, SerialLink] = {}
        self.cameras: dict[str, CameraHub] = {}
        self.screens: dict[str, ScreenMonitor] = {}
        self.ssh_hosts: dict[str, SshHost] = {}
        self.adb_clients: dict[str, Adb] = {}

    # -- config lookup ------------------------------------------------------

    @staticmethod
    def _find(configs, name: str, kind: str):
        for cfg in configs:
            if cfg.name == name:
                return cfg
        known = ", ".join(c.name for c in configs) or "（未配置）"
        raise BenchError(f"未找到{kind}「{name}」。已配置：{known}")

    def relay_cfg(self, name: str) -> RelayCfg:
        return self._find(self.settings.relays, name, "继电器板")

    def console_cfg(self, name: str) -> ConsoleCfg:
        return self._find(self.settings.consoles, name, "串口")

    def camera_cfg(self, name: str) -> CameraCfg:
        return self._find(self.settings.cameras, name, "摄像头")

    def screen_cfg(self, name: str) -> ScreenCfg:
        return self._find(self.settings.screens, name, "监控屏幕")

    def ssh_cfg(self, name: str) -> SshCfg:
        return self._find(self.settings.ssh, name, "SSH 主机")

    def device(self, key: str) -> DeviceCfg:
        """Look a device up by its name or by its adb serial."""
        for cfg in self.settings.devices:
            if cfg.name == key or (cfg.adb_serial and cfg.adb_serial == key):
                return cfg
        known = ", ".join(d.name for d in self.settings.devices) or "（未配置）"
        raise BenchError(f"未找到被测设备「{key}」。已配置：{known}")

    # -- physical devices ---------------------------------------------------

    def relay(
        self,
        name: str,
        on_traffic: Callable[[str, bytes], None] | None = None,
    ) -> RelayBoard:
        board = self.relays.get(name)
        if board is None:
            board = RelayBoard(
                self.relay_cfg(name), on_traffic=on_traffic, on_error=self._on_error
            )
            self.relays[name] = board
        board.open()
        return board

    def close_relay(self, name: str) -> None:
        board = self.relays.pop(name, None)
        if board is not None:
            board.close()

    def console(self, name: str, on_line=None, on_raw=None) -> SerialLink:
        link = self.consoles.get(name)
        if link is None:
            link = SerialLink(
                self.console_cfg(name),
                on_line=on_line,
                on_raw=on_raw,
                on_error=self._on_error,
            )
            self.consoles[name] = link
        elif on_line is not None or on_raw is not None:
            link.add_listener(on_line, on_raw)
        link.open()
        return link

    def close_console(self, name: str) -> None:
        link = self.consoles.pop(name, None)
        if link is not None:
            link.close()

    def camera(self, name: str) -> CameraHub:
        hub = self.cameras.get(name)
        if hub is None:
            hub = CameraHub(self.camera_cfg(name), on_error=self._on_error)
            self.cameras[name] = hub
        return hub

    def close_camera(self, name: str) -> None:
        hub = self.cameras.pop(name, None)
        if hub is not None:
            hub.stop()

    def ssh(self, name: str) -> SshHost:
        host = self.ssh_hosts.get(name)
        if host is None:
            host = SshHost(self.ssh_cfg(name), on_error=self._on_error)
            self.ssh_hosts[name] = host
        host.open()
        return host

    def close_ssh(self, name: str) -> None:
        host = self.ssh_hosts.pop(name, None)
        if host is not None:
            host.close()

    def adb(self, device_name: str) -> Adb:
        client = self.adb_clients.get(device_name)
        if client is None:
            cfg = self.device(device_name)
            client = Adb(cfg.adb_path, cfg.adb_serial)
            self.adb_clients[device_name] = client
        return client

    def close_adb(self, device_name: str) -> None:
        client = self.adb_clients.pop(device_name, None)
        if client is not None:
            client.stop_all()

    # -- screens ------------------------------------------------------------

    def screen(
        self, name: str, on_event: Callable | None = None
    ) -> ScreenMonitor:
        """The monitor for one screen, creating it and its camera hub if needed.

        Not started: several screens can share one camera, and starting is a
        separate decision so a caller can attach everything first.
        """
        monitor = self.screens.get(name)
        if monitor is None:
            cfg = self.screen_cfg(name)
            monitor = ScreenMonitor(cfg, on_event=on_event)
            self.camera(cfg.camera).attach(monitor)
            self.screens[name] = monitor
        elif on_event is not None:
            monitor.add_listener(on_event)
        return monitor

    def start_screen(self, name: str, on_event: Callable | None = None) -> ScreenMonitor:
        monitor = self.screen(name, on_event)
        if monitor.hub is None:
            raise BenchError(f"屏幕「{name}」没有关联摄像头")
        monitor.hub.start()
        return monitor

    def stop_screen(self, name: str) -> None:
        """Stop watching one screen, leaving a shared camera running."""
        monitor = self.screens.pop(name, None)
        if monitor is None:
            return
        hub = monitor.hub
        if hub is not None:
            hub.detach(name)
            if not hub.screens():
                hub.stop()

    # -- resources within a device ------------------------------------------

    @staticmethod
    def _membership_error(name: str, cfg: DeviceCfg, kind: str) -> BenchError:
        section, label = RESOURCE_KINDS[kind]
        members = getattr(cfg, kind)
        # "defined but not yours" needs a different fix from "not defined at
        # all", and conflating them sends people looking in the wrong file.
        return BenchError(
            f"{label}「{name}」已定义，但没有绑定到设备「{cfg.name}」。"
            f"该设备声明的{label}：{', '.join(members) or '（无）'}"
        )

    def resolve(self, device: str, kind: str, name: str):
        """Find a resource belonging to a device.

        Pure: opens nothing. Kept separate from the accessors below because the
        DUT panel has to show the whole topology, and pre-flight has to report
        every missing resource, before any hardware is touched.
        """
        cfg = self.device(device)
        if name not in getattr(cfg, kind):
            section, label = RESOURCE_KINDS[kind]
            pool = getattr(self.settings, section)
            if any(c.name == name for c in pool):
                raise self._membership_error(name, cfg, kind)
            known = ", ".join(c.name for c in pool) or "（无）"
            raise BenchError(f"{label}「{name}」未定义。已定义：{known}")
        section, label = RESOURCE_KINDS[kind]
        return self._find(getattr(self.settings, section), name, label)

    def resolve_all(self, device: str) -> list[str]:
        """Every problem with a device's resource set, without opening anything.

        Used to refuse a run up front rather than failing at the step that
        happens to touch the broken resource.
        """
        cfg = self.device(device)
        problems: list[str] = []
        for kind in ("power", "consoles", "screens"):
            for name in getattr(cfg, kind):
                try:
                    self.resolve(device, kind, name)
                except BenchError as exc:
                    problems.append(str(exc))
        return problems

    def power(self, device: str, name: str) -> tuple[RelayBoard, RelayChannelCfg]:
        """Resolve a named relay channel and make sure its board is open."""
        channel = self.resolve(device, "power", name)
        return self.relay(channel.relay), channel

    def device_console(self, device: str, name: str, **listeners) -> SerialLink:
        console = self.resolve(device, "consoles", name)
        return self.console(console.name, **listeners)

    def device_screen(
        self, device: str, name: str, on_event: Callable | None = None
    ) -> ScreenMonitor:
        screen = self.resolve(device, "screens", name)
        return self.screen(screen.name, on_event)

    # -- whole-device bring-up ----------------------------------------------

    def open_device(self, key: str) -> list[str]:
        """Bring up everything a device needs, collecting every problem.

        Reporting all of them at once matters because the operator is standing
        at a rack: finding out about the second bad port only after fixing the
        first means another round trip.

        Relays open last. Opening one only claims its serial port -- nothing is
        energised until a step says so -- but doing it last keeps the report
        ordered from harmless to consequential.
        """
        cfg = self.device(key)

        # Pre-flight the declarations before touching any hardware: a typo in a
        # resource list should be reported as such, not as a port that failed.
        declared = self.resolve_all(key)
        if declared:
            raise BenchError(
                f"设备「{cfg.name}」的资源声明有问题，未打开任何设备：\n"
                + "\n".join(f"  · {p}" for p in declared)
            )

        problems: list[str] = []
        opened: list[str] = []

        for name in cfg.consoles:
            try:
                self.console(name)
                opened.append(f"串口 {name}")
            except Exception as exc:  # noqa: BLE001 - collected for the report
                problems.append(f"串口 {name}：{exc}")

        for name in cfg.screens:
            try:
                self.screen_cfg(name)  # membership already checked above
                self.screen(name)
                self.start_screen(name)
                opened.append(f"屏幕 {name}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"屏幕 {name}：{exc}")

        try:
            if cfg.adb_serial:
                self.adb(cfg.name).list_devices()
            opened.append(f"ADB {cfg.adb_serial or '（未绑定序列号）'}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"ADB：{exc}")

        for name in cfg.power:
            try:
                self.power(cfg.name, name)
                opened.append(f"电源 {name}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"电源 {name}：{exc}")

        if problems:
            raise BenchError(
                f"设备「{cfg.name}」未能完全拉起，共 {len(problems)} 个问题：\n"
                + "\n".join(f"  · {p}" for p in problems)
            )
        return opened

    def relay_channel_board(self, name: str) -> str:
        """Which board a named channel lives on, or "" if it is not defined."""
        for channel in self.settings.relay_channels:
            if channel.name == name:
                return channel.relay
        return ""

    def close_device(self, key: str) -> None:
        cfg = self.device(key)
        for name in cfg.consoles:
            self.close_console(name)
        for name in cfg.screens:
            self.stop_screen(name)
        self.close_adb(cfg.name)

        # A board may serve several devices, so it is only dropped once no
        # other device still lists one of its channels.
        boards = {self.relay_channel_board(n) for n in cfg.power} - {""}
        elsewhere = {
            self.relay_channel_board(channel)
            for other in self.settings.devices
            if other.name != cfg.name
            for channel in other.power
        }
        for board in boards - elsewhere:
            self.close_relay(board)

    # -- teardown -----------------------------------------------------------

    def close_all(self) -> None:
        for name in list(self.screens):
            self.stop_screen(name)
        for name in list(self.cameras):
            self.close_camera(name)
        for name in list(self.relays):
            self.close_relay(name)
        for name in list(self.consoles):
            self.close_console(name)
        for name in list(self.ssh_hosts):
            self.close_ssh(name)
        for name in list(self.adb_clients):
            self.close_adb(name)
