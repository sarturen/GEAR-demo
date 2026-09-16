"""Device layers: everything the bench talks to over a wire or a network."""

from .adb import Adb, AdbDevice, AdbError, parse_devices, resolve_adb
from .camera import CameraError, CameraStream, is_stream_url, probe_sources
from .relay_modbus import RelayBoard, RelayError, crc16, with_crc
from .serial_link import LineCollector, SerialLink
from .serial_ports import PortInfo, find_ports, list_com_ports
from .ssh import SshError, SshHost
from .stream import LinePump

__all__ = [
    "Adb",
    "AdbDevice",
    "AdbError",
    "parse_devices",
    "resolve_adb",
    "CameraError",
    "CameraStream",
    "is_stream_url",
    "probe_sources",
    "RelayBoard",
    "RelayError",
    "crc16",
    "with_crc",
    "SerialLink",
    "LineCollector",
    "PortInfo",
    "find_ports",
    "list_com_ports",
    "SshError",
    "SshHost",
    "LinePump",
]
