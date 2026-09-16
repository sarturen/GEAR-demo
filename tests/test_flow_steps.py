"""Flow-engine behaviour, with the hardware stubbed out.

The engine is the part that decides pass or fail, so the cases that matter most
here are the ones where it could pass for the wrong reason. The headline test is
``test_lit_fails_on_a_screen_that_never_powered``: before the calibration guard
existed, a panel that never lit up sailed through a "check it lit" step, because
the detector cannot call anything dark until it has a brightness baseline, and
"no black event" was being read as "the screen is fine".
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from geartest.bench import Bench
from geartest.config import (
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    RelayCfg,
    RelayChannelCfg,
    ScreenCfg,
    Settings,
)
from geartest.flow.engine import FlowEngine
from geartest.vision.monitor import ScreenMonitor
from tests.synthetic import render

FPS = 30


# -- stubs ------------------------------------------------------------------


class FakeRelay:
    def __init__(self, cfg: RelayCfg) -> None:
        self.cfg = cfg
        self.state = [False] * 16
        self.calls: list = []
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def set_channel(self, channel: int, on: bool) -> None:
        self.calls.append((channel, on))
        self.state[channel - 1] = on

    def write_channels(self, start: int, values: list) -> None:
        for offset, value in enumerate(values):
            self.state[start - 1 + offset] = value

    def read_channels(self, count: int = 16) -> list:
        return self.state[:count]

    def pulse(self, channel: int, milliseconds: int) -> None:
        self.calls.append((channel, "pulse", milliseconds))

    def cancel_all_pulses(self) -> None:
        pass


class FakeConsole:
    def __init__(self, cfg: ConsoleCfg) -> None:
        self.cfg = cfg
        self.written: list = []
        self._listeners: list = []

    def add_listener(self, on_line=None, on_raw=None) -> None:
        if on_line is not None:
            self._listeners.append(on_line)

    def emit(self, line: str) -> None:
        for listener in list(self._listeners):
            listener(line)

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def write_bytes(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def is_open(self) -> bool:
        return True


class FakeHub:
    """Stands in for a camera: the test feeds the screen monitor directly."""

    def __init__(self, cfg: CameraCfg) -> None:
        self.cfg = cfg
        self.running = True
        self.fps = float(FPS)
        self.error = ""

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def last_frame(self):
        return None

    def last_frame_and_time(self):
        return None, 0.0

    def detach(self, name: str) -> None:
        pass

    def screens(self) -> list:
        return []


class Feeder(threading.Thread):
    """Pushes synthetic frames into a screen monitor on its own clock."""

    def __init__(self, monitor: ScreenMonitor, **scene) -> None:
        super().__init__(daemon=True)
        self.monitor = monitor
        self.scene = scene
        # Not ``_stop``: Thread has an internal method of that name and
        # shadowing it breaks join().
        self._halt = threading.Event()

    def run(self) -> None:
        started = time.perf_counter()
        while not self._halt.is_set():
            now = time.perf_counter() - started
            self.monitor.feed(render(now, **self.scene), time.perf_counter())
            time.sleep(1.0 / FPS)

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=1.0)


def feed_for(monitor: ScreenMonitor, seconds: float, **scene) -> None:
    """Push frames synchronously, on the same clock the engine reads."""
    deadline = time.perf_counter() + seconds
    started = time.perf_counter()
    while time.perf_counter() < deadline:
        monitor.feed(render(time.perf_counter() - started, **scene), time.perf_counter())
        time.sleep(1.0 / FPS)


def make_bench() -> Bench:
    settings = Settings(
        relays=[RelayCfg(name="relay1", port="FAKE")],
        relay_channels=[
            RelayChannelCfg(name="KL15", relay="relay1", channel=8),
            RelayChannelCfg(name="KL30", relay="relay1", channel=9),
            # Defined but deliberately not given to box1, for the membership test.
            RelayChannelCfg(name="KL31", relay="relay1", channel=10),
        ],
        consoles=[ConsoleCfg(name="SOC", port="FAKE"), ConsoleCfg(name="MCU", port="FAKE")],
        cameras=[CameraCfg(name="cam4"), CameraCfg(name="cam5")],
        screens=[
            ScreenCfg(name="屏幕1", camera="cam4"),
            ScreenCfg(name="屏幕2", camera="cam5"),
        ],
        devices=[
            DeviceCfg(
                name="box1",
                adb_serial="0123456789",
                power=["KL15", "KL30"],
                consoles=["SOC", "MCU"],
                screens=["屏幕1", "屏幕2"],
            )
        ],
    )
    bench = Bench(settings)
    bench.relays["relay1"] = FakeRelay(settings.relays[0])
    bench.consoles["SOC"] = FakeConsole(settings.consoles[0])
    bench.consoles["MCU"] = FakeConsole(settings.consoles[1])
    for screen_cfg, camera_cfg in zip(settings.screens, settings.cameras):
        monitor = ScreenMonitor(screen_cfg)
        monitor.hub = FakeHub(camera_cfg)
        bench.screens[screen_cfg.name] = monitor
    return bench


def run_flow(bench: Bench, steps: list, **extra):
    flow = {"name": extra.pop("name", "t"), "device": extra.pop("device", "box1")}
    flow.update(extra)
    flow["steps"] = steps
    return FlowEngine(bench).run(flow)


# -- the headline regression -------------------------------------------------


def test_lit_fails_on_a_screen_that_never_powered():
    """A panel that never lights must not pass a "check it lit" step.

    Before the calibration guard, this passed: the detector emits no black event
    without a baseline, and "no black event" was being read as "screen is fine".
    """
    bench = make_bench()
    feeder = Feeder(bench.screens["屏幕1"], level=0.0)  # black forever
    feeder.start()
    try:
        result = run_flow(
            bench,
            [{"expect_screen": {"screen": "屏幕1", "state": "lit", "timeout": 2.0}}],
        )
    finally:
        feeder.stop()

    assert not result.ok, "从未点亮的屏幕被判为通过"
    message = result.results[0].message
    assert "基线未建立" in message, message


def test_lit_passes_for_a_healthy_screen():
    bench = make_bench()
    feeder = Feeder(bench.screens["屏幕1"])  # moving content
    feeder.start()
    try:
        result = run_flow(
            bench,
            [{"expect_screen": {"screen": "屏幕1", "state": "lit", "timeout": 4.0}}],
        )
    finally:
        feeder.stop()
    assert result.ok, [r.message for r in result.results]


def test_lit_fails_when_the_screen_goes_dark_midway():
    bench = make_bench()
    monitor = bench.screens["屏幕1"]
    feed_for(monitor, 3.0)  # 3 s of content, enough to calibrate
    assert monitor.calibrated, "前置条件：3 秒正常画面应当已经建立基线"

    feeder = Feeder(monitor, level=0.0)
    feeder.start()
    try:
        result = run_flow(
            bench,
            [{"expect_screen": {"screen": "屏幕1", "state": "lit", "timeout": 3.0}}],
        )
    finally:
        feeder.stop()
    assert not result.ok, "屏幕变黑却判为通过"
    assert "黑屏" in result.results[0].message, result.results[0].message


# -- screen states -----------------------------------------------------------


def test_dark_and_sequence_transitions():
    """Go dark, then come back: the sequence step covers the whole transition."""
    bench = make_bench()
    monitor = bench.screens["屏幕1"]
    feed_for(monitor, 3.0)

    def darken_then_recover():
        time.sleep(0.4)
        now = time.perf_counter()
        for _ in range(60):  # 2 s black -> a sustained black screen
            monitor.feed(render(0.0, level=0.0), time.perf_counter())
            time.sleep(1.0 / FPS)
        started = time.perf_counter()
        while time.perf_counter() - started < 2.0:
            elapsed = time.perf_counter() - started
            monitor.feed(render(elapsed), time.perf_counter())
            time.sleep(1.0 / FPS)

    thread = threading.Thread(target=darken_then_recover, daemon=True)
    thread.start()
    result = run_flow(
        bench,
        [
            {
                "expect_screen": {
                    "screen": "屏幕1",
                    "sequence": ["dark", "lit"],
                    "timeout": 8.0,
                }
            }
        ],
    )
    thread.join(timeout=10)
    assert result.ok, result.results[0].message
    assert "恢复" in result.results[0].message, result.results[0].message


def test_unknown_screen_state_is_rejected():
    bench = make_bench()
    result = run_flow(
        bench, [{"expect_screen": {"screen": "屏幕1", "state": "whatever"}}]
    )
    assert not result.ok
    assert "未知的画面状态" in result.results[0].message


# -- resource binding --------------------------------------------------------


def test_unbound_resource_names_are_distinguished_from_undefined_ones():
    bench = make_bench()
    result = run_flow(
        bench, [{"relay": {"resource": "KL31", "action": "on"}}]
    )
    assert not result.ok
    assert "没有绑定到设备" in result.results[0].message, result.results[0].message

    result = run_flow(bench, [{"relay": {"resource": "KL99", "action": "on"}}])
    assert not result.ok
    assert "未定义" in result.results[0].message, result.results[0].message


def test_all_off_touches_only_this_devices_rails():
    """A board-wide all_off would drop another device's power as a side effect."""
    bench = make_bench()
    bench.settings.relays.append(RelayCfg(name="relay2", port="FAKE"))
    bench.relays["relay2"] = FakeRelay(bench.settings.relays[-1])
    bench.settings.relay_channels.append(
        RelayChannelCfg(name="OTHER", relay="relay2", channel=1)
    )
    bench.settings.devices.append(
        DeviceCfg(name="box2", power=["OTHER"], consoles=[], screens=[])
    )
    bench.relays["relay2"].set_channel(1, True)

    result = run_flow(bench, [{"relay": {"action": "all_off"}}])
    assert result.ok, result.results[0].message
    assert not any(bench.relays["relay1"].state), "本设备的电源应当断开"
    assert bench.relays["relay2"].state[0], "另一台设备的电源不应被波及"


