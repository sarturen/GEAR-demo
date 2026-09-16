"""Modbus RTU master for a 16-way relay board (the PL2303GT port).

Only what a relay board needs: read coils (0x01), write single coil (0x05) and
write multiple coils (0x0F). The board is driven synchronously -- request,
response, verify -- under a lock, which is a different access pattern from the
streaming console in serial_link.py, so the two do not share a port object.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import serial

from ..config import RelayCfg

TrafficCallback = Callable[[str, bytes], None]
ErrorCallback = Callable[[Exception], None]

FC_READ_COILS = 0x01
FC_WRITE_COIL = 0x05
FC_WRITE_COILS = 0x0F

COIL_ON = 0xFF00
COIL_OFF = 0x0000


class RelayError(RuntimeError):
    pass


def crc16(data: bytes) -> int:
    """Modbus CRC-16 (polynomial 0xA001, init 0xFFFF), returned as an int."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(pdu: bytes) -> bytes:
    crc = crc16(pdu)
    return pdu + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def check_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise RelayError(f"frame too short: {frame.hex(' ')}")
    body, sent = frame[:-2], frame[-2:]
    expected = crc16(body)
    if sent != bytes((expected & 0xFF, (expected >> 8) & 0xFF)):
        raise RelayError(f"bad CRC in response: {frame.hex(' ')}")


