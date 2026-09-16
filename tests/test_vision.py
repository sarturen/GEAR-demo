"""Behavioural tests for the screen anomaly detector.

These run without a camera or a device under test. Each scenario synthesises a
video at 30 fps and asserts on the events that come out. The brightness
invariance tests are the load-bearing ones: every scenario that matters is
replayed at several brightness settings and must behave identically, which is
the machine-checkable form of "a black screen must not be missed just because
the backlight makes it look grey".
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from geartest.config import ScreenCfg
from geartest.vision.detector import AnomalyDetector, EventType
from tests.synthetic import (
    BRIGHTNESS_SETTINGS,
    SIZE,
    black_at,
    find,
    play,
    render,
    types,
)

ROI = (0, 0, SIZE[0], SIZE[1])
FPS = 30


def detector(**overrides) -> AnomalyDetector:
    settings = {
        "sensitivity": 50,
        "freeze_timeout_s": 3.0,
        "black_confirm_s": 2.0,
        "min_transient_ms": 80.0,
        "transient_max_s": 1.5,
        "roi": None,
    }
    settings.update(overrides)
    settings.pop("roi", None)
    det = AnomalyDetector(ScreenCfg(**settings), ROI)
    det.freeze_warmup_s = 1.5
    return det


# -- no spurious events -----------------------------------------------------


def test_content_is_quiet():
    """A normal moving picture must produce nothing at all."""
    events, _ = play(detector(), [(6.0, {})])
    assert types(events) == [], f"unexpected events: {types(events)}"


def test_brightness_alone_is_quiet():
    """Dimming, brightening and ambient offset must not be events."""
    for setting in BRIGHTNESS_SETTINGS:
        det = detector()
        events, _ = play(det, [(6.0, setting)])
        assert types(events) == [], f"brightness {setting} produced {types(events)}"


# -- black screen -----------------------------------------------------------


def test_black_screen_detected():
    det = detector()
    events, _ = play(det, [(3.0, {}), (5.0, {"level": 0.0}), (2.0, {})])
    assert "black_screen" in types(events), types(events)
    assert types(events).count("black_screen") == 1, types(events)


def test_greyish_black_detected_at_every_brightness():
    """The headline requirement.

    A black screen under a raised backlight photographs as grey. It must still
    be called black, at every brightness setting, because the decision is made
    on the panel's own learned baseline rather than on any fixed level.
    """
    for setting in BRIGHTNESS_SETTINGS:
        det = detector()
        segments = [
            (3.0, setting),
            (5.0, {**setting, **black_at(setting)}),
            (2.0, setting),
        ]
        events, _ = play(det, segments)
        assert "black_screen" in types(events), (
            f"greyish black missed at {setting}: {types(events)}"
        )


def test_blue_no_signal_slate_is_not_black():
    """A saturated blue slate is dark in luma but is plainly not a black screen."""
    det = detector()
    events, _ = play(det, [(3.0, {}), (5.0, {"color_bgr": (255, 0, 0)}), (2.0, {})])
    assert "black_screen" not in types(events), types(events)


def test_white_page_is_not_black():
    """A flat bright page is flat, but flatness alone must not condemn it."""
    det = detector()
    events, _ = play(det, [(3.0, {}), (5.0, {"level": 255.0}), (2.0, {})])
    assert "black_screen" not in types(events), types(events)


# -- transient blackout -----------------------------------------------------


def test_transient_blackout_measured():
    """A brief blackout is reported with a duration, not as a black screen."""
    for frames in (5, 9):
        nominal_ms = frames / FPS * 1000
        det = detector()
        segments = [(3.0, {}), (frames / FPS, {"level": 0.0}), (2.0, {})]
        events, _ = play(det, segments)
        blackouts = find(events, EventType.TRANSIENT_BLACKOUT)
        assert len(blackouts) == 1, f"{frames} frames -> {types(events)}"
        measured_ms = blackouts[0].metrics["duration_s"] * 1000
        # The episode is bracketed by the last good frame before and the first
        # good frame after, so the measurement is an upper bound.
        assert nominal_ms <= measured_ms <= nominal_ms + 150, (
            f"{frames} frames reported {measured_ms:.0f}ms"
        )


def test_single_corrupt_frame_is_ignored():
    """One bad frame must not become a phantom event."""
    det = detector()
    events, _ = play(det, [(3.0, {}), (1 / FPS, {"level": 0.0}), (3.0, {})])
    assert types(events) == [], types(events)


def test_brightness_change_between_frames_is_not_a_blackout():
    """A sudden exposure step must not read as content disappearing."""
    det = detector()
    segments = [(3.0, {"a": 1.0, "b": 0.0}), (3.0, {"a": 0.5, "b": 60.0})]
    events, _ = play(det, segments)
    assert "black_screen" not in types(events), types(events)
    assert "transient_blackout" not in types(events), types(events)


# -- freeze -----------------------------------------------------------------


def test_freeze_detected():
    det = detector()
    events, _ = play(det, [(2.5, {}), (7.0, {"freeze_at": 2.5})])
    assert "freeze" in types(events), types(events)


def test_static_menu_with_blinking_cursor_is_not_a_freeze():
    """A menu that never moves, with only a cursor animating, is alive.

    The cursor keeps the blocks that ever animate busy, so the picture is
    correctly judged to be changing.
    """
    det = detector()
    events, _ = play(det, [(10.0, {"moving": False, "cursor": True})])
    assert "freeze" not in types(events), types(events)


def test_frozen_video_with_ticking_clock_is_a_freeze():
    """The mirror case: one small widget ticking must not hide a stuck picture.

    A whole-frame similarity metric fails here; the per-block activity map is
    what makes it work.
    """
    det = detector()
    events, _ = play(det, [(2.5, {}), (7.0, {"freeze_at": 2.5, "clock": True})])
    assert "freeze" in types(events), types(events)


def test_brightness_ramp_neither_blackens_nor_resets_freeze():
    """A brightness drift must not be mistaken for content change.

    This is the case a naive frame-differencing detector gets wrong: the ramp
    would keep resetting the freeze timer and mask a genuinely stuck picture.
    Comparing affine-normalised frames is what prevents it.
    """
    det = detector()
    segments = [(2.0, {})]
    steps = 40
    for i in range(steps):
        fraction = i / (steps - 1)
        segments.append(
            (0.1, {"moving": False, "a": 0.6 + 0.8 * fraction, "b": 20.0 * fraction})
        )
    segments.append((2.0, {"moving": False, "a": 1.4, "b": 20.0}))
    events, _ = play(det, segments)
    assert "black_screen" not in types(events), types(events)
    assert "freeze" in types(events), (
        f"brightness ramp masked the freeze: {types(events)}"
    )


# -- capture stall ----------------------------------------------------------


def test_capture_stall_is_not_reported_as_freeze():
    """A dead camera must not be diagnosed as a frozen screen."""
    det = detector()
    frame = render(0.0)
    t = 0.0
    events = []
    for _ in range(120):
        events.extend(det.process(frame, t))
        t += 1 / FPS
    seen = [e.type.value for e in events]
    assert "capture_stall" in seen, seen
    assert "freeze" not in seen, seen


def test_frozen_screen_with_noise_is_not_a_capture_stall():
    """A static picture still has sensor noise, so it is a freeze, not a stall."""
    det = detector()
    events, _ = play(det, [(2.5, {}), (7.0, {"freeze_at": 2.5})])
    seen = [e.type.value for e in events]
    assert "capture_stall" not in seen, seen
    assert "freeze" in seen, seen


# -- invariance -------------------------------------------------------------


def test_event_timing_is_invariant_to_brightness():
    """The same scenario must produce the same events at every brightness.

    If a scenario's event types or timings shift when the light changes, the
    brightness invariance is broken somewhere, and this catches it mechanically
    rather than by inspection.
    """
    baseline: list[tuple[str, float]] | None = None
    for setting in BRIGHTNESS_SETTINGS:
        det = detector()
        segments = [
            (3.0, setting),
            (5.0, {**setting, **black_at(setting)}),
            (2.0, setting),
        ]
        events, _ = play(det, segments)
        observed = [(e.type.value, round(e.t_start, 2)) for e in events]
        if baseline is None:
            baseline = observed
        else:
            assert observed == baseline, (
                f"{setting} diverged from the first run:\n"
                f"  first: {baseline}\n  this:  {observed}"
            )


def test_sensitivity_moves_the_line_without_moving_it_with_brightness():
    """Sensitivity must change detection strength, not brightness behaviour."""
    for sensitivity in (20, 50, 80):
        for setting in BRIGHTNESS_SETTINGS:
            det = detector(sensitivity=sensitivity)
            segments = [
                (3.0, setting),
                (5.0, {**setting, **black_at(setting)}),
                (2.0, setting),
            ]
            events, _ = play(det, segments)
            assert "black_screen" in types(events), (
                f"sensitivity={sensitivity} setting={setting}: {types(events)}"
            )


# -- performance ------------------------------------------------------------


def test_per_frame_budget():
    """Detection must leave the GUI thread plenty of room at 30 fps.

    Runs against a HD frame, which is the realistic capture size.
    """
    import numpy as np

    det = detector()
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:, :, :] = 60
    frame[100:400, 200:900] = 180
    frame = render(1.0, size=(1280, 720))
    det.process(frame, 0.0)

    iterations = 60
    start = time.perf_counter()
    for i in range(iterations):
        det.process(frame, i / FPS)
    elapsed = (time.perf_counter() - start) / iterations

    assert elapsed < 0.020, f"{elapsed * 1000:.1f} ms per frame exceeds the budget"
    print(f"      per-frame cost at 1280x720: {elapsed * 1000:.1f} ms")


# -- runner -----------------------------------------------------------------


def main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception:
            failures += 1
            print(f"ERROR {name}\n{traceback.format_exc()}")
        else:
            print(f"ok    {name}")

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
