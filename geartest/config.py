"""Bench configuration: typed sections, persisted as YAML.

The bench is described around the **device under test**. Physical things -- relay
boards, cameras, consoles, the SSH host -- are declared once. Logical things are
layered on top of them: a named relay channel (a "power resource"), a named
monitored region (a screen), and the device that owns a set of them. A flow names
one device and then refers to its resources by bare name.

Which means a COM port number appears exactly once, in the physical section,
while a flow talks about ``KL30`` and ``屏幕2`` and never mentions a port.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
FLOW_DIR = ROOT / "flows"
EVIDENCE_DIR = ROOT / "evidence"


class ConfigError(RuntimeError):
    """A settings file that cannot be used as written."""


# -- physical devices -------------------------------------------------------


@dataclass
class RelayCfg:
    """A Modbus RTU relay board. Several may be open at once."""

    name: str = "relay1"
    port: str = ""
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    slave_id: int = 1
    coil_base: int = 0
    """Modbus coil address that channel 1 maps to; boards differ in 0/1-based."""
    timeout: float = 1.0
    inter_frame_delay: float = 0.03
    pulse_ms: int = 500


@dataclass
class ConsoleCfg:
    """A CH340 device console. Several may be open at once."""

    name: str = "SOC"
    port: str = ""
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    encoding: str = "utf-8"


@dataclass
class CameraCfg:
    """A capture device. It knows nothing about what is in front of it."""

    name: str = "cam4"
    source: str = "0"
    """A capture index ("0", "1") or a stream URL (rtsp://, http://)."""
    width: int = 1280
    height: int = 720


@dataclass
class SshCfg:
    """An SSH target, used to drive a build/test container remotely.

    Not part of a device's resource set: a flow reaches it by name from anywhere.
    """

    name: str = "container"
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: str = ""
    workdir: str = "/root/gear"
    docker_exec: str = ""
    """Optional command prefix, e.g. ``docker exec -i mycontainer sh -c``, to
    enter a container already running on the SSH host."""


# -- logical resources ------------------------------------------------------


@dataclass
class RelayChannelCfg:
    """One channel of one relay board, given a name.

    This is the unit a flow drives. Naming it here rather than in the flow keeps
    the wiring in one place: moving a rail to a different channel, or to a
    different board, is a one-line change that no test case has to know about.
    """

    name: str = "KL30"
    relay: str = ""
    channel: int = 1
    pulse_ms: int = 500
    description: str = ""


@dataclass
class RoiCfg:
    """Monitored region as fractions of the frame, so it survives a resolution
    change of the camera or of the preview."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


@dataclass
class ScreenCfg:
    """A monitored region: one camera, one rectangle of its view.

    A screen rather than a camera because that is what the detector actually
    watches, and because two panels can sit in one camera's field of view. The
    detection settings live here for the same reason -- they describe the region,
    not the sensor.
    """

    name: str = "屏幕1"
    camera: str = ""
    roi: RoiCfg = field(default_factory=RoiCfg)
    sensitivity: int = 50
    """0-100. Scales the detector's flatness/block/darkness constants together.
    It is deliberately the only knob that touches detection strength."""
    freeze_timeout_s: float = 10.0
    black_confirm_s: float = 2.0
    min_transient_ms: float = 80.0
    transient_max_s: float = 1.5


@dataclass
class DeviceCfg:
    """The device under test, and which resources belong to it.

    Membership is declared, not inferred. A resource that exists but is not
    listed here is an error when a flow of this device reaches for it, not a
    fallback -- driving a different board's coil because the names happened to
    be unique would be a safety bug, not a convenience.
    """

    name: str = "box1"
    adb_serial: str = ""
    adb_path: str = "adb"
    power: list[str] = field(default_factory=list)
    consoles: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Settings:
    relays: list[RelayCfg] = field(default_factory=list)
    relay_channels: list[RelayChannelCfg] = field(default_factory=list)
    consoles: list[ConsoleCfg] = field(default_factory=list)
    cameras: list[CameraCfg] = field(default_factory=list)
    screens: list[ScreenCfg] = field(default_factory=list)
    devices: list[DeviceCfg] = field(default_factory=list)
    ssh: list[SshCfg] = field(default_factory=list)


# -- serialisation ----------------------------------------------------------


def _coerce(tp: Any, value: Any, where: str) -> Any:
    origin = get_origin(tp)
    if origin is list:
        item_tp = get_args(tp)[0] if get_args(tp) else Any
        return [_coerce(item_tp, v, f"{where}[{i}]") for i, v in enumerate(value)]
    if dataclasses.is_dataclass(tp):
        return _from_dict(tp, value, where)
    return value


def _from_dict(cls: type, data: Any, where: str = "") -> Any:
    label = where or cls.__name__
    if not isinstance(data, dict):
        raise ConfigError(f"{label} 应该是一个映射，实际是 {type(data).__name__}")

    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        # Silently dropping these is how a renamed section turns into a bench
        # with no devices and no explanation.
        raise ConfigError(
            f"{label} 里有无法识别的键：{', '.join(unknown)}。"
            f"可用：{', '.join(sorted(known))}"
        )

    kwargs = {
        f.name: _coerce(hints[f.name], data[f.name], f"{label}.{f.name}")
        for f in dataclasses.fields(cls)
        if f.name in data and f.name in hints
    }
    return cls(**kwargs)


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    if not path.exists():
        return Settings()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _from_dict(Settings, raw, f"{path.name}")


#: Written at the top of every generated settings file. The file is rebuilt from
#: the dataclasses on save, so hand-written comments below this header do not
#: survive; anything that must persist has to live in the code or a tooltip.
SETTINGS_HEADER = """\
# GEAR 测试台配置 —— 本文件由程序生成，退出时会整体重写，手写的注释不会保留。
#
# 物理设备（板卡、摄像头、串口、SSH）与逻辑资源（具名继电资源、具名监控屏幕、
# 被测设备）分开声明。用例只引用逻辑名，不出现 COM 口。
#
# 字段说明在界面悬停提示里：
#   relays[].coil_base      第 1 路对应的 Modbus 线圈地址，板子 0 基/1 基不同
#   relay_channels[]        一条 = 某块板的某一路，作为具名电源资源给用例引用
#   consoles[].port         例如 COM35；留空时不会打开任何设备
#   screens[]               一条 = 某个摄像头的某块区域（ROI）+ 该区域的检测参数
#   devices[].adb_serial    物理序列号，在界面里绑定
#   devices[].power/consoles/screens   该设备拥有哪些资源（成员关系显式声明）
#   ssh[].docker_exec       进容器前缀，需自带 shell，如 docker exec -i ctr sh -c
"""


def save_settings(settings: Settings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(settings)
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    path.write_text(SETTINGS_HEADER + body, encoding="utf-8")