def test_relay_steps_drive_the_named_channel():
    bench = make_bench()
    result = run_flow(bench, [{"relay": {"resource": "KL30", "action": "on"}}])
    assert result.ok, result.results[0].message
    relay = bench.relays["relay1"]
    assert relay.calls == [(9, True)], relay.calls  # KL30 -> channel 9


def test_preflight_refuses_a_flow_with_a_broken_declaration():
    bench = make_bench()
    bench.settings.devices[0].screens.append("屏幕9")
    try:
        run_flow(bench, [{"log": "x"}])
    except Exception as exc:
        assert "屏幕9" in str(exc), str(exc)
    else:
        raise AssertionError("声明有问题的设备不应开始运行")


def test_flow_without_device_is_rejected():
    bench = make_bench()
    try:
        FlowEngine(bench).run({"name": "t", "steps": [{"log": "x"}]})
    except Exception as exc:
        assert "device" in str(exc), str(exc)
    else:
        raise AssertionError("缺少 device 的流程应当被拒绝")


# -- watches -----------------------------------------------------------------


def test_watch_counts_hits_and_reports_the_first():
    bench = make_bench()
    console = bench.consoles["MCU"]

    def chatter():
        time.sleep(0.3)
        console.emit("boot ok")
        console.emit("MCU reset detected, reason=0x12")
        console.emit("MCU reset detected, reason=0x14")

    thread = threading.Thread(target=chatter, daemon=True)
    thread.start()
    result = run_flow(
        bench,
        [
            {"watch": {"id": "mcu_reset", "console": "MCU", "regex": "reset"}},
            {"wait": 1.0},
            {"expect_watch": {"id": "mcu_reset", "at_most": 0}},
        ],
    )
    thread.join(timeout=5)

    assert not result.ok, "命中关键字却判为通过"
    message = result.results[-1].message
    assert "2 次" in message, message
    assert "reason=0x12" in message, message


