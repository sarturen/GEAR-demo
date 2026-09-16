"""Line-oriented serial console with a background reader thread.

This is the CH340 side: a device console that streams text, which the bench
writes commands to and monitors. Byte-level access to the same port is exposed
through raw listeners so the UI can offer a hex view without a second reader.

Listeners are a list rather than a single callback because the serial panel and
the flow engine both watch the same port at once, and one must not displace the
other.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import serial

from ..config import ConsoleCfg

LineCallback = Callable[[str], None]
RawCallback = Callable[[bytes], None]
ErrorCallback = Callable[[Exception], None]

# A device that prints progress without a newline should not stall the display.
PARTIAL_FLUSH_S = 0.2


class LineCollector:
    """Remembers recent lines so a caller can assert on output already seen.

    Usable directly as a line listener. Steps need this because the output a
    step cares about usually arrives before the step starts waiting for it.
    """

    def __init__(self, maxlen: int = 4000) -> None:
        self._lines: deque[tuple[float, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        with self._lock:
            self._lines.append((time.monotonic(), text))

    def since(self, when: float) -> list[str]:
        with self._lock:
            return [line for at, line in self._lines if at >= when]

    def text(self, since: float | None = None) -> str:
        with self._lock:
            lines = self._lines if since is None else [
                (at, line) for at, line in self._lines if at >= since
            ]
        return "\n".join(line for _, line in lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class SerialLink:
    """Owns one serial port. Not safe to open twice; not thread-safe to close
    from within a callback."""

    def __init__(
        self,
        cfg: ConsoleCfg,
        on_line: LineCallback | None = None,
        on_raw: RawCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self._line_listeners: list[LineCallback] = []
        self._raw_listeners: list[RawCallback] = []
        self._on_error = on_error
        if on_line is not None:
            self._line_listeners.append(on_line)
        if on_raw is not None:
            self._raw_listeners.append(on_raw)
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self.tx_bytes = 0
        self.rx_bytes = 0

    def add_listener(
        self, on_line: LineCallback | None = None, on_raw: RawCallback | None = None
    ) -> None:
        if on_line is not None:
            self._line_listeners.append(on_line)
        if on_raw is not None:
            self._raw_listeners.append(on_raw)

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        if self.is_open:
            return
        c = self.cfg
        if not c.port:
            raise ValueError("no serial port configured")
        self._serial = serial.Serial(
            port=c.port,
            baudrate=c.baudrate,
            bytesize=c.bytesize,
            parity=c.parity,
            stopbits=c.stopbits,
            timeout=0.05,
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._read_loop, name=f"rx-{c.port}", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        port, self._serial = self._serial, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def write(self, text: str) -> int:
        return self.write_bytes(text.encode(self.cfg.encoding, errors="replace"))

    def write_bytes(self, data: bytes) -> int:
        port = self._serial
        if port is None or not port.is_open:
            raise RuntimeError(f"{self.cfg.port} is not open")
        with self._write_lock:
            written = port.write(data)
        self.tx_bytes += written
        return written

    def _read_loop(self) -> None:
        buf = bytearray()
        last_data_at = 0.0
        while not self._stop.is_set():
            port = self._serial
            if port is None:
                break
            try:
                data = port.read(max(1, port.in_waiting))
            except Exception as exc:
                if not self._stop.is_set() and self._on_error is not None:
                    self._on_error(exc)
                continue

            now = time.monotonic()
            if data:
                self.rx_bytes += len(data)
                for listener in self._raw_listeners:
                    listener(data)
                buf.extend(data)
                last_data_at = now

            while (idx := buf.find(b"\n")) >= 0:
                line = bytes(buf[:idx])
                del buf[: idx + 1]
                self._emit(line)

            if buf and now - last_data_at > PARTIAL_FLUSH_S:
                line = bytes(buf)
                buf.clear()
                self._emit(line)

    def _emit(self, line: bytes) -> None:
        if line.endswith(b"\r"):
            line = line[:-1]
        text = line.decode(self.cfg.encoding, errors="replace")
        for listener in self._line_listeners:
            listener(text)
