"""One tab per device family."""

from .camera_panel import CameraPanel
from .dut_panel import DutPanel
from .flow_panel import FlowPanel
from .relay_panel import RelayPanel
from .serial_panel import SerialPanel
from .ssh_panel import SshPanel

__all__ = [
    "CameraPanel",
    "DutPanel",
    "FlowPanel",
    "RelayPanel",
    "SerialPanel",
    "SshPanel",
]