class RelayBoard:
    """One Modbus RTU relay board of up to 16 channels."""

    CHANNELS = 16

    def __init__(
        self,
        cfg: RelayCfg,
        on_traffic: TrafficCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self._on_traffic = on_traffic
        self._on_error = on_error
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._pulse_lock = threading.Lock()
        self._pulse_timers: dict[int, threading.Timer] = {}
        self.state: list[bool] = [False] * self.CHANNELS

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        if self.is_open:
            return
        c = self.cfg
        if not c.port:
            raise ValueError("no relay port configured")
        self._serial = serial.Serial(
            port=c.port,
            baudrate=c.baudrate,
            bytesize=c.bytesize,
            parity=c.parity,
            stopbits=c.stopbits,
            timeout=c.timeout,
        )

    def close(self) -> None:
        self.cancel_all_pulses()
        port, self._serial = self._serial, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    # -- addressing ---------------------------------------------------------

    def coil(self, channel: int) -> int:
        """Modbus coil address for a 1-based channel number.

        Boards disagree on whether channel 1 is coil 0 or coil 1, and some
        vendors add a further offset, hence ``RelayCfg.coil_base``.
        """
        if not 1 <= channel <= self.CHANNELS:
            raise ValueError(f"channel must be 1..{self.CHANNELS}, got {channel}")
        return self.cfg.coil_base + (channel - 1)

    # -- transport ----------------------------------------------------------

    def _read_exact(self, count: int) -> bytes:
        port = self._serial
        if port is None or not port.is_open:
            raise RelayError(f"relay port {self.cfg.port} is not open")
        deadline = time.monotonic() + self.cfg.timeout
        out = bytearray()
        while len(out) < count and time.monotonic() < deadline:
            chunk = port.read(count - len(out))
            if chunk:
                out.extend(chunk)
        if len(out) < count:
            raise RelayError(
                f"short response from slave {self.cfg.slave_id}: "
                f"got {len(out)} of {count} bytes ({bytes(out).hex(' ')})"
            )
        return bytes(out)

    def _call(self, pdu: bytes, expect_fc: int, body_len: int | None) -> bytes:
        """Send a PDU and return the full verified response frame.

        ``body_len`` is the number of bytes between the function code and the
        CRC. Pass ``None`` for responses that carry a byte-count field (0x01).
        """
        frame = with_crc(pdu)
        with self._lock:
            port = self._serial
            if port is None or not port.is_open:
                raise RelayError(f"relay port {self.cfg.port} is not open")
            port.reset_input_buffer()
            port.write(frame)
            self._traffic("tx", frame)

            head = self._read_exact(2)
            if head[1] & 0x80:
                exc = self._read_exact(3)
                self._traffic("rx", head + exc)
                raise RelayError(
                    f"slave {head[0]} rejected function {expect_fc:#04x}: "
                    f"exception code {exc[0]:#04x}"
                )
            if head[0] != self.cfg.slave_id:
                raise RelayError(
                    f"reply from slave {head[0]}, expected {self.cfg.slave_id}"
                )
            if head[1] != expect_fc:
                raise RelayError(
                    f"reply function {head[1]:#04x}, expected {expect_fc:#04x}"
                )

            if body_len is None:
                count = self._read_exact(1)
                body = count + self._read_exact(count[0] + 2)
            else:
                body = self._read_exact(body_len + 2)

            response = head + body
            self._traffic("rx", response)

        check_crc(response)
        if self.cfg.inter_frame_delay > 0:
            time.sleep(self.cfg.inter_frame_delay)
        return response

    def _traffic(self, direction: str, frame: bytes) -> None:
        if self._on_traffic is not None:
            self._on_traffic(direction, frame)

    # -- relay operations ---------------------------------------------------

    def set_channel(self, channel: int, on: bool) -> None:
        coil = self.coil(channel)
        value = COIL_ON if on else COIL_OFF
        pdu = bytes(
            (
                self.cfg.slave_id,
                FC_WRITE_COIL,
                coil >> 8,
                coil & 0xFF,
                value >> 8,
                value & 0xFF,
            )
        )
        self._call(pdu, FC_WRITE_COIL, body_len=4)
        self.state[channel - 1] = on

    def write_channels(self, start_channel: int, values: list[bool]) -> None:
        """Write a contiguous run of channels in one transaction."""
        if not values:
            return
        coil = self.coil(start_channel)
        self.coil(start_channel + len(values) - 1)

        packed = bytearray((len(values) + 7) // 8)
        for i, on in enumerate(values):
            if on:
                packed[i // 8] |= 1 << (i % 8)

        pdu = (
            bytes(
                (
                    self.cfg.slave_id,
                    FC_WRITE_COILS,
                    coil >> 8,
                    coil & 0xFF,
                    len(values) >> 8,
                    len(values) & 0xFF,
                    len(packed),
                )
            )
            + bytes(packed)
        )
        self._call(pdu, FC_WRITE_COILS, body_len=4)
        for i, on in enumerate(values):
            self.state[start_channel - 1 + i] = on

    def all_off(self) -> None:
        self.write_channels(1, [False] * self.CHANNELS)

    def read_channels(self, count: int = CHANNELS) -> list[bool]:
        base = self.coil(1)
        if not 1 <= count <= self.CHANNELS:
            raise ValueError(f"count must be 1..{self.CHANNELS}")
        pdu = bytes(
            (
                self.cfg.slave_id,
                FC_READ_COILS,
                base >> 8,
                base & 0xFF,
                count >> 8,
                count & 0xFF,
            )
        )
        response = self._call(pdu, FC_READ_COILS, body_len=None)
        byte_count = response[2]
        data = response[3 : 3 + byte_count]
        if len(data) < (count + 7) // 8:
            raise RelayError(
                f"read {count} coils but board returned {byte_count} bytes"
            )
        states = [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
        self.state[:count] = states
        return states

    # -- pulse --------------------------------------------------------------

    def pulse(self, channel: int, duration_ms: int) -> None:
        """Energise a channel and release it automatically after ``duration_ms``.

        Re-pulsing a channel cancels the pending release rather than racing it.
        """
        self.set_channel(channel, True)
        timer = threading.Timer(duration_ms / 1000.0, self._release, args=(channel,))
        timer.daemon = True
        with self._pulse_lock:
            previous = self._pulse_timers.pop(channel, None)
            if previous is not None:
                previous.cancel()
            self._pulse_timers[channel] = timer
        timer.start()

    def cancel_all_pulses(self) -> None:
        with self._pulse_lock:
            timers = list(self._pulse_timers.values())
            self._pulse_timers.clear()
        for timer in timers:
            timer.cancel()

    def _release(self, channel: int) -> None:
        with self._pulse_lock:
            self._pulse_timers.pop(channel, None)
        try:
            self.set_channel(channel, False)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(exc)
