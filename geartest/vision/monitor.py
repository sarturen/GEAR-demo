"""Camera capture and per-screen detection.

A camera is a capture device; a screen is a region of that camera's view worth
watching. They are separate objects because two panels can sit in one camera's
field of view, and because the capture thread should be shared rather than opened
once per watched region.

The split also puts the evidence ring next to the detector. It used to live in
the camera panel, fed by whatever the operator happened to be looking at, so a
flow running while the panel was on another tab recorded nothing at all.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable

from ..config import CameraCfg, RoiCfg, ScreenCfg
from ..devices.camera import CameraStream
from .detector import AnomalyDetector, Event, EventType
from .evidence import EvidenceRing


def roi_to_pixels(roi: RoiCfg, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a fractional ROI to a clamped pixel rectangle."""
    x = int(round(roi.x * width))
    y = int(round(roi.y * height))
    w = int(round(roi.w * width))
    h = int(round(roi.h * height))
    x = max(0, min(x, max(0, width - 1)))
    y = max(0, min(y, max(0, height - 1)))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


class ScreenMonitor:
    """One watched region: its detector, its evidence, its current state.

    ``feed`` is called by its camera hub's capture thread, so everything it
    exposes is read under a lock.
    """

    def __init__(
        self,
        cfg: ScreenCfg,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub: CameraHub | None = None
        self.evidence = EvidenceRing()
        self.events: deque[Event] = deque(maxlen=1000)
        self.black = False
        self.freeze = False
        self.stall = False
        self.error = ""
        # A list, not a single callback: the screen panel and a running flow
        # watch the same screens, and one must not displace the other.
        self._listeners: list[Callable[[Event], None]] = []
        if on_event is not None:
            self._listeners.append(on_event)
        self._detector: AnomalyDetector | None = None
        self._lock = threading.Lock()

    # -- state --------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.cfg.name

    def add_listener(self, callback: Callable[[Event], None] | None) -> None:
        if callback is not None:
            self._listeners.append(callback)

    @property
    def calibrated(self) -> bool:
        """Whether the detector has learnt what this panel looks like.

        Until it has, no black decision can be made at all -- see
        ``AnomalyDetector._is_dark``, which cannot call anything dark without a
        baseline. A caller asking "is the screen lit?" before this is true would
        get a yes for a screen that has never been powered.
        """
        detector = self._detector
        return bool(detector is not None and detector.calibrated)

    def snapshot(self) -> dict:
        detector = self._detector
        state = {
            "name": self.cfg.name,
            "running": bool(self.hub is not None and self.hub.running),
            "fps": round(self.hub.fps, 1) if self.hub is not None else 0.0,
            "error": self.error or (self.hub.error if self.hub is not None else ""),
            "calibrated": self.calibrated,
            "black": self.black,
            "freeze": self.freeze,
            "stall": self.stall,
        }
        if detector is not None:
            state.update(detector.snapshot())
        return state

    def events_since(self, when: float) -> list[Event]:
        with self._lock:
            return [e for e in self.events if e.t_start >= when]

    def clear_events(self) -> None:
        with self._lock:
            self.events.clear()

    def latest_frame(self):
        return None if self.hub is None else self.hub.last_frame()

    # -- lifecycle ----------------------------------------------------------

    def set_roi(self, roi: RoiCfg) -> None:
        """Move the watched region. Rebuilds the detector, since the learned
        baseline and activity map describe the old region."""
        self.cfg.roi = roi
        self.reset_detector()

    def reset_detector(self) -> None:
        """Drop the learned state, e.g. after the screen's settings changed.

        The evidence ring goes with it: frames kept from before the region
        moved are not evidence about the region being watched now.
        """
        with self._lock:
            self._detector = None
            self.evidence.clear()
        self.black = False
        self.freeze = False
        self.stall = False

    # -- fed by the camera hub ---------------------------------------------

    def feed(self, frame, t: float) -> None:
        with self._lock:
            detector = self._detector
            if detector is None:
                height, width = frame.shape[:2]
                detector = AnomalyDetector(
                    self.cfg, roi_to_pixels(self.cfg.roi, width, height)
                )
                self._detector = detector

        try:
            events = detector.process(frame, t)
        except Exception as exc:  # noqa: BLE001 - surfaced on the tile
            self.error = f"{type(exc).__name__}: {exc}"
            return
        self.error = ""

        with self._lock:
            self.evidence.add(frame, t)
            for event in events:
                self._apply(event)
                self.events.append(event)
        for event in events:
            for listener in list(self._listeners):
                listener(event)

    def _apply(self, event: Event) -> None:
        """Fold an event into the flags a flow asserts against."""
        if event.type is EventType.BLACK_SCREEN:
            self.black = True
        elif event.type is EventType.CAPTURE_STALL:
            self.stall = True
        elif event.type is EventType.FREEZE:
            self.freeze = True
        elif event.type is EventType.RECOVERED:
            was = event.metrics.get("was")
            if was == "black_screen":
                self.black = False
            elif was == "freeze":
                self.freeze = False
            elif was == "capture_stall":
                self.stall = False


class CameraHub:
    """Owns one capture stream and feeds every screen watching it."""

    def __init__(
        self,
        cfg: CameraCfg,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._stream = CameraStream(cfg.source, cfg.width, cfg.height, cfg.name)
        self._screens: dict[str, ScreenMonitor] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame = None
        self._frame_t = 0.0
        self.error = ""

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def fps(self) -> float:
        return self._stream.fps

    def attach(self, monitor: ScreenMonitor) -> None:
        with self._lock:
            self._screens[monitor.cfg.name] = monitor
        monitor.hub = self

    def detach(self, name: str) -> None:
        with self._lock:
            monitor = self._screens.pop(name, None)
        if monitor is not None:
            monitor.hub = None

    def screens(self) -> list[ScreenMonitor]:
        with self._lock:
            return list(self._screens.values())

    def start(self) -> None:
        if self.running:
            return
        self._stream.open()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"hub-{self.cfg.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._stream.stop()

    def last_frame(self):
        with self._lock:
            return self._frame

    def last_frame_and_time(self):
        with self._lock:
            return self._frame, self._frame_t

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._stream.read(timeout=0.5)
            except Exception as exc:  # noqa: BLE001 - surfaced on the tiles
                self.error = f"{type(exc).__name__}: {exc}"
                break
            if item is None:
                continue

            frame, t = item
            with self._lock:
                self._frame = frame
                self._frame_t = t
                screens = list(self._screens.values())

            # Detection runs on this one thread for every screen on this camera.
            # At a few milliseconds per region that leaves the capture rate
            # alone, and it keeps the ordering between screens deterministic.
            for monitor in screens:
                monitor.feed(frame, t)
