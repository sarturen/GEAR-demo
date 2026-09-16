"""Camera capture: enumerate sources, open one, pull frames off the GUI thread."""

from __future__ import annotations

import os
import threading
import time
from collections import deque

import cv2
import numpy as np


class CameraError(RuntimeError):
    pass


class _Mailbox:
    """Single-slot hand-off that newer frames overwrite.

    The detector must always work on the most recent frame. Queueing would make
    it fall further behind a live camera the moment it is slower than the
    capture rate, which is the one failure mode that matters here.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._item = None
        self._closed = False

    def put(self, item) -> None:
        with self._condition:
            self._item = item
            self._condition.notify()

    def get(self, timeout: float | None = None):
        with self._condition:
            self._condition.wait_for(
                lambda: self._item is not None or self._closed, timeout
            )
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def is_stream_url(source: str) -> bool:
    return "://" in source


def probe_sources(max_index: int = 6) -> list[str]:
    """Capture indices that actually open and deliver a frame.

    Slow, because the only reliable way to find a camera is to open it. Run it
    from a worker thread, never from the GUI thread.
    """
    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    found: list[str] = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index, backend)
        try:
            if cap.isOpened() and cap.read()[0]:
                found.append(str(index))
        finally:
            cap.release()
    return found


class CameraStream:
    """One camera, read continuously on a daemon thread."""

    def __init__(
        self,
        source: str,
        width: int = 1280,
        height: int = 720,
        name: str = "",
    ) -> None:
        self.source = source
        self.width = width
        self.height = height
        self.name = name
        self.frames = 0
        self.fps = 0.0
        self.last_error = ""
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mailbox = _Mailbox()
        self._recent: deque[float] = deque(maxlen=30)

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> None:
        if self.is_open:
            return
        source: str | int = (
            int(self.source) if self.source.strip().isdigit() else self.source
        )
        if isinstance(source, int):
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            cap = cv2.VideoCapture(source, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            # Without this a USB camera hands back a stale queued frame and
            # every reading is a second out of date.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(source)

        if not cap.isOpened():
            cap.release()
            raise CameraError(f"无法打开摄像头：{self.source}")
        self._cap = cap

    def start(self) -> None:
        if self._cap is None:
            self.open()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._mailbox = _Mailbox()
        self._thread = threading.Thread(
            target=self._run, name=f"cam-{self.name or self.source}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._mailbox.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()

    def read(self, timeout: float = 1.0):
        """Latest ``(frame, monotonic_timestamp)``, or None on timeout.

        The timestamp is taken next to the read, so durations derived from it
        are real rather than assumed from the nominal frame rate.
        """
        return self._mailbox.get(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                self.last_error = "读帧失败"
                time.sleep(0.05)
                continue

            now = time.perf_counter()
            self.frames += 1
            self._recent.append(now)
            if len(self._recent) > 1:
                span = self._recent[-1] - self._recent[0]
                if span > 0:
                    self.fps = (len(self._recent) - 1) / span
            self.last_error = ""
            self._mailbox.put((frame, now))

    def grab(self, timeout: float = 2.0) -> np.ndarray | None:
        """One frame, opening and starting the stream first if needed."""
        if not self.is_open:
            self.open()
        if self._thread is None or not self._thread.is_alive():
            self.start()
        result = self.read(timeout)
        return None if result is None else result[0]
