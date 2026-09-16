"""Brightness-invariant image statistics.

The whole detector rests on one model of what a brightness change looks like.
Backlight, ambient light and camera auto-exposure all act on the ROI
approximately as an affine map ``g' = a*g + b`` with ``a > 0``. Two rules
follow, and they are the reason nothing here compares a raw pixel value against
a fixed number:

1. ``norm = (g - median(g)) / std(g)`` is *exactly* invariant under that map,
   so it is the representation used for change detection.
2. Any statistic compared against a constant must be a ratio whose numerator
   and denominator carry the same gain. ``P95 - P5`` alone does **not** qualify:
   it is invariant to the offset ``b`` but scales with ``a``, so a black screen
   read at high gain looks "high contrast" compared to the same screen at low
   gain. Dividing by a co-measured noise floor fixes that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np

#: Guard for the normalisation denominator, in gray levels. A perfectly flat
#: frame is dominated by noise either way; this keeps that from exploding.
NORM_FLOOR = 1.0

_MAD_TO_SIGMA = 1.4826


@dataclass
class FrameStats:
    """Everything the detector needs from one frame."""

    median: float
    std: float
    sigma_n: float
    """Per-pixel noise floor in gray levels, measured from this frame."""
    flatness: float
    """High-pass spread over noise floor. Dimensionless; ~3.3 for flat content."""
    brightness: float
    """High percentile of the per-pixel max over B/G/R channels, in gray levels.

    A high percentile rather than the median, because on a normal screen the
    median is whatever the background happens to be while the bright end is what
    the panel can actually produce. Under an additive offset (ambient light on a
    glossy panel, camera black-level lift) the whole frame shifts up by the same
    amount, so a median-based ratio collapses toward 1 and a greyish black screen
    slips through; a high percentile keeps the margin.
    """
    norm: np.ndarray
    """Affine-invariant frame, float32, mean 0 and unit std."""


def max_channel(bgr: np.ndarray) -> np.ndarray:
    """Per-pixel maximum over B, G and R.

    Darkness has to be judged on this rather than on luma. A saturated blue
    "no signal" slate has luma ~29 -- dark by any luma test -- but its blue
    channel is 255, so it is plainly not a black screen. Max-channel sees 255
    and rejects it. This removes the single most likely false positive.
    """
    return np.max(bgr, axis=2)


def hist_percentile(gray: np.ndarray, fraction: float) -> float:
    """Continuous percentile of a uint8 image via a 256-bin histogram.

    The result is interpolated inside the bin rather than being the bin index.
    That matters more than it looks: a quantised percentile steps by a whole
    gray level, and because the median is what normalisation subtracts, every
    step shifts the entire normalised frame at once. The detector would then see
    every block change simultaneously -- which keeps resetting the freeze timer,
    so a picture that drifts in brightness while genuinely stuck is never
    reported. Interpolation turns that step into a fraction of a level per
    frame, which the block threshold ignores as it should.

    ``np.percentile`` would do the same thing but sorts the whole ROI, costing
    milliseconds a frame; this is O(256).
    """
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    cumulative = np.cumsum(hist)
    total = cumulative[-1]
    if total == 0:
        return 0.0
    target = fraction * total
    idx = min(int(np.searchsorted(cumulative, target, side="left")), 255)
    below = cumulative[idx - 1] if idx > 0 else 0.0
    within = hist[idx]
    if within <= 0:
        return float(idx)
    # Bin idx stands for the interval [idx, idx+1); centre the estimate.
    return float(np.clip(idx + (target - below) / within - 0.5, 0.0, 255.0))


def sorted_percentiles(values: np.ndarray, fractions: Sequence[float]) -> list[float]:
    """Several percentiles of a float array with a single sort."""
    if values.size == 0:
        return [0.0] * len(fractions)
    flat = np.sort(values.ravel())
    n = flat.size
    idx = np.clip(
        (np.asarray(fractions, dtype=np.float64) * (n - 1)).round().astype(np.intp),
        0,
        n - 1,
    )
    return [float(flat[i]) for i in idx]


def noise_sigma(gray: np.ndarray, stride: int = 4) -> float:
    """Robust per-pixel noise estimate, in gray levels.

    Neighbouring pixels of smooth content differ by almost nothing, so the
    spread of horizontal neighbour differences is dominated by sensor and
    compression noise. For i.i.d. noise ``var(d) = 2*sigma^2``, hence the
    ``sqrt(2)``. The MAD is used instead of a standard deviation so that sharp
    content edges -- a minority of pixels -- do not inflate the estimate.

    ``stride`` subsamples rows: the estimate is unchanged by it and the two
    medians are the most expensive thing in the per-frame budget.
    """
    rows = gray[::stride].astype(np.float32)
    if rows.shape[1] < 2:
        return 1.0
    diffs = (rows[:, 1:] - rows[:, :-1]).ravel()
    median = np.median(diffs)
    mad = np.median(np.abs(diffs - median))
    return max(float(_MAD_TO_SIGMA * mad / np.sqrt(2.0)), 1.0)


def normalize(gray: np.ndarray, median: float, std: float) -> np.ndarray:
    """Affine-invariant representation of the frame.

    ``norm`` is unchanged by ``g -> a*g + b``, so two frames of the same content
    at different brightness normalise to nearly the same image. That is what
    lets a freeze be detected *through* a brightness drift instead of the drift
    masking it, and symmetrically stops a brightness flicker from registering as
    a content change.
    """
    return (gray.astype(np.float32) - median) / max(std, NORM_FLOOR)


def flatness_ratio(
    gray: np.ndarray, sigma_n: float, hp_sigma: float = 8.0, stride: int = 2
) -> float:
    """High-pass spread divided by the noise floor -- dimensionless.

    Both terms carry the gain ``a``, so the ratio is invariant to backlight and
    exposure. Genuinely flat content (a black screen, a solid colour) lands near
    3.3, the P95-P5 of Gaussian noise in units of sigma; real picture content is
    orders of magnitude larger.

    The high-pass is not optional: vignetting, a lamp to one side of the panel
    and the panel's own backlight gradient put large *low-frequency* variation
    onto an otherwise perfectly flat screen. Without removing those, a genuine
    black screen under uneven ambient light reads as "not flat" and is missed.
    """
    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=hp_sigma)
    high_pass = gray[::stride].astype(np.float32) - blurred[::stride].astype(np.float32)
    p5, p95 = sorted_percentiles(high_pass, (0.05, 0.95))
    return float((p95 - p5) / max(sigma_n, 1.0))


def analyse(
    bgr: np.ndarray,
    work_width: int = 320,
    hp_sigma: float = 8.0,
    stride: int = 4,
) -> FrameStats:
    """Measure one ROI.

    Statistics are computed on a copy downscaled to ``work_width``: the extra
    resolution of a full HD ROI buys no discrimination and costs milliseconds
    per frame that the GUI would rather have.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    channels = max_channel(bgr)

    height, width = gray.shape[:2]
    if width > work_width:
        target = (work_width, max(1, round(height * work_width / width)))
        gray = cv2.resize(gray, target, interpolation=cv2.INTER_AREA)
        channels = cv2.resize(channels, target, interpolation=cv2.INTER_AREA)

    median = hist_percentile(gray, 0.5)
    std = float(gray.std())
    sigma_n = noise_sigma(gray, stride=stride)

    return FrameStats(
        median=median,
        std=std,
        sigma_n=sigma_n,
        flatness=flatness_ratio(gray, sigma_n, hp_sigma=hp_sigma, stride=max(1, stride // 2)),
        brightness=hist_percentile(channels, 0.95),
        norm=normalize(gray, median, std),
    )
