"""On-screen anomaly detection: black screen, transient blackout, freeze.

Three failure modes are watched for, and they are all decided on
brightness-invariant evidence -- see ``invariants`` for why that is the whole
point of the design.

* **Black screen** -- the ROI has no content (it is flat) *and* it is dark
  relative to what this panel normally produces. Either term alone is wrong:
  flatness alone condemns a full-screen solid colour, darkness alone condemns a
  dark-but-detailed scene.
* **Transient blackout** -- the same condition, but it clears again quickly. The
  interesting number is the duration, which is why this shares one episode
  state machine with the black screen rather than having its own detector.
* **Freeze** -- the content blocks have stopped changing. Measured per block,
  not globally, so a blinking cursor or a ticking clock in an otherwise stuck
  picture does not hide the freeze.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import cv2
import numpy as np

from ..config import ScreenCfg
from .invariants import FrameStats, analyse, noise_sigma

_SQRT2 = 2.0**0.5

#: Blocks used for the activity map: 20x15 over the working ROI.
BLOCK_COLS = 20
BLOCK_ROWS = 15

#: A block counts as moving if it changed within this many seconds.
ACTIVITY_WINDOW_S = 2.0

#: Shortest content change that counts as motion; OR-ing a lag this long with
#: the frame-to-frame diff catches sub-pixel motion that a lag-1 diff misses.
LAG_S = 0.5

#: Fraction of content blocks that must be moving to call the picture "alive".
ACTIVE_FRACTION = 0.15

#: Consecutive frames a block must exceed the change threshold before it counts
#: as moving. A single frame's noise spike is uncorrelated with the next one's,
#: so requiring two in a row removes essentially all false motion; genuine
#: motion and a blinking cursor both persist far longer than that.
CHANGED_PERSISTENCE = 2

#: Consecutive flat+dark frames needed to open a blackout episode. Two frames
#: is what stops a single corrupt or dropped frame becoming a phantom event.
BLACK_DEBOUNCE_FRAMES = 2

#: Frames must be bit-identical for this long before the camera rather than the
#: screen is blamed.
STALL_S = 1.0

#: Brightness baseline window, and the minimum samples before it is trusted.
BASELINE_WINDOW_S = 60.0
BASELINE_MIN_SAMPLES = 20

#: Warm-up before freeze detection arms, so the activity map has real data.
FREEZE_WARMUP_S = 10.0


class EventType(str, Enum):
    BLACK_SCREEN = "black_screen"
    TRANSIENT_BLACKOUT = "transient_blackout"
    FREEZE = "freeze"
    CAPTURE_STALL = "capture_stall"
    RECOVERED = "recovered"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    t_start: float
    t_end: float | None = None
    severity: Severity = Severity.WARNING
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    @property
    def duration(self) -> float | None:
        return None if self.t_end is None else self.t_end - self.t_start

    @property
    def label(self) -> str:
        names = {
            EventType.BLACK_SCREEN: "黑屏",
            EventType.TRANSIENT_BLACKOUT: "短暂闪黑",
            EventType.FREEZE: "冻屏",
            EventType.CAPTURE_STALL: "采集停滞",
            EventType.RECOVERED: "恢复正常",
        }
        return names[self.type]


@dataclass
class _Constants:
    """Detection thresholds derived from the single sensitivity knob.

    Higher sensitivity means anomalies are called more readily: flatness is
    easier to satisfy, darkness is easier to satisfy, and blocks need less
    change to count as moving. No absolute brightness value appears here.
    """

    k_flat: float
    r_dark: float
    k_block: float

    @classmethod
    def from_sensitivity(cls, sensitivity: int) -> "_Constants":
        t = (max(0, min(100, sensitivity)) - 50) / 50.0
        return cls(
            k_flat=6.0 * (1.0 + 0.5 * t),
            r_dark=min(0.45 * (1.0 + 0.35 * t), 0.85),
            k_block=3.5 * (1.0 - 0.4 * t),
        )


class AnomalyDetector:
    """Stateful detector for one camera's ROI.

    Pure computation: it takes frames and timestamps and returns events. It
    knows nothing about cameras, Qt or threads, which is what makes it testable
    against synthetic frames.
    """

    def __init__(self, cfg: ScreenCfg, roi: tuple[int, int, int, int]) -> None:
        self.cfg = cfg
        self.roi = roi
        self._const = _Constants.from_sensitivity(cfg.sensitivity)
        self._block_pixels = max(
            1, (roi[2] // BLOCK_COLS) * (roi[3] // BLOCK_ROWS)
        )

        self.freeze_warmup_s = FREEZE_WARMUP_S
        self._baseline_samples: deque[tuple[float, float]] = deque()
        self._baseline: float | None = None

        self._prev_norm: np.ndarray | None = None
        self._lag: deque[tuple[float, np.ndarray]] = deque()
        self._last_change = np.full((BLOCK_ROWS, BLOCK_COLS), -1e9, dtype=np.float64)
        self._ever_active = np.zeros((BLOCK_ROWS, BLOCK_COLS), dtype=bool)
        self._run = np.zeros((BLOCK_ROWS, BLOCK_COLS), dtype=np.int32)

        self._black_frames = 0
        self._episode_start: float | None = None
        self._t_last_good: float | None = None
        self._black_announced = False

        self._freeze_since: float | None = None
        self._freeze_active = False
        self._started_at: float | None = None

        self._prev_signature: int | None = None
        self._identical_since: float | None = None
        self._stall_active = False

        self.stats: FrameStats | None = None
        self.frozen_fraction: float = 1.0
        self.calibrated = False

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        self.__init__(self.cfg, self.roi)

    def snapshot(self) -> dict:
        """Live metrics for the UI. Cheap; safe to call after every frame."""
        stats = self.stats
        return {
            "calibrated": self.calibrated,
            "flatness": None if stats is None else round(stats.flatness, 1),
            "brightness": None if stats is None else round(stats.brightness, 1),
            "baseline": None if self._baseline is None else round(self._baseline, 1),
            "noise": None if stats is None else round(stats.sigma_n, 2),
            "frozen_fraction": round(self.frozen_fraction, 3),
            "black": self._episode_start is not None,
            "freeze": self._freeze_active,
        }

    # -- main entry ---------------------------------------------------------

    def process(self, frame_bgr: np.ndarray, t: float) -> list[Event]:
        """Measure one frame and return any events it produced.

        ``t`` must be a monotonic seconds clock sampled in the capture thread,
        not a frame counter -- USB cameras jitter and drop frames, so assuming
        1/fps would corrupt every duration the detector reports.
        """
        if self._started_at is None:
            self._started_at = t

        events: list[Event] = []
        roi = self._crop(frame_bgr)
        if roi.size == 0:
            return events

        stall = self._check_stall(roi, t)
        if stall:
            events.extend(stall)
            events.extend(self._flush_episode(t))
            return events

        stats = analyse(roi, work_width=min(320, roi.shape[1]))
        self.stats = stats

        self._learn_baseline(stats, t)

        events.extend(self._check_black(roi, stats, t))
        events.extend(self._check_freeze(roi, stats, t))
        return events

    # -- helpers ------------------------------------------------------------

    def _crop(self, frame: np.ndarray) -> np.ndarray:
        x, y, w, h = self.roi
        height, width = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            return frame[0:0, 0:0]
        return frame[y0:y1, x0:x1]

    def _learn_baseline(self, stats: FrameStats, t: float) -> None:
        """Track how bright this panel normally gets.

        Updated only on frames that are *not* flat and only while no event is
        running. That is what stops a persisting black screen from dragging the
        baseline down to meet itself, which would make it permanently immune to
        detection. A high quantile over a long window then survives the screen
        legitimately being dark for a while (a night scene, a dark UI) without
        decaying anywhere near as fast as the black condition needs to fire.
        """
        flat_limit = self._const.k_flat
        if stats.flatness < flat_limit or self._episode_start is not None:
            return

        self._baseline_samples.append((t, stats.brightness))
        cutoff = t - BASELINE_WINDOW_S
        while self._baseline_samples and self._baseline_samples[0][0] < cutoff:
            self._baseline_samples.popleft()

        if not self._baseline_samples:
            # Everything we knew about this panel has aged out, which happens
            # after a long dark stretch. Judging a re-lit screen against a
            # minute-old figure from a different brightness would be worse than
            # admitting we no longer know.
            self._baseline = None
            self.calibrated = False
            return

        if len(self._baseline_samples) >= BASELINE_MIN_SAMPLES:
            values = np.fromiter(
                (b for _, b in self._baseline_samples), dtype=np.float32
            )
            self._baseline = float(np.percentile(values, 90))
            self.calibrated = True

    def _is_flat(self, stats: FrameStats) -> bool:
        return stats.flatness < self._const.k_flat

    def _is_dark(self, stats: FrameStats) -> bool:
        if self._baseline is None or self._baseline <= 0:
            return False
        return stats.brightness < self._const.r_dark * self._baseline

    def _check_stall(self, roi: np.ndarray, t: float) -> list[Event]:
        """Separate "the camera stopped delivering" from "the screen froze".

        A frozen screen still lights the sensor, so consecutive frames differ by
        sensor noise. A stalled capture -- a USB hiccup, or ``read()`` handing
        back the same buffer -- returns a byte-identical frame. Reporting a
        screen freeze when the camera is what died would send someone to debug
        the wrong box, so this is called out as its own event.

        Sameness is tested on a strided subsample of the ROI: equality is
        exact for a repeated buffer and effectively never coincidental for two
        genuinely different frames, at a fraction of the cost of a full compare.
        """
        signature = int(roi[::4, ::4].sum(dtype=np.int64))
        previous, self._prev_signature = self._prev_signature, signature

        if previous is None:
            return []

        if signature != previous:
            self._identical_since = None
            if self._stall_active:
                self._stall_active = False
                return [
                    Event(
                        type=EventType.RECOVERED,
                        t_start=t,
                        t_end=t,
                        severity=Severity.INFO,
                        detail="采集恢复",
                        metrics={"was": "capture_stall"},
                    )
                ]
            return []

        if self._identical_since is None:
            self._identical_since = t
            return []

        if not self._stall_active and t - self._identical_since >= STALL_S:
            self._stall_active = True
            return [
                Event(
                    type=EventType.CAPTURE_STALL,
                    t_start=self._identical_since,
                    severity=Severity.ERROR,
                    detail="摄像头持续返回完全相同的帧，疑似采集卡死（非屏幕冻屏）",
                    metrics={"identical_for_s": round(t - self._identical_since, 3)},
                )
            ]
        return []

    # -- black / transient --------------------------------------------------

    def _check_black(self, roi: np.ndarray, stats: FrameStats, t: float) -> list[Event]:
        events: list[Event] = []

        if self._stall_active:
            return events

        if self._is_flat(stats) and self._is_dark(stats):
            self._black_frames += 1
            if self._black_frames == BLACK_DEBOUNCE_FRAMES:
                self._episode_start = self._t_last_good if self._t_last_good else t
                self._black_announced = False
        else:
            self._black_frames = 0
            self._t_last_good = t
            if self._episode_start is not None:
                events.append(self._close_episode(t))

        if self._episode_start is not None and not self._black_announced:
            if t - self._episode_start >= self.cfg.black_confirm_s:
                self._black_announced = True
                events.append(
                    Event(
                        type=EventType.BLACK_SCREEN,
                        t_start=self._episode_start,
                        severity=Severity.ERROR,
                        detail="画面持续无内容且低于本机正常亮度基线",
                        metrics={
                            "brightness": round(stats.brightness, 1),
                            "baseline": None
                            if self._baseline is None
                            else round(self._baseline, 1),
                            "flatness": round(stats.flatness, 1),
                        },
                    )
                )
        return events

    def _close_episode(self, t: float) -> Event:
        """End a flat+dark run and classify it by how long it lasted."""
        start, self._episode_start = self._episode_start, None
        announced, self._black_announced = self._black_announced, False
        self._black_frames = 0
        duration = t - start

        if announced or duration >= self.cfg.black_confirm_s:
            return Event(
                type=EventType.RECOVERED,
                t_start=start,
                t_end=t,
                severity=Severity.INFO,
                detail=f"黑屏持续 {duration:.2f}s 后恢复",
                metrics={"was": "black_screen", "duration_s": round(duration, 3)},
            )

        if duration * 1000 < self.cfg.min_transient_ms:
            return Event(
                type=EventType.RECOVERED,
                t_start=start,
                t_end=t,
                severity=Severity.INFO,
                detail=f"忽略 {duration * 1000:.0f}ms 的瞬时异常（低于最小时长）",
                metrics={"was": "below_min", "duration_s": round(duration, 3)},
            )

        return Event(
            type=EventType.TRANSIENT_BLACKOUT,
            t_start=start,
            t_end=t,
            severity=Severity.WARNING,
            detail=f"闪黑 {duration * 1000:.0f}ms",
            metrics={"duration_s": round(duration, 3)},
        )

    def _flush_episode(self, t: float) -> list[Event]:
        if self._stall_active:
            return []
        if self._episode_start is not None:
            return [self._close_episode(t)]
        return []

    # -- freeze -------------------------------------------------------------

    def _check_freeze(self, roi: np.ndarray, stats: FrameStats, t: float) -> list[Event]:
        events: list[Event] = []
        self._update_activity(stats, t)

        if self._started_at is not None and t - self._started_at < self.freeze_warmup_s:
            return events
        # A black screen is supposed to be static; that is not a freeze.
        if self._episode_start is not None:
            self._freeze_since = None
            return events

        content = self._ever_active
        if not content.any():
            self.frozen_fraction = 1.0
        else:
            recent = (t - self._last_change) <= ACTIVITY_WINDOW_S
            self.frozen_fraction = float(recent[content].mean())

        if self.frozen_fraction < ACTIVE_FRACTION:
            if self._freeze_since is None:
                self._freeze_since = t
            elif not self._freeze_active and t - self._freeze_since >= self.cfg.freeze_timeout_s:
                self._freeze_active = True
                events.append(
                    Event(
                        type=EventType.FREEZE,
                        t_start=self._freeze_since,
                        severity=Severity.ERROR,
                        detail=f"画面 {self.cfg.freeze_timeout_s:.0f}s 无变化",
                        metrics={"active_fraction": round(self.frozen_fraction, 3)},
                    )
                )
        else:
            self._freeze_since = None
            if self._freeze_active:
                self._freeze_active = False
                events.append(
                    Event(
                        type=EventType.RECOVERED,
                        t_start=t,
                        t_end=t,
                        severity=Severity.INFO,
                        detail="画面恢复变化",
                        metrics={"was": "freeze"},
                    )
                )
        return events

    def _update_activity(self, stats: FrameStats, t: float) -> None:
        """Mark which blocks changed, and remember which blocks ever change.

        Blocks that never move during the run are static by design -- menu
        chrome, a wallpaper border -- and are dropped from the freeze
        judgement entirely. That is how a legitimately static screen avoids
        being reported as frozen without the user configuring anything.
        """
        norm = stats.norm
        block = cv2.resize(
            norm, (BLOCK_COLS, BLOCK_ROWS), interpolation=cv2.INTER_AREA
        )

        self._lag.append((t, block))
        while self._lag and t - self._lag[0][0] > LAG_S:
            self._lag.popleft()

        raw = np.zeros((BLOCK_ROWS, BLOCK_COLS), dtype=bool)
        if self._prev_norm is not None:
            raw |= self._block_diff(self._prev_norm, block, stats)
        if len(self._lag) > 1:
            raw |= self._block_diff(self._lag[0][1], block, stats)

        self._run = np.where(raw, self._run + 1, 0)
        changed = self._run >= CHANGED_PERSISTENCE

        self._last_change[changed] = t
        self._ever_active |= changed
        self._prev_norm = block

    def _block_diff(
        self, previous: np.ndarray, current: np.ndarray, stats: FrameStats
    ) -> np.ndarray:
        """Which blocks changed by more than the noise this camera produces.

        The threshold is derived from the frame's own noise floor rather than
        being a tunable number, so it adapts per camera: a noisy USB sensor
        needs a bigger block difference to count than a clean one.

        The ``sqrt(2)`` is not decoration. Block noise falls as ``sqrt(P)`` for
        a block of ``P`` pixels, but what is compared is the difference of *two*
        independent block means, whose noise is ``sqrt(2)`` times larger. Leaving
        it out puts the threshold at roughly the 99th percentile of the diff
        distribution instead of well beyond it, and a couple of blocks per frame
        get flagged as moving even on a perfectly frozen picture -- which is
        exactly what stops a freeze ever being detected.
        """
        block_noise = (
            _SQRT2 * (stats.sigma_n / max(stats.std, 1.0)) / np.sqrt(self._block_pixels)
        )
        return np.abs(current - previous) > self._const.k_block * block_noise


def timestamp() -> float:
    """Monotonic seconds. Every duration the detector reports uses this clock."""
    return time.perf_counter()
