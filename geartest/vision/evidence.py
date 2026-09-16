"""Recent-frame ring buffer.

A transient blackout is over before it can be reported: by the time the
detector has seen two dark frames, the picture is back. The only way to keep
evidence of one is to have been holding the past, so a rolling window of recent
frames is encoded and kept, and a window around an event is written out when it
fires.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np


class EvidenceRing:
    """Holds the last few seconds of frames as small JPEGs."""

    def __init__(
        self,
        seconds: float = 3.0,
        fps: float = 10.0,
        max_width: int = 640,
        quality: int = 80,
    ) -> None:
        self._frames: deque[tuple[float, bytes]] = deque(
            maxlen=max(1, int(seconds * fps))
        )
        self.min_interval = 1.0 / fps
        self.max_width = max_width
        self.quality = quality
        self._last_stored = 0.0

    def add(self, frame_bgr: np.ndarray, t: float) -> None:
        """Store a frame, throttled. Safe to call at capture rate."""
        if t - self._last_stored < self.min_interval:
            return
        self._last_stored = t

        height, width = frame_bgr.shape[:2]
        if width > self.max_width:
            scale = self.max_width / width
            frame_bgr = cv2.resize(
                frame_bgr,
                (self.max_width, max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, buffer = cv2.imencode(
            ".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        )
        if ok:
            self._frames.append((t, buffer.tobytes()))

    def clear(self) -> None:
        self._frames.clear()

    def window(self, t_start: float, t_end: float, pad: float = 1.0):
        """Frames around an event, widened by ``pad`` seconds on each side."""
        low, high = t_start - pad, t_end + pad
        return [(t, data) for t, data in self._frames if low <= t <= high]

    def save(
        self,
        root: Path,
        camera: str,
        label: str,
        t_start: float,
        t_end: float | None = None,
        pad: float = 1.0,
    ) -> Path | None:
        """Write the frames around an event and return the directory used."""
        frames = self.window(t_start, t_end if t_end is not None else t_start, pad)
        if not frames:
            return None

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        directory = root / camera / f"{stamp}_{label}"
        directory.mkdir(parents=True, exist_ok=True)

        base = frames[0][0]
        for index, (at, data) in enumerate(frames):
            name = f"{index:03d}_{(at - base) * 1000:+06.0f}ms.jpg"
            (directory / name).write_bytes(data)

        with (directory / "index.txt").open("w", encoding="utf-8") as handle:
            handle.write(f"camera: {camera}\n")
            handle.write(f"event: {label}\n")
            handle.write(f"t_start: {t_start:.3f}\n")
            handle.write(f"t_end: {t_end if t_end is None else round(t_end, 3)}\n")
            handle.write(f"frames: {len(frames)}\n")
        return directory
