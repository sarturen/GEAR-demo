"""Enumerate COM ports and identify the USB-serial chip behind each one.

The bench has two roles to fill -- a PL2303GT for the relay board and a CH340
for the device console -- so telling the two apart matters. Vendor IDs do that
reliably; the port description string does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from serial.tools import list_ports

PROLIFIC_VID = 0x067B
WCH_VID = 0x1A86

# PIDs seen on CH340-family parts. 0x7523 is the common CH340G.
CH34X_PIDS = {0x7523, 0x5523, 0x7522, 0x7524}

# Prolific PIDs: 0x2303 is PL2303(HXA), 0x23A3 is PL2303GC/GT/GS.
_PL2303_PIDS = {0x2303, 0x23A3, 0x23A4, 0x23A5}


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    chip: str

    @property
    def label(self) -> str:
        return f"{self.device} — {self.chip} ({self.description})"

    @property
    def is_relay_candidate(self) -> bool:
        return self.chip == "PL2303"

    @property
    def is_console_candidate(self) -> bool:
        return self.chip in ("CH340", "CH34x")


def _classify(vid: int | None, pid: int | None) -> str:
    if vid == PROLIFIC_VID:
        return "PL2303" if pid is None or pid in _PL2303_PIDS else "PL2303?"
    if vid == WCH_VID:
        return "CH340" if pid is None or pid in CH34X_PIDS else "CH34x"
    if vid is None:
        return "unknown"
    return f"VID:{vid:04X}"


def list_com_ports() -> list[PortInfo]:
    """All serial ports currently present, sorted by name."""
    out: list[PortInfo] = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        out.append(
            PortInfo(
                device=p.device,
                description=p.description or "",
                hwid=p.hwid or "",
                vid=p.vid,
                pid=p.pid,
                chip=_classify(p.vid, p.pid),
            )
        )
    return out


def find_ports(chip: str) -> list[PortInfo]:
    """Ports whose detected chip matches, e.g. ``find_ports("PL2303")``."""
    return [p for p in list_com_ports() if p.chip == chip]