def test_watch_passes_when_the_keyword_never_appears():
    bench = make_bench()
    result = run_flow(
        bench,
        [
            {"watch": {"id": "soc_panic", "console": "SOC", "regex": "panic"}},
            {"wait": 0.3},
            {"expect_watch": {"id": "soc_panic", "at_most": 0}},
        ],
    )
    assert result.ok, result.results[-1].message


def test_expect_watch_without_a_watch_is_reported():
    bench = make_bench()
    result = run_flow(bench, [{"expect_watch": {"id": "nope", "at_most": 0}}])
    assert not result.ok
    assert "没有名为" in result.results[0].message


# -- loops -------------------------------------------------------------------


def test_repeat_runs_every_iteration_and_attributes_a_failure():
    bench = make_bench()
    result = run_flow(
        bench,
        [
            {
                "repeat": {
                    "times": 3,
                    "name": "上下电",
                    "steps": [
                        {"relay": {"resource": "KL15", "action": "off"}},
                        {"relay": {"resource": "KL15", "action": "on"}},
                        {"expect_watch": {"id": "missing", "at_most": 0}},
                    ],
                }
            }
        ],
    )
    assert not result.ok
    message = result.results[0].message
    assert "第 1/3 轮" in message, message
    assert "第 3 步" in message, message


