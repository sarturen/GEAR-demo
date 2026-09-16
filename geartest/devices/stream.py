"""Line streaming over an open pipe or channel.

adb output arrives on a subprocess pipe and SSH output on a paramiko channel.
The transport differs; how the output has to be consumed does not, so both use
this.
"""

from __future__ import annotations

import threading
from typing import Callable, IO, Iterable


class LinePump:
    """Reads lines from a stream on a daemon thread and hands them to a callback.

    The callback runs on the pump thread, so it must not block and must not
    touch a GUI directly -- queue the line and let the GUI thread drain it.
    """

    def __init__(
        self,
        stream: Iterable,
        on_line: Callable[[str], None],
        name: str = "lines",
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._stream = stream
        self._on_line = on_line
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> "LinePump":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            for raw in self._stream:
                if self._stop.is_set():
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                self._on_line(raw.rstrip("\r\n"))
        except Exception as exc:
            # A pipe closing under us is the normal way this ends; anything else
            # is worth surfacing, because a silently dead stream looks exactly
            # like a quiet device.
            if not self._stop.is_set() and self._on_error is not None:
                self._on_error(exc)
