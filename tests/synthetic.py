"""Synthetic scenes for exercising the detector without a camera or a device.

The generator models what actually changes in the real setup: the panel's
content, then a brightness transform standing in for backlight/ambient/exposure,
then sensor noise. Keeping those separate is what makes it possible to assert
that a scenario behaves identically at every brightness.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

SIZE = (320, 240)

#: One shared generator so a scenario replays identically.
_RNG = np.random.default_rng(20260916)


def reseed(seed: int = 20260916) -> None:
    global _RNG
    _RNG = np.random.default_rng(seed)


def scene(
    t: float,
    *,
    moving: bool = True,
    cursor: bool = False,
    clock: bool = False,
    freeze_at: float | None = None,
    size: tuple[int, int] = SIZE,
) -> np.ndarray:
    """A screen-like gray image: static chrome plus optional moving content."""
    width, height = size
    img = np.full((height, width), 40, np.uint8)

    # Static chrome: a border and a title bar. These never change, and the
    # detector is expected to learn that and ignore them.
    cv2.rectangle(img, (3, 3), (width - 4, height - 4), 120, 2)
    cv2.rectangle(img, (3, 3), (width - 4, 28), 70, -1)
    cv2.putText(
        img, "MENU", (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 150, 1, cv2.LINE_AA
    )

    content_t = min(t, freeze_at) if freeze_at is not None else t

    if moving:
        x = int((content_t * 80) % (width - 60))
        cv2.rectangle(img, (x, 50), (x + 36, 130), 140, -1)
        cy = int(height * 0.68 + 40 * math.sin(content_t * 3.0))
        cv2.circle(img, (width // 2, cy), 14, 160, -1)

    if cursor and int(content_t * 2) % 2 == 0:
        cv2.rectangle(img, (width - 40, height - 30), (width - 20, height - 12), 200, -1)

    if clock:
        cv2.putText(
            img,
            str(int(content_t * 2) % 10),
            (width - 60, height - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            200,
            2,
            cv2.LINE_AA,
        )
    return img


def render(
    t: float,
    *,
    a: float = 1.0,
    b: float = 0.0,
    noise: float = 3.0,
    level: float | None = None,
    color_bgr: tuple[int, int, int] | None = None,
    size: tuple[int, int] = SIZE,
    rng: np.random.Generator | None = None,
    **scene_kwargs,
) -> np.ndarray:
    """Build one BGR frame.

    ``a`` and ``b`` are the affine brightness transform. Everything the detector
    must be blind to -- backlight, ambient light, camera auto-exposure -- is
    expressed through them, which is how the cross-brightness test below can
    assert that behaviour does not move when the light does.
    """
    width, height = size
    if color_bgr is not None:
        base = np.zeros((height, width, 3), np.uint8)
        base[:, :] = color_bgr
    else:
        gray = (
            np.full((height, width), level, np.uint8)
            if level is not None
            else scene(t, size=size, **scene_kwargs)
        )
        base = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    generator = rng if rng is not None else _RNG
    img = base.astype(np.float32) * a + b
    if noise:
        img += generator.normal(0.0, noise, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


#: Brightness settings a scenario is replayed at. Covers dim, offset (ambient
#: light), nominal, and bright with a raised black level.
BRIGHTNESS_SETTINGS = [
    {"a": 0.45, "b": 0.0},
    {"a": 0.75, "b": 20.0},
    {"a": 1.0, "b": 0.0},
    {"a": 1.25, "b": 30.0},
]


def black_at(_setting: dict, black_level: float = 12.0) -> dict:
    """Render kwargs for a *black* screen under a given brightness setting.

    A black screen is simply the panel's own black level -- a small number for a
    lit LCD, not zero. The brightness setting then transforms it by exactly the
    same ``a``/``b`` it applies to content, which is the whole point: the ratio
    of black to content is then invariant to the brightness, so a scenario that
    survives at one setting must survive at all of them. Raising the backlight
    lifts the black level and the content together, and the black screen merely
    stops looking black to a human.
    """
    return {"level": black_level}


def play(
    detector,
    segments: list[tuple[float, dict]],
    *,
    fps: int = 30,
    t0: float = 0.0,
    seed: int = 20260916,
) -> tuple[list, float]:
    """Feed a timed sequence of scenes through a detector.

    ``segments`` is a list of ``(duration_seconds, render_kwargs)``.
    """
    reseed(seed)
    events: list = []
    t = t0
    dt = 1.0 / fps
    for duration, kwargs in segments:
        for _ in range(max(1, round(duration * fps))):
            events.extend(detector.process(render(t, **kwargs), t))
            t += dt
    return events, t


def types(events) -> list[str]:
    """Event type names, dropping the informational recovery notices."""
    return [e.type.value for e in events if e.type.value != "recovered"]


def find(events, event_type):
    return [e for e in events if e.type == event_type]