def test_repeat_completes_when_every_iteration_passes():
    bench = make_bench()
    result = run_flow(
        bench,
        [
            {
                "repeat": {
                    "times": 5,
                    "name": "上下电",
                    "steps": [{"relay": {"resource": "KL15", "action": "off"}}],
                }
            }
        ],
    )
    assert result.ok, result.results[0].message
    assert "5/5" in result.results[0].message, result.results[0].message
    assert len(bench.relays["relay1"].calls) == 5


def test_serial_since_iteration_covers_a_whole_loop():
    """A per-iteration keyword check must see output from that whole iteration."""
    bench = make_bench()
    console = bench.consoles["MCU"]

    def chatter():
        for index in range(6):
            time.sleep(0.2)
            console.emit(f"iteration tick {index}")
        console.emit("RESET happened")

    thread = threading.Thread(target=chatter, daemon=True)
    thread.start()
    result = run_flow(
        bench,
        [
            {
                "repeat": {
                    "times": 2,
                    "name": "轮",
                    "steps": [
                        {"wait": 0.7},
                        {
                            "serial_expect": {
                                "console": "MCU",
                                "regex": "RESET",
                                "absent": True,
                                "timeout": 0.0,
                                "since": "iteration",
                            }
                        },
                    ],
                }
            }
        ],
    )
    thread.join(timeout=5)
    assert not result.ok, "循环内出现 RESET 却判为通过"
    assert "RESET" in result.results[0].message, result.results[0].message


# -- failure handling --------------------------------------------------------


def test_on_failure_runs_and_keeps_the_original_reason():
    bench = make_bench()
    result = run_flow(
        bench,
        [{"relay": {"resource": "KL99", "action": "on"}}],
        on_failure=[
            {"log": "处理中"},
            {"expect_watch": {"id": "nonexistent", "at_most": 0}},  # 处理函数自己会失败
            {"snapshot": {"screens": ["屏幕1"], "note": "未点亮"}},
        ],
    )
    assert not result.ok
    # The step's own failure is what must survive, not the handler's.
    assert "未定义" in result.results[0].message, result.results[0].message
    assert result.incident_dir is not None, "失败后应当产生证据目录"
    summary = (result.incident_dir / "summary.txt").read_text(encoding="utf-8")
    assert "box1" in summary and "0123456789" in summary
    # ...and the physical binding, which is the point of the bundle.
    assert "板卡 relay1 第 9 路" in summary, summary
    assert "屏幕1" in summary, summary


def test_step_level_on_failure_runs_before_the_flows():
    bench = make_bench()
    seen: list[str] = []
    engine = FlowEngine(bench, on_log=lambda level, text: seen.append(text))
    engine.run(
        {
            "name": "t",
            "device": "box1",
            "on_failure": [{"log": "流程级处理"}],
            "steps": [
                {
                    "relay": {"resource": "KL99", "action": "on"},
                    "on_failure": [{"log": "步骤级处理"}],
                }
            ],
        }
    )
    joined = "\n".join(seen)
    assert "步骤级处理" in joined and "流程级处理" in joined
    assert joined.index("步骤级处理") < joined.index("流程级处理")


# -- misc --------------------------------------------------------------------


def test_yaml_boolean_actions_are_understood():
    bench = make_bench()
    result = run_flow(bench, [{"relay": {"resource": "KL15", "action": True}}])
    assert result.ok, result.results[0].message
    assert bench.relays["relay1"].calls == [(8, True)]


def test_describe_labels_every_step_kind():
    bench = make_bench()
    flow = {
        "name": "t",
        "device": "box1",
        "steps": [
            {"relay": {"resource": "KL15", "action": "on"}},
            {"adb_wait": {"timeout": 60}},
            {"expect_screen": {"screen": "屏幕2", "state": "lit"}},
            {"serial_expect": {"console": "SOC", "regex": "ready"}},
            {"watch": {"id": "w", "console": "MCU", "regex": "reset"}},
            {"expect_watch": {"id": "w", "at_most": 0}},
            {"snapshot": {"screens": "all"}},
            {"adb_logs": {}},
            {"repeat": {"times": 2, "name": "轮", "steps": [{"wait": 0.01}]}},
        ],
    }
    labels = FlowEngine(bench).describe(flow)
    assert len(labels) == len(flow["steps"])
    assert not any("未知步骤类型" in label for label in labels), labels
    assert any("KL15" in label for label in labels), labels
    assert any("屏幕2" in label for label in labels), labels


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
