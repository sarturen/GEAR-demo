"""Smoke test for the window: does it build, load, and tear down cleanly.

Runs on Qt's offscreen platform, so it needs no display and no hardware. It
cannot check that anything *looks* right -- only that the widgets construct,
the flow files parse, and the teardown path does not throw.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from geartest.bench import Bench  # noqa: E402
from geartest.config import (  # noqa: E402
    CameraCfg,
    ConsoleCfg,
    DeviceCfg,
    FLOW_DIR,
    RelayCfg,
    RelayChannelCfg,
    RoiCfg,
    ScreenCfg,
    Settings,
    SshCfg,
)
from geartest.flow import FlowEngine, Recorder, load_flow  # noqa: E402
from geartest.ui import main_window as main_window_module  # noqa: E402
from geartest.ui.main_window import MainWindow  # noqa: E402
from geartest.ui.video_view import VideoView  # noqa: E402

_app: QApplication | None = None


def app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def sample_settings() -> Settings:
    """Names but no ports: exactly the no-hardware bench."""
    return Settings(
        relays=[RelayCfg(name="relay1", port="")],
        relay_channels=[
            RelayChannelCfg(name="KL15", relay="relay1", channel=8),
            RelayChannelCfg(name="KL30", relay="relay1", channel=9),
        ],
        consoles=[ConsoleCfg(name="SOC", port=""), ConsoleCfg(name="MCU", port="")],
        cameras=[CameraCfg(name="cam4"), CameraCfg(name="cam5")],
        screens=[
            ScreenCfg(name="屏幕1", camera="cam4"),
            ScreenCfg(name="屏幕2", camera="cam5"),
        ],
        devices=[
            DeviceCfg(
                name="box1",
                power=["KL15", "KL30"],
                consoles=["SOC", "MCU"],
                screens=["屏幕1", "屏幕2"],
            )
        ],
        ssh=[SshCfg(name="container")],
    )


def test_window_builds_without_hardware():
    app()
    window = MainWindow(sample_settings())
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == [
        "被测设备",
        "继电器",
        "串口",
        "ADB",
        "SSH 容器",
        "监控画面",
        "测试流程",
    ], titles
    # The status bar must say something useful when nothing is plugged in.
    assert window.statusBar().currentMessage()
    window.shutdown_panels()


def test_panels_list_the_configured_resources():
    app()
    window = MainWindow(sample_settings())
    assert list(window.relay_panel.rows) == ["relay1"]
    assert list(window.serial_panel.tabs) == ["SOC", "MCU"]
    assert list(window.camera_panel.tiles) == ["屏幕1", "屏幕2"]
    assert list(window.dut_panel.views) == ["box1"]
    window.shutdown_panels()


def test_example_flows_parse_and_describe():
    app()
    window = MainWindow(sample_settings())
    paths = sorted(FLOW_DIR.glob("*.yaml"))
    assert paths, f"no example flows in {FLOW_DIR}"

    engine = FlowEngine(window.bench)
    for path in paths:
        flow = load_flow(path)
        assert flow.get("device"), f"{path.name} 没有声明 device"
        labels = engine.describe(flow)
        assert labels, path
        assert len(labels) == len(flow["steps"]), path
        assert not any(label.startswith("未知") for label in labels), (path, labels)
    window.shutdown_panels()


def test_yaml_boolean_actions_are_understood():
    """An unquoted ``action: on`` parses as the boolean True, not "on"."""
    app()
    window = MainWindow(sample_settings())
    flow = {
        "name": "t",
        "device": "box1",
        "steps": [{"relay": {"resource": "KL15", "action": True}}],
    }
    result = FlowEngine(window.bench).run(flow)
    assert len(result.results) == 1
    # It must fail on the missing COM port, not on an unrecognised action.
    assert "action" not in result.results[0].message.lower(), result.results[0].message
    window.shutdown_panels()


def test_flow_reports_missing_hardware_without_crashing():
    app()
    window = MainWindow(sample_settings())
    flow = {
        "name": "无硬件",
        "device": "box1",
        "steps": [
            {"relay": {"resource": "KL30", "action": "on"}},
            {"log": "这一步不该被执行"},
        ],
    }
    result = FlowEngine(window.bench).run(flow)
    assert not result.ok
    assert result.aborted
    assert len(result.results) == 1, [r.message for r in result.results]
    message = result.results[0].message
    assert message and "relay port" in message, message
    window.shutdown_panels()


def test_recorder_emits_resource_names():
    app()
    recorder = Recorder()
    assert len(recorder) == 0
    recorder.record("relay", {"resource": "KL30", "action": "on"})
    assert len(recorder) == 0, "recording must be off by default"

    recorder.set_enabled(True)
    recorder.record("relay", {"resource": "KL30", "action": "on"})
    recorder.record("serial_write", {"console": "SOC", "text": "hi\r\n"})
    assert len(recorder) == 2

    text = recorder.to_yaml("录制的流程")
    # A recorded flow has to replay, which means resource names, not channels.
    assert "resource: KL30" in text, text
    assert "console: SOC" in text, text
    recorder.set_enabled(False)
    recorder.record("relay", {"resource": "KL15", "action": "off"})
    assert len(recorder) == 2, "disarming must stop capture"


def test_roi_widget_round_trips_fractions():
    app()
    view = VideoView()
    view.resize(800, 600)
    view.set_roi(RoiCfg(0.25, 0.25, 0.5, 0.5))
    assert view.roi().w == 0.5

    import numpy as np

    view.set_frame(np.zeros((720, 1280, 3), dtype=np.uint8))
    rect = view._image_rect()
    assert rect.width() > 0 and rect.height() > 0, rect

    x, y = view._widget_to_norm(rect.center())
    assert abs(x - 0.5) < 0.02 and abs(y - 0.5) < 0.02, (x, y)

    x0, y0 = view._widget_to_norm(rect.topLeft() - rect.topLeft())
    assert 0.0 <= x0 <= 1.0 and 0.0 <= y0 <= 1.0


def test_unknown_settings_keys_are_reported(tmp_path=None):
    """A renamed section must not silently load as an empty bench."""
    import tempfile
    from geartest.config import ConfigError, load_settings

    app()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "bad.yaml"
        path.write_text("serial:\n  - name: CH340\n    port: COM5\n", encoding="utf-8")
        try:
            load_settings(path)
        except ConfigError as exc:
            assert "serial" in str(exc), str(exc)
            assert "consoles" in str(exc), str(exc)
        else:
            raise AssertionError("未知键应当报错，而不是被静默丢弃")


def test_dut_panel_refuses_a_broken_declaration():
    app()
    settings = sample_settings()
    settings.devices[0].screens.append("屏幕9")
    window = MainWindow(settings)
    bench = Bench(settings)
    problems = bench.resolve_all("box1")
    assert any("屏幕9" in problem for problem in problems), problems
    window.shutdown_panels()


def test_close_event_tears_down():
    app()
    saved = []
    original = main_window_module.save_settings
    main_window_module.save_settings = lambda *a, **k: saved.append(a)
    try:
        window = MainWindow(sample_settings())
        window.close()
    finally:
        main_window_module.save_settings = original
    assert saved, "closing must persist the settings"


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
            import traceback

            failures += 1
            print(f"ERROR {name}\n{traceback.format_exc()}")
        else:
            print(f"ok    {name}")
    # Panels kick off device refreshes when they are constructed. Let those
    # finish, or the interpreter tears them down mid-call.
    from geartest.ui.worker import drain

    drain(5000)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
